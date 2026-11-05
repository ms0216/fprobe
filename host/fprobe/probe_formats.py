"""Probe の道具: ログの形式の取り込み（U15）と、ログ・トレースの受け口（rtt・swo・uart）の共通の処理。

Probe（fprobe.probe）が API の名前を委ねるサービス（fprobe.probe_service。ホストから使う名前は NEEDS）。Probe の _lock・session・target・_symbols・_recorder・_debug と、
RttService（RTT の受信 `rtt_start`・`_rtt_listen`・`_rtt`）、WindowService（デバイスの時刻付きの SWO・UART の
バイトの窓 `_open_window`・`_window_cursor`・`_window_new`・write_registers）、TraceService（`_trace_feed`）を使う。機器に触るのは必ず _lock の中。

- ログ（fprobe.logfmt）: defmt・Trice・compact・memfault・text を RTT（listener）・SWO（ITM のポート）・UART の
  流れから解読し、行の貯め場（log_read）と記録の stream "log" へ。本文の中の 0x の数のうち実行区画に入る値は関数・ファイル:行に。
- 受け口（_feed_start・_feed_stop）: ログもトレース（TraceService）も、受け取り _Feed（fprobe.probe_formats_feed）を作ってここで
  始める・止める。解読した結果は _Feed.deliver（作った側が決める）へ渡す。

時刻: SWO・UART はバイトを FPGA が受けた時刻（タイムベース）。RTT はホストで受けた時刻を時計の対応付けで写す
（不確かさ = RTT の読みの周期程度）。トレースの時刻の写し方は probe_trace。

仕様: docs/superpowers/specs/2026-10-18-u14-u15-trace-log-formats-design.md
"""

from __future__ import annotations

import functools
import threading
import time
from pathlib import Path

from .probe_formats_feed import LOG_LIMIT, PULL_S, SOURCES, _START_COMMANDS, _Feed, timebase_hz, with_error_and_window
from .logfmt.addresses import AddressAnnotator
from .logfmt.record import STREAM as LOG_STREAM, LogRecord, level_at_least
from .logfmt.stream import FORMATS as LOG_FORMATS, LogStream
from .probe_service import HostView
from .probe_window import write_registers
from .profile.dwt import ITM_TER, RegisterWrite, itm_port_writes, plan_swo
from .recording import ClockMap
from .testpoint import TimedSwo
from .tracefmt.stream import CHANNELS

__all__ = ["FormatService", "LOG_LIMIT", "SOURCES"]


class FormatService:
    """ログの形式の取り込みと、ログ・トレースの受け口（Probe のサービス）。"""

    API = ("log_annotate", "log_decode", "log_read", "log_start", "log_status", "log_stop")
    # ほかの領域（Probe の委譲）・試験に出す名前
    SHARED = ("_arrival", "_close_formats", "_feed_start", "_feed_stop", "_log_feed", "_rtt_channel_index")
    # ホスト（Probe の核と、ほかの領域）から使う名前
    NEEDS = ("_close_window", "_debug", "_lock", "_open_window", "_recorder", "_rtt", "_rtt_listen", "_rtt_unlisten",
             "_swo_baud", "_symbols", "_trace_feed", "_window_cursor", "_window_info", "_window_new", "rtt_start",
             "rtt_stop", "session", "swo_start", "target")

    def __init__(self, host):
        self._host = HostView(host, self.NEEDS, type(self).__name__)
        self._log_feed: _Feed | None = None
        self._log_last: _Feed | None = None

    def _close_formats(self) -> None:
        """Probe.close から: 背景のスレッドを止める。"""
        for feed in (self._log_feed, self._host._trace_feed):
            if feed is not None and feed.pump is not None:
                feed.halt.set()
                feed.pump.join(timeout=2)

    # --- 共通: 受け口 --------------------------------------------------------------------------

    def _feed_start(self, feed: _Feed, channel: int | str | None, port: int, swo_baud: int | None,
                    core_clock_hz: float | None) -> None:
        """受け口を始める（rtt: listener、swo / uart: 窓を読む背景のスレッド）。rtt で実際に選んだチャンネルは
        settings["channel"] に入れ直す。"""
        if feed.source == "rtt":
            self._feed_start_rtt(feed, channel)
            if feed.rtt_channel is not None:
                feed.settings["channel"] = feed.rtt_channel
        elif feed.source == "swo":
            self._feed_start_swo(feed, port, swo_baud, core_clock_hz)
        elif feed.source == "uart":
            with self._host._lock:
                feed.timebase_hz = self._host.session.device.capabilities().timebase_hz
                feed.cursor = self._host._window_cursor(self._host._open_window(), "uart")
            feed.pump = threading.Thread(target=self._feed_pump, args=(feed,), name=f"fprobe-{feed.kind}-uart",
                                         daemon=True)
            feed.pump.start()
        else:
            raise ValueError(f"source は {' / '.join(SOURCES)}")

    def _feed_start_rtt(self, feed: _Feed, channel: int | str | None) -> None:
        feed.started_rtt = self._host._rtt is None
        if feed.started_rtt:
            self._host.rtt_start()
        try:
            with self._host._lock:
                up = self._host._rtt.control_block.up
                feed.rtt_channel = self._rtt_channel_index(up, channel, feed.format)
                feed.timebase_hz = timebase_hz(self._host)
                if self._host._recorder is None and feed.timebase_hz:
                    feed.host_clock = self._host_clock(feed.timebase_hz)
        except Exception:
            if feed.started_rtt:
                self._host.rtt_stop()
            raise

        def listener(key, data: bytes, host_s: float, feed=feed) -> None:
            if key == feed.rtt_channel and feed.running:
                if feed.host_clock is not None and self._host._recorder is None:
                    self._clock_point(feed.host_clock)            # 背景のスレッドから、_lock の中で
                self._feed_bytes(feed, data, host_s=host_s)
        feed.listener = listener
        self._host._rtt_listen(listener)
        if feed.kind == "trace" and feed.format in _START_COMMANDS and feed.settings.get("send_start", True):
            self._send_command(feed, start=True)

    @staticmethod
    def _rtt_channel_index(up, channel: int | str | None, format: str) -> int:
        names = {c.index: c.name for c in up}
        if isinstance(channel, int):
            if channel not in names:
                raise ValueError(f"RTT の上りのチャンネル {channel} が無い（{sorted(names)}）")
            return channel
        # 省けば、形式が決める名前（SEGGER の SystemView = "SysView"・Percepio の Tracealyzer・defmt-rtt = "defmt"）。
        # その名前が無ければ誤り（ほかのチャンネルで代えない）。名前を決めていない形式は、SEGGER RTT の規定の端末の
        # チャンネル 0（UM08001「channel 0 は端末の入出力」）
        wanted = channel or {"defmt": "defmt", **CHANNELS}.get(format)
        if wanted is None:
            if 0 not in names:
                raise ValueError(f"RTT の上りにチャンネル 0 が無い: channel（番号か名前）を渡す（{names}）")
            return 0
        for index, name in names.items():
            if name == wanted:
                return index
        raise ValueError(f"RTT の上りに {wanted!r} という名前のチャンネルが無い: channel（番号か名前）を渡す（{names}）")

    def _send_command(self, feed: _Feed, start: bool) -> None:
        """SystemView・Tracealyzer: 下りのチャンネルに開始・停止のコマンドを書く。"""
        name, on, off = _START_COMMANDS[feed.format]
        with self._host._lock:
            if self._host._rtt is None:
                return
            down = [c for c in self._host._rtt.control_block.down if c.name == name]
            if not down:
                feed.warnings.append(f"RTT の下りに {name} が無い: ターゲットが記録を始めていなければ届かない"
                                     "（ターゲット側で開始する設定なら問題ない）")
                return
            written = self._host._rtt.write(down[0].index, on if start else off)
        if written < len(on if start else off):
            feed.warnings.append(f"{name} にコマンドを書き切れなかった（下りのバッファが一杯）")

    def _feed_start_swo(self, feed: _Feed, port: int, swo_baud: int | None, core_clock_hz: float | None) -> None:
        if not 0 <= port < 32:
            raise ValueError("port は 0〜31")
        if swo_baud is None and self._host._swo_baud is None:
            raise RuntimeError("SWO の受信の速度が分からない（swo_baud を渡すか swo_start を先に）")
        with self._host._lock:
            target = self._host.target
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
                    self._host.swo_start(baud)
                window = self._host._open_window()
                target.write_memory(ITM_TER, before[ITM_TER] | 1 << port)
            except Exception:
                target.write_memory(ITM_TER, before[ITM_TER])
                raise
            feed.timebase_hz = self._host.session.device.capabilities().timebase_hz
        feed.restore = {ITM_TER: before[ITM_TER]}
        feed.cursor, feed.itm, feed.port = self._host._window_cursor(window, "swo"), TimedSwo(), port
        feed.settings["swo_baud"] = baud or self._host._swo_baud
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
                feed.poll(feed)
                return
            events = self._host._window_new(feed.cursor)
            if feed.source == "uart":
                for t, _, b in events:
                    self._feed_bytes(feed, bytes([b]), t=t)
                return
            from .itm import Instrumentation
            for t, packet in feed.itm.feed([(t, b) for t, _, b in events]):
                if isinstance(packet, Instrumentation) and packet.port == feed.port:
                    self._feed_bytes(feed, packet.value.to_bytes(packet.size, "little"), t=t)

    # --- 共通: 時刻 ----------------------------------------------------------------------------

    def _host_clock(self, hz: float) -> ClockMap:
        """記録していないときのホストの時計 → タイムベース（いまのタイムベースを 1 回読む。_lock の中で）。受けるたびに
        _clock_point で組を足す（1 点から公称の速さで伸ばし続けない。ClockMap の不確かさは点の幅と距離で決まる）。"""
        clock = ClockMap(hz)
        self._clock_point(clock)
        return clock

    def _clock_point(self, clock: ClockMap) -> None:
        """いまのホストの時刻とタイムベースの組を足す（_lock の中で）。"""
        device = self._host.session.device
        h0 = time.monotonic()
        ticks = device.time()
        h1 = time.monotonic()
        clock.add((h0 + h1) / 2, ticks, h1 - h0)

    def _arrival(self, feed: _Feed, host_s: float | None, t: int | None) -> tuple[int | None, float | None]:
        """受けた時刻（タイムベース, 不確かさ）。どちらも対象の出来事の時刻の上限（出来事はそれより前）。
        - t（デバイスがバイトを受けた時刻。SWO・UART）: 端子の辺からの遅れは方式・分周で決まり（UART 方式と Manchester で
          違う。8N1 の 10 ビットを仮定しない）、対象の中の遅れは分からないので、不確かさは None（分からない）。
        - host_s（PC が RTT を読んだ時刻）: 不確かさは PC の時計の写しの分だけ。設定の読みの周期を幅として足さない
          （実際の間隔は周期と限らず、出来事が読みの何周期前かも分からない）。"""
        if t is not None:
            return t, None
        if host_s is None:
            return None, None
        rec = self._host._recorder
        try:
            if rec is not None:
                ticks, unc = rec.clock.to_ticks(host_s)
            elif feed.host_clock is not None:
                ticks, unc = feed.host_clock.to_ticks(host_s)
            else:
                return None, None
        except RuntimeError:                                     # 時計の対応付けの前
            return None, None
        return ticks, unc

    def _feed_bytes(self, feed: _Feed, data: bytes, host_s: float | None = None, t: int | None = None) -> None:
        feed.bytes += len(data)
        arrival, unc = self._arrival(feed, host_s, t)
        # 時刻の印: デバイスが受けた時刻か、PC が読んだ時刻か（どちらも出来事の上限）
        source = "device_received" if t is not None else "host_received"
        feed.deliver(feed.decoder.feed(data), arrival, unc, source)

    # --- ログ（U15） -------------------------------------------------------------------------------

    def _log_stream(self, format: str, elf: str | None, til: str | None, li: str | None, framing: str | None,
                    big_endian: bool, transport: str | None, annotate: bool,
                    channel_levels: dict[str, str] | None = None) -> LogStream:
        if channel_levels is not None and format != "trice":
            raise ValueError("channel_levels は trice だけ（行頭のチャンネルのタグ → 重要度）")
        symbols = self._host._symbols if elf is None else None
        return LogStream.create(format, elf=elf, symbols=symbols, til=til, li=li, framing=framing,
                                big_endian=big_endian, transport=transport, annotate=annotate,
                                channel_levels=channel_levels)

    def log_start(self, format: str, source: str = "rtt", channel: int | str | None = None, port: int = 0,
                  elf: str | None = None, til: str | None = None, li: str | None = None, framing: str | None = None,
                  big_endian: bool = False, transport: str | None = None, annotate: bool = True,
                  swo_baud: int | None = None, core_clock_hz: float | None = None,
                  channel_levels: dict[str, str] | None = None) -> dict:
        """ログの流れの解読を始める（止めずに。すぐ戻る）。format: defmt（ELF の .defmt の表）/ trice（til・li =
        til.json・li.json、framing tcobs / cobs / none。channel_levels = チャンネルとする行頭のタグ → 重要度（なしは None）を使う側が
        明示する。無ければチャンネル・重要度なし）/ compact（ELF の書式の番地 + 引数の語）/ memfault（ELF の
        log_fmt、transport cbor / text）/ text（行）。source: rtt（channel は番号か名前。省略時は defmt なら "defmt"（無ければ
        誤り）、ほかは SEGGER RTT の端末のチャンネル 0）/ swo（ITM の port。swo_baud・core_clock_hz で受信と TPIU も設定）/ uart（プローブの UART の受信）。
        ELF は elf か読み込み済みのもの。annotate なら本文の中の 0x の数のうち実行区画に入る値を関数・ファイル:行に。記録中なら
        stream "log"（kind = 重要度、解読できないものは undecoded）。"""
        if format not in LOG_FORMATS:
            raise ValueError(f"format は {' / '.join(LOG_FORMATS)}")
        if source not in SOURCES:
            raise ValueError(f"source は {' / '.join(SOURCES)}")
        if self._log_feed is not None:
            raise RuntimeError("ログの受け取りが動いている（log_stop を先に）")
        stream = self._log_stream(format, elf, til, li, framing, big_endian, transport, annotate, channel_levels)
        feed = _Feed("log", format, source, {"channel": channel, "port": port, "framing": framing,
                                             "annotate": stream.annotator is not None}, stream, time.monotonic())
        feed.deliver = functools.partial(self._log_received, feed)
        self._feed_start(feed, channel, port, swo_baud, core_clock_hz)
        self._log_feed = feed
        return self._log_state(feed)

    def _log_received(self, feed: _Feed, records: list[LogRecord], t: int | None, unc: float | None,
                      source: str = "host_received") -> None:
        rec = self._host._recorder
        for r in records:
            feed.seq += 1
            feed.counts[r.kind] = feed.counts.get(r.kind, 0) + 1
            feed.records.append((feed.seq, r, t, unc, source))
            if rec is not None and t is not None:
                rec.add(LOG_STREAM, r.kind, t, a=r.id or 0, body={**r.to_dict(), "time_source": source},
                        uncertainty=unc)

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
            self._host._rtt_unlisten(feed.listener)
        if feed.kind == "trace" and feed.format in _START_COMMANDS and feed.source == "rtt" and \
                feed.settings.get("send_start", True):
            try:
                self._send_command(feed, start=False)
            except Exception as e:
                feed.warnings.append(f"停止のコマンドを送れなかった: {e}")
        with self._host._lock:
            if feed.restore and self._host._debug is not None:
                for address, value in feed.restore.items():
                    self._host.target.write_memory(address, value)
            if feed.cursor is not None:
                self._host._close_window(feed.cursor.window)
            feed.stopped = time.monotonic()
        if feed.started_rtt:
            self._host.rtt_stop()

    def _log_state(self, feed: _Feed) -> dict:
        out = {"running": feed.running, "format": feed.format, "source": feed.source, "settings": dict(feed.settings),
               "records": feed.seq, "kept": len(feed.records), "kinds": dict(feed.counts), "bytes": feed.bytes,
               "errors": feed.decoder.errors, "recorded": self._host._recorder is not None,
               "elapsed_s": (feed.stopped or time.monotonic()) - feed.started, "warnings": list(feed.warnings)}
        return with_error_and_window(feed, out, self._host._window_info)

    def log_status(self) -> dict:
        """ログの受け取りの様子（形式・行の数・重要度ごとの数・解読できなかった数・受けたバイト）。"""
        feed = self._log_feed or self._log_last
        return {"running": False} if feed is None else self._log_state(feed)

    def log_read(self, since: int = 0, limit: int = 200, level: str | None = None, contains: str | None = None) -> dict:
        """解読したログの行（since の番号より後を limit 件）。level 以上の重要度だけ（重要度の無い行は常に）、
        contains を含むものだけ。next を次の since に。t は受けた時刻（タイムベース）。more = limit で止めて、next より後に
        まだ見ていない行が残っている（続けて読む）。"""
        feed = self._log_feed or self._log_last
        if feed is None:
            raise RuntimeError("ログが無い（log_start）")
        out, last = [], since
        records = list(feed.records)
        for seq, r, t, unc, source in records:
            if seq <= since:
                continue
            if len(out) >= limit:                       # 上限に達した後に、まだ見ていない行がある
                break
            last = seq
            if not level_at_least(r.level, level) or (contains and contains not in r.text):
                continue
            item = {"seq": seq, **r.to_dict()}
            if t is not None:
                item["t"] = t
                item["time_source"] = source            # device_received / host_received（どちらも出来事の上限）
            out.append(item)
        more = any(seq > last for seq, *_ in records)
        dropped = max(0, feed.seq - len(feed.records) - since)
        return {"records": out, "next": last, "more": more, "dropped": dropped, "running": feed.running}

    def log_decode(self, format: str, data_hex: str | None = None, path: str | None = None, elf: str | None = None,
                   til: str | None = None, li: str | None = None, framing: str | None = None,
                   big_endian: bool = False, transport: str | None = None, limit: int = 1000,
                   channel_levels: dict[str, str] | None = None) -> dict:
        """ログのバイト（data_hex か path のファイル）を解読する（機器に触らない）。形式と引数は log_start と同じ。"""
        if (data_hex is None) == (path is None):
            raise ValueError("data_hex か path のどちらか 1 つ")
        data = bytes.fromhex(data_hex) if data_hex is not None else Path(path).read_bytes()
        stream = self._log_stream(format, elf, til, li, framing, big_endian, transport, True, channel_levels)
        records = stream.feed(data)
        return {"format": format, "bytes": len(data), "count": len(records), "errors": stream.errors,
                "records": [r.to_dict() for r in records[:limit]], "truncated": len(records) > limit}

    def log_annotate(self, text: str, elf: str | None = None) -> dict:
        """文字（UART のパニックの出力等）の中の 0x の数のうち、値が ELF の実行区画に入るものを関数・ファイル:行に
        する（機器に触らない。番地として書かれた数かどうかは決めない。fprobe.logfmt.addresses）。"""
        from .symbols import Symbols
        symbols = Symbols(elf) if elf is not None else self._host._symbols
        if symbols is None:
            raise RuntimeError("ELF が読み込まれていない（elf か debug_elf_load）")
        annotator = AddressAnnotator(symbols)
        lines = text.splitlines()
        return {"text": "\n".join(annotator.annotate(line) for line in lines),
                "addresses": [h.to_dict() for line in lines for h in annotator.find(line)]}
