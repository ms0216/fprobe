"""変数の観測の条件つきの取得（U43。STM32CubeMonitor のトリガ CUBE-13、µC/Probe のオシロのプリ/ポストトリガ UCP-03、
PALMiCE のリアルタイムモニタの条件での記録の開始・停止 CTX-01 に相当）。機器に依らない純粋な部品。

観測している変数の値の更新 (時刻, 名前, 値) とクロストリガの出来事 (時刻) を時刻順に受け、
- 待つ（armed）: 直近 pre の間の更新を取っておく（プリトリガ）。始まりの条件が成り立ったら始める。
- 取る（capturing）: 更新をためる。止まりの条件・post の経過・件数の上限のどれかで終える。
- 終わり（done）: 始まりの時刻 − pre から終わりの時刻までの更新が samples に残る。

条件:
- {"variable": 名前, "op": ">"|">="|"<"|"<="|"=="|"!=", "value": 数, "edge": false}: 変数の値の比較。
  edge = true なら「偽から真に変わったとき」（既定は成り立っていればすぐ）。列挙は数の値、真偽は 0 / 1 で比べる。
- {"event": "xtrig"}: クロストリガの割り込み書き込み（FPGA のイベント記録の種類 2）が起きたとき。
  ロジアナのトリガ・DAP の一致・多段の条件（xtrig_set_sequence）を xtrig_route の to_inject につなぐと、その時刻に
  なる（ハードウェアで決まる時刻。ホストのポーリングのゆらぎを含まない）。
"""

import collections
import operator
from dataclasses import dataclass, field

__all__ = ["Capture", "Condition", "OPS", "number_of"]

OPS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le, "==": operator.eq,
       "!=": operator.ne}
EVENTS = ("xtrig",)


def number_of(value: object) -> int | float | None:
    """比べられる数（整数・浮動小数・真偽、列挙・ビットフィールドの {"value": …}）。比べられなければ None。"""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, dict) and isinstance(value.get("value"), (int, float)):
        return value["value"]
    return None


@dataclass(frozen=True)
class Condition:
    variable: str | None = None
    op: str | None = None
    value: float | None = None
    edge: bool = False
    event: str | None = None

    @classmethod
    def parse(cls, spec: dict, names: set[str]) -> "Condition":
        """API の条件の辞書から。names は観測している変数の名前（無い名前は誤り）。"""
        if "event" in spec:
            unknown = set(spec) - {"event"}
            if spec["event"] not in EVENTS or unknown:
                raise ValueError(f"出来事の条件は {{\"event\": \"xtrig\"}}（{spec}）")
            return cls(event=spec["event"])
        unknown = set(spec) - {"variable", "op", "value", "edge"}
        if unknown or not {"variable", "op", "value"} <= set(spec):
            raise ValueError(f"値の条件は {{\"variable\", \"op\", \"value\", \"edge\"}}（{spec}）")
        if spec["variable"] not in names:
            raise ValueError(f"{spec['variable']!r} は観測していない（watch_set の式の名前: {sorted(names)}）")
        if spec["op"] not in OPS:
            raise ValueError(f"op は {' '.join(OPS)}（{spec['op']!r}）")
        if number_of(spec["value"]) is None:
            raise ValueError(f"value は数（{spec['value']!r}）")
        return cls(spec["variable"], spec["op"], number_of(spec["value"]), bool(spec.get("edge", False)))

    def describe(self) -> dict:
        if self.event is not None:
            return {"event": self.event}
        return {"variable": self.variable, "op": self.op, "value": self.value, "edge": self.edge}


@dataclass
class _Tracker:
    """1 つの条件の判定（edge のために前の真偽を覚える）。"""
    condition: Condition
    last: bool | None = None

    def update(self, name: str, value: object) -> bool:
        c = self.condition
        if c.event is not None or name != c.variable:
            return False
        n = number_of(value)
        now = n is not None and OPS[c.op](n, c.value)
        fired = now and (not c.edge or self.last is False)
        self.last = now
        return fired

    def event(self, kind: str) -> bool:
        return self.condition.event == kind


@dataclass
class Capture:
    """条件つきの取得 1 回。時刻はタイムベースのカウント。start = None なら arm した直後から取る。"""
    start: Condition | None
    stop: Condition | None
    pre_ticks: int
    post_ticks: int | None
    max_samples: int
    state: str = "armed"
    t_start: int | None = None
    t_end: int | None = None
    reason: str | None = None                       # 終わった理由: stop / post / max_samples / cancelled
    samples: list[tuple[int, str, object]] = field(default_factory=list)
    dropped: int = 0                                # 上限を超えて捨てた更新
    _pending: collections.deque = field(default_factory=collections.deque)
    _start: _Tracker | None = None
    _stop: _Tracker | None = None

    def __post_init__(self):
        self._start = _Tracker(self.start) if self.start is not None else None
        self._stop = _Tracker(self.stop) if self.stop is not None else None

    @property
    def done(self) -> bool:
        return self.state == "done"

    def feed(self, t: int, name: str, value: object) -> None:
        """変数 name の値の更新。"""
        if self.state == "done":
            return
        if self.state == "armed":
            self._pending.append((t, name, value))
            while self._pending and self._pending[0][0] < t - self.pre_ticks:
                self._pending.popleft()
            if self._start is None or self._start.update(name, value):
                self._begin(t)
            return
        self._take(t, name, value)
        if self._stop is not None and self._stop.update(name, value):
            self._finish(t, "stop")

    def event(self, t: int, kind: str) -> None:
        """クロストリガ等の出来事。"""
        if self.state == "armed" and self._start is not None and self._start.event(kind):
            self._begin(t)
        elif self.state == "capturing" and self._stop is not None and self._stop.event(kind):
            self._finish(t, "stop")

    def tick(self, now: int) -> None:
        """時刻が now まで進んだ（更新が無くても post の経過で終える）。"""
        if self.state == "capturing" and self.post_ticks is not None and now >= self.t_start + self.post_ticks:
            self._finish(self.t_start + self.post_ticks, "post")

    def cancel(self, now: int) -> None:
        if self.state != "done":
            self._finish(now, "cancelled")

    def _begin(self, t: int) -> None:
        self.state, self.t_start = "capturing", t
        for sample in self._pending:
            if sample[0] >= t - self.pre_ticks:
                self._take(*sample)
        self._pending.clear()
        self._stop_tracker_reset()

    def _stop_tracker_reset(self) -> None:
        if self._stop is not None:                  # 止まりの edge は始まった後の変化から
            self._stop.last = None

    def _take(self, t: int, name: str, value: object) -> None:
        if self.post_ticks is not None and t > self.t_start + self.post_ticks:
            self._finish(self.t_start + self.post_ticks, "post")
            return
        if len(self.samples) >= self.max_samples:
            self.dropped += 1
            self._finish(t, "max_samples")
            return
        self.samples.append((t, name, value))

    def _finish(self, t: int, reason: str) -> None:
        if self.state == "done":
            return
        self.state, self.t_end, self.reason = "done", t, reason
        self._pending.clear()
