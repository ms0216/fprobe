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
    """そのタスクが CPU を使っていた区間 [t0, t1)。

    start_cut / end_cut: その端は切り替えでなく、観測の端・抜け（サンプルの抜け・ITM のあふれ）で切った所（本当の端は
    分からない。観測の端なら外側）。uncertainty は切っていない端の不確かさの大きい方（タイムベース）。両端とも切った・
    分解能が分からないなら None（0 にしない）。"""
    handle: int
    task: str
    t0: int
    t1: int
    uncertainty: float | None = None
    samples: int | None = None    # 方法 A: この区間に入ったサンプルの数（1 なら周期より短いかもしれない）
    start_cut: bool = False
    end_cut: bool = False

    @property
    def duration(self) -> int:
        return self.t1 - self.t0

    @property
    def cut(self) -> bool:
        return self.start_cut or self.end_cut


def _edge_uncertainty(start: float | None, start_cut: bool, end: float | None, end_cut: bool) -> float | None:
    edges = [u for u, cut in ((start, start_cut), (end, end_cut)) if not cut]
    if not edges or any(u is None for u in edges):
        return None
    return max(edges)


@dataclass(frozen=True)
class Switch:
    t: int
    previous: int | None          # 前のタスクの TCB（抜けの直後で分からなければ None）
    next: int
    uncertainty: float | None = None      # None = 分からない（分解能が分からない）


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
    resolution: float | None      # 時刻の分解能（タイムベース。None = 分からない）
    lower_bound: bool             # 回数（実行・切り替え）が下限（方法 A）
    runs: list[Run] = field(default_factory=list)
    switches: list[Switch] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)

    def covered(self) -> int:
        """情報のある時間（範囲 − 抜け）。"""
        return (self.t1 - self.t0) - sum(g.t1 - g.t0 for g in self.gaps)

    def stats(self) -> dict:
        """タスクごとの合計・CPU の使用率・実行の回数・最長・最短・平均と、全体の切り替えの回数。

        合計・使用率は観測した時間の中の事実なので全部の区間で数える。最長・最短・平均は両端が切り替えの区間だけ
        （観測の端・抜けで切った区間は本当の長さが分からない: その数 cut_runs と、切った区間の観測した長さの最大
        max_cut_run = その区間の長さの下限）。両端が切り替えの区間が無ければ None。"""
        covered = self.covered()
        per: dict[str, list[Run]] = {}
        for r in self.runs:
            per.setdefault(r.task, []).append(r)
        tasks = {}
        for name, runs in per.items():
            total = sum(r.duration for r in runs)
            whole = [r.duration for r in runs if not r.cut]
            cut = [r.duration for r in runs if r.cut]
            tasks[name] = {"handle": runs[0].handle, "total": total, "cpu": total / covered if covered else 0.0,
                           "runs": len(runs), "max_run": max(whole) if whole else None,
                           "min_run": min(whole) if whole else None,
                           "mean_run": statistics.fmean(whole) if whole else None,
                           "cut_runs": len(cut), "max_cut_run": max(cut) if cut else None,
                           "short_runs": sum(1 for r in runs if r.samples == 1)}
        return {"method": self.method, "resolution": self.resolution, "lower_bound": self.lower_bound,
                "covered": covered, "switches": len(self.switches), "gaps": len(self.gaps), "tasks": tasks}

    def to_dict(self) -> dict:
        return {"method": self.method, "t0": self.t0, "t1": self.t1, "resolution": self.resolution,
                "lower_bound": self.lower_bound, "runs": [asdict(r) for r in self.runs],
                "switches": [asdict(s) for s in self.switches], "gaps": [asdict(g) for g in self.gaps]}


# --- 方法 A: サンプル ---------------------------------------------------------------------------

def timeline_from_samples(samples: Iterable[tuple[int, int]], names: Mapping[int, str], period: float | None = None,
                          gap: float | None = None) -> Timeline:
    """(時刻, pxCurrentTCB の値) の列 → 時間軸。

    値が変わった 2 つのサンプルの中点を切り替えの時刻とし、不確かさは間隔の半分（中点は置き場所の決まりで、切り替えは
    その間のどこか）。分解能は実際のサンプルの間隔の最大（抜けにした間隔は除く。どの切り替えの時刻もこの幅の中に
    ある。代表値（中央値）では決めない）。サンプルが 1 つなら period（設定の周期。無ければ 0）。分解能は表すだけで、
    判定には使わない。gap（タイムベースの刻み）を明示したときだけ、間隔が gap を超えたところを抜けにする（区間を切り、
    切り替えに数えない。代表値の倍率で決めない）。既定では抜けにしない（長い間隔は切り替えの時刻の不確かさ = 間隔の
    半分、として表れる）。最初・最後のサンプル、抜けの両側で閉じた端は切った端（Run.start_cut / end_cut。不確かさ
    0 で閉じない）。分解能はサンプルが 1 つで period も無ければ None（分からない）。
    """
    points = sorted(samples)
    if not points:
        return Timeline("sampled", 0, 0, period, True)
    diffs = [b[0] - a[0] for a, b in zip(points, points[1:]) if gap is None or b[0] - a[0] <= gap]
    resolution = max(diffs) if diffs else period
    tl = Timeline("sampled", points[0][0], points[-1][0], resolution, True)
    start, start_unc, start_cut, value, count = points[0][0], None, True, points[0][1], 1
    last = points[0][0]

    def close(end: int, end_unc: float | None, end_cut: bool) -> None:
        tl.runs.append(Run(value, name_of(names, value), start, end,
                           _edge_uncertainty(start_unc, start_cut, end_unc, end_cut), count, start_cut, end_cut))
    for t, v in points[1:]:
        dt = t - last
        if gap is not None and dt > gap:
            close(last, None, True)
            tl.gaps.append(Gap(last, t, "no_samples"))
            start, start_unc, start_cut, value, count = t, None, True, v, 1
        elif v != value:
            middle, unc = (last + t) // 2, dt / 2
            close(middle, unc, False)
            tl.switches.append(Switch(middle, value, v, unc))
            start, start_unc, start_cut, value, count = middle, unc, False, v, 1
        else:
            count += 1
        last = t
    close(last, None, True)
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
    """時刻付きの SWO のバイト（記録の stream swo）→ (パケットの先頭のバイトの時刻, パケット)。パケットの位置が
    バイトの外なら誤り（黙って最後のバイトの時刻にしない）。"""
    times: list[int] = []
    data = bytearray()
    for t, b in byte_events:
        times.append(t)
        data.append(b)
    decoder = ItmDecoder(synced)
    out = []
    for p in decoder.feed(bytes(data)):
        if not 0 <= p.offset < len(times):
            raise ValueError(f"ITM のパケットの位置 {p.offset} がバイトの外（{len(times)} バイト）")
        out.append((times[p.offset], p))
    return out


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


def timeline_from_events(events: Iterable[TaskEvent], names: Mapping[int, str], resolution: float | None = None,
                         t_end: int | None = None) -> Timeline:
    """タスクの出来事 → 時間軸。task_in で区間を始め、task_out か次の task_in で閉じる。

    overflow / lost から次の task_in までは抜け（前のタスクが分からないので、その task_in は切り替えに数えない）。
    最後に開いている区間は t_end（観測の終わり）で閉じる。t_end が無ければ最後の出来事で閉じ、長さ 0 なら捨てる。
    抜け・観測の終わりで閉じた端は切った端（Run.end_cut）。resolution は出来事の時刻の分解能（分からなければ None）。
    """
    items = sorted(events, key=lambda e: e.t)
    if not items:
        return Timeline("itm", t_end or 0, t_end or 0, resolution, False)
    tl = Timeline("itm", items[0].t, max(items[-1].t, t_end if t_end is not None else items[-1].t), resolution, False)
    running: tuple[int, int] | None = None        # (TCB, 始め)
    previous: int | None = None                   # 最後に CPU を使っていたタスク（分からなければ None）
    gap_start: int | None = None
    gap_reason = ""                                          # gap_start があるときの理由（出来事の種類）

    def close(t: int, cut: bool = False) -> None:
        nonlocal running, previous
        if running is not None:
            handle, start = running
            tl.runs.append(Run(handle, name_of(names, handle), start, t,
                               _edge_uncertainty(resolution, False, resolution, cut), end_cut=cut))
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
            close(e.t, cut=True)
            if gap_start is None:
                gap_start, gap_reason = e.t, e.kind
    if running is not None:
        end = tl.t1
        if end > running[1] or t_end is not None:
            close(end, cut=True)
    if gap_start is not None:
        tl.gaps.append(Gap(gap_start, tl.t1, gap_reason))
    return tl


# --- 記録 ------------------------------------------------------------------------------------------

def samples_from_recording(recording, address: int, t0: int | None = None, t1: int | None = None) -> list[tuple[int, int]]:
    """記録の stream sample（DAP サンプラ）から、番地 address の読めた語の (時刻, 値)（読めなかった語 = 本体に ACK、
    誤りの区間 kind error は入れない）。"""
    return [(e.t, e.b) for e in recording.events(t0, t1, streams=["sample"], kinds=["word"])
            if e.a == address and not (isinstance(e.body, dict) and "ack" in e.body)]


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
        if r.start_cut:
            body["start_cut"] = True
        if r.end_cut:
            body["end_cut"] = True
        rows.append((STREAM, "run", r.t0, r.handle, 0, body, r.t1, r.uncertainty))      # None = 分からない、0 は 0
    for s in timeline.switches:
        rows.append((STREAM, "switch", s.t, s.previous or 0, s.next,
                     {"from": name_of(names, s.previous), "to": name_of(names, s.next)}, None, s.uncertainty))
    for g in timeline.gaps:
        rows.append((STREAM, "gap", g.t0, 0, 0, {"reason": g.reason}, g.t1))
    rec.add_many(rows)


def write_task_events(rec, events: Iterable[TaskEvent], names: Mapping[int, str]) -> None:
    """方法 B の生の出来事を記録の stream rtos に（a = TCB、body = タスク名）。"""
    rec.add_many((STREAM, e.kind, e.t, e.handle, 0, {"task": name_of(names, e.handle)} if e.handle else None)
                 for e in events)
