"""Probe の道具: 既存のログ・トレースの形式の取り込み（U14・U15）。

Probe（fprobe.mcp_server）に混ぜて使う mixin。Probe の _lock・session・target・_symbols・_recorder・_debug と、
ObserveTools（RTT の受信 `rtt_start`・`_rtt_listen`・`_rtt`）、WindowTools（デバイスの時刻付きの SWO・UART の
バイトの窓 `_open_window`・`_window_cursor`・`_window_new`・write_registers）、RtosTools（時間軸の結果
`_rtos_last` = 解析の道具が読むもの）を使う。機器に触るのは必ず _lock の中。

- ログ（fprobe.logfmt）: defmt・Trice・compact・memfault・text を RTT（listener）・SWO（ITM のポート）・UART の
  流れから解読し、行の貯め場（log_read）と記録の stream "log" へ。本文の中のコードの番地は関数・ファイル:行に。
- トレース（fprobe.tracefmt）: SystemView・Zephyr の CTF・Tracealyzer を流れから、Event Recorder を RAM から周期的に
  （止めずに）、SystemView・CTF・TraceX・Event Recorder を RAM から一度に（スナップショット・ポストモーテム）、
  ファイルから。結果は出来事（stream "trace"）と、タスクの出入りがあれば時間軸（stream "rtos" の run / switch /
  gap。rtos_timeline_load 等の解析がそのまま使える）。
- SCVD（Component Viewer）: scvd_load → scvd_view（ターゲットのメモリを読んで表示の木）。Event Recorder の書式にも。

時刻: SWO・UART はバイトを FPGA が受けた時刻（タイムベース）。RTT はホストで受けた時刻を時計の対応付けで写す
（不確かさ = RTT の読みの周期程度）。ターゲットの時刻を持つ形式（SystemView・CTF・Event Recorder 等）は、最初に
解読した出来事をその受けた時刻に合わせ、以後はターゲットの時刻の速さで写す（ずれは補正しない）。RAM の
スナップショットは最後の出来事を読んだ時刻に合わせる（近似）。

仕様: docs/superpowers/specs/2026-10-18-u14-u15-trace-log-formats-design.md
"""

from __future__ import annotations

import collections
import dataclasses
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .debug import PyocdAccess
from .logfmt.addresses import AddressAnnotator
from .logfmt.record import STREAM as LOG_STREAM, LogRecord, level_at_least
from .logfmt.stream import FORMATS as LOG_FORMATS, LogStream
from .probe_window import EventWindow, WindowCursor, write_registers
from .profile.dwt import ITM_TER, RegisterWrite, itm_port_writes, plan_swo
from .recording import ClockMap
from .rtos.timeline import timeline_from_events, write_timeline
from .testpoint import TimedSwo
from .tracefmt.model import TargetClock, Trace, TraceEvent, activations, task_events, write_trace
from .tracefmt.scvd import Scvd, ScvdContext
from .tracefmt.stream import (CHANNELS, SNAPSHOT_FORMATS, STREAM_FORMATS, TraceStream,
                              decode_trace, snapshot)
from .tracefmt.systemview import COMMAND_START as SV_START, COMMAND_STOP as SV_STOP
from .tracefmt.tracealyzer import COMMAND_START as TZ_START, COMMAND_STOP as TZ_STOP, CONTROL_NAME as TZ_CONTROL

__all__ = ["FormatTools", "LOG_LIMIT", "SOURCES"]

SOURCES = ("rtt", "swo", "uart")
LOG_LIMIT = 10_000                       # 貯めておく行の数（それより古いものは捨てる。全部は記録の stream "log"）
EVENTS_LIMIT = 200_000                   # トレースの出来事を持っておく数
PULL_S = 0.1                             # SWO・UART の窓・Event Recorder の RAM を読む周期
_START_COMMANDS = {"systemview": (CHANNELS["systemview"], bytes([SV_START]), bytes([SV_STOP])),
                   "tracealyzer": (TZ_CONTROL, TZ_START, TZ_STOP)}
_PRIORITY_ORDER = {"ctf": -1, "tracex": -1}            # 優先度の向き（1 = 大きい方が高い）。Zephyr・ThreadX は小さい方


@dataclass
class _Feed:
    """1 つの受け取り（ログかトレース）。"""
    kind: str                                        # log / trace
    format: str
    source: str                                      # rtt / swo / uart / memory
    settings: dict
    decoder: object                                  # LogStream / TraceStream / None（memory）
    started: float
    timebase_hz: float | None = None
    rtt_channel: int | None = None
    listener: object = None
    started_rtt: bool = False
    cursor: WindowCursor | None = None
    itm: TimedSwo | None = None
    port: int | None = None
    restore: dict[int, int] = field(default_factory=dict)
    pump: threading.Thread | None = None
    halt: threading.Event = field(default_factory=threading.Event)
    error: str | None = None
    stopped: float | None = None
    host_clock: ClockMap | None = None               # 記録していないときのホストの時計 → タイムベース
    bytes: int = 0
    warnings: list[str] = field(default_factory=list)
    # ログ
    records: collections.deque = field(default_factory=lambda: collections.deque(maxlen=LOG_LIMIT))
    seq: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    # トレース
    trace: Trace | None = None
    clock: TargetClock | None = None
    written: int = 0                                 # 記録に書いた出来事の数
    since: int | None = None                         # eventrecorder: 次に読む通し番号
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def running(self) -> bool:
        return self.stopped is None


class FormatTools:
    """既存のログ・トレースの形式の取り込み（Probe の mixin）。"""

    def _init_formats(self) -> None:
        self._log_feed: _Feed | None = None
        self._log_last: _Feed | None = None
        self._trace_feed: _Feed | None = None
        self._trace_last: _Feed | None = None
        self._scvd: Scvd | None = None
        self._scvd_files: list[str] = []

    def _close_formats(self) -> None:
        """Probe.close から: 背景のスレッドを止める。"""
        for feed in (self._log_feed, self._trace_feed):
            if feed is not None and feed.pump is not None:
                feed.halt.set()
                feed.pump.join(timeout=2)

    # --- 共通: 受け口 --------------------------------------------------------------------------

    def _feed_start(self, feed: _Feed, channel: int | str | None, port: int, swo_baud: int | None,
                    core_clock_hz: float | None) -> None:
        """受け口を始める（rtt: listener、swo / uart: 窓を読む背景のスレッド）。"""
        if feed.source == "rtt":
            self._feed_start_rtt(feed, channel)
        elif feed.source == "swo":
            self._feed_start_swo(feed, port, swo_baud, core_clock_hz)
        elif feed.source == "uart":
            with self._lock:
                feed.timebase_hz = self.session.device.capabilities().timebase_hz
                feed.cursor = self._window_cursor(self._open_window(), "uart")
            feed.pump = threading.Thread(target=self._feed_pump, args=(feed,), name=f"fprobe-{feed.kind}-uart",
                                         daemon=True)
            feed.pump.start()
        else:
            raise ValueError(f"source は {' / '.join(SOURCES)}")

    def _feed_start_rtt(self, feed: _Feed, channel: int | str | None) -> None:
        feed.started_rtt = self._rtt is None
        if feed.started_rtt:
            self.rtt_start()
        try:
            with self._lock:
                up = self._rtt.control_block.up
                feed.rtt_channel = self._rtt_channel_index(up, channel, feed.format)
                feed.timebase_hz = self._timebase_hz()
                if self._recorder is None and feed.timebase_hz:
                    feed.host_clock = self._host_clock(feed.timebase_hz)
        except Exception:
            if feed.started_rtt:
                self.rtt_stop()
            raise

        def listener(key, data: bytes, host_s: float, feed=feed) -> None:
            if key == feed.rtt_channel and feed.running:
                self._feed_bytes(feed, data, host_s=host_s)
        feed.listener = listener
        self._rtt_listen(listener)
        if feed.kind == "trace" and feed.format in _START_COMMANDS and feed.settings.get("send_start", True):
            self._send_command(feed, start=True)

    @staticmethod
    def _rtt_channel_index(up, channel: int | str | None, format: str) -> int:
        names = {c.index: c.name for c in up}
        if isinstance(channel, int):
            if channel not in names:
                raise ValueError(f"RTT の上りのチャンネル {channel} が無い（{sorted(names)}）")
            return channel
        wanted = channel or {"defmt": "defmt", **CHANNELS}.get(format)
        for index, name in names.items():
            if wanted is not None and name == wanted:
                return index
        if channel is not None:
            raise ValueError(f"RTT の上りに {channel!r} という名前のチャンネルが無い（{names}）")
        return 0

    def _send_command(self, feed: _Feed, start: bool) -> None:
        """SystemView・Tracealyzer: 下りのチャンネルに開始・停止のコマンドを書く。"""
        name, on, off = _START_COMMANDS[feed.format]
        with self._lock:
            if self._rtt is None:
                return
            down = [c for c in self._rtt.control_block.down if c.name == name]
            if not down:
                feed.warnings.append(f"RTT の下りに {name} が無い: ターゲットが記録を始めていなければ届かない"
                                     "（ターゲット側で開始する設定なら問題ない）")
                return
            written = self._rtt.write(down[0].index, on if start else off)
        if written < len(on if start else off):
            feed.warnings.append(f"{name} にコマンドを書き切れなかった（下りのバッファが一杯）")

    def _feed_start_swo(self, feed: _Feed, port: int, swo_baud: int | None, core_clock_hz: float | None) -> None:
        if not 0 <= port < 32:
            raise ValueError("port は 0〜31")
        if swo_baud is None and self._swo_baud is None:
            raise RuntimeError("SWO の受信の速度が分からない（swo_baud を渡すか swo_start を先に）")
        with self._lock:
            target = self.target
            setup = itm_port_writes(port)[:-1]
            baud = swo_baud
            if swo_baud is not None and core_clock_hz is not None:
                plan = plan_swo(core_clock_hz, swo_baud)
                setup += list(plan.writes)
                feed.warnings += plan.warnings
                baud = round(plan.baud)
            # 受信を始めてから出力を有効にする（逆だとパケットの途中から窓が始まる。testpoint と同じ）
            before = write_registers(target, setup + [RegisterWrite(ITM_TER, 0, note="ITM: 全ポートを止める")])
            try:
                if baud is not None:
                    self.swo_start(baud)
                window = self._open_window()
                target.write_memory(ITM_TER, before[ITM_TER] | 1 << port)
            except Exception:
                target.write_memory(ITM_TER, before[ITM_TER])
                raise
            feed.timebase_hz = self.session.device.capabilities().timebase_hz
        feed.restore = {ITM_TER: before[ITM_TER]}
        feed.cursor, feed.itm, feed.port = self._window_cursor(window, "swo"), TimedSwo(), port
        feed.settings["swo_baud"] = baud or self._swo_baud
        feed.pump = threading.Thread(target=self._feed_pump, args=(feed,), name=f"fprobe-{feed.kind}-swo", daemon=True)
        feed.pump.start()

    def _feed_pump(self, feed: _Feed) -> None:
        """背景: 窓の新しいバイト（SWO・UART）か、Event Recorder の RAM を周期的に読む。"""
        while not feed.halt.wait(PULL_S):
            try:
                self._feed_pull(feed)
            except Exception as e:                                # 切断等: 理由を残して止める
                feed.error = f"{type(e).__name__}: {e}"
                return

    def _feed_pull(self, feed: _Feed) -> None:
        with feed.lock:
            if feed.source == "memory":
                self._er_poll(feed)
                return
            events = self._window_new(feed.cursor)
            if feed.source == "uart":
                for t, _, b in events:
                    self._feed_bytes(feed, bytes([b]), t=t)
                return
            from .itm import Instrumentation
            for t, packet in feed.itm.feed([(t, b) for t, _, b in events]):
                if isinstance(packet, Instrumentation) and packet.port == feed.port:
                    self._feed_bytes(feed, packet.value.to_bytes(packet.size, "little"), t=t)

    # --- 共通: 時刻 ----------------------------------------------------------------------------

    def _timebase_hz(self) -> float | None:
        try:
            return self.session.device.capabilities().timebase_hz
        except Exception:                                         # 機器が無い（オフライン）
            return None

    def _host_clock(self, hz: float) -> ClockMap:
        """記録していないときのホストの時計 → タイムベース（いまのタイムベースを 1 回読む。_lock の中で）。"""
        clock = ClockMap(hz)
        device = self.session.device
        h0 = time.monotonic()
        ticks = device.time()
        h1 = time.monotonic()
        clock.add((h0 + h1) / 2, ticks, h1 - h0)
        return clock

    def _arrival(self, feed: _Feed, host_s: float | None, t: int | None) -> tuple[int | None, float | None]:
        """受けた時刻（タイムベース, 不確かさ）。"""
        if t is not None:
            byte_s = 10 / feed.settings["swo_baud"] if feed.source == "swo" and feed.settings.get("swo_baud") else 0
            return t, byte_s * (feed.timebase_hz or 0) or None
        if host_s is None:
            return None, None
        poll = getattr(self, "poll_s", 0.01) * (feed.timebase_hz or 0)
        rec = self._recorder
        try:
            if rec is not None:
                ticks, unc = rec.clock.to_ticks(host_s)
            elif feed.host_clock is not None:
                ticks, unc = feed.host_clock.to_ticks(host_s)
            else:
                return None, None
        except RuntimeError:                                     # 時計の対応付けの前
            return None, None
        return ticks, unc + poll

    def _feed_bytes(self, feed: _Feed, data: bytes, host_s: float | None = None, t: int | None = None) -> None:
        feed.bytes += len(data)
        arrival, unc = self._arrival(feed, host_s, t)
        if feed.kind == "log":
            self._log_received(feed, feed.decoder.feed(data), arrival, unc)
        else:
            self._trace_received(feed, feed.decoder.feed(data), arrival, unc)

    # --- ログ（U15） -------------------------------------------------------------------------------

    def _log_stream(self, format: str, elf: str | None, til: str | None, li: str | None, framing: str | None,
                    big_endian: bool, transport: str | None, annotate: bool) -> LogStream:
        symbols = self._symbols if elf is None else None
        return LogStream.create(format, elf=elf, symbols=symbols, til=til, li=li, framing=framing,
                                big_endian=big_endian, transport=transport, annotate=annotate)

    def log_start(self, format: str, source: str = "rtt", channel: int | str | None = None, port: int = 0,
                  elf: str | None = None, til: str | None = None, li: str | None = None, framing: str | None = None,
                  big_endian: bool = False, transport: str | None = None, annotate: bool = True,
                  swo_baud: int | None = None, core_clock_hz: float | None = None) -> dict:
        """ログの流れの解読を始める（止めずに。すぐ戻る）。format: defmt（ELF の .defmt の表）/ trice（til・li =
        til.json・li.json、framing tcobs / cobs / none）/ compact（ELF の書式の番地 + 引数の語）/ memfault（ELF の
        log_fmt、transport cbor / text）/ text（行）。source: rtt（channel は番号か名前。省略時は defmt なら "defmt"、
        ほかは 0）/ swo（ITM の port。swo_baud・core_clock_hz で受信と TPIU も設定）/ uart（プローブの UART の受信）。
        ELF は elf か読み込み済みのもの。annotate なら本文の中のコードの番地を関数・ファイル:行に。記録中なら
        stream "log"（kind = 重要度、解読できないものは undecoded）。"""
        if format not in LOG_FORMATS:
            raise ValueError(f"format は {' / '.join(LOG_FORMATS)}")
        if source not in SOURCES:
            raise ValueError(f"source は {' / '.join(SOURCES)}")
        if self._log_feed is not None:
            raise RuntimeError("ログの受け取りが動いている（log_stop を先に）")
        stream = self._log_stream(format, elf, til, li, framing, big_endian, transport, annotate)
        feed = _Feed("log", format, source, {"channel": channel, "port": port, "framing": framing,
                                             "annotate": stream.annotator is not None}, stream, time.monotonic())
        self._feed_start(feed, channel, port, swo_baud, core_clock_hz)
        if feed.rtt_channel is not None:
            feed.settings["channel"] = feed.rtt_channel
        self._log_feed = feed
        return self._log_state(feed)

    def _log_received(self, feed: _Feed, records: list[LogRecord], t: int | None, unc: float | None) -> None:
        rec = self._recorder
        for r in records:
            feed.seq += 1
            feed.counts[r.kind] = feed.counts.get(r.kind, 0) + 1
            feed.records.append((feed.seq, r, t, unc))
            if rec is not None and t is not None:
                rec.add(LOG_STREAM, r.kind, t, a=r.id or 0, body=r.to_dict(), uncertainty=unc)

    def log_stop(self) -> dict:
        """ログの受け取りを止める（貯めた行は log_read で読める）。"""
        feed = self._log_feed
        if feed is None:
            raise RuntimeError("ログを受け取っていない（log_start）")
        self._feed_stop(feed)
        self._log_feed, self._log_last = None, feed
        return self._log_state(feed)

    def _feed_stop(self, feed: _Feed) -> None:
        if feed.pump is not None:
            feed.halt.set()
            feed.pump.join(timeout=5)
            if feed.error is None and feed.source in ("swo", "uart", "memory"):
                try:
                    self._feed_pull(feed)                         # 止める前に届いた分
                except Exception as e:
                    feed.error = f"{type(e).__name__}: {e}"
        if feed.listener is not None:
            self._rtt_unlisten(feed.listener)
        if feed.kind == "trace" and feed.format in _START_COMMANDS and feed.source == "rtt" and \
                feed.settings.get("send_start", True):
            try:
                self._send_command(feed, start=False)
            except Exception as e:
                feed.warnings.append(f"停止のコマンドを送れなかった: {e}")
        with self._lock:
            if feed.restore and self._debug is not None:
                for address, value in feed.restore.items():
                    self.target.write_memory(address, value)
            if feed.cursor is not None:
                self._close_window(feed.cursor.window)
            feed.stopped = time.monotonic()
        if feed.started_rtt:
            self.rtt_stop()

    def _log_state(self, feed: _Feed) -> dict:
        out = {"running": feed.running, "format": feed.format, "source": feed.source, "settings": dict(feed.settings),
               "records": feed.seq, "kept": len(feed.records), "kinds": dict(feed.counts), "bytes": feed.bytes,
               "errors": feed.decoder.errors, "recorded": self._recorder is not None,
               "elapsed_s": (feed.stopped or time.monotonic()) - feed.started, "warnings": list(feed.warnings)}
        if feed.error is not None:
            out["error"] = feed.error
        if feed.cursor is not None:
            out["window"] = self._window_info(feed.cursor.window, feed.timebase_hz or 1.0)
        return out

    def log_status(self) -> dict:
        """ログの受け取りの様子（形式・行の数・重要度ごとの数・解読できなかった数・受けたバイト）。"""
        feed = self._log_feed or self._log_last
        return {"running": False} if feed is None else self._log_state(feed)

    def log_read(self, since: int = 0, limit: int = 200, level: str | None = None, contains: str | None = None) -> dict:
        """解読したログの行（since の番号より後を limit 件）。level 以上の重要度だけ（重要度の無い行は常に）、
        contains を含むものだけ。next を次の since に。t は受けた時刻（タイムベース）。"""
        feed = self._log_feed or self._log_last
        if feed is None:
            raise RuntimeError("ログが無い（log_start）")
        out, last = [], since
        for seq, r, t, unc in list(feed.records):
            if seq <= since:
                continue
            last = seq
            if not level_at_least(r.level, level) or (contains and contains not in r.text):
                continue
            item = {"seq": seq, **r.to_dict()}
            if t is not None:
                item["t"] = t
            out.append(item)
            if len(out) >= limit:
                break
        dropped = max(0, feed.seq - len(feed.records) - since)
        return {"records": out, "next": last, "dropped": dropped, "running": feed.running}

    def log_decode(self, format: str, data_hex: str | None = None, path: str | None = None, elf: str | None = None,
                   til: str | None = None, li: str | None = None, framing: str | None = None,
                   big_endian: bool = False, transport: str | None = None, limit: int = 1000) -> dict:
        """ログのバイト（data_hex か path のファイル）を解読する（機器に触らない）。形式と引数は log_start と同じ。"""
        if (data_hex is None) == (path is None):
            raise ValueError("data_hex か path のどちらか 1 つ")
        data = bytes.fromhex(data_hex) if data_hex is not None else Path(path).read_bytes()
        stream = self._log_stream(format, elf, til, li, framing, big_endian, transport, True)
        records = stream.feed(data)
        return {"format": format, "bytes": len(data), "count": len(records), "errors": stream.errors,
                "records": [r.to_dict() for r in records[:limit]], "truncated": len(records) > limit}

    def log_annotate(self, text: str, elf: str | None = None) -> dict:
        """文字（UART のパニックの出力等）の中のコードの番地を、ELF で関数・ファイル:行にする（機器に触らない）。"""
        from .symbols import Symbols
        symbols = Symbols(elf) if elf is not None else self._symbols
        if symbols is None:
            raise RuntimeError("ELF が読み込まれていない（elf か debug_elf_load）")
        annotator = AddressAnnotator(symbols)
        lines = text.splitlines()
        return {"text": "\n".join(annotator.annotate(line) for line in lines),
                "addresses": [h.to_dict() for line in lines for h in annotator.find(line)]}

    # --- トレース（U14） ---------------------------------------------------------------------------

    def _elf_strings(self):
        if self._symbols is None:
            return None
        from .logfmt.compact import ElfStrings
        return ElfStrings(self._symbols.path).get

    @staticmethod
    def _text_file(path: str | None) -> str | None:
        return Path(path).read_text(encoding="utf-8") if path else None

    def trace_start(self, format: str, source: str = "rtt", channel: int | str | None = None, port: int = 1,
                    description: str | None = None, metadata: str | None = None, ctf_header: str = "ts32_id8",
                    send_start: bool = True, period_s: float = PULL_S, address: int | None = None,
                    swo_baud: int | None = None,
                    core_clock_hz: float | None = None) -> dict:
        """トレースの流れの取り込みを始める（すぐ戻る）。format: systemview（RTT の "SysView"。send_start なら
        開始のコマンドを書く）/ ctf（Zephyr。metadata = ビルドの TSDL のファイル、無ければ組み込みの表と ctf_header
        ts32_id8 / ts32_id16 / ts64_id16 / id16）/ tracealyzer（RTT の "TzData"、開始のコマンドは "TzCtrl"）/
        eventrecorder（source memory: RAM を period_s ごとに止めずに読む。EventRecorderInfo か address。書式は
        scvd_load の SCVD）。source: rtt /
        swo（ITM の port）/ uart / memory。description は SystemView の記述ファイル（SYSVIEW_<OS>.txt）。記録中なら
        出来事を stream "trace" に。trace_stop で時間軸（stream "rtos"・rtos_timeline_* の解析）にもする。"""
        if self._trace_feed is not None:
            raise RuntimeError("トレースの取り込みが動いている（trace_stop を先に）")
        if format == "eventrecorder":
            if source not in ("memory", "rtt"):
                raise ValueError("eventrecorder は source memory（RAM を周期的に読む）")
            return self._er_start(period_s, address)
        if format not in STREAM_FORMATS:
            raise ValueError(f"流れで受けられる形式は {' / '.join(STREAM_FORMATS)} と eventrecorder（memory）")
        if source not in SOURCES:
            raise ValueError(f"source は {' / '.join(SOURCES)}")
        stream = TraceStream.create(format, description=self._text_file(description),
                                    metadata=self._text_file(metadata), ctf_header=ctf_header,
                                    strings=self._elf_strings())
        feed = _Feed("trace", format, source, {"channel": channel, "port": port, "send_start": send_start,
                                               "description": description, "metadata": metadata,
                                               "ctf_header": ctf_header}, stream, time.monotonic())
        feed.trace = stream.trace
        self._feed_start(feed, channel, port, swo_baud, core_clock_hz)
        if feed.rtt_channel is not None:
            feed.settings["channel"] = feed.rtt_channel
        self._trace_feed = feed
        return self._trace_state(feed)

    def _trace_received(self, feed: _Feed, events: list[TraceEvent], t: int | None, unc: float | None) -> None:
        if not events:
            return
        trace = feed.trace
        if len(trace.events) > EVENTS_LIMIT:
            del trace.events[:len(trace.events) - EVENTS_LIMIT]
            feed.warnings = [w for w in feed.warnings if not w.startswith("出来事が多い")] + [
                f"出来事が多い: 最近の {EVENTS_LIMIT} 個だけ持っている（全部は記録の stream \"trace\"）"]
        if feed.clock is None and trace.ts_hz and t is not None and feed.timebase_hz:
            feed.clock = TargetClock(trace.ts_hz, feed.timebase_hz, events[-1].ts, t, unc or 0.0)
        rec = self._recorder
        if rec is not None and feed.clock is not None:
            write_trace(rec, trace, feed.clock, events)
            feed.written += len(events)

    def trace_stop(self, timeline: bool = True) -> dict:
        """トレースの取り込みを止める。要約（出来事の種類ごとの数・タスク・失った数・時刻の速さ）を返し、timeline なら
        タスクの出入りを時間軸にして rtos_timeline_get / rtos_timeline_load 等で解析できるようにする（記録中なら
        stream "rtos" にも）。"""
        feed = self._trace_feed
        if feed is None:
            raise RuntimeError("トレースを取り込んでいない（trace_start）")
        self._feed_stop(feed)
        self._trace_feed, self._trace_last = None, feed
        out = self._trace_state(feed)
        if timeline:
            out["timeline"] = self._publish_timeline(feed)
        return out

    def _trace_state(self, feed: _Feed) -> dict:
        trace = feed.trace
        out = {"running": feed.running, "format": feed.format, "source": feed.source,
               "settings": {k: v for k, v in feed.settings.items() if v is not None}, "bytes": feed.bytes,
               **trace.summary(), "recorded_events": feed.written, "warnings": list(feed.warnings),
               "elapsed_s": (feed.stopped or time.monotonic()) - feed.started}
        out["notes"] = list(trace.notes)
        if feed.clock is not None:
            out["clock"] = {"anchor_ts": feed.clock.anchor_ts, "anchor_t": feed.clock.anchor_t,
                            "uncertainty": feed.clock.uncertainty, "timebase_hz": feed.timebase_hz}
        elif trace.events:
            out["notes"].append("時刻の対応が無い（ターゲットの時刻の速さか受けた時刻が分からない）: 記録には書かない")
        if feed.error is not None:
            out["error"] = feed.error
        if feed.cursor is not None:
            out["window"] = self._window_info(feed.cursor.window, feed.timebase_hz or 1.0)
        return out

    def trace_status(self) -> dict:
        """トレースの取り込みの様子（形式・出来事の数・タスク・失った数・時刻の対応）。"""
        feed = self._trace_feed or self._trace_last
        if feed is None:
            return {"running": False}
        return self._trace_state(feed)

    def trace_events(self, offset: int = 0, limit: int = 200, kinds: list[str] | None = None,
                     task: int | None = None) -> dict:
        """取り込んだトレース（取り込み中・最後の結果・スナップショット・ファイル）の出来事を offset から limit 件。
        kinds（task_in / isr_enter / print / marker / object 等）・task（タスクの ID・番地）で絞る。t は
        タイムベース（時刻の対応があるとき）。"""
        feed = self._trace_feed or self._trace_last
        if feed is None:
            raise RuntimeError("トレースが無い（trace_start / trace_snapshot / trace_import）")
        events = [e for e in list(feed.trace.events)
                  if (kinds is None or e.kind in kinds) and (task is None or e.task == task)]
        names = feed.trace.names()
        out = []
        for e in events[offset:offset + limit]:
            item = e.to_dict()
            if feed.clock is not None:
                item["t"] = feed.clock.to_timebase(e.ts)
            if e.task is not None and e.task in names:
                item["task_name"] = names[e.task]
            out.append(item)
        return {"format": feed.format, "total": len(events), "offset": offset, "events": out}

    def _publish_timeline(self, feed: _Feed) -> dict:
        """タスクの出入りを時間軸にし、RtosTools の最後の結果にする（解析の道具が読む）。記録中なら stream "rtos"。"""
        from .probe_rtos import _TimelineResult, _TimelineRun
        trace = feed.trace
        clock = feed.clock
        hz = feed.timebase_hz
        if clock is None:
            if not trace.ts_hz:
                return {"made": False, "reason": "ターゲットの時刻の速さが分からない（ts_hz）"}
            clock = TargetClock(trace.ts_hz, trace.ts_hz, 0, 0)          # 機器の無い取り込み: ターゲットの時刻のまま
            hz = trace.ts_hz
        events = task_events(trace.events, clock)
        if not any(e.kind == "task_in" for e in events):
            return {"made": False, "reason": "タスクの切り替えの出来事が無い"}
        if getattr(self, "_rtos_run", None) is not None:
            return {"made": False, "reason": "rtos_timeline_start の時間軸を取っている最中（止めてから）"}
        names = {k: v for k, v in trace.names().items()}
        resolution = max(1.0, clock.ratio)
        tl = timeline_from_events(events, names, resolution=resolution,
                                  t_end=clock.to_timebase(trace.events[-1].ts) if trace.events else None)
        tl.method = trace.format
        isr = activations(trace.events, clock) if any(e.kind == "isr_enter" for e in trace.events) else None
        run = _TimelineRun(trace.format, EventWindow(tl.t0, False, t1=tl.t1), names, hz, resolution=resolution,
                           order=_PRIORITY_ORDER.get(trace.format, self._priority_order(trace)),
                           priorities={t.id: t.priority for t in trace.tasks.values() if t.priority is not None},
                           idle=set(trace.idle), exceptions=isr is not None)
        run.warnings.append(f"取り込んだトレース（{trace.format}）から作った時間軸" +
                            ("" if feed.clock is not None else "（時刻はターゲットの時計のまま）"))
        self._rtos_last = _TimelineResult(tl, events, run, activations=isr)
        rec = self._recorder
        recorded = rec is not None and feed.clock is not None
        if recorded:
            write_timeline(rec, tl)
        return {"made": True, "runs": len(tl.runs), "switches": len(tl.switches), "gaps": len(tl.gaps),
                "isr": len(isr) if isr is not None else None, "recorded": recorded, "timebase_hz": hz,
                "stats": tl.stats()}

    @staticmethod
    def _priority_order(trace: Trace) -> int:
        os_name = str(trace.system.get("O", "")).lower()
        return -1 if "zephyr" in os_name or "threadx" in os_name else 1

    # --- スナップショット・ファイル ---------------------------------------------------------------

    def _symbol_lookup(self, name: str) -> tuple[int, int] | None:
        if self._symbols is None:
            return None
        try:
            s = self._symbols.variable(name)
        except KeyError:
            try:
                s = self._symbols.symbol(name)
            except KeyError:
                return None
        return s.address, s.size

    def trace_snapshot(self, format: str, address: int | None = None, size: int | None = None,
                       description: str | None = None,
                       metadata: str | None = None, ctf_header: str = "ts32_id8", ts_hz: float | None = None,
                       timeline: bool = True, rtt_address: int | None = None) -> dict:
        """ターゲットの RAM のトレースのバッファを吸い出して解読する（ポストモーテム・スナップショット。止めなくてよいが、
        止めた方が書きかけが無い）。format: systemview（RTT の "SysView" のリングを消費せずに。rtt_address 省略時は
        rtt_start の制御ブロック）/ ctf（Zephyr の ram_tracing。address なら size も）/ tracex（_tx_trace_header_ptr か address。ts_hz =
        時刻の速さ）/ eventrecorder（EventRecorderInfo か address。書式は scvd_load の SCVD）。スナップショットを
        開いていれば（snapshot_open）その RAM から。最後の出来事を読んだ時刻に合わせて記録・時間軸にする（近似）。"""
        if format not in SNAPSHOT_FORMATS:
            raise ValueError(f"RAM から吸い出せる形式は {' / '.join(SNAPSHOT_FORMATS)}")
        with self._lock:
            memory = PyocdAccess(self.target)
            if format == "systemview" and rtt_address is None:
                if self._rtt is not None:
                    rtt_address = self._rtt.address
                else:
                    found = self._symbol_lookup("_SEGGER_RTT")
                    rtt_address = found[0] if found else None
            trace, info = snapshot(format, memory.read_memory, self._symbol_lookup, rtt_address=rtt_address,
                                   memory=memory, address=address, size=size, description=self._text_file(description),
                                   metadata=self._text_file(metadata), ctf_header=ctf_header,
                                   strings=self._elf_strings(), ts_hz=ts_hz, scvd=self._scvd,
                                   scvd_context=self._scvd_context(memory) if format == "eventrecorder" else None)
            hz = self._timebase_hz() if self._debug is not None else None
            now = self.session.device.time() if hz else None
        feed = _Feed("trace", format, "memory", {"address": info.get("address"), "snapshot": True}, None,
                     time.monotonic(), timebase_hz=hz, trace=trace)
        feed.stopped = feed.started
        if hz and trace.ts_hz and trace.events:
            feed.clock = TargetClock(trace.ts_hz, hz, trace.events[-1].ts, now, 0.0)
            feed.warnings.append("時刻の対応は、最後の出来事を読んだ時刻に合わせた近似（読むまでの時間だけ遅れる）")
            if self._recorder is not None:
                write_trace(self._recorder, trace, feed.clock)
                feed.written = len(trace.events)
        self._trace_last = feed
        out = {**self._trace_state(feed), "read": info}
        if timeline:
            out["timeline"] = self._publish_timeline(feed)
        return out

    def trace_import(self, format: str, path: str, description: str | None = None, metadata: str | None = None,
                     ctf_header: str = "ts32_id8", ts_hz: float | None = None, ram_buffer: bool = False,
                     timeline: bool = True) -> dict:
        """トレースのファイル（SystemView の .SVDat・CTF のストリーム・Tracealyzer の PSF・TraceX のバッファのダンプ）を
        解読する（機器に触らない）。時刻はターゲットの時刻のまま（記録には書かない）。timeline なら時間軸の解析にも。"""
        data = Path(path).read_bytes()
        trace = decode_trace(format, data, description=self._text_file(description),
                             metadata=self._text_file(metadata), ctf_header=ctf_header, strings=self._elf_strings(),
                             ts_hz=ts_hz, ram_buffer=ram_buffer)
        feed = _Feed("trace", format, "file", {"path": str(path)}, None, time.monotonic(), trace=trace)
        feed.stopped = feed.started
        self._trace_last = feed
        out = {**self._trace_state(feed), "bytes": len(data)}
        if timeline:
            out["timeline"] = self._publish_timeline(feed)
        return out

    # --- Event Recorder を止めずに読む ---------------------------------------------------------------

    def _er_start(self, period_s: float, address: int | None) -> dict:
        if period_s <= 0:
            raise ValueError("period_s は正")
        feed = _Feed("trace", "eventrecorder", "memory", {"period_s": period_s, "address": address}, None,
                     time.monotonic(),
                     trace=Trace("eventrecorder"))
        with self._lock:
            feed.timebase_hz = self._timebase_hz()
        self._er_poll(feed)                                        # 初めの読み（無ければここで誤り）
        feed.pump = threading.Thread(target=self._er_pump, args=(feed, period_s), name="fprobe-eventrecorder",
                                     daemon=True)
        feed.pump.start()
        self._trace_feed = feed
        return self._trace_state(feed)

    def _er_pump(self, feed: _Feed, period_s: float) -> None:
        while not feed.halt.wait(period_s):
            try:
                with feed.lock:
                    self._er_poll(feed)
            except Exception as e:
                feed.error = f"{type(e).__name__}: {e}"
                return

    def _er_poll(self, feed: _Feed) -> None:
        with self._lock:
            memory = PyocdAccess(self.target)
            part, info = snapshot("eventrecorder", memory.read_memory, self._symbol_lookup,
                                  address=feed.settings.get("address"), scvd=self._scvd,
                                  scvd_context=self._scvd_context(memory), since=feed.since)
            now = self.session.device.time() if feed.timebase_hz else None
        trace = feed.trace
        trace.ts_hz = trace.ts_hz or part.ts_hz
        trace.system = part.system
        trace.lost += part.lost
        trace.notes += [n for n in part.notes if n not in trace.notes]
        for task in part.tasks.values():
            info_t = trace.task(task.id)
            info_t.name = task.name or info_t.name
        trace.idle |= part.idle
        if feed.since is not None and part.events:
            last = trace.events[-1].ts if trace.events else None
            if last is not None:                                   # 32 ビットの時刻を前の読みから続ける
                from .tracefmt.model import unwrap32
                base = unwrap32(last, part.events[0].ts & 0xFFFFFFFF) - (part.events[0].ts & 0xFFFFFFFF)
                part.events = [dataclasses.replace(e, ts=e.ts + base) for e in part.events]
        feed.since = info["next"]
        trace.events.extend(part.events)
        if feed.clock is None and trace.ts_hz and part.events and now is not None:
            feed.clock = TargetClock(trace.ts_hz, feed.timebase_hz, part.events[-1].ts, now, 0.0)
            feed.warnings.append("時刻の対応は、最初に読んだ最後の出来事を読んだ時刻に合わせた近似")
        if self._recorder is not None and feed.clock is not None and part.events:
            write_trace(self._recorder, trace, feed.clock, part.events)
            feed.written += len(part.events)

    # --- SCVD（Component Viewer） ----------------------------------------------------------------

    def scvd_load(self, paths: list[str]) -> dict:
        """SCVD のファイル（RTX5.scvd・EventRecorder.scvd・ミドルウェアのもの）を読む。scvd_view の表示と、
        Event Recorder の出来事の名前・書式に使う（前に読んだものに足す。同じ ID は後のものが勝つ）。"""
        scvd = self._scvd or Scvd()
        for p in paths:
            scvd.add(Path(p).read_text(encoding="utf-8"), str(p))
            self._scvd_files.append(str(p))
        self._scvd = scvd
        return {"files": list(self._scvd_files), "components": scvd.names, "typedefs": len(scvd.typedefs),
                "events": len(scvd.events), "objects": [o.get("name") for o in scvd.objects]}

    def _scvd_context(self, memory) -> ScvdContext:
        symbols = self._symbols

        def symbol_name(address: int) -> str | None:
            if symbols is None:
                return None
            found = symbols.symbol_at(address & ~1)
            return found[0].name if found is not None and found[1] == 0 else None

        def register(name: str) -> int | None:
            try:
                return self.target.read_core_register(name.lower())
            except Exception:
                return None
        running = False
        try:
            running = not self.target.is_halted() if hasattr(self.target, "is_halted") else False
        except Exception:
            pass
        return ScvdContext(read=memory.read_memory, symbol=self._symbol_lookup, symbol_name=symbol_name,
                           string=self._elf_strings(), register=register, running=running)

    def scvd_view(self) -> dict:
        """SCVD の objects（Component Viewer）を評価して、ターゲットのメモリから表示の木（property・value・children）を
        作る（止めずに読める。一貫させたいなら止めて）。読めない・式の誤りは notes。"""
        if self._scvd is None:
            raise RuntimeError("SCVD が読み込まれていない（scvd_load）")
        with self._lock:
            memory = PyocdAccess(self.target)
            view = self._scvd.view(self._scvd_context(memory))
        return {"files": list(self._scvd_files), "objects": view}
