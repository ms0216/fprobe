"""DAP のイベント記録（events_start / events_stop / events_read）のサービス。

頼る相手: 核（fprobe.probe_service.ProbeCore の lock・session）、la_idle（ロジアナの捕捉を止める。LongRecordingService）、
swo_mode（この道具の swo_start で最後に選んだ SWO の方式。SwoService）。記録の窓の読み read_events は FpgaObsService も使う。
"""

import dataclasses

from .collector import unwrap_events
from .probe_service import HostView
from .events import BYTE_KINDS, EventLog, INJECT_KINDS, resolve
from .timing import byte_edge_window

_BYTE_NAMES = set(BYTE_KINDS.values())
_LINE_NAMES = _BYTE_NAMES | {"aux"}                 # 端子の上の点の範囲 line を付ける種類（補助マイコンの組は段階 S3 の B2）


__all__ = ["EventService"]


class EventService:
    """DAP のイベント記録の道具。"""

    API = ("events_start", "events_stop", "events_read")

    # ほかの領域（Probe の委譲）に出す名前（FPGA の観測の読みが記録の窓を読む）
    SHARED = ("_read_events",)
    NEEDS = ("_la_idle", "_lock", "_swo_baud", "_swo_mode", "session")

    def __init__(self, host):
        self._host = HostView(host, self.NEEDS, type(self).__name__)

    def events_start(self) -> dict:
        """DAP 転送のイベント記録を始める（外部メモリのリング。古いものから上書き）。"""
        with self._host._lock:
            self._host._la_idle()
            log = EventLog(self._host.session.device)
            log.start()
            return {"capacity": log.capacity}

    def events_stop(self) -> dict:
        """イベント記録を止める（記録は残り、events_read で読める）。"""
        with self._host._lock:
            self._host._la_idle()
            log = EventLog(self._host.session.device)
            log.stop()
            written, lost = log.counts()
            return {"written": written, "lost": lost}

    def events_read(self, last: int = 200, raw: bool = False, since: int | None = None) -> dict:
        """イベントを古い順に返す。since が無ければ最新の last 件。since（前回の結果の next）があれば、その番号から
        last 件まで（続けて読めば取りこぼさない。読む前に上書きされた数は skipped）。first = 返した最初の番号、
        next = 次に since に渡す番号（番号は記録を始めてから書いた数で数える）。raw = False ならアクセス（ポストされた
        読みを対応付けた後。クロストリガの割り込み書き込みは kind "inject" と、停止の書き込みまでの転送数 transfers（旧い回路は
        null））、True なら線上の転送そのもの。time_us は記録開始とは無関係なタイムベースの時刻（µs）。
        ポストされた AP の読み（値が後の転送で返ったもの）は、対象が読んだ瞬間の範囲 [time_lo_us, time_us] を持つ:
        time_us = 値を返した転送、requested_us = 読みを頼んだ転送、time_lo_us = その前に線上で終わった転送（確かな下限。
        返した中に見えていなければ null）。頼んだ転送の時刻は転送の完了の時刻なので、下限にはならない。
        UART・SWO のバイト（kind "uart_rx" / "uart_tx" / "swo"）は、端子の上の点の範囲 line（point = "start_bit_edge"（開始ビットの
        立ち下がり）/ "first_bit_center"（Manchester の最初のビットの中央。段階 S3 の R4）/ "last_bit_center"（S1〜S2 の回路の
        Manchester の最後のビットの中央）、t_lo_us 以上 t_hi_us 未満（同じなら ちょうど）、unknown = 含まない項目）を持つ（回路の
        申告 TLV TIMING から。申告の無い古い回路・SWO の方式が分からないときは範囲が null）。補助マイコンの計測値（kind "aux"）も
        line（組の先頭のバイトの開始ビット。段階 S3 の B2。補助マイコンの中の変換の時刻は target_internal で分からない）を持つ。
        DAP の時刻が線上の点の回路（ケイパビリティ dap_line_time。docs/superpowers/specs/2026-11-01-time-accuracy-design.md S2）:
        時刻は転送の線上の点（SWD は ACK の後、JTAG は Update-DR）。ポストされた読みの time_lo_us は頼んだ転送の始め（SWD）・
        Update-DR（JTAG）。サンプラの語（kind "sample"）は time_us = 読みに行った転送の点、time_lo_us・time_hi_us = 対象が
        読んだ瞬間の範囲（null = 返した中で分からない）、waits = 値を返した転送の WAIT の回数。"sample_error" は値を出さな
        かった読みの区間（address = 最初の番号、value = 読みの数、time_hi_us = 見つけた転送）、"dap_config" は PHY の設定（detail）。
        raw = True でも種類 16・17・18 と waits を返す。"""
        with self._host._lock:
            written, lost, first, records, events, extra = self._read_events(last, since)
            caps = self._host.session.device.capabilities()
            hz = caps.timebase_hz
            line_time = bool(caps.dap_line_time)
            output_ticks = caps.dap_output_ticks

            def us(t):
                return None if t is None else t * 1e6 / hz
            events = unwrap_events(events, self._host.session.device.time())     # 48 ビットの時刻を 64 ビットに（折り返しをまたいでも戻らない）
            if raw:
                items = [{"time_us": e.time * 1e6 / hz, "kind": e.kind, "ap": e.ap, "read": e.read,
                          "register": e.register, "ack": e.ack, "parity_error": e.parity_error,
                          "address": f"0x{e.address:08X}", "data": f"0x{e.data:08X}",
                          **({"transfers": e.transfers} if e.kind in INJECT_KINDS else {}),
                          **({"waits": e.waits} if e.waits is not None else {}),
                          **({"config": dataclasses.asdict(e.config)} if e.config is not None else {}),
                          **({"gap": {**dataclasses.asdict(e.gap), "end_us": us(e.gap.end)}} if e.gap is not None else {})}
                         for e in events]
            else:
                items = [{"time_us": a.time * 1e6 / hz, "kind": a.kind, "address": f"0x{a.address:08X}",
                          "value": f"0x{a.value:08X}", "ack": a.ack,
                          **({"transfers": a.transfers} if a.kind == "inject" else {}),
                          **({"requested_us": a.requested * 1e6 / hz,
                              "time_lo_us": None if a.time_lo is None else a.time_lo * 1e6 / hz,
                              **({"time_hi_us": us(a.time_hi)} if line_time else {})}
                             if a.requested is not None else {}),
                          **({"time_lo_us": us(a.time_lo), "time_hi_us": us(a.time_hi),
                              **({"waits": a.detail.get("waits")} if a.kind == "sample" else {})}
                             if line_time and a.kind in ("sample", "sample_error") else {}),
                          **({"detail": a.detail} if a.kind in ("dap_config", "sample_error") else {}),
                          **({"line": self._byte_line(caps, a.kind, a.time, hz)} if a.kind in _LINE_NAMES else {})}
                         for a in resolve(events, line_time=line_time, output_ticks=output_ticks)]
            return {"written": written, "lost": lost, "first": first, "next": first + records, **extra,
                    "items": items}

    def _byte_line(self, caps, kind: str, time: int, hz: float) -> dict:
        """バイトのイベントの端子の上の点の範囲（fprobe.timing.byte_edge_window を µs に）。SWO の方式は、この道具の swo_start で
        最後に選んだもの（swo_start を呼んでいなければ分からない = 範囲を返さない。ほかの道具が設定した方式は知らない）。"""
        # SWO の方式は、この道具の swo_start で最後に選んだもの（swo_start を呼んでいなければ分からない = None）
        mode = self._host._swo_mode if self._host._swo_baud is not None else None
        w = byte_edge_window(caps, kind, time, mode)
        return {"point": w["point"], "t_lo_us": None if w["t_lo"] is None else w["t_lo"] * 1e6 / hz,
                "t_hi_us": None if w["t_hi"] is None else w["t_hi"] * 1e6 / hz, "from": w["from"], "unknown": w["unknown"]}

    def _read_events(self, last: int, since: int | None) -> tuple[int, int, int, int, list, dict]:
        """イベントの記録の窓（events_read の説明）: (written, lost, first, 返した記録の件数, events, extra)。番号は記録
        （16 バイト）の件数で数える（詰めた形の記録は複数のイベントになる）。ロックの中で呼ぶ。"""
        self._host._la_idle()
        log = EventLog(self._host.session.device)
        written, lost = log.counts()
        extra = {}
        if since is None:
            events = log.read(last, end=written)
            first = written - log.records
        else:
            since = since if 0 <= since <= written else 0      # 範囲の外 = 記録が始め直された: 最初から
            first = max(since, written - log.capacity)
            extra["skipped"] = first - since
            n = min(last, written - first)
            events = log.read(n, end=first + n) if n > 0 else []
            if n > 0 and log.overwritten:           # 読む間に上書きされた先頭: 返した最初の番号は後ろにずれ、読み飛ばしに数える
                first += log.overwritten
                extra["skipped"] += log.overwritten
        return written, lost, first, log.records, events, extra
