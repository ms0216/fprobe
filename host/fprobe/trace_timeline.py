"""取り込んだトレース（_Feed）のタスクの出入りを時間軸にして、RtosService の最後の結果にする。

TraceService（probe_trace）が trace_stop・trace_snapshot・trace_import の後に呼ぶ。RtosService の状態（取っている最中か・最後の結果）と記録は
呼び出し側が渡す。アイドルのタスクは使う側が名前で明示したものだけ（fprobe は名前でアイドルを決めない）。
"""

from __future__ import annotations

from .probe_formats_feed import _PRIORITY_ORDER, _Feed, _idle_tasks, _order_name
from .probe_window import EventWindow
from .rtos.timeline import timeline_from_events, write_timeline
from .tracefmt.model import TargetClock, activations, task_events

__all__ = ["publish_timeline"]

def publish_timeline(feed: _Feed, idle_tasks: list[str] | None, *, rtos_running: bool, recorder, set_last) -> dict:
    """タスクの出入りを時間軸にし、RtosService の最後の結果にする（解析の道具が読む）。記録中なら stream "rtos"。
    idle_tasks: アイドルとして扱うタスクの名前（使う側が明示する。名前の完全一致。無ければアイドルは不明）。
    rtos_running: rtos_timeline_start の時間軸を取っている最中か、recorder: 記録中の記録（無ければ None）、
    set_last(結果): RtosService の最後の結果にする。"""
    from .probe_rtos import _TimelineResult, _TimelineRun
    trace = feed.trace
    clock = feed.clock
    hz = feed.timebase_hz
    if clock is None:
        if not trace.timed:
            return {"made": False, "reason": "時刻の無い形（出来事の順だけ）"}
        if not trace.ts_hz:
            return {"made": False, "reason": "ターゲットの時刻の速さが分からない（ts_hz）"}
        # 機器の無い取り込み: ターゲットの時刻のまま（区切りごとに ts をそのまま。区切りをまたぐ間隔は分からないので
        # 時間軸では区切りの所が overflow の抜けになり、抜けの長さは意味を持たない）
        clock = TargetClock(trace.ts_hz, trace.ts_hz, 0, 0)
        for s in range(1, trace.segment + 1):
            clock.add_anchor(s, 0, 0)
        hz = trace.ts_hz
    events = task_events(trace.events, clock)
    if not any(e.kind == "task_in" for e in events):
        return {"made": False, "reason": "タスクの切り替えの出来事が無い"}
    if rtos_running:
        return {"made": False, "reason": "rtos_timeline_start の時間軸を取っている最中（止めてから）"}
    names = {k: v for k, v in trace.names().items()}
    idle, given = _idle_tasks(names, idle_tasks)
    resolution = max(1.0, clock.ratio)
    tl = timeline_from_events(events, names, resolution=resolution,
                              t_end=clock.place(trace.events[-1]) if trace.events else None)
    tl.method = trace.format
    isr = activations(trace.events, clock) if any(e.kind == "isr_enter" for e in trace.events) else None
    run = _TimelineRun(trace.format, EventWindow(tl.t0, False, t1=tl.t1), names, hz, resolution=resolution,
                       order=_PRIORITY_ORDER.get(trace.format),
                       priorities={t.id: t.priority for t in trace.tasks.values() if t.priority is not None},
                       idle=idle, exceptions=isr is not None)
    if idle_tasks is not None:
        run.warnings.append(f"アイドルのタスクは idle_tasks で渡された名前: {', '.join(idle_tasks) or '（なし）'}")
    run.warnings.append(f"取り込んだトレース（{trace.format}）から作った時間軸" +
                        ("" if feed.clock is not None else "（時刻はターゲットの時計のまま）"))
    if trace.breaks:
        run.warnings.append(f"時刻の区切り {len(trace.breaks)} か所（trace の breaks）: 区切りの所は overflow の抜けにした"
                            + ("" if feed.clock is not None else "（ターゲットの時計のままなので、抜けの長さは意味を持たない）"))
    set_last(_TimelineResult(tl, events, run, activations=isr))
    rec = recorder
    recorded = rec is not None and feed.clock is not None
    if recorded:
        write_timeline(rec, tl, run.idle)
    return {"made": True, "runs": len(tl.runs), "switches": len(tl.switches), "gaps": len(tl.gaps),
            "isr": len(isr) if isr is not None else None, "recorded": recorded, "timebase_hz": hz,
            "idle_tasks": given, "priority_order": _order_name(run.order), "stats": tl.stats()}
