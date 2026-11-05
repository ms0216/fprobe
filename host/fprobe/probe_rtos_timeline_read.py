"""RTOS の時間軸を止める・読む（RtosService に混ぜ込む部品）: 止めて解析・記録、結果の取り出しと辞書化。"""


from .api import NoData
from .profile.exceptions import exception_name
from .rtos.analysis import exception_activations
from .rtos.flow import blocking, inversions_from_ops
from .rtos.kernel_trace import kernel_trace
from .rtos.timeline import (STREAM, TRACE_PORT, itm_packets, task_events, timeline_from_events,
                            timeline_from_samples, write_task_events, write_timeline)
from .sampler import Sampler
from .probe_rtos_run import RUNS_LIMIT, DURATIONS, _mutex_tasks, _TimelineResult, _TimelineRun, _us

class RtosTimelineReadMixin:
    """rtos_timeline_stop / rtos_timeline_get と、解析・記録・辞書化。"""

    def rtos_timeline_stop(self) -> dict:
        """時間軸を取るのを止め、結果の要約（タスクごとの合計・CPU の使用率・実行の回数・最長／最短、切り替えの回数）を
        返す。記録中なら時間軸を記録の stream "rtos" に書く（run・switch・gap。方法 itm は task_in 等の生の出来事も）。"""
        with self._host._lock:
            run = self._rtos_run
            if run is None:
                raise RuntimeError("時間軸を取っていない（rtos_timeline_start）")
            try:
                if run.sampler:
                    Sampler(self._host.session.device).stop()
                if run.method == "itm" and self._host._debug is not None:
                    self._host.target.write_memory(run.flag, 0)            # トレースフックを止める（ITM の設定は残す）
                    for address, value in run.restore.items():       # 例外トレースを元に戻す
                        self._host.target.write_memory(address, value)
            finally:
                if run.sampler:
                    self._host._release_sampler("rtos")
                self._host._close_window(run.window)
                self._rtos_run = None
        result = self._build_timeline(run)
        self._rtos_last = result
        rec = self._host._recorder
        if rec is not None:
            self._record_timeline(rec, result)
        return self._timeline_dict(result, 0, 0, recorded=rec is not None)

    def _record_timeline(self, rec, result: _TimelineResult) -> None:
        """時間軸（run・switch・gap、itm なら生の出来事・割り込みの区間・カーネルの出来事）を記録の stream rtos に。"""
        run = result.run
        write_timeline(rec, result.timeline, run.idle)
        if result.events is not None:
            write_task_events(rec, result.events, run.names)
        if result.activations:
            names = self._exception_names(run)
            rec.add_many((STREAM, "isr", a.start, a.number, 0, {"name": exception_name(a.number, names)}, a.end)
                         for a in result.activations)
        if result.has_kernel:
            self._record_kernel(rec, result)

    def _record_kernel(self, rec, result: _TimelineResult) -> None:
        """カーネルの出来事（kind = 出来事、a = オブジェクトか対象のタスク、b = 行為者のタスク）、待ちの区間 blocked、
        逆転の区間 inversion を記録の stream rtos に。"""
        run, trace = result.run, result.kernel
        objects, isr_names = result.objects(), self._exception_names(run)
        rows = []
        for op in trace.ops:
            if op.kind == "lost":
                continue
            body = op.to_dict(run.names, objects, isr_names)
            del body["t"], body["kind"]
            rows.append((STREAM, op.kind, op.t, op.obj or op.task, op.actor or 0, body))
        for w in blocking(trace, run.names, objects, result.timeline.t1)["waits"]:
            rows.append((STREAM, "blocked", w["t0"], w["handle"], w["object_handle"] or 0,
                         {"task": w["task"], "on": w["on"], "outcome": w["outcome"]}, w["t1"]))
        found = (inversions_from_ops(result.timeline, trace, run.priorities, run.order, run.names, objects)
                 if run.order is not None else {"episodes": []})            # 向きが不明なら逆転を判定しない
        for ep in found["episodes"]:
            rows.append((STREAM, "inversion", ep["t0"], 0, 0, ep, ep["t1"]))
        rec.add_many(rows)

    def rtos_timeline_get(self, offset: int = 0, limit: int = RUNS_LIMIT) -> dict:
        """時間軸（取っている最中ならいままでの分、止めた後なら最後の結果）: 要約と、区間（run: タスク・始め・終わり・
        不確かさ）の offset から limit 件、抜け（gap）。時刻はタイムベース（timebase_hz）。"""
        return self._timeline_dict(self._current_timeline(), offset, limit)

    def _current_timeline(self) -> _TimelineResult:
        """取っている最中ならいままでの分、止めた後なら最後の結果。"""
        run = self._rtos_run
        if run is not None:
            return self._build_timeline(run)
        if self._rtos_last is not None:
            return self._rtos_last
        raise NoData("時間軸が無い（rtos_timeline_start）")

    def _exception_names(self, run: _TimelineRun) -> dict[int, str]:
        """割り込みの名前（rtos_timeline_start に渡したベクタ表から。無ければ番号の名前）。"""
        return self._host._vector_names(run.vector_table)

    def _timeline_state(self) -> dict | None:
        run = self._rtos_run
        if run is None:
            return None
        out = {"running": True, "method": run.method, "t0": run.window.t0, "timebase_hz": run.timebase_hz,
               "source": "recording" if run.window.recorded else "event_log", "tasks_known": len(run.names),
               "warnings": list(run.warnings)}
        if run.method == "sampled":
            out.update(address=run.address, rate_hz=run.rate_hz)
        else:
            out.update(port=TRACE_PORT, resolution=run.resolution, exceptions=run.exceptions)
        if run.slots:
            out["memory"] = {"stacks": [s.name for s in run.slots.values() if s.kind == "stack"],
                             "heap": any(s.kind == "heap" for s in run.slots.values()),
                             "rate_hz": run.rate_hz if run.method == "sampled" else run.memory_rate_hz}
        if run.mutexes:
            out["mutexes"] = [w.name for w in run.mutexes]
        return out

    def _build_timeline(self, run: _TimelineRun) -> _TimelineResult:
        words = run.words
        sampled = self._host._window_events(run.window, "sample") if run.sampler else []
        extra = [(t, a, v) for t, a, v in sampled if a in words]
        mutex_tasks = _mutex_tasks(run.mutexes, extra)
        if run.method == "sampled":
            samples = [(t, v) for t, a, v in sampled if a == run.address]
            self._learn_names(run, {v for _, v in samples} | mutex_tasks)
            period = run.timebase_hz / run.rate_hz
            tl = timeline_from_samples(samples, run.names, period=period)      # 実際の間隔は分解能（resolution）
            return _TimelineResult(tl, None, run, extra)
        data = [(t, b) for t, _, b in self._host._window_events(run.window, "swo")]
        packets = itm_packets(data)
        events = task_events(packets)
        kernel = kernel_trace(packets)
        actors = {op.actor for op in kernel.ops if op.actor} | {op.task for op in kernel.ops if op.task}
        self._learn_names(run, {e.handle for e in events} | mutex_tasks | actors)
        tl = timeline_from_events(events, run.names, resolution=run.resolution, t_end=run.window.t1)
        activations, issues = None, None
        if run.exceptions:
            activations, inconsistencies, overflows = exception_activations(packets)
            issues = {"inconsistencies": inconsistencies, "overflows": overflows}
        return _TimelineResult(tl, events, run, extra, activations, issues, kernel)

    def _learn_names(self, run: _TimelineRun, handles: set[int]) -> None:
        """一覧に無い TCB の名前と元の優先度を、動作中の読みで引く（読めなければ番地のまま・優先度は分からない）。"""
        unknown = sorted(h for h in handles if h and (h not in run.names or h not in run.priorities))
        if not unknown or self._host._debug is None:
            return
        with self._host._lock:
            try:
                kernel = self._kernel(halted=False)
            except Exception:
                return
            for h in unknown:
                try:
                    name = kernel.handle_name(h)
                    priority = kernel.handle_priority(h)
                except Exception:                                     # 壊れた値・読めない番地: 番地のまま
                    continue
                if name:
                    run.names[h] = name
                run.priorities.setdefault(h, priority)

    def _timeline_dict(self, result: _TimelineResult, offset: int, limit: int, recorded: bool | None = None) -> dict:
        tl, run = result.timeline, result.run
        hz = run.timebase_hz
        stats = tl.stats()
        for row in stats["tasks"].values():
            row.update({f"{k}_us": _us(row[k], hz) for k in DURATIONS})
        out = {"running": run.window.t1 is None, "method": tl.method, "timebase_hz": hz,
               "t0": tl.t0, "t1": tl.t1, "resolution": tl.resolution, "resolution_us": _us(tl.resolution, hz),
               "lower_bound": tl.lower_bound, "stats": stats, "window": self._host._window_info(run.window, hz),
               "warnings": list(run.warnings), "runs_total": len(tl.runs)}
        if limit:
            out["offset"] = offset
            out["runs"] = [{"task": r.task, "handle": r.handle, "t0": r.t0, "t1": r.t1,
                            "duration_us": r.duration * 1e6 / hz, "uncertainty": r.uncertainty,
                            **({"samples": r.samples} if r.samples is not None else {})}
                           for r in tl.runs[offset:offset + limit]]
            out["gaps"] = [{"t0": g.t0, "t1": g.t1, "reason": g.reason} for g in tl.gaps]
        if recorded is not None:
            out["recorded"] = recorded
        if tl.lower_bound:
            out["notes"] = ["方法 sampled: 周期より短い実行は見えないことがある。切り替え・実行の回数は下限、区間の端は"
                            "±周期の半分（samples = 1 の区間は周期より短いかもしれない）"]
        if result.activations is not None:
            out["exceptions"] = {"activations": len(result.activations), **(result.exception_issues or {})}
            if (result.exception_issues or {}).get("overflows"):
                out["warnings"].append(
                    f"ITM のあふれが {result.exception_issues['overflows']} 回: 例外の出入りが続くと SWO が追いつかず、"
                    "パケットが欠ける（割り込みの回数・時間は下限）。swo_baud を上げるか kernel=false で帯域を空ける")
        if result.kernel is not None:
            gaps = sum(1 for op in result.kernel.ops if op.kind == "lost")
            out["kernel"] = {"events": len(result.kernel.ops) - gaps, "lost_records": result.kernel.lost, "gaps": gaps}
        if run.slots or run.mutexes:
            out["words_samples"] = len(result.samples)
        return out
