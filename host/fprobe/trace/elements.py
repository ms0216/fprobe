"""命令トレースの共通の要素（プロトコルに依らない）。

各プロトコルの解読器（etmv4 等）はパケットをこの要素に落とす。再構成（reconstruct）はこの要素だけを見る。
どの要素も、元のバイト列の位置 offset と、そのバイト raw を持つ（パケットの一覧・照合のため）。
"""

from dataclasses import asdict, dataclass, field
from typing import ClassVar

__all__ = ["Address", "Async", "Atoms", "Context", "CycleCount", "DecodeError", "Discard", "Element", "Event",
           "ExceptionEntry", "ExceptionReturn", "FunctionReturn", "Ignore", "Overflow", "Timestamp", "TraceInfo",
           "TraceOn", "Truncated", "exception_name"]

_M_EXCEPTIONS = {1: "Reset", 2: "NMI", 3: "HardFault", 4: "MemManage", 5: "BusFault", 6: "UsageFault",
                 7: "SecureFault", 11: "SVCall", 12: "DebugMonitor", 14: "PendSV", 15: "SysTick"}


def exception_name(number: int) -> str:
    """M-profile の例外番号（IPSR と同じ番号）の名前。16 以上は IRQ（番号 - 16）。"""
    if number >= 16:
        return f"IRQ{number - 16}"
    return _M_EXCEPTIONS.get(number, f"exception{number}")


@dataclass(frozen=True, kw_only=True)
class Element:
    kind: ClassVar[str] = "element"
    offset: int                                  # プロトコルのバイト列の中の位置
    raw: bytes = field(repr=False)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["raw"] = self.raw.hex()
        return {"kind": self.kind, **d}


@dataclass(frozen=True, kw_only=True)
class Async(Element):
    """同期（パケットの境目が分かった）。skipped = その前に捨てたバイト数。"""
    kind: ClassVar[str] = "async"
    skipped: int = 0


@dataclass(frozen=True, kw_only=True)
class TraceInfo(Element):
    kind: ClassVar[str] = "trace_info"
    cycle_counting: bool = False
    cc_threshold: int | None = None


@dataclass(frozen=True, kw_only=True)
class TraceOn(Element):
    """トレースの再開（ここまでとの間に途切れがある）。"""
    kind: ClassVar[str] = "trace_on"


@dataclass(frozen=True, kw_only=True)
class Address(Element):
    """命令のアドレス: 同期点・間接分岐の行き先・例外の後の行き先。isa = "thumb" / "arm"。
    form = "long" / "short" / "exact" / "context"（アドレスのパケットの形）。"""
    kind: ClassVar[str] = "address"
    address: int
    isa: str = "thumb"
    form: str = "long"


@dataclass(frozen=True, kw_only=True)
class Context(Element):
    kind: ClassVar[str] = "context"
    unchanged: bool = False                      # 中身の無い Context（前と同じ）
    non_secure: bool | None = None
    exception_level: int | None = None
    vmid: int | None = None
    context_id: int | None = None


@dataclass(frozen=True, kw_only=True)
class Atoms(Element):
    """分岐（waypoint）ごとの成否を古い順に: "E" = 成立（実行した）、"N" = 不成立。"""
    kind: ClassVar[str] = "atoms"
    pattern: str


@dataclass(frozen=True, kw_only=True)
class ExceptionEntry(Element):
    """例外。return_address = 例外から戻る先（その前の命令までを実行した）。分からなければ None。
    この後に来る Address が例外の入口（ハンドラの先頭）。"""
    kind: ClassVar[str] = "exception"
    number: int
    name: str = ""
    return_address: int | None = None
    fault_pending: bool = False
    address_kind: int = 0                        # ETMv4 の E1:E0（戻り先のアドレスの意味。照合用に残す）


@dataclass(frozen=True, kw_only=True)
class ExceptionReturn(Element):
    """例外からの戻り（印。戻り先は分岐の行き先として Address で来る）。"""
    kind: ClassVar[str] = "exception_return"


@dataclass(frozen=True, kw_only=True)
class FunctionReturn(Element):
    kind: ClassVar[str] = "function_return"


@dataclass(frozen=True, kw_only=True)
class Timestamp(Element):
    kind: ClassVar[str] = "timestamp"
    value: int
    cycles: int | None = None


@dataclass(frozen=True, kw_only=True)
class CycleCount(Element):
    """サイクル数（count = None は不明）。"""
    kind: ClassVar[str] = "cycle_count"
    count: int | None


@dataclass(frozen=True, kw_only=True)
class Event(Element):
    kind: ClassVar[str] = "event"
    events: int


@dataclass(frozen=True, kw_only=True)
class Overflow(Element):
    """トレースの装置の中のあふれ（ここで命令の流れが途切れる）。"""
    kind: ClassVar[str] = "overflow"


@dataclass(frozen=True, kw_only=True)
class Discard(Element):
    kind: ClassVar[str] = "discard"


@dataclass(frozen=True, kw_only=True)
class Ignore(Element):
    kind: ClassVar[str] = "ignore"


@dataclass(frozen=True, kw_only=True)
class DecodeError(Element):
    """解けないバイト。解読器は次の同期まで読み飛ばす。"""
    kind: ClassVar[str] = "decode_error"
    message: str


@dataclass(frozen=True, kw_only=True)
class Truncated(Element):
    """バイト列の終わりで切れたパケット。"""
    kind: ClassVar[str] = "truncated"
