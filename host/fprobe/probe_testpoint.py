"""Probe の道具: テストポイント（F7、DT+ 方式）。ソースへの挿入、ロジアナ（GPIO）・SWO（ITM）・RTT での受け取り、
関数の時間・呼び出しの木・遷移・カバレッジ（C0 / C1）・異常の解析、共通の時間軸の記録への書き出し。

Probe（fprobe.probe）が API の名前を委ねるサービス（fprobe.probe_service。ホストから使う名前は NEEDS）。Probe の _lock・session・target・_debug・_recorder・_collector・
_swo_baud・swo_start・_keep_capture と、ObserveService（RTT の受信 `rtt_start`・`_rtt_listen`）、WindowService（SWO の
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

領域ごとの分け方: ここには挿入・始める・止める（gpio）・背景の受け取りを置く。itm は probe_testpoint_itm.py、rtt は
probe_testpoint_rtt.py、結果を読む API は probe_testpoint_read.py、受け取り 1 回分の状態（_Run）は probe_testpoint_run.py
（いずれも TestpointService に混ぜ込む部品）。

解析は fprobe.testpoint.live.LiveAnalysis（逐次・メモリ一定）。記録中に始めたら stream "testpoint"（call / hit /
anomaly）に書く。

仕様: docs/superpowers/specs/2026-10-17-f7-testpoint-design.md（7 節が組み込みの提案）
"""

import threading
import time
from pathlib import Path

from .la import DeepAnalyzer, Transitions
from .la_wave import PIECE_CHANGES, _array_of, _np
from .recording_stream import anchor_stream
from .testpoint import (LiveAnalysis, Options, ParallelDecoder, ProbeTable, SerialDecoder, TimeMap, WatchVar,
                        instrument_tree)
from .testpoint.live import report_dict
from .probe_service import HostView
from .probe_testpoint_itm import TestpointItmMixin
from .probe_testpoint_read import TestpointReadMixin
from .probe_testpoint_rtt import TestpointRttMixin
from .probe_testpoint_run import _Run  # noqa: F401  (旧名を保つ。試験が読む)

__all__ = ["DEEP_AFTER_BEATS", "METHODS", "TestpointService"]

METHODS = ("gpio_serial", "gpio_parallel", "itm", "rtt")
CAPTURES = ("deep", "stream")
DEEP_AFTER_BEATS = 1 << 18               # 深いバッファでトリガの後に取る拍の既定（RLE で 100 万変化点、4 MB の読み）
ITM_PULL_S = 0.25                        # itm: 背景で窓の新しい SWO のバイトを解析へ渡す周期



class TestpointService(TestpointItmMixin, TestpointRttMixin, TestpointReadMixin):
    """テストポイント（Probe のサービス）。"""

    API = ("testpoint_calls", "testpoint_coverage", "testpoint_instrument", "testpoint_report", "testpoint_start",
           "testpoint_status", "testpoint_stop")
    # ほかの領域（Probe の委譲）・試験に出す名前
    SHARED = ("_close_testpoint", "_tp_current", "_tp_last", "_tp_map", "_tp_run")
    # ホスト（Probe の核と、ほかの領域）から使う名前
    NEEDS = ("_close_window", "_collector", "_debug", "_keep_capture", "_lock", "_open_window", "_recorder", "_rtt",
             "_rtt_listen", "_rtt_unlisten", "_swo_baud", "_window_events", "_window_info", "rtt_start", "rtt_stop",
             "session", "swo_start", "target")

    def __init__(self, host):
        self._host = HostView(host, self.NEEDS, type(self).__name__)
        self._tp_map: Path | None = None             # 最後に挿入した対応表（testpoint_start の既定）
        self._tp_run: _Run | None = None
        self._tp_last: _Run | None = None

    # --- 挿入 ------------------------------------------------------------------------------------

    def testpoint_instrument(self, source_dir: str, out_dir: str, map_path: str | None = None,
                             variables: list[str] | None = None, include: list[str] | None = None,
                             exclude: list[str] | None = None, branches: bool = True, only_changed: bool = False,
                             baseline: str | None = None, renumber: bool = False,
                             line_root: str | None = None, non_type_words: list[str] | None = None) -> dict:
        """C のソース（source_dir の下の *.c）にテストポイントを入れた写しを out_dir に作り、ID の対応表（map_path、
        既定 out_dir/testpoints.json）を書く。対応表が既にあれば前回として読み、中身の変わらない関数の ID を保つ
        （差分の再挿入。renumber で振り直す）。only_changed は前回（baseline、既定は前回の対応表）から変わった・新しい
        関数にだけ入れる。variables は書き込みを記録する変数（"name" か "name:novalue"）。include / exclude は関数名の glob。
        写しは FPROBE_TP_BACKEND（fprobe_tp.h）で方式を選んでビルドする。以後の testpoint_start の既定の対応表になる。"""
        out = Path(out_dir)
        table_path = Path(map_path) if map_path else out / "testpoints.json"
        previous = ProbeTable.load(table_path) if table_path.exists() else None
        options = Options(branches=branches, variables=tuple(WatchVar.parse(v) for v in variables or ()),
                          include=tuple(include or ()), exclude=tuple(exclude or ()), only_changed=only_changed,
                          non_type_words=tuple(non_type_words or ()))
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
                        long_s: float | None = None, guard_samples: int | None = None,
                        tree_max_depth: int = 64) -> dict:
        """テストポイントの受け取りを始める（すぐ戻る。結果は testpoint_report / testpoint_stop）。method:
        gpio_serial（clock_channel・data_channel・guard_samples = 開始・終了を認めるまで CLK が 1 のままのサンプル数。
        CLK と DAT のずれを何サンプルまで許すか。既定は無い）/ gpio_parallel（data_channels = D0..D(W-1)、最後が先頭の印。
        strobe_channel）はロジアナで、capture deep（深いバッファ。trigger なら CLK / ストローブの最初の立ち上がりから
        after_beats 拍。stop で読む）か stream（流し続ける。長時間向け）。itm は SWO（port のスティミュラス・ポート。
        swo_baud で受信を始め、core_clock_hz も渡すと TPIU も設定する）。rtt は RTT の上りの rtt_channel（core_clock_hz =
        CYCCNT の速さが要る。RTT の受信が無ければ始める）。duration_s を渡すと stop はそこまで待つ（stream は自分で終わる）。
        bucket_s・silence_s・open_s・long_s は印の設定（どれも明示したときだけ。long_s = これより長い呼び出しに印）。
        tree_max_depth = 呼び出しの木の深さの上限（より深い呼び出しは木に入れず tree_cut に数える）。記録中なら
        stream "testpoint" に書く。"""
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
        if tree_max_depth < 0:
            raise ValueError("tree_max_depth は 0 以上")
        options = {"bucket_s": bucket_s, "silence_s": silence_s, "open_s": open_s, "long_s": long_s,
                   "max_depth": tree_max_depth}
        with self._host._lock:
            if self._tp_run is not None:
                raise RuntimeError("テストポイントを受け取っている（testpoint_stop を先に）")
            if method == "gpio_serial":
                run = self._tp_start_gpio(table, table_path, method, capture, options, rate_hz, rle, trigger, after_beats,
                                       SerialDecoder(*self._tp_serial_channels(clock_channel, data_channel),
                                                     self._tp_guard(guard_samples)),
                                       clock_channel, {"clock_channel": clock_channel, "data_channel": data_channel,
                                                       "guard_samples": guard_samples})
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

    @staticmethod
    def _tp_guard(guard_samples: int | None) -> int:
        if guard_samples is None or guard_samples < 0:
            raise ValueError("gpio_serial には guard_samples（0 以上。CLK と DAT のずれを許すサンプル数）が要る")
        return guard_samples

    def _tp_analysis(self, table: ProbeTable, hz: float, options: dict) -> LiveAnalysis:
        return LiveAnalysis(table, hz, recorder=self._host._recorder, **options)

    def _tp_start_gpio(self, table, table_path, method, capture, options, rate_hz, rle, trigger, after_beats,
                       decoder, trigger_channel, settings) -> _Run:
        device = self._host.session.device
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
            # サンプル 0 のピンの時間軸の時刻は記録の連続記録と同じ決め方（recording_stream.anchor_stream: 回路が記録した起点の
            # 1 点。記録しない古い回路は前後の読みと段数から確かに言える範囲。時刻の正しさ S1b の A6。それまでは前後の読みの
            # 中点 ± 幅の半分で、ピンの段数・位相の幅を引いていなかった）
            stream, anchor = anchor_stream(device, lambda: deep.stream(rate_hz=rate_hz, rle=rle))
        except Exception:
            self._tp_pause_events(False)
            raise
        hz = 1 / stream.sample_period_s
        run = _Run(method, capture, self._tp_analysis(table, hz, options), str(table_path), decoder, settings,
                   time.monotonic(), stream=stream)
        run.settings["anchor"] = {"ticks": anchor.ticks, "uncertainty_ticks": anchor.uncertainty, "method": anchor.method,
                                  "t_lo": anchor.t_lo, "t_hi": anchor.t_hi, "basis": anchor.basis}
        run.live.anchor(TimeMap(hz, device.capabilities().timebase_hz, anchor.ticks, anchor.uncertainty))
        run.pump = threading.Thread(target=self._tp_pump, args=(run,), name="fprobe-testpoint", daemon=True)
        return run

    def _tp_pause_events(self, paused: bool) -> None:
        """深いバッファ・流し続ける捕捉の間はイベント記録を読めない（読み出しの経路を共有する）。"""
        if self._host._collector is not None:
            self._host._collector.pause_events(paused)

    def _tp_pump(self, run: _Run) -> None:
        """背景: 流し続ける捕捉のかたまりを受け取り、解読・解析する（受け取りの間だけ _lock を持つ）。"""
        stream, decoder = run.stream, run.decoder
        chunks = stream.chunks(run.duration_s if run.duration_s is not None else float("inf"))
        try:
            while True:
                with self._host._lock:
                    chunk = next(chunks, None)
                if chunk is None:
                    break
                # 断片ごとに解析へ渡す（REPEAT が何億もの変化点に展開されても、メモリは断片の大きさで決まる。断片は順に続く変化点）
                for values, starts, end in chunk.iter_transitions(PIECE_CHANGES):
                    if _np is not None:                                # 従来の transitions() と同じ array（"H"・"Q"）で渡す
                        values, starts = _array_of("H", values), _array_of("Q", starts)
                    tr = Transitions(values, starts, end, 0, 0, stream.sample_period_s, stream.channels)
                    run.live.feed(decoder.feed(tr))
        except Exception as e:                                    # 機器の誤り等: 止めて理由を残す
            run.error = f"{type(e).__name__}: {e}"
            with self._host._lock:
                chunks.close()

    def _tp_itm_puller(self, run: _Run) -> None:
        """背景: 窓の新しい SWO のバイトを周期的に解析へ渡す（イベント記録のリングが一周する前に。長時間でも取りこぼさない）。"""
        while not run.halt.wait(ITM_PULL_S):
            try:
                self._tp_pull_itm(run)
            except Exception as e:                                # 切断等: 理由を残して止める（stop が最後に読む）
                run.error = f"{type(e).__name__}: {e}"
                return

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
            with self._host._lock:
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
            with self._host._lock:
                self._tp_pause_events(False)
            run.live.feed(run.decoder.flush())
            if run.stream.stats().overrun:
                run.warnings.append("ホストが遅れて流し続ける捕捉が途中で止まった（overrun）。rate_hz を下げる")
            return
        try:
            with self._host._lock:
                try:
                    run.deep.wait(max(deadline - time.monotonic(), 0.001))
                except TimeoutError as e:
                    raise TimeoutError("深いバッファが埋まらない（CLK / ストローブに立ち上がりが来ない、または記録が少ない）:"
                                       " 配線・ファームの方式を確かめるか、after_beats を減らす・trigger=false・"
                                       "capture stream にする") from e
                tr = run.deep.read_transitions(before_beats=0)
        finally:
            with self._host._lock:
                self._tp_pause_events(False)
        with self._host._lock:
            self._host._keep_capture(tr, "testpoint")                   # la_view・記録の波形にも
        hz_tb = self._tp_timebase_hz()
        offset = tr.trigger_time - round(tr.trigger * tr.sample_period_s * hz_tb)
        run.live.anchor(TimeMap(run.live.hz, hz_tb, offset))
        run.live.feed(run.decoder.feed(tr) + run.decoder.flush())

    def _close_testpoint(self) -> None:
        """Probe.close から: 背景のスレッドを止める（解析は終えない）。"""
        run = self._tp_run
        if run is not None and run.pump is not None and run.pump.is_alive():
            run.halt.set()
            if run.stream is not None:
                run.stream.stop()
            run.pump.join(timeout=5)
