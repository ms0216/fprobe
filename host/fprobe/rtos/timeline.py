"""タスクの時間軸（止めずに見る）: どのタスクがいつ CPU を使っていたか。

仕様: docs/superpowers/specs/2026-10-17-f6-rtos-design.md の 5 節。

方法 A（組み込み無し）: DAP サンプラ（F4）が周期的に読んだ `pxCurrentTCB` の値の列から。サンプリングの周期より短い
実行は見えないことがある（切り替えの回数は下限、区間の端は ±周期の半分）。

    samples = samples_from_recording(recording, address_of_pxCurrentTCB)
    tl = timeline_from_samples(samples, rtos.task_names())

方法 B（組み込みあり）: トレースフック（freertos_demo の fprobe_trace.h）が ITM のポート 31 に書く 1 語から。

    events = task_events(itm_packets(swo_bytes_from_recording(recording)))
    tl = timeline_from_events(events, rtos.task_names(), resolution=swo_packet_ticks)

どちらも `write_timeline(recorder, tl)` で共通の時間軸の記録の stream "rtos" に書ける（kind: run / switch / gap）。
方法 B の生の出来事は `write_task_events`（kind: task_in / task_out / create / delete / overflow / lost）。
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field

from ..itm import DecodeError, Instrumentation, ItmDecoder, Overflow, Packet, Sync

__all__ = ["EVENT_KINDS", "TRACE_PORT", "Gap", "Run", "Switch", "TaskEvent", "Timeline", "decode_trace_word",
           "itm_packets", "name_of", "samples_from_recording", "swo_bytes_from_recording", "task_events",
           "timeline_from_events", "timeline_from_samples", "write_task_events", "write_timeline"]

STREAM = "rtos"
TRACE_PORT = 31                                   # fprobe_trace.h の FPROBE_TRACE_PORT
EVENT_KINDS = ("task_in", "task_out", "create", "delete")      # 語の bit 1..0


def name_of(names: Mapping[int, str], handle: int | None) -> str:
    if handle is None:
        return "?"
    if handle in names:
        return names[handle]
    return "(none)" if handle == 0 else f"0x{handle:08X}"


@dataclass(frozen=True)
class Run:
    """そのタスクが CPU を使っていた区間 [t0, t1)。"""
    handle: int
    task: str
    t0: int
    t1: int
    uncertainty: float = 0        # 区間の端の不確かさ（タイムベース）
    samples: int | None = None    # 方法 A: この区間に入ったサンプルの数（1 なら周期より短いかもしれない）

    @property
    def duration(self) -> int:
        return self.t1 - self.t0


@dataclass(frozen=True)
class Switch:
    t: int
    previous: int | None          # 前のタスクの TCB（抜けの直後で分からなければ None）
    next: int
    uncertainty: float = 0


@dataclass(frozen=True)
class Gap:
    """情報の無い区間（サンプルの抜け・ITM のあふれ）。統計から除く。"""
    t0: int
    t1: int
    reason: str


@dataclass
class Timeline:
    method: str                   # sampled / itm
    t0: int
    t1: int
    resolution: float             # 時刻の分解能（タイムベース）
    lower_bound: bool             # 回数（実行・切り替え）が下限（方法 A）
    runs: list[Run] = field(default_factory=list)
    switches: list[Switch] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)

    def covered(self) -> int:
        """情報のある時間（範囲 − 抜け）。"""
        return (self.t1 - self.t0) - sum(g.t1 - g.t0 for g in self.gaps)

    def stats(self) -> dict:
        """タスクごとの合計・CPU の使用率・実行の回数・最長・最短・平均と、全体の切り替えの回数。"""
        covered = self.covered()
        per: dict[str, list[Run]] = {}
        for r in self.runs:
            per.setdefault(r.task, []).append(r)
        tasks = {}
        for name, runs in per.items():
            durations = [r.duration for r in runs]
            total = sum(durations)
            tasks[name] = {"handle": runs[0].handle, "total": total, "cpu": total / covered if covered else 0.0,
                           "runs": len(runs), "max_run": max(durations), "min_run": min(durations),
                           "mean_run": statistics.fmean(durations),
                           "short_runs": sum(1 for r in runs if r.samples == 1)}
        return {"method": self.method, "resolution": self.resolution, "lower_bound": self.lower_bound,
                "covered": covered, "switches": len(self.switches), "gaps": len(self.gaps), "tasks": tasks}

    def to_dict(self) -> dict:
        return {"method": self.method, "t0": self.t0, "t1": self.t1, "resolution": self.resolution,
                "lower_bound": self.lower_bound, "runs": [asdict(r) for r in self.runs],
                "switches": [asdict(s) for s in self.switches], "gaps": [asdict(g) for g in self.gaps]}


# --- 方法 A: サンプル ---------------------------------------------------------------------------

def timeline_from_samples(samples: Iterable[tuple[int, int]], names: Mapping[int, str], period: float | None = None,
                          gap_factor: float | None = None) -> Timeline:
    """(時刻, pxCurrentTCB の値) の列 → 時間軸。

    値が変わった 2 つのサンプルの中点を切り替えの時刻とし、不確かさは間隔の半分。分解能は period（無ければ
    サンプルの間隔の中央値）。ただし実際の間隔の中央値が period より長ければそれ（サンプラが設定の周期に追いつかない
    とき。SWD の速さで 1 巡回が長い）。gap_factor を明示したときだけ、間隔が gap_factor × 分解能を超えたところを抜けに
    する（区間を切り、切り替えに数えない）。既定では抜けにしない（経験則の倍率を持たない。長い間隔は切り替えの時刻の
    不確かさ = 間隔の半分、として表れる）。
    """
    points = sorted(samples)
    if not points:
        return Timeline("sampled", 0, 0, period or 0, True)
    diffs = [b[0] - a[0] for a, b in zip(points, points[1:])]
    achieved = statistics.median(diffs) if diffs else 0
    resolution = max(period, achieved) if period is not None else achieved
    tl = Timeline("sampled", points[0][0], points[-1][0], resolution, True)
    start, start_unc, value, count = points[0][0], 0.0, points[0][1], 1
    last = points[0][0]

    def close(end: int, end_unc: float) -> None:
        tl.runs.append(Run(value, name_of(names, value), start, end, max(start_unc, end_unc), count))
    for t, v in points[1:]:
        dt = t - last
        if gap_factor is not None and resolution and dt > gap_factor * resolution:
            close(last, 0)
            tl.gaps.append(Gap(last, t, "no_samples"))
            start, start_unc, value, count = t, 0.0, v, 1
        elif v != value:
            middle, unc = (last + t) // 2, dt / 2
            close(middle, unc)
            tl.switches.append(Switch(middle, value, v, unc))
            start, start_unc, value, count = middle, unc, v, 1
        else:
            count += 1
        last = t
    close(last, 0)
    return tl


# --- 方法 B: トレースフック → ITM ------------------------------------------------------------------

@dataclass(frozen=True)
class TaskEvent:
    t: int
    kind: str                     # task_in / task_out / create / delete / overflow（ITM のあふれ）/ lost（解読の誤り）
    handle: int = 0


def decode_trace_word(word: int) -> tuple[str, int]:
    """fprobe_trace.h の 1 語 → (出来事, TCB の番地)。"""
    return EVENT_KINDS[word & 3], word & ~3 & 0xFFFF_FFFF


def itm_packets(byte_events: Iterable[tuple[int, int]], synced: bool = True) -> list[tuple[int, Packet]]:
    """時刻付きの SWO のバイト（記録の stream swo）→ (パケットの先頭のバイトの時刻, パケット)。"""
    times: list[int] = []
    data = bytearray()
    for t, b in byte_events:
        times.append(t)
        data.append(b)
    decoder = ItmDecoder(synced)
    return [(times[min(p.offset, len(times) - 1)], p) for p in decoder.feed(bytes(data))]


def task_events(packets: Iterable[tuple[int, Packet]], port: int = TRACE_PORT) -> list[TaskEvent]:
    """ITM のパケット → タスクの出来事。あふれ・解読の誤り・同期の取り直しは overflow / lost（時間軸の抜け）。"""
    out: list[TaskEvent] = []
    for t, p in packets:
        if isinstance(p, Instrumentation):
            if p.port == port and p.size == 4:
                kind, handle = decode_trace_word(p.value)
                out.append(TaskEvent(t, kind, handle))
        elif isinstance(p, Overflow):
            out.append(TaskEvent(t, "overflow"))
        elif isinstance(p, DecodeError) or (isinstance(p, Sync) and p.skipped):
            out.append(TaskEvent(t, "lost"))
    return out


def timeline_from_events(events: Iterable[TaskEvent], names: Mapping[int, str], resolution: float = 0,
                         t_end: int | None = None) -> Timeline:
    """タスクの出来事 → 時間軸。task_in で区間を始め、task_out か次の task_in で閉じる。

    overflow / lost から次の task_in までは抜け（前のタスクが分からないので、その task_in は切り替えに数えない）。
    最後に開いている区間は t_end（観測の終わり）で閉じる。t_end が無ければ最後の出来事で閉じ、長さ 0 なら捨てる。
    """
    items = sorted(events, key=lambda e: e.t)
    if not items:
        return Timeline("itm", t_end or 0, t_end or 0, resolution, False)
    tl = Timeline("itm", items[0].t, max(items[-1].t, t_end if t_end is not None else items[-1].t), resolution, False)
    running: tuple[int, int] | None = None        # (TCB, 始め)
    previous: int | None = None                   # 最後に CPU を使っていたタスク（分からなければ None）
    gap_start: int | None = None

    def close(t: int) -> None:
        nonlocal running, previous
        if running is not None:
            handle, start = running
            tl.runs.append(Run(handle, name_of(names, handle), start, t, resolution))
            previous, running = handle, None
    for e in items:
        if e.kind == "task_in":
            if running is not None and running[0] == e.handle:
                continue
            close(e.t)
            if gap_start is not None:
                tl.gaps.append(Gap(gap_start, e.t, gap_reason))
                gap_start, previous = None, None
            if previous is not None and previous != e.handle:
                tl.switches.append(Switch(e.t, previous, e.handle, resolution))
            running = (e.handle, e.t)
        elif e.kind == "task_out":
            if running is not None and running[0] == e.handle:
                close(e.t)
        elif e.kind in ("overflow", "lost"):
            close(e.t)
            if gap_start is None:
                gap_start, gap_reason = e.t, e.kind
    if running is not None:
        end = tl.t1
        if end > running[1] or t_end is not None:
            close(end)
    if gap_start is not None:
        tl.gaps.append(Gap(gap_start, tl.t1, gap_reason))
    return tl


# --- 記録 ------------------------------------------------------------------------------------------

def samples_from_recording(recording, address: int, t0: int | None = None, t1: int | None = None) -> list[tuple[int, int]]:
    """記録の stream sample（DAP サンプラ）から、番地 address の (時刻, 値)。"""
    return [(e.t, e.b) for e in recording.events(t0, t1, streams=["sample"]) if e.a == address]


def swo_bytes_from_recording(recording, t0: int | None = None, t1: int | None = None) -> list[tuple[int, int]]:
    """記録の stream swo の (時刻, バイト)。"""
    return [(e.t, e.b) for e in recording.events(t0, t1, streams=["swo"], kinds=["byte"])]


def write_timeline(rec, timeline: Timeline, idle: Iterable[int] = ()) -> None:
    """時間軸を記録の stream rtos に（run: 区間、switch: 切り替え、gap: 抜け）。idle のタスクの run には idle: true
    （画面は名前でなくこの印でアイドルを知る）。"""
    names = {r.handle: r.task for r in timeline.runs}
    idle = set(idle)
    rows = []
    for r in timeline.runs:
        body = {"task": r.task, "method": timeline.method, "resolution": timeline.resolution}
        if r.handle in idle:
            body["idle"] = True
        if r.samples is not None:
            body["samples"] = r.samples
        rows.append((STREAM, "run", r.t0, r.handle, 0, body, r.t1, r.uncertainty or None))
    for s in timeline.switches:
        rows.append((STREAM, "switch", s.t, s.previous or 0, s.next,
                     {"from": name_of(names, s.previous), "to": name_of(names, s.next)}, None, s.uncertainty or None))
    for g in timeline.gaps:
        rows.append((STREAM, "gap", g.t0, 0, 0, {"reason": g.reason}, g.t1))
    rec.add_many(rows)


def write_task_events(rec, events: Iterable[TaskEvent], names: Mapping[int, str]) -> None:
    """方法 B の生の出来事を記録の stream rtos に（a = TCB、body = タスク名）。"""
    rec.add_many((STREAM, e.kind, e.t, e.handle, 0, {"task": name_of(names, e.handle)} if e.handle else None)
                 for e in events)
