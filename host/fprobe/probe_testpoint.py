"""Probe の道具: テストポイント（F7、DT+ 方式）。ソースへの挿入、ロジアナ（GPIO）・SWO（ITM）・RTT での受け取り、
関数の時間・呼び出しの木・遷移・カバレッジ（C0 / C1）・異常の解析、共通の時間軸の記録への書き出し。

Probe（fprobe.mcp_server）に混ぜて使う mixin。Probe の _lock・session・target・_debug・_recorder・_collector・
_swo_baud・swo_start・_keep_capture と、ObserveTools（RTT の受信 `rtt_start`・`_rtt_listen`）、WindowTools（SWO の
バイトのデバイスの時刻の窓・write_registers）を使う。機器に触るのは必ず _lock の中。

方式（method）と受け口:
- gpio_serial（2 線: CLK・DAT）/ gpio_parallel（データ W 本 + ストローブ）: ロジアナ。capture deep は深いバッファ
  （CLK / ストローブの最初の立ち上がりから after_beats 拍。stop で読む。時刻はトリガ時刻から = 確か）、stream は
  流し続ける捕捉（背景のスレッドが受け取ったかたまりごとに解読・解析する。何時間でも。時刻は流し始めの時刻から）。
- itm: SWO。FPGA が SWO のバイトに付けた時刻（タイムベース）で受ける（窓。記録中なら記録から、そうでなければ
  イベント記録から）。受信を始めてから出力（ITM_TER）を有効にする（逆だと受信がパケットの途中から始まり解読が
  ずれる。2026-10-18 に実機で）。止めると ITM_TER を元に戻す。
- rtt: RTT の上りのチャンネル。時刻はターゲットの CYCCNT（core_clock_hz）。タイムベースへの写しは、最初に受けた
  かたまりの最後の記録をホストの時計で受けた時刻に合わせる（不確かさ = RTT の読みの周期程度）。

解析は fprobe.testpoint.live.LiveAnalysis（逐次・メモリ一定）。記録中に始めたら stream "testpoint"（call / hit /
anomaly）に書く。

仕様: docs/superpowers/specs/2026-10-17-f7-testpoint-design.md（7 節が組み込みの提案）
"""

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .la import DeepAnalyzer, LaStream, Transitions
from . import probe_window
from .probe_window import EventWindow, write_registers
from .profile.dwt import ITM_TER, RegisterWrite, itm_port_writes, plan_swo
from .testpoint import (Hit, ItmDecoder, LiveAnalysis, Options, ParallelDecoder, ProbeTable, RttDecoder,
                        SerialDecoder, TimedSwo, TimeMap, WatchVar, instrument_tree)
from .testpoint.live import calls_dict, coverage_dict, report_dict

__all__ = ["DEEP_AFTER_BEATS", "METHODS", "TestpointTools"]

METHODS = ("gpio_serial", "gpio_parallel", "itm", "rtt")
CAPTURES = ("deep", "stream")
DEEP_AFTER_BEATS = 1 << 18               # 深いバッファでトリガの後に取る拍の既定（RLE で 100 万変化点、4 MB の読み）
ITM_PULL_S = 0.25                        # itm: 背景で窓の新しい SWO のバイトを解析へ渡す周期
_SWO_BYTE_BITS = 10


@dataclass
class _Run:
    """動いている（止めた）テストポイントの受け取り。"""
    method: str
    capture: str | None                  # gpio: deep / stream
    live: LiveAnalysis
    map_path: str
    decoder: SerialDecoder | ParallelDecoder | ItmDecoder | RttDecoder
    settings: dict
    started: float                       # ホストの単調時計
    duration_s: float | None = None
    warnings: list[str] = field(default_factory=list)
    stopped: float | None = None
    # gpio deep / stream
    deep: DeepAnalyzer | None = None
    stream: LaStream | None = None
    pump: threading.Thread | None = None  # 背景のスレッド（stream: 受け取り、itm: 窓の読み）
    halt: threading.Event = field(default_factory=threading.Event)     # itm: 背景の読みを止める
    error: str | None = None             # 背景のスレッドの誤り
    # itm
    window: EventWindow | None = None
    swo: TimedSwo | None = None
    last_t: int | None = None            # 解析に渡した最後の SWO のバイトの時刻
    at_last: int = 0                     # その時刻のバイトのうち渡した数
    ring_seen: int = 0                   # 記録していないとき: 前回までに読んだイベント記録の総数
    restore: dict[int, int] = field(default_factory=dict)
    pulling: threading.Lock = field(default_factory=threading.Lock)
    # rtt
    rtt_channel: int | None = None
    rtt_listener: object = None
    started_rtt: bool = False
    synced: bool = False                 # rtt: 最初の正しい記録を見た（受け始めの途中の記録は数えない）
    host_clock: tuple[float, int, float] | None = None     # (ホストの時刻, タイムベース, 不確かさ) 記録していないとき

    @property
    def running(self) -> bool:
        return self.stopped is None


class TestpointTools:
    """テストポイント（Probe の mixin）。"""

    def _init_testpoint(self) -> None:
        self._tp_map: Path | None = None             # 最後に挿入した対応表（testpoint_start の既定）
        self._tp_run: _Run | None = None
        self._tp_last: _Run | None = None

    # --- 挿入 ------------------------------------------------------------------------------------

    def testpoint_instrument(self, source_dir: str, out_dir: str, map_path: str | None = None,
                             variables: list[str] | None = None, include: list[str] | None = None,
                             exclude: list[str] | None = None, branches: bool = True, only_changed: bool = False,
                             baseline: str | None = None, renumber: bool = False,
                             line_root: str | None = None) -> dict:
        """C のソース（source_dir の下の *.c）にテストポイントを入れた写しを out_dir に作り、ID の対応表（map_path、
        既定 out_dir/testpoints.json）を書く。対応表が既にあれば前回として読み、中身の変わらない関数の ID を保つ
        （差分の再挿入。renumber で振り直す）。only_changed は前回（baseline、既定は前回の対応表）から変わった・新しい
        関数にだけ入れる。variables は書き込みを記録する変数（"name" か "name:novalue"）。include / exclude は関数名の glob。
        写しは FPROBE_TP_BACKEND（fprobe_tp.h）で方式を選んでビルドする。以後の testpoint_start の既定の対応表になる。"""
        out = Path(out_dir)
        table_path = Path(map_path) if map_path else out / "testpoints.json"
        previous = ProbeTable.load(table_path) if table_path.exists() else None
        options = Options(branches=branches, variables=tuple(WatchVar.parse(v) for v in variables or ()),
                          include=tuple(include or ()), exclude=tuple(exclude or ()), only_changed=only_changed)
        result = instrument_tree(Path(source_dir), out, options=options, previous=previous,
                                 baseline=ProbeTable.load(Path(baseline)) if baseline else None, map_path=table_path,
                                 renumber=renumber, line_root=Path(line_root) if line_root else None)
        self._tp_map = table_path.resolve()
        files = [{"path": f.path, "probes": f.probes, "instrumented": f.instrumented, "reused": f.reused,
                  "changed": f.changed, "skipped": [{"function": s.name, "line": s.line, "reason": s.reason}
                                                    for s in f.skipped],
                  "warnings": f.warnings, "error": f.error} for f in result.files]
        return {"map": str(self._tp_map), "probes": result.probe_count, "next_id": result.table.next_id,
                "header": str(out.resolve() / "fprobe_tp.h"), "files": files, "removed": result.removed,
                "written": [str(p) for p in result.written]}

    # --- 受け取りを始める・止める --------------------------------------------------------------------

    def testpoint_start(self, method: str, map_path: str | None = None, clock_channel: int | None = None,
                        data_channel: int | None = None, data_channels: list[int] | None = None,
                        strobe_channel: int | None = None, capture: str = "deep", rate_hz: float | None = None,
                        rle: bool = True, trigger: bool = True, after_beats: int = DEEP_AFTER_BEATS,
                        port: int = 1, swo_baud: int | None = None, core_clock_hz: float | None = None,
                        rtt_channel: int = 1, duration_s: float | None = None, bucket_s: float = 1.0,
                        silence_s: float | None = None, open_s: float | None = None,
                        long_s: float | None = None) -> dict:
        """テストポイントの受け取りを始める（すぐ戻る。結果は testpoint_report / testpoint_stop）。method:
        gpio_serial（clock_channel・data_channel）/ gpio_parallel（data_channels = D0..D(W-1)、最後が先頭の印。
        strobe_channel）はロジアナで、capture deep（深いバッファ。trigger なら CLK / ストローブの最初の立ち上がりから
        after_beats 拍。stop で読む）か stream（流し続ける。長時間向け）。itm は SWO（port のスティミュラス・ポート。
        swo_baud で受信を始め、core_clock_hz も渡すと TPIU も設定する）。rtt は RTT の上りの rtt_channel（core_clock_hz =
        CYCCNT の速さが要る。RTT の受信が無ければ始める）。duration_s を渡すと stop はそこまで待つ（stream は自分で終わる）。
        bucket_s・silence_s・open_s・long_s は印の設定（どれも明示したときだけ。long_s = これより長い呼び出しに印）。記録中なら stream "testpoint" に書く。"""
        if method not in METHODS:
            raise ValueError(f"method は {' / '.join(METHODS)}")
        if method.startswith("gpio") and capture not in CAPTURES:
            raise ValueError("capture は deep か stream")
        if duration_s is not None and duration_s <= 0:
            raise ValueError("duration_s は正")
        table_path = Path(map_path).resolve() if map_path else self._tp_map
        if table_path is None:
            raise RuntimeError("対応表が無い（testpoint_instrument の後か、map_path で testpoints.json を指定する）")
        table = ProbeTable.load(table_path)
        options = {"bucket_s": bucket_s, "silence_s": silence_s, "open_s": open_s, "long_s": long_s}
        with self._lock:
            if self._tp_run is not None:
                raise RuntimeError("テストポイントを受け取っている（testpoint_stop を先に）")
            if method == "gpio_serial":
                run = self._tp_start_gpio(table, table_path, method, capture, options, rate_hz, rle, trigger, after_beats,
                                       SerialDecoder(*self._tp_serial_channels(clock_channel, data_channel)),
                                       clock_channel, {"clock_channel": clock_channel, "data_channel": data_channel})
            elif method == "gpio_parallel":
                if not data_channels or strobe_channel is None:
                    raise ValueError("gpio_parallel には data_channels（2 本以上）と strobe_channel が要る")
                run = self._tp_start_gpio(table, table_path, method, capture, options, rate_hz, rle, trigger, after_beats,
                                       ParallelDecoder(data_channels, strobe_channel), strobe_channel,
                                       {"data_channels": list(data_channels), "strobe_channel": strobe_channel})
            elif method == "itm":
                run = self._tp_start_itm(table, table_path, options, port, swo_baud, core_clock_hz)
            else:
                run = self._tp_start_rtt(table, table_path, options, rtt_channel, core_clock_hz)
            run.duration_s = duration_s
            self._tp_run, self._tp_last = run, None
        if run.pump is not None:
            run.pump.start()
        return self._tp_state(run)

    @staticmethod
    def _tp_serial_channels(clock_channel: int | None, data_channel: int | None) -> tuple[int, int]:
        if clock_channel is None or data_channel is None:
            raise ValueError("gpio_serial には clock_channel と data_channel が要る")
        return clock_channel, data_channel

    def _tp_analysis(self, table: ProbeTable, hz: float, options: dict) -> LiveAnalysis:
        return LiveAnalysis(table, hz, recorder=self._recorder, **options)

    def _tp_start_gpio(self, table, table_path, method, capture, options, rate_hz, rle, trigger, after_beats,
                       decoder, trigger_channel, settings) -> _Run:
        device = self.session.device
        deep = DeepAnalyzer(device)
        settings = {**settings, "rate_hz": rate_hz, "rle": rle}
        if capture == "deep":
            settings.update(trigger=trigger, after_beats=after_beats)
            self._tp_pause_events(True)
            try:
                period = deep.start(rate_hz=rate_hz, rise=(1 << trigger_channel) if trigger else 0, rle=rle,
                                    post_beats=after_beats)
            except Exception:
                self._tp_pause_events(False)
                raise
            run = _Run(method, capture, self._tp_analysis(table, 1 / period, options), str(table_path), decoder,
                       settings, time.monotonic(), deep=deep)
            return run
        self._tp_pause_events(True)
        try:
            before = device.time()
            stream = deep.stream(rate_hz=rate_hz, rle=rle)
            after = device.time()
        except Exception:
            self._tp_pause_events(False)
            raise
        hz = 1 / stream.sample_period_s
        run = _Run(method, capture, self._tp_analysis(table, hz, options), str(table_path), decoder, settings,
                   time.monotonic(), stream=stream)
        # 流し始めはタイムベースの 2 回の読みの間のどこか（サンプル 0 の時刻。中点と、その幅の半分の不確かさ）
        run.live.anchor(TimeMap(hz, device.capabilities().timebase_hz, (before + after) // 2, (after - before) / 2))
        run.pump = threading.Thread(target=self._tp_pump, args=(run,), name="fprobe-testpoint", daemon=True)
        return run

    def _tp_pause_events(self, paused: bool) -> None:
        """深いバッファ・流し続ける捕捉の間はイベント記録を読めない（読み出しの経路を共有する）。"""
        if self._collector is not None:
            self._collector.pause_events(paused)

    def _tp_pump(self, run: _Run) -> None:
        """背景: 流し続ける捕捉のかたまりを受け取り、解読・解析する（受け取りの間だけ _lock を持つ）。"""
        stream, decoder = run.stream, run.decoder
        chunks = stream.chunks(run.duration_s if run.duration_s is not None else float("inf"))
        try:
            while True:
                with self._lock:
                    chunk = next(chunks, None)
                if chunk is None:
                    break
                values, starts = chunk.transitions()
                tr = Transitions(values, starts, chunk.first + chunk.length, 0, 0, stream.sample_period_s,
                                 stream.channels)
                run.live.feed(decoder.feed(tr))
        except Exception as e:                                    # 機器の誤り等: 止めて理由を残す
            run.error = f"{type(e).__name__}: {e}"
            with self._lock:
                chunks.close()

    def _tp_start_itm(self, table, table_path, options, port, swo_baud, core_clock_hz) -> _Run:
        target = self.target
        if not 0 <= port < 32:
            raise ValueError("port は 0〜31")
        if swo_baud is None and self._swo_baud is None:
            raise RuntimeError("SWO の受信の速度が分からない（swo_baud を渡すか swo_start を先に）")
        warnings: list[str] = []
        enable = itm_port_writes(port)                           # DEMCR・LAR・TCR・TER（port のビット）
        setup = enable[:-1]
        baud = swo_baud
        if swo_baud is not None and core_clock_hz is not None:
            swo = plan_swo(core_clock_hz, swo_baud)
            setup += list(swo.writes)
            warnings += swo.warnings
            baud = round(swo.baud)
        # 受信を始めてから出力を有効にする: ITM の全ポートを止めて（他のポートの printf もパケットの途中で窓が
        # 始まらないように）受信と窓を始め、元のポートと port を有効にする
        before = write_registers(target, setup + [RegisterWrite(ITM_TER, 0, note="ITM: 全ポートを止める")])
        try:
            if baud is not None:
                self.swo_start(baud)
            baud = baud or self._swo_baud
            window = self._open_window()
            seen = 0 if window.recorded else window.seen
            target.write_memory(ITM_TER, before[ITM_TER] | 1 << port)
        except Exception:
            target.write_memory(ITM_TER, before[ITM_TER])        # 止めたポートを戻す
            raise
        hz = self.session.device.capabilities().timebase_hz
        run = _Run("itm", None, self._tp_analysis(table, hz, options), str(table_path), ItmDecoder(port),
                   {"port": port, "swo_baud": baud, "core_clock_hz": core_clock_hz}, time.monotonic(),
                   warnings=warnings, window=window, swo=TimedSwo(), restore={ITM_TER: before[ITM_TER]},
                   ring_seen=seen)
        # 時刻は記録の先頭のバイトを FPGA が受けた時刻（送られ始めてから 1 バイト分ほど後）
        run.live.anchor(TimeMap(hz, hz, 0, _SWO_BYTE_BITS / baud * hz))
        run.pump = threading.Thread(target=self._tp_itm_puller, args=(run,), name="fprobe-testpoint-itm",
                                    daemon=True)
        return run

    def _tp_itm_puller(self, run: _Run) -> None:
        """背景: 窓の新しい SWO のバイトを周期的に解析へ渡す（イベント記録のリングが一周する前に。長時間でも取りこぼさない）。"""
        while not run.halt.wait(ITM_PULL_S):
            try:
                self._tp_pull_itm(run)
            except Exception as e:                                # 切断等: 理由を残して止める（stop が最後に読む）
                run.error = f"{type(e).__name__}: {e}"
                return

    def _tp_start_rtt(self, table, table_path, options, rtt_channel, core_clock_hz) -> _Run:
        if core_clock_hz is None or core_clock_hz <= 0:
            raise ValueError("method rtt には core_clock_hz（CYCCNT の速さ = コアのクロック）が要る")
        started_rtt = self._rtt is None
        if started_rtt:
            self.rtt_start()
        try:
            up = {c.index for c in self._rtt.control_block.up}
            if rtt_channel not in up:
                raise ValueError(f"RTT の上りのチャンネル {rtt_channel} が無い（{sorted(up)}）")
            device = self.session.device
            h0 = time.monotonic()
            ticks = device.time()
            h1 = time.monotonic()
            hz_tb = device.capabilities().timebase_hz
        except Exception:
            if started_rtt:
                self.rtt_stop()
            raise
        run = _Run("rtt", None, self._tp_analysis(table, core_clock_hz, options), str(table_path), RttDecoder(),
                   {"rtt_channel": rtt_channel, "core_clock_hz": core_clock_hz}, time.monotonic(),
                   rtt_channel=rtt_channel, started_rtt=started_rtt,
                   host_clock=((h0 + h1) / 2, ticks, (h1 - h0) / 2 * hz_tb))
        run.warnings.append("rtt: 時刻はターゲットの CYCCNT。タイムベースへの写しはホストの時計で受けた時刻に合わせる"
                            "（不確かさ付き。ターゲットとプローブのクロックのずれは補正しない）")

        def listener(channel, data: bytes, host_s: float, run=run) -> None:
            if channel == run.rtt_channel and run.running:
                self._tp_rtt_received(run, data, host_s)
        run.rtt_listener = listener
        self._rtt_listen(listener)
        return run

    def _tp_rtt_received(self, run: _Run, data: bytes, host_s: float) -> None:
        """RTT の受信（ObserveTools の背景のスレッドから、_lock の中で）。"""
        items = run.decoder.feed(data)
        if not run.synced:                                        # 受け始めの途中の記録は取りこぼしではない
            first = next((k for k, i in enumerate(items) if isinstance(i, Hit)), None)
            if first is None:
                return
            items, run.synced = items[first:], True
        if run.live.time_map is None:
            # かたまりの最後の記録は、前の読みからこの読みまでの間に書かれた: 不確かさに RTT の読みの周期を足す
            last = max(i.t for i in items if isinstance(i, Hit))
            hz_tb = self._tp_timebase_hz()
            ticks, uncertainty = self._tp_host_ticks(run, host_s)
            run.live.anchor(TimeMap(run.live.hz, hz_tb, ticks - round(last * hz_tb / run.live.hz),
                                    uncertainty + self.poll_s * hz_tb))
        run.live.feed(items)

    def _tp_timebase_hz(self) -> float:
        return self.session.device.capabilities().timebase_hz

    def _tp_host_ticks(self, run: _Run, host_s: float) -> tuple[int, float]:
        """ホストの単調時計 → タイムベース（記録中なら記録の時計の対応付け、そうでなければ始めたときの 1 点と公称の速さ）。"""
        rec = run.live.recorder
        if rec is not None:
            try:
                return rec.clock.to_ticks(host_s)
            except RuntimeError:
                pass                                              # 記録を始めた直後で対応付けの点が無い
        h, ticks, uncertainty = run.host_clock
        return ticks + round((host_s - h) * self._tp_timebase_hz()), uncertainty

    def testpoint_stop(self, timeout_s: float = 10.0, top: int = 10) -> dict:
        """受け取りを止めて解析を終える（開いたままの入口に open_s を超えれば no_exit）。duration_s を渡して始めたなら
        そこまで待つ（timeout_s まで）。gpio の deep は深いバッファが埋まるのを待って読む。要約（上位 top の関数・
        カバレッジ・異常）を返す。詳しくは testpoint_report / testpoint_calls / testpoint_coverage。"""
        run = self._tp_run
        if run is None:
            raise RuntimeError("テストポイントを受け取っていない（testpoint_start）")
        deadline = time.monotonic() + timeout_s
        if run.duration_s is not None and run.method in ("itm", "rtt"):
            remaining = min(run.started + run.duration_s, deadline) - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
        try:
            if run.method.startswith("gpio"):
                self._tp_stop_gpio(run, deadline)
            elif run.method == "itm":
                self._tp_stop_itm(run)
            else:
                self._tp_stop_rtt(run)
        finally:
            with self._lock:
                run.stopped = time.monotonic()
                self._tp_run, self._tp_last = None, run
        report = run.live.report(final=True)
        return {**self._tp_state(run), **report_dict(report, run.live.time_map, top=top, anomalies=20, buckets=0,
                                                     tree_depth=3)}

    def _tp_stop_gpio(self, run: _Run, deadline: float) -> None:
        if run.capture == "stream":
            if run.duration_s is not None:                        # 決めた長さで自分で終わるのを待つ
                run.pump.join(max(deadline - time.monotonic(), 0))
            if run.pump.is_alive():
                run.stream.stop()                                 # 時間の前に終える（残りは出し切る）
                run.pump.join()
            with self._lock:
                self._tp_pause_events(False)
            run.live.feed(run.decoder.flush())
            if run.stream.stats().overrun:
                run.warnings.append("ホストが遅れて流し続ける捕捉が途中で止まった（overrun）。rate_hz を下げる")
            return
        try:
            with self._lock:
                try:
                    run.deep.wait(max(deadline - time.monotonic(), 0.001))
                except TimeoutError as e:
                    raise TimeoutError("深いバッファが埋まらない（CLK / ストローブに立ち上がりが来ない、または記録が少ない）:"
                                       " 配線・ファームの方式を確かめるか、after_beats を減らす・trigger=false・"
                                       "capture stream にする") from e
                tr = run.deep.read_transitions(before_beats=0)
        finally:
            with self._lock:
                self._tp_pause_events(False)
        with self._lock:
            self._keep_capture(tr, "testpoint")                   # la_view・記録の波形にも
        hz_tb = self._tp_timebase_hz()
        offset = tr.trigger_time - round(tr.trigger * tr.sample_period_s * hz_tb)
        run.live.anchor(TimeMap(run.live.hz, hz_tb, offset))
        run.live.feed(run.decoder.feed(tr) + run.decoder.flush())

    def _tp_stop_itm(self, run: _Run) -> None:
        run.halt.set()
        run.pump.join()
        with self._lock:
            if self._debug is not None:
                for address, value in run.restore.items():
                    self.target.write_memory(address, value)
            self._close_window(run.window)
        self._tp_pull_itm(run)
        run.live.feed(run.decoder.flush())

    def _tp_stop_rtt(self, run: _Run) -> None:
        self._rtt_unlisten(run.rtt_listener)
        with self._lock:
            run.stopped = time.monotonic()                        # 背景の受信が以後は渡さない
            if self._rtt is not None and run.started_rtt:
                self.rtt_stop()
        items = run.decoder.flush()
        if run.synced:
            run.live.feed(items)

    def _close_testpoint(self) -> None:
        """Probe.close から: 背景のスレッドを止める（解析は終えない）。"""
        run = self._tp_run
        if run is not None and run.pump is not None and run.pump.is_alive():
            run.halt.set()
            if run.stream is not None:
                run.stream.stop()
            run.pump.join(timeout=5)

    # --- ITM: 窓から届いた分を解析へ --------------------------------------------------------------

    def _tp_pull_itm(self, run: _Run) -> None:
        """窓の SWO のバイトのうち、まだ解析に渡していないものを渡す（_lock の外で。背景と道具の呼び出しの両方から）。
        記録からは前回の最後の時刻から後を、イベント記録からは前回までに読んだ数より後を読む（同じ時刻のバイトは
        渡した数で見分ける）。"""
        with run.pulling:
            events = self._tp_new_swo(run)
            fresh = []
            seen_at_last = 0
            for t, b in events:
                if run.last_t is not None and t < run.last_t:
                    continue
                if run.last_t is not None and t == run.last_t:
                    seen_at_last += 1
                    if seen_at_last <= run.at_last:
                        continue
                fresh.append((t, b))
            if not fresh:
                return
            last = fresh[-1][0]
            run.at_last = sum(1 for t, _ in fresh if t == last) + (run.at_last if last == run.last_t else 0)
            run.last_t = last
            run.live.feed(run.decoder.feed(run.swo.feed(fresh)))

    def _tp_new_swo(self, run: _Run) -> list[tuple[int, int]]:
        window = run.window
        if window.recorded:
            if run.last_t is not None:
                window = EventWindow(run.last_t, True, t1=run.window.t1)
            return [(t, b) for t, _, b in self._window_events(window, "swo")]
        with self._lock:
            written = probe_window.EventLog(self.session.device).counts()[0]
        part = EventWindow(window.t0, False, run.ring_seen, window.t1)
        events = self._window_events(part, "swo")
        run.ring_seen = written                                   # 間に増えた分は次も読む（時刻で重複を除く）
        window.lost += part.lost
        window.notes += [n for n in part.notes if n not in window.notes]
        return [(t, b) for t, _, b in events]

    def _tp_refresh(self, run: _Run) -> None:
        if run.running and run.method == "itm":
            self._tp_pull_itm(run)

    # --- 読む --------------------------------------------------------------------------------------

    def _tp_current(self) -> _Run:
        run = self._tp_run or self._tp_last
        if run is None:
            raise RuntimeError("テストポイントの結果が無い（testpoint_start）")
        self._tp_refresh(run)
        return run

    def _tp_state(self, run: _Run) -> dict:
        out = {"running": run.running, "method": run.method, "map": run.map_path, "hz": run.live.hz,
               "settings": dict(run.settings), "hits": run.live.hits, "losses": run.live.losses,
               "elapsed_s": (run.stopped or time.monotonic()) - run.started, "recorded": run.live.recorder is not None,
               "warnings": list(run.warnings)}
        if run.capture is not None:
            out["capture"] = run.capture
        if run.duration_s is not None:
            out["duration_s"] = run.duration_s
        if run.error is not None:
            out["error"] = run.error
        if isinstance(run.decoder, ParallelDecoder) and run.decoder.unsynced:
            out["unsynced_symbols"] = run.decoder.unsynced
        if run.stream is not None:
            stats = run.stream.stats()
            out["stream"] = {"samples": stats.samples, "duration_s": stats.duration_s, "mb_per_s": stats.mb_per_s,
                             "overrun": stats.overrun}
        if run.window is not None:
            out["window"] = self._window_info(run.window, run.live.hz)
        if run.live.time_map is not None:
            out["time_map"] = run.live.time_map.to_dict()
        return out

    def testpoint_status(self) -> dict:
        """受け取りの様子（方式・設定・受けた印の数・取りこぼし・経過・流し続ける捕捉の速さ・警告）。"""
        run = self._tp_run or self._tp_last
        if run is None:
            return {"running": False}
        self._tp_refresh(run)
        return self._tp_state(run)

    def testpoint_report(self, top: int = 20, anomalies: int = 100, buckets: int = 120, tree_depth: int = 8) -> dict:
        """解析の要約（受け取っている最中ならいままでの分）: 関数ごとの回数・子を含む／自分だけの時間（合計・最小・
        最大・平均・σ）を多い順に top、カバレッジの割合、異常（long_call / missing_exit / exit_without_entry /
        unknown_id / silence / no_exit / loss）の種類ごとの数と先頭 anomalies 件、時間の区切り（bucket_s ごとの件数）
        の最後の buckets 個、変数（最新・最小・最大）、開いたままの呼び出し、呼び出しの木（深さ tree_depth まで）。
        t_s は最初の印からの秒、t はタイムベース（記録・`recording_events` の時刻）。"""
        run = self._tp_current()
        report = run.live.report(final=False)
        return {"running": run.running, "method": run.method,
                **report_dict(report, run.live.time_map, top=top, anomalies=anomalies, buckets=buckets,
                              tree_depth=tree_depth)}

    def testpoint_calls(self, t0_s: float | None = None, t1_s: float | None = None, function: str | None = None,
                        limit: int = 200) -> dict:
        """関数の遷移の時間軸: 呼び出しを始まりの順に（関数・始まり・時間・自分だけの時間・深さ・出口を見たか）と、
        深さで字下げした文字列 text。t0_s ≤ 始まり < t1_s（最初の印からの秒）・function で絞る。持っているのは最近の
        65536 件（dropped はそれより前に捨てた数。全部は記録の stream "testpoint" の call）。"""
        run = self._tp_current()
        calls, dropped = run.live.calls()
        return calls_dict(calls, dropped, run.live.report(final=False), run.live.time_map, t0_s=t0_s, t1_s=t1_s,
                          function=function, limit=limit)

    def testpoint_coverage(self, file: str | None = None, function: str | None = None, limit: int = 500) -> dict:
        """カバレッジ: 関数（入った / 入れた）・C0（ソースにある塊の印）・C1（分岐の組の結果。足した else・default を
        含む）の割合と、通らなかった印（ファイル・行・関数・種類。試験を足す手がかり）、分岐の組ごとの結果の回数。
        file（対応表の相対パス）・function で絞る。"""
        run = self._tp_current()
        return {"running": run.running,
                **coverage_dict(run.live.report(final=False).coverage, file=file, function=function, limit=limit)}
