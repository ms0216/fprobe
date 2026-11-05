"""テストポイントの結果を読む API（TestpointService に混ぜ込む部品）: 状態・要約・呼び出しの時間軸・カバレッジ。"""

import time

from .probe_testpoint_run import _Run
from .testpoint import ParallelDecoder, RttDecoder
from .testpoint.live import calls_dict, coverage_dict, report_dict

__all__ = ["TestpointReadMixin"]


class TestpointReadMixin:
    """受け取り中・受け取り後の結果の読み出し。"""

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
        if run.method == "rtt":
            out["skipped_before_sync"] = run.skipped_before_sync      # 受け始めの途中で捨てた項目（取りこぼしとは数えない）
        if run.capture is not None:
            out["capture"] = run.capture
        if run.duration_s is not None:
            out["duration_s"] = run.duration_s
        if run.error is not None:
            out["error"] = run.error
        if isinstance(run.decoder, ParallelDecoder) and run.decoder.unsynced:
            out["unsynced_symbols"] = run.decoder.unsynced
        if isinstance(run.decoder, RttDecoder):           # 記録の間が 2^32 クロックより短い前提で戻した回数
            out["cyccnt_wraps"] = run.decoder.wraps
        if run.stream is not None:
            stats = run.stream.stats()
            out["stream"] = {"samples": stats.samples, "duration_s": stats.duration_s, "mb_per_s": stats.mb_per_s,
                             "overrun": stats.overrun, "link_buffer": stats.link_buffer_dict}
        if run.window is not None:
            out["window"] = self._host._window_info(run.window, run.live.hz)
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
