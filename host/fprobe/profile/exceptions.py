"""例外トレース（ITM の例外のパケット: 入る・出る・戻る）から、処理中の例外と、割り込みごとの回数・時間。

- `ExceptionState`: 処理中の例外のスタック。PC サンプルに「そのとき処理中の例外」を付けるのにも使う。
- `exception_profile`: 時刻付きのパケット（fprobe.itm.Timestamper）から、割り込みごとの回数・含む時間・自分の時間。

仕様: docs/superpowers/specs/2026-10-17-f5-profile-design.md の 4 節。
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from ..itm import ExceptionAction, ExceptionTrace, Overflow, Packet, TimedPacket
from .stats import Distribution

__all__ = ["Activation", "ExceptionProfile", "ExceptionState", "exception_name", "exception_profile"]

_CORE = {1: "Reset", 2: "NMI", 3: "HardFault", 4: "MemManage", 5: "BusFault", 6: "UsageFault", 7: "SecureFault",
         11: "SVCall", 12: "DebugMonitor", 14: "PendSV", 15: "SysTick"}


def exception_name(number: int, names: Mapping[int, str] | None = None) -> str:
    """0 = Thread、1〜15 = コアの例外、16 以上 = IRQn（n = 番号 - 16）。names で上書きできる。"""
    if names and number in names:
        return names[number]
    if number == 0:
        return "Thread"
    if number >= 16:
        return f"IRQ{number - 16}"
    return _CORE.get(number, f"Exception{number}")


@dataclass(frozen=True)
class Activation:
    """1 回の例外の処理。inclusive = 入ってから出るまで、self_time = その間に横取りした別の例外の時間を引いたもの。"""
    number: int
    start: int
    end: int
    inclusive: int
    self_time: int


@dataclass
class _Frame:
    number: int
    enter: int | None             # 入った時刻（分からなければ None: 回として数えない）
    preempted: int = 0            # 横取りされていた時間


class ExceptionState:
    """処理中の例外のスタック。最初とオーバーフローの後は「どこにいるか分からない」（current = None）。
    戻る（RETURN 0）か入る（ENTER）で分かるようになる。"""

    def __init__(self):
        self._stack: list[_Frame] = []
        self._floor_known = False     # スタックの下がスレッド（True）か、分からない（False）か
        self.inconsistencies = 0      # 例外の並びが合わなかった回数（パケットの欠け）

    @property
    def current(self) -> int | None:
        """処理中の例外番号（0 = スレッド）。分からなければ None。"""
        if self._stack:
            return self._stack[-1].number
        return 0 if self._floor_known else None

    def reset(self) -> None:
        """どこにいるか分からなくなった（オーバーフロー等）。"""
        self._stack.clear()
        self._floor_known = False

    def feed(self, packet: Packet, t: int | None = None) -> list[Activation]:
        """パケットを 1 つ反映する。終わった回（入る・出るの時刻が両方分かるもの）を返す。"""
        if isinstance(packet, Overflow):
            self.reset()
            return []
        if not isinstance(packet, ExceptionTrace):
            return []
        n = packet.number
        if packet.action == ExceptionAction.ENTER:
            self._stack.append(_Frame(n, t))
            return []
        if packet.action == ExceptionAction.EXIT:
            return self._exit(n, t)
        return self._return(n)

    def _exit(self, n: int, t: int | None) -> list[Activation]:
        k = self._find(n)
        if k is None:
            if self._floor_known:
                self.inconsistencies += 1
            self.reset()
            return []
        if k != len(self._stack) - 1:
            self.inconsistencies += 1                     # 上の回の出るが欠けた: その回は数えない
        frame = self._stack[k]
        del self._stack[k:]
        if frame.enter is None or t is None:
            return []
        inclusive = t - frame.enter
        if self._stack:
            self._stack[-1].preempted += inclusive
        return [Activation(n, frame.enter, t, inclusive, inclusive - frame.preempted)]

    def _return(self, n: int) -> list[Activation]:
        if n == 0:
            if self._stack:
                self.inconsistencies += 1
            self._stack.clear()
            self._floor_known = True
            return []
        k = self._find(n)
        if k is None:
            if self._floor_known:
                self.inconsistencies += 1
            self._stack = [_Frame(n, None)]               # n にいることは分かる。下は分からない
            self._floor_known = False
        elif k != len(self._stack) - 1:
            self.inconsistencies += 1
            del self._stack[k + 1:]
        return []

    def _find(self, n: int) -> int | None:
        for k in range(len(self._stack) - 1, -1, -1):
            if self._stack[k].number == n:
                return k
        return None


@dataclass
class ExceptionProfile:
    activations: list[Activation]
    span: int | None                         # 時刻の分かったパケットの最初から最後まで（クロック）
    clock_hz: float | None = None
    overflows: int = 0
    inconsistencies: int = 0
    names: Mapping[int, str] | None = field(default=None, repr=False)

    def exceptions(self) -> list[dict]:
        """割り込みごとの統計（自分の時間の合計の多い順）。"""
        by: dict[int, list[Activation]] = {}
        for a in self.activations:
            by.setdefault(a.number, []).append(a)
        rows = []
        for number, acts in by.items():
            inclusive = Distribution([a.inclusive for a in acts])
            own = Distribution([a.self_time for a in acts])
            longest = max(acts, key=lambda a: a.inclusive)
            total_self = sum(a.self_time for a in acts)
            row = {"exception": number, "name": exception_name(number, self.names), "count": len(acts),
                   "inclusive": inclusive.as_dict(), "self": own.as_dict(),
                   "total_inclusive": sum(a.inclusive for a in acts), "total_self": total_self,
                   "max_at": longest.start, "max_end": longest.end,
                   "load": total_self / self.span if self.span else None}
            if self.clock_hz:
                row["seconds"] = {"inclusive": _scaled(inclusive.as_dict(), self.clock_hz),
                                  "self": _scaled(own.as_dict(), self.clock_hz)}
                row["rate_hz"] = len(acts) / (self.span / self.clock_hz) if self.span else None
            rows.append(row)
        return sorted(rows, key=lambda r: -r["total_self"])

    def as_dict(self) -> dict:
        return {"span": self.span, "clock_hz": self.clock_hz, "overflows": self.overflows,
                "inconsistencies": self.inconsistencies, "exceptions": self.exceptions()}


def _scaled(stats: dict, hz: float) -> dict:
    return {k: (v / hz if k != "count" else v) for k, v in stats.items()}


def exception_profile(packets: Iterable[TimedPacket], clock_hz: float | None = None,
                      names: Mapping[int, str] | None = None) -> ExceptionProfile:
    """時刻付きのパケット（local_time = クロック数）から割り込みごとの時間。時刻の無いパケットは状態だけ反映する。"""
    state = ExceptionState()
    activations: list[Activation] = []
    first = last = None
    overflows = 0
    for tp in packets:
        t = tp.local_time
        if isinstance(tp.packet, Overflow):
            overflows += 1
        if t is not None:
            first = t if first is None else first
            last = t
        activations += state.feed(tp.packet, t)
    span = last - first if first is not None and last > first else None
    return ExceptionProfile(activations, span, clock_hz, overflows, state.inconsistencies, names)
