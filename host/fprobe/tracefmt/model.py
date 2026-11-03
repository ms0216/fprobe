"""取り込んだトレースの共通の形。形式（SystemView・CTF・Event Recorder・TraceX・Tracealyzer）の解読は、それぞれ
この形の出来事（`TraceEvent`）の列と、タスクの一覧（`TaskInfo`）にする。

- 時刻 `ts` はターゲットの時計（形式の単位。`Trace.ts_hz` が速さ。分からなければ None）で、解読が 32 ビットの
  桁あふれを直した単調な値。ただし差が意味を持つのは同じ区切り（`TraceEvent.segment`）の中だけ: 形式の中で時刻の
  つながりが切れた所（流れの読み飛ばし・記録の再開・時刻の基準の食い違い）で区切りを変え、`Trace.breaks` に理由を
  事実として残す（区切りをまたぐ間隔は分からない。ts は並べるために前の値から続けるだけ）。
- 時刻の無い形（`Trace.timed` が False）の ts は出来事の順の番号で、時刻ではない（速さを付けない）。
- 出来事の種類（kind）は形式に依らない名前にそろえる（`KINDS`）。形式ごとの名前は `name`、値は `args`。
- 共通の時間軸の記録へは `TargetClock` でタイムベースに写して stream "trace" に書き（`write_trace`）、タスクの
  出入りは fprobe.rtos.timeline の `TaskEvent` にして stream "rtos" の時間軸（run / switch / gap）にもできる
  （`task_events` → `timeline_from_events`）。こうすると時間軸の解析（rtos_timeline_load 等）がそのまま使える。
"""

from __future__ import annotations


from collections.abc import Iterable
from dataclasses import dataclass, field

from ..profile.exceptions import Activation
from ..rtos.timeline import TaskEvent

__all__ = ["KINDS", "STREAM", "TargetClock", "TaskInfo", "Trace", "TraceEvent", "activations", "task_events",
           "write_trace"]

STREAM = "trace"
KINDS = (
    "task_in",        # タスクが CPU を使い始めた（task = そのタスク）
    "task_out",       # タスクが CPU を手放した
    "task_ready",     # 実行できる状態になった
    "task_block",     # 待ちに入った（理由は args）
    "task_create",
    "task_delete",
    "task_info",      # 名前・優先度・スタック（tasks に反映済み）
    "isr_enter",      # 割り込みに入った（args["number"] = 例外の番号。分かれば）
    "isr_exit",
    "idle",           # アイドル（どのタスクも動いていない）に入った
    "print",          # 文字のログ（text）
    "marker",         # 名前付きの出来事・利用者の印（ZEP-03・SystemView の Mark 等）
    "object",         # カーネルオブジェクトの操作（セマフォ・ミューテックス・キュー等。name と args）
    "overflow",       # ターゲットのバッファがあふれて出来事を失った
    "info",           # 記録の情報（システムの説明・時刻の速さ等）
    "other",          # 解読はしたが上のどれでもない
)


@dataclass(frozen=True)
class TraceEvent:
    ts: int                         # ターゲットの時計（単調）
    kind: str                       # KINDS のどれか
    name: str                       # 形式での名前（"TASK_START_EXEC"・"thread_switched_in" 等）
    task: int | None = None         # かかわるタスク（形式の ID・TCB の番地）
    args: dict = field(default_factory=dict)
    text: str | None = None
    segment: int = 0                # 時刻の区切り（Trace.breaks）。ts の差は同じ区切りの中だけ意味を持つ

    def to_dict(self) -> dict:
        out: dict = {"ts": self.ts, "kind": self.kind, "name": self.name}
        if self.segment:
            out["segment"] = self.segment
        if self.task is not None:
            out["task"] = self.task
        if self.args:
            out["args"] = self.args
        if self.text is not None:
            out["text"] = self.text
        return out


@dataclass
class TaskInfo:
    id: int
    name: str | None = None
    priority: int | None = None
    stack_base: int | None = None
    stack_size: int | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in vars(self).items() if v is not None}


@dataclass
class Trace:
    """1 つの形式の解読の結果（流れの途中でも、そこまでの分）。"""
    format: str
    events: list[TraceEvent] = field(default_factory=list)
    tasks: dict[int, TaskInfo] = field(default_factory=dict)
    ts_hz: float | None = None      # ターゲットの時計の速さ
    cpu_hz: float | None = None
    system: dict = field(default_factory=dict)    # システムの説明（形式が送るもの）
    lost: int = 0                   # あふれ・解読の誤りで失った出来事の数（分かる範囲）
    errors: int = 0                 # 解読できなかったかたまりの数
    notes: list[str] = field(default_factory=list)
    ts_wraps: int = 0               # 時刻の桁あふれを「前の出来事から 1 周未満」の前提で戻した回数（unwrap）
    ts_wrap_min_back: int | None = None     # 戻した所のうち、生の値が前の値からいちばん少なく戻った量（刻み）
    timed: bool = True              # False: 形式に時刻が無い（ts は順の番号）
    segment: int = 0                # いまの時刻の区切り（解読が出来事に付ける）
    breaks: list[dict] = field(default_factory=list)   # 区切りの始まり {segment, reason, ...事実}

    def unwrap(self, previous: int | None, value: int, mask: int = 0xFFFFFFFF) -> int:
        """mask の幅で送られた時刻を、前の値（単調）から続く値にする。前の値より小さく戻った所は 1 周の前進とみなし
        （前提: 出来事の間は 1 周（mask + 1）未満で、時刻は戻らない）、その回数を ts_wraps に、生の値が前の値から
        戻った量の最小を ts_wrap_min_back に残す。前提が外れた所（本当に時刻が戻った・2 周以上たった）は形式に
        上位の桁が無いので解読からは分からない: 前提は summary の assumptions に、戻った量は事実として返し、判断は
        使う側がする。"""
        if previous is None:
            return value
        if value < previous & mask:
            self.ts_wraps += 1
            back = (previous & mask) - value
            self.ts_wrap_min_back = back if self.ts_wrap_min_back is None else min(self.ts_wrap_min_back, back)
        return previous + ((value - previous) & mask)

    def new_segment(self, reason: str, **facts) -> None:
        """ここから時刻の区切りを変える（前の出来事との間隔が分からない）。いまの区切りにまだ出来事が無ければ
        何もしない（前の出来事とはすでに区切られているか、前に出来事が無い）。"""
        if not self.events or self.events[-1].segment != self.segment:
            return
        self.segment += 1
        self.breaks.append({"segment": self.segment, "reason": reason, **facts})

    def task(self, task_id: int) -> TaskInfo:
        return self.tasks.setdefault(task_id, TaskInfo(task_id))

    def names(self) -> dict[int, str]:
        return {t.id: t.name for t in self.tasks.values() if t.name}

    def summary(self) -> dict:
        counts: dict[str, int] = {}
        for e in self.events:
            counts[e.kind] = counts.get(e.kind, 0) + 1
        out = {"format": self.format, "events": len(self.events), "kinds": counts,
               "tasks": [t.to_dict() for t in sorted(self.tasks.values(), key=lambda t: t.id)],
               "lost": self.lost, "errors": self.errors, "notes": list(self.notes), "ts_wraps": self.ts_wraps,
               "timed": self.timed, "segments": self.segment + 1, "breaks": [dict(b) for b in self.breaks]}
        if self.ts_wraps:
            out["ts_wrap_min_back"] = self.ts_wrap_min_back
            out["assumptions"] = ["ts_wrap_less_than_one_period"]
        if self.events:
            out.update(ts_first=self.events[0].ts, ts_last=self.events[-1].ts)
            if self.ts_hz and self.timed and self.events[0].segment == self.events[-1].segment:
                out["duration_s"] = (self.events[-1].ts - self.events[0].ts) / self.ts_hz
        for key in ("ts_hz", "cpu_hz"):
            if getattr(self, key):
                out[key] = getattr(self, key)
        if self.system:
            out["system"] = self.system
        return out


class TargetClock:
    """ターゲットの時計 → デバイスのタイムベース。1 つの対応点（anchor_ts のときタイムベース anchor_t）とトレースが
    申告した速さで写す。

    対応点は「その出来事を受けた・読んだ時刻」で、出来事はそれより前に起きた（どれだけ前かは分からない）: anchor_t は
    対応点の出来事の時刻の上限（anchor_bound = "upper"）。uncertainty = anchor_t そのものの不確かさ（受けた時刻の
    写しの幅。None = 分からない。0 で埋めない）で、出来事までの遅れ（anchor_lag）と速さの誤差（rate_error）は
    含まない（unknown に名前で返す）。写した時刻は、申告の速さが正しければ各出来事の時刻の上限。

    対応点は時刻の区切り（`TraceEvent.segment`）ごと: 区切りをまたぐ間隔は分からないので、対応点の無い区切りの
    出来事は写さない（`place` が None）。"""

    def __init__(self, ts_hz: float, timebase_hz: float, anchor_ts: int, anchor_t: int,
                 uncertainty: float | None = None, segment: int = 0):
        self.ratio = timebase_hz / ts_hz
        self.anchors: dict[int, tuple[int, int, float | None]] = {segment: (anchor_ts, anchor_t, uncertainty)}

    def add_anchor(self, segment: int, anchor_ts: int, anchor_t: int, uncertainty: float | None = None) -> bool:
        """区切りに対応点が無ければ足す（足したら True。ある区切りは最初の対応点のまま）。"""
        if segment in self.anchors:
            return False
        self.anchors[segment] = (anchor_ts, anchor_t, uncertainty)
        return True

    def to_timebase(self, ts: int, segment: int = 0) -> int:
        anchor_ts, anchor_t, _ = self.anchors[segment]
        return anchor_t + round((ts - anchor_ts) * self.ratio)

    def place(self, e: TraceEvent) -> int | None:
        """出来事のタイムベースの時刻。その区切りに対応点が無ければ None（分からない）。"""
        return self.to_timebase(e.ts, e.segment) if e.segment in self.anchors else None

    def describe(self) -> dict:
        """時刻の写しの事実（API の clock）。"""
        anchors = [{"segment": s, "anchor_ts": a, "anchor_t": t, "uncertainty": u}
                   for s, (a, t, u) in sorted(self.anchors.items())]
        unknown = ["anchor_lag", "rate_error"]
        if any(a["uncertainty"] is None for a in anchors):
            unknown.append("anchor_time")
        return {"anchors": anchors, "anchor_bound": "upper", "assumptions": ["declared_rate"], "unknown": unknown}


def task_events(events: Iterable[TraceEvent], clock: TargetClock) -> list[TaskEvent]:
    """タスクの出入り（task_in / task_out / task_create / task_delete / idle / overflow）→ 時間軸の出来事（タイムベース）。
    idle はどのタスクでもない時間なので、直前のタスクの task_out にする（アイドルのタスクを持つ RTOS はその task_in が来る）。
    対応点の無い区切りの出来事は入れない。区切りが変わる所は、前の区切りの最後の出来事の時刻に overflow を置く（そこから
    先の様子は分からない。区切りをまたいで実行の区間をつなげない）。"""
    out: list[TaskEvent] = []
    current: int | None = None
    segment: int | None = None
    last_t: int | None = None
    for e in events:
        t = clock.place(e)
        if t is None:
            continue
        if segment is not None and e.segment != segment:
            out.append(TaskEvent(last_t, "overflow"))
            current = None
        segment, last_t = e.segment, t
        if e.kind == "task_in" and e.task is not None:
            out.append(TaskEvent(t, "task_in", e.task))
            current = e.task
        elif e.kind == "task_out" and e.task is not None:
            out.append(TaskEvent(t, "task_out", e.task))
            current = None if current == e.task else current
        elif e.kind == "idle" and current is not None:
            out.append(TaskEvent(t, "task_out", current))
            current = None
        elif e.kind == "task_create" and e.task is not None:
            out.append(TaskEvent(t, "create", e.task))
        elif e.kind == "task_delete" and e.task is not None:
            out.append(TaskEvent(t, "delete", e.task))
        elif e.kind == "overflow":
            out.append(TaskEvent(t, "overflow"))
            current = None
    return out


def activations(events: Iterable[TraceEvent], clock: TargetClock) -> list[Activation]:
    """isr_enter / isr_exit の入れ子 → 割り込みの回（fprobe.profile.exceptions.Activation。タイムベース）。
    出口の無い入口・入口の無い出口は数えない。番号の分からない割り込みは 0。出口が番号（args["number"]）を持てば
    同じ番号の入口と対にする（間の出口の欠けた内側の回は捨てる。fprobe.rtos.flow.isr_pairing と同じ）。番号の無い出口
    （SystemView・CTF 等の形式に番号が無い）は最も内側の入口と対にする。対応点の無い区切りの出来事は使わず、
    区切りが変わる所で入れ子を捨てる（区切りをまたぐ長さは分からない）。"""
    out: list[Activation] = []
    stack: list[list[int]] = []                 # [番号, 入った時刻, 横取りした時間]
    segment: int | None = None
    for e in events:
        t = clock.place(e)
        if t is None:
            continue
        if e.segment != segment:
            stack.clear()
            segment = e.segment
        if e.kind == "isr_enter":
            stack.append([int(e.args.get("number", 0)), t, 0])
        elif e.kind == "isr_exit" and stack:
            exit_number = e.args.get("number")
            if exit_number is not None:
                if not any(f[0] == exit_number for f in stack):
                    continue                                    # 入口の無い出口
                while stack[-1][0] != exit_number:
                    stack.pop()                                 # 出口の欠けた内側の回
            number, start, nested = stack.pop()
            inclusive = t - start
            out.append(Activation(number, start, t, inclusive, inclusive - nested))
            if stack:
                stack[-1][2] += inclusive
        elif e.kind == "overflow":
            stack.clear()
    return out


def write_trace(rec, trace: Trace, clock: TargetClock, events: Iterable[TraceEvent] | None = None) -> int:
    """出来事を記録の stream "trace" に（kind = 種類、a = タスク、body = 形式の名前・値・本文・ターゲットの時刻）。
    時刻は TargetClock の写し（申告の速さが正しければ出来事の時刻の上限）で、本体に time_source = "target_clock"・
    time_bound = "upper"。不確かさの列は入れない（出来事までの遅れが分からず、± の 1 つの数で表せない）。対応点の
    無い区切りの出来事は書かない（時刻が分からない）。書いた数を返す。"""
    names = trace.names()
    rows = []
    for e in (trace.events if events is None else events):
        t = clock.place(e)
        if t is None:
            continue
        body = {"format": trace.format, **e.to_dict(), "time_source": "target_clock", "time_bound": "upper"}
        if e.task is not None and e.task in names:
            body["task_name"] = names[e.task]
        rows.append((STREAM, e.kind, t, e.task or 0, 0, body, None, None))
    rec.add_many(rows)
    return len(rows)

