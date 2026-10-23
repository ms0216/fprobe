"""Probe の道具: タスクの時間軸の解析（U09）。窓ごとの CPU 負荷・ジョブの指標・横取りを除いた区間・スタックとヒープの
推移・スレッドと割り込みの合成・優先度の逆転。

Probe（fprobe.mcp_server）に混ぜて使う mixin。RtosTools の時間軸（_current_timeline: 取っている最中ならいままでの分、
止めた後なら最後の結果）と、記録（_reading。2 点の組を取る）を使う。計算はすべて fprobe.rtos.analysis。
機器には触らない（一覧に無い TCB の名前を読むのは RtosTools の _learn_names が _lock の中で）。

カーネルの出来事（U10。方法 itm で fprobe_trace.h の記録があるとき）: 出来事の一覧・Communication Flow・ブロック時間・
キュー使用率・フックの負荷。出来事があれば、ジョブは ready / block で区切り（応答時間）、逆転は出来事から正確に、
割り込みの区間は isr_enter / isr_exit からも取る（例外トレースが無いとき）。

仕様: docs/superpowers/specs/2026-10-18-u08-u09-rtos-objects-timeline-design.md の 2 節、
docs/superpowers/specs/2026-10-18-u10-u16-trace-hooks-user-events-design.md の 2 節。
"""

from .rtos.analysis import (Intervals, execution_profile, find_jobs, inversion_episodes, isr_intervals, job_summary, load_series, run_summary,
                            memory_series, run_time_in)
from .rtos.flow import (blocking, communication_flow, inversions_from_ops, isr_activations, job_events, overhead,
                        queue_usage)

__all__ = ["RtosAnalysisTools"]

WINDOWS_LIMIT = 1000                     # rtos_timeline_load が返す窓の数の既定の上限
_SAMPLED_ISR = "方法 sampled では割り込みは見えない（タスクの時間に含まれる）。方法 itm の exceptions=true で分かる"
_NO_KERNEL = ("カーネルの出来事が無い: rtos_timeline_start(method='itm') と、ファームの fprobe_trace.h（freertos_demo）の"
              "カーネルの出来事のフック（ITM のポート 30・29）が要る")
EVENTS_LIMIT = 500                       # rtos_timeline_events が 1 回に返す数の既定
_JOB_EVENTS = "トレースフックの ready / block の出来事で区切った（応答時間 = 終わり − ready）"
_JOB_NONE = ("トレースフックの ready / block の出来事が無いので、ジョブ（起きてから待つまで）は作らない（次に動いた"
             "タスクの優先度から横取りか待ちかを推定しない）。runs は区間（続けて動いた間）の事実")


class RtosAnalysisTools:
    """時間軸の解析（Probe の mixin）。"""

    def _analysis_base(self, result) -> dict:
        tl, run = result.timeline, result.run
        hz = run.timebase_hz
        out = {"running": run.window.t1 is None, "method": tl.method, "timebase_hz": hz, "t0": tl.t0, "t1": tl.t1,
               "resolution_us": tl.resolution * 1e6 / hz, "notes": []}
        if tl.lower_bound:
            out["notes"].append("方法 sampled: 周期より短い実行は見えないことがある（回数は下限、区間の端は ± 周期の半分）")
        return out

    @staticmethod
    def _activations(result):
        """割り込みの回: 例外トレースがあればそれ、無ければトレースフックの isr_enter / isr_exit（あれば）。"""
        if result.activations is not None:
            return result.activations
        if result.kernel is not None and any(op.kind == "isr_enter" for op in result.kernel.ops):
            return isr_activations(result.kernel)
        return None

    def _isr(self, result) -> list[tuple[int, int]]:
        acts = self._activations(result)
        return isr_intervals(acts) if acts is not None else []

    def _isr_note(self, result, out: dict) -> None:
        if result.activations is None and self._activations(result) is not None:
            out["notes"].append("割り込みの区間はトレースフックの isr_enter / isr_exit から（フックを呼ぶ ISR だけ。"
                                "すべての割り込みは rtos_timeline_start の exceptions=true）")
        elif result.activations is None:
            out["notes"].append(_SAMPLED_ISR if result.run.method == "sampled" else
                                "例外トレースを流していない（rtos_timeline_start の exceptions=true）: 割り込みはタスクの時間に含まれる")

    def rtos_timeline_load(self, window_us: float = 10_000.0, limit: int = WINDOWS_LIMIT) -> dict:
        """窓（window_us）ごとの CPU 負荷（1 − アイドルの割合）・タスクごとの割合・割り込みの割合の推移と、最大の窓・平均。
        情報の無い時間（抜け）は除く。窓が limit を超えたら先頭の limit 個（windows_total に全体の数）。"""
        if window_us <= 0:
            raise ValueError("window_us は正")
        result = self._current_timeline()
        out = self._analysis_base(result)
        hz = result.run.timebase_hz
        window = max(1, round(window_us * hz / 1e6))
        load = load_series(result.timeline, window, result.run.idle, self._isr(result), hz)
        if not result.run.idle:
            out["notes"].append("アイドルのタスクが分からない: 負荷は 1（すべて動いている）になる")
        self._isr_note(result, out)
        load["windows_total"] = len(load["windows"])
        load["windows"] = load["windows"][:limit]
        out.update(load)
        return out

    def rtos_timeline_jobs(self, task: str | None = None, limit: int = 50) -> dict:
        """tasks: カーネルの出来事（ready / block）で区切ったジョブ（起きてから待つまでの 1 回の仕事）ごとの実行時間
        （横取り・割り込みを除く）・経過時間・周期・応答時間の分布と最悪の回。出来事の無いタスクは入らない。
        runs: タスクごとの区間（続けて動いた間）の回数・時間・周期・次に動いたタスク（run_summary。いつも出す）。
        task を渡すと、そのタスクのジョブ（jobs）と区間（task_runs）の一覧も（自分の時間の長い順に limit 件）。"""
        result = self._current_timeline()
        run = result.run
        out = self._analysis_base(result)
        events = job_events(result.kernel, result.events or ()) if result.has_kernel else None
        isr = self._isr(result)
        jobs = find_jobs(result.timeline, run.idle, isr, events)
        out["notes"].append(_JOB_EVENTS if events else _JOB_NONE)
        self._isr_note(result, out)
        out["tasks"] = job_summary(jobs, run.timebase_hz)
        out["runs"] = run_summary(result.timeline, run.idle, isr, run.timebase_hz)
        if task is not None:
            mine = [j for j in jobs if j.task == task]
            if not mine and task not in run.names.values():
                raise KeyError(f"タスク {task!r} は時間軸に無い")
            mine.sort(key=lambda j: -j.exec_time)
            out["jobs_total"] = len(mine)
            out["jobs"] = [j.to_dict() for j in mine[:limit]]
            isr_iv = Intervals(isr)
            spans = [{"start": r.t0, "end": r.t1, "exec": r.duration - isr_iv.overlap(r.t0, r.t1)}
                     for r in result.timeline.runs if r.task == task]
            spans.sort(key=lambda r: -r["exec"])
            out["task_runs_total"] = len(spans)
            out["task_runs"] = spans[:limit]
        return out

    def rtos_timeline_interval(self, start: dict, end: dict, task: str | None = None, limit: int = 1000) -> dict:
        """記録の 2 点（start / end は recording_find と同じ条件。timing_measure と同じ組の規則）の各組について、
        タスク（省けば開始の時刻に動いていたタスク）が実際に CPU を使った時間（ほかのタスク・割り込みを除く）と
        経過時間の分布。時間軸と記録は同じタイムベース。"""
        result = self._current_timeline()
        out = self._analysis_base(result)
        pairs = self._reading().intervals(start, end)["intervals"]
        handle = None
        if task is not None:
            handles = [h for h, n in result.run.names.items() if n == task]
            if not handles:
                raise KeyError(f"タスク {task!r} は時間軸に無い")
            handle = handles[0]
        measured = run_time_in(result.timeline, pairs, handle, self._isr(result), result.run.timebase_hz)
        self._isr_note(result, out)
        if len(measured["intervals"]) > limit:
            measured["intervals_total"] = len(measured["intervals"])
            measured["intervals"] = measured["intervals"][:limit]
        out.update(measured)
        return out

    def rtos_timeline_memory(self, points: int = 200) -> dict:
        """タスクごとのスタックの使用量（保存された SP から: 切り替えのたびの深さ）とヒープの残量の推移
        （rtos_timeline_start の memory=true）。points 個の窓ごとに最悪の値（スタックは最大、ヒープは最小）。"""
        result = self._current_timeline()
        run = result.run
        if not run.slots:
            raise RuntimeError("スタック・ヒープの語を読んでいない（rtos_timeline_start の memory=true）")
        out = self._analysis_base(result)
        out.update(memory_series(result.samples, run.slots, points, run.timebase_hz))
        out["notes"].append("スタックは切り替えのときに保存された SP（待った・横取りされたときの深さ）: 動いている間の"
                            "最も深いところは rtos_tasks の peak_used（塗りの残り）で")
        return out

    def rtos_timeline_profile(self) -> dict:
        """情報のある時間に対する、スレッド（タスクごと。割り込みの時間を除く）・割り込み（例外ごとの回数と自分の時間）・
        アイドル・どれでもない時間の割合（実行のプロファイル）。割り込みは方法 itm の exceptions=true のとき。"""
        result = self._current_timeline()
        out = self._analysis_base(result)
        names = self._exception_names()
        out.update(execution_profile(result.timeline, self._activations(result), result.run.idle, names,
                                     result.run.timebase_hz))
        if result.exception_issues is not None:
            out["exception_trace"] = dict(result.exception_issues)
            if result.exception_issues["overflows"]:
                out["notes"].append("ITM があふれた: 割り込みの回が欠けている（SWO を速くする）")
        self._isr_note(result, out)
        return out

    def rtos_timeline_inversions(self, limit: int = 100) -> dict:
        """優先度の逆転の区間（rtos_timeline_start の mutexes で読んだ持ち主・待ちの先頭から）: 持ち主の元の優先度より
        高いタスクが待っていた区間、その間に動いた中間の優先度のタスクの時間（正なら際限の無い逆転）、持ち主が動いた時間。
        ミューテックスを読んでいなければ available=false（推測しない）。"""
        result = self._current_timeline()
        run = result.run
        out = self._analysis_base(result)
        if result.has_kernel:
            found = inversions_from_ops(result.timeline, result.kernel, run.priorities, run.order, run.names,
                                        result.objects(), run.timebase_hz)
        else:
            found = inversion_episodes(result.timeline, result.samples, run.mutexes, run.priorities, run.order,
                                       run.names, run.timebase_hz)
            found["source"] = "sampled"
        notes = found.pop("notes", [])
        if len(found["episodes"]) > limit:
            found["episodes_total"] = len(found["episodes"])
            found["episodes"] = found["episodes"][:limit]
        out.update(found)
        out["notes"] += notes
        return out

    # --- カーネルの出来事（U10） ----------------------------------------------------------------------

    def _kernel_result(self):
        result = self._current_timeline()
        if not result.has_kernel:
            raise RuntimeError(_NO_KERNEL)
        return result

    def _kernel_base(self, result) -> dict:
        out = self._analysis_base(result)
        trace = result.kernel
        out["kernel"] = {"events": sum(1 for op in trace.ops if op.kind != "lost"), "lost_records": trace.lost,
                         "gaps": sum(1 for op in trace.ops if op.kind == "lost")}
        if trace.lost or out["kernel"]["gaps"]:
            out["notes"].append("ITM があふれた・記録が欠けた: その間の出来事は無い（待ち・持ち主の状態を忘れる）。"
                                "SWO を速くする")
        return out

    def rtos_timeline_events(self, kinds: list[str] | None = None, task: str | None = None,
                             object: str | None = None, offset: int = 0, limit: int = EVENTS_LIMIT) -> dict:
        """カーネルの出来事の一覧（時刻順）: キュー・セマフォ・ミューテックスの送り・受け（give・take）と失敗・待ち（block）・
        準備完了（ready）・delay・suspend・優先度（prio_*）・通知・イベントグループ・ストリームバッファ・割り込みの出入り。
        kinds・task（行為者か対象のタスクの名前）・object（オブジェクトの名前）で絞り、offset から limit 件。"""
        result = self._kernel_result()
        run, objects, isr_names = result.run, result.objects(), self._exception_names()
        rows = [op.to_dict(run.names, objects, isr_names) for op in result.kernel.ops if op.kind != "lost"]
        selected = [r for r in rows if (kinds is None or r["kind"] in kinds)
                    and (task is None or task in (r.get("actor"), r.get("task")))
                    and (object is None or r.get("object") == object)]
        out = self._kernel_base(result)
        out.update(total=len(selected), offset=offset, events=selected[offset:offset + limit])
        return out

    def rtos_timeline_flow(self) -> dict:
        """Communication Flow: タスク・割り込み・オブジェクトの有向グラフ。辺は送り・give（行為者 → オブジェクト）、
        受け・take（オブジェクト → 行為者）、通知（行為者 → タスク）、set（イベントグループ）、wait（待った）と回数・
        失敗の回数。dot は Graphviz の形。"""
        result = self._kernel_result()
        out = self._kernel_base(result)
        out.update(communication_flow(result.kernel, result.run.names, result.objects(), self._exception_names()))
        return out

    def rtos_timeline_blocking(self, task: str | None = None, limit: int = 200) -> dict:
        """ブロック時間: 待ちの区間（block = オブジェクト・通知を待った / delay / suspend から ready まで）と、タスクごと・
        オブジェクトごとの回数・合計・平均・最長（いつ）・失敗（failed）の回数。task で区間の一覧を絞る（limit 件）。"""
        result = self._kernel_result()
        out = self._kernel_base(result)
        found = blocking(result.kernel, result.run.names, result.objects(), result.timeline.t1,
                         result.run.timebase_hz)
        waits = [w for w in found.pop("waits") if task is None or w["task"] == task]
        out.update(found, waits_total=len(waits), waits=waits[:limit])
        return out

    def rtos_timeline_queues(self, points: int = 500) -> dict:
        """キュー使用率: キュー・セマフォごとのメッセージ数（数）の推移（points 点まで、山を落とさない）・最大・平均・
        長さに対する割合、満杯（send_failed）・空（receive_failed）・待ちの回数。長さは始めのオブジェクトの一覧から。"""
        result = self._kernel_result()
        out = self._kernel_base(result)
        out.update(queue_usage(result.kernel, result.run.names, result.objects(), result.timeline.t1, points))
        return out

    def rtos_timeline_overhead(self) -> dict:
        """トレースフックの負荷: 語の数（ポート 31・見出し・中身）・出来事の種類ごとの数・SWO のバイト数と帯域の使用率。
        CPU の負荷は見積もらない（実測は HIL の手順）。"""
        result = self._kernel_result()
        run, tl = result.run, result.timeline
        out = self._kernel_base(result)
        found = overhead(result.kernel, tl.t1 - tl.t0, run.timebase_hz, run.baud, run.core_clock_hz)
        out["notes"] += found.pop("notes")
        out.update(found)
        return out
