"""Probe の道具: ターゲットが出す値とイベント（U16）。ITM のポート・RTT のチャンネルを型で解釈した値と、fprobe_user.h の
名前付きイベント（mark / start / stop / value）を集め、区間を対にして集計する。

Probe（fprobe.mcp_server）に混ぜて使う mixin。Probe の _lock・target・session・_symbols・_recorder・swo_start・_swo_baud、
WindowTools（出来事の窓）、ObserveTools（RTT の受信の横取り _rtt_listen）を使う。機器に触るのは必ず _lock の中。

- ITM: SWO のバイト（イベント記録・記録の stream swo。デバイスの時刻）を窓で読む。SWO の受信のエンドポイント
  （swo_read / swo_decode）は読まない（取り合わない。rtos.timeline の方法 itm と同時に使える）。
  受信を始めて窓を開いてから、ITM のポートを有効にする（逆だと始めのバイトで解読がずれる）。止めるときに足したポートを戻す。
- RTT: rtt_start 済みの受信を横で受け取る（ホストの時刻 → デバイスの時刻は、始め・読むたびに取る時計の対応付けで。
  不確かさ付き）。
- 名前付きイベントの名前は ELF の文字列（debug_elf_load の ELF）。
- 止めたとき記録中なら stream "user" に書く（value / mark / start / stop / interval）。timing_measure がそのまま使える。

仕様: docs/superpowers/specs/2026-10-18-u10-u16-trace-hooks-user-events-design.md の 3 節。
"""

import time
from dataclasses import dataclass, field

from .probe_window import EventWindow, write_registers
from .profile.dwt import ITM_TER, RegisterWrite, itm_port_writes, plan_swo
from .recording import ClockMap
from .rtos.kernel_trace import KERNEL_PORT, PAYLOAD_PORT
from .rtos.timeline import TRACE_PORT, itm_packets
from .usertrace import (EVENT_PORT, VALUE_PORT, Channel, UserDecoder, elf_strings, interval_stats, pair_intervals,
                        record_rows, value_stats)

__all__ = ["UserTools"]

RECORDS_LIMIT = 500                      # user_trace_get が 1 回に返す数の既定
_RESERVED = {TRACE_PORT: "RTOS のトレースフック", KERNEL_PORT: "RTOS のトレースフック",
             PAYLOAD_PORT: "RTOS のトレースフック"}


@dataclass
class _UserRun:
    channels: list[Channel]
    events: bool
    event_rtt_channel: int | None
    itm_ports: list[int]
    rtt_channels: list[int]
    timebase_hz: float
    clock: ClockMap
    window: EventWindow | None = None
    added_ter: int = 0                   # 足した TER のビット（止めるときに戻す）
    listener: object = None
    received: list[tuple[float, int, bytes]] = field(default_factory=list)   # RTT: (ホストの時刻, チャンネル, バイト)
    warnings: list[str] = field(default_factory=list)
    stopped: bool = False


class UserTools:
    """ターゲットが出す値とイベント（Probe の mixin）。"""

    def _init_user(self) -> None:
        self._user_run: _UserRun | None = None

    def user_trace_start(self, channels: list[dict] | None = None, events: bool = True,
                         event_rtt_channel: int | None = None, swo_baud: int | None = None,
                         core_clock_hz: float | None = None) -> dict:
        """値とイベントを取り始める。channels: [{name, source: itm|rtt, index: ポートかチャンネル, type: i8〜u32・f32・
        qM.N・{"type": "struct", "fields": [[名前, 型], …]}, unit}]。events: fprobe_user.h の名前付きイベント（ITM の
        ポート 24・25）。event_rtt_channel: 名前付きイベントを RTT のこのチャンネルからも読む。ITM を使うなら swo_baud で
        SWO の受信も始め、core_clock_hz も渡すと TPIU も設定する（ピンの設定はチップ固有なので含まない）。"""
        parsed = [Channel.from_dict(c) for c in channels or []]
        itm_ports = sorted({c.index for c in parsed if c.source == "itm"} | ({EVENT_PORT, VALUE_PORT} if events else set()))
        rtt_channels = sorted({c.index for c in parsed if c.source == "rtt"} |
                              ({event_rtt_channel} if event_rtt_channel is not None else set()))
        if not itm_ports and not rtt_channels:
            raise ValueError("何も取らない: channels か events か event_rtt_channel を指定する")
        for c in parsed:
            if c.source == "itm" and c.index in _RESERVED:
                raise ValueError(f"ITM のポート {c.index} は{_RESERVED[c.index]}が使う")
            if c.source == "itm" and events and c.index in (EVENT_PORT, VALUE_PORT):
                raise ValueError(f"ITM のポート {EVENT_PORT}・{VALUE_PORT} は名前付きイベントが使う（events=false なら使える）")
        with self._lock:
            if self._user_run is not None and not self._user_run.stopped:
                raise RuntimeError("値とイベントを取っている（user_trace_stop を先に）")
            if rtt_channels and self._rtt is None:
                raise RuntimeError("RTT の受信を始めていない（rtt_start を先に）")
            device = self.session.device
            hz = device.capabilities().timebase_hz
            run = _UserRun(parsed, events, event_rtt_channel, itm_ports, rtt_channels, hz, ClockMap(hz))
            self._user_clock(run)
            if itm_ports:
                self._user_start_itm(run, swo_baud, core_clock_hz)
            if rtt_channels:
                def listener(key, data: bytes, host_s: float, run=run) -> None:
                    if key in run.rtt_channels:
                        run.received.append((host_s, key, bytes(data)))
                run.listener = listener
                self._rtt_listen(listener)
            if self._symbols is None:
                run.warnings.append("ELF が読み込まれていない（debug_elf_load）: 名前付きイベントの名前は番地で出る")
            self._user_run = run
            return self._user_state(run)

    def _user_start_itm(self, run: _UserRun, swo_baud: int | None, core_clock_hz: float | None) -> None:
        """受信を始め、窓を開いてから ITM（・TPIU）を設定する（_lock の中で）。"""
        target = self.target
        writes = list(itm_port_writes(run.itm_ports[0]))[:3]          # DEMCR・鍵・TCR
        mask = sum(1 << p for p in run.itm_ports)
        writes.append(RegisterWrite(ITM_TER, mask, mask, f"ITM: ポート {run.itm_ports} を有効に"))
        baud = swo_baud
        if swo_baud is not None and core_clock_hz is not None:
            swo = plan_swo(core_clock_hz, swo_baud)
            writes[2:2] = swo.writes                                   # 鍵を外した後、ITM を有効にする前に
            run.warnings += swo.warnings
            baud = round(swo.baud)
        if baud is not None:
            self.swo_start(baud)
        elif self._swo_baud is None:
            run.warnings.append("SWO の受信を始めていない（swo_start か swo_baud）: ITM の値は届かない")
        run.window = self._open_window()
        before = write_registers(target, writes)
        run.added_ter = mask & ~before.get(ITM_TER, 0)

    def _user_clock(self, run: _UserRun) -> None:
        """ホストの時刻とデバイスの時刻の組を 1 つ足す（_lock の中で。RTT の受信の時刻を写すため）。"""
        h0 = time.monotonic()
        ticks = self.session.device.time()
        h1 = time.monotonic()
        run.clock.add((h0 + h1) / 2, ticks, h1 - h0)

    def user_trace_stop(self) -> dict:
        """取るのを止め（足した ITM のポートを戻す）、要約を返す。記録中なら stream "user" に書く
        （value / mark / start / stop / interval）。"""
        with self._lock:
            run = self._user_run
            if run is None or run.stopped:
                raise RuntimeError("値とイベントを取っていない（user_trace_start）")
            try:
                self._user_clock(run)
                if run.added_ter and self._debug is not None:
                    current = self.target.read_memory(ITM_TER)
                    self.target.write_memory(ITM_TER, current & ~run.added_ter)
            finally:
                if run.listener is not None:
                    self._rtt_unlisten(run.listener)
                if run.window is not None:
                    self._close_window(run.window)
                run.stopped = True
        records, pairing, errors = self._user_build(run)
        rec = self._recorder
        if rec is not None:
            rec.add_many(record_rows(records, pairing.intervals))
        out = self._user_summary(run, records, pairing, errors)
        out["recorded"] = rec is not None
        return out

    def _user_current(self) -> _UserRun:
        if self._user_run is None:
            raise RuntimeError("値とイベントが無い（user_trace_start）")
        return self._user_run

    def _user_build(self, run: _UserRun):
        """窓の SWO と RTT の受信を始めから解読し直す（取っている最中は、いまの時計の対応付けで）。"""
        if not run.stopped:
            with self._lock:
                self._user_clock(run)
        decoder = UserDecoder(run.channels, elf_strings(self._symbols) if self._symbols is not None else (lambda a: None),
                              event_port=EVENT_PORT if run.events else None, event_rtt_channel=run.event_rtt_channel)
        records = []
        if run.window is not None:
            data = [(t, b) for t, _, b in self._window_events(run.window, "swo")]
            records += decoder.feed_itm(itm_packets(data))
        for host_s, channel, chunk in list(run.received):
            t, unc = run.clock.to_ticks(host_s)
            records += decoder.feed_rtt(channel, t, chunk, unc)
        records.sort(key=lambda r: r.t)
        return records, pair_intervals(records), dict(decoder.errors)

    def _user_state(self, run: _UserRun) -> dict:
        return {"running": not run.stopped, "channels": [c.to_dict() for c in run.channels], "events": run.events,
                "itm_ports": run.itm_ports, "rtt_channels": run.rtt_channels, "timebase_hz": run.timebase_hz,
                "t0": run.window.t0 if run.window is not None else None, "warnings": list(run.warnings)}

    def _user_summary(self, run: _UserRun, records, pairing, errors: dict) -> dict:
        out = self._user_state(run)
        out.update(records=len(records), intervals=len(pairing.intervals), errors=errors,
                   unmatched_stops=pairing.unmatched_stops, open_intervals=pairing.open,
                   started_before=pairing.started_before)
        if run.window is not None:
            out["window"] = self._window_info(run.window, run.timebase_hz)
        if errors.get("lost"):
            out["warnings"].append("ITM があふれた・解読を誤った: その間の値・イベントは無い（SWO を速くする）")
        return out

    def user_trace_get(self, name: str | None = None, kinds: list[str] | None = None, offset: int = 0,
                       limit: int = RECORDS_LIMIT) -> dict:
        """値とイベントの一覧（時刻順。取っている最中ならいままでの分）。name・kinds（value / mark / start / stop）で絞り、
        offset から limit 件。時刻はタイムベース（RTT は時計の対応付けで写した時刻と不確かさ）。"""
        run = self._user_current()
        records, pairing, errors = self._user_build(run)
        selected = [r for r in records if (name is None or r.name == name) and (kinds is None or r.kind in kinds)]
        out = self._user_summary(run, records, pairing, errors)
        out.update(total=len(selected), offset=offset, records=[r.to_dict() for r in selected[offset:offset + limit]])
        return out

    def user_trace_stats(self, name: str | None = None, bins: int = 20, outliers: str = "iqr",
                         limit: int = 100) -> dict:
        """集計: 値は名前ごと（構造体は "名前.欄" ごと）の数・最小・最大・平均・最後、区間（start / stop を名前ごとに
        後入れ先出しで対にしたもの）は名前ごとの回数・最小・最大・平均・σ・分位・分布・外れ値（timing_measure と同じ）。
        区間の一覧は limit 件まで。"""
        run = self._user_current()
        records, pairing, errors = self._user_build(run)
        values = value_stats(records)
        intervals = interval_stats(pairing.intervals, run.timebase_hz, bins=bins, outliers=outliers)
        marks: dict[str, int] = {}
        for r in records:
            if r.kind == "mark":
                marks[r.name] = marks.get(r.name, 0) + 1
        if name is not None:
            values = {k: v for k, v in values.items() if k == name or k.startswith(name + ".")}
            intervals = {k: v for k, v in intervals.items() if k == name}
            marks = {k: v for k, v in marks.items() if k == name}
        for row in intervals.values():
            for key in ("intervals", "outliers"):
                if len(row.get(key, ())) > limit:
                    row[f"{key}_total"] = len(row[key])
                    row[key] = row[key][:limit]
        out = self._user_summary(run, records, pairing, errors)
        out.update(values=values, intervals=intervals, marks=marks)
        return out
