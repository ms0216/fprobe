"""PC サンプリングのプロファイル: サンプルを関数・行・命令・例外に割り当て、サンプル数と割合を数える。

    profiler = Profiler(Symbols("app.elf"), Disassembler.from_elf("app.elf"))
    prof = profiler.profile(PcSamples.from_itm(packets))
    prof.top(10, by="function")            # 上位 10 関数（サンプル数・割合）
    prof.as_dict()                         # JSON にできる形

割合は「自分の時間」（その関数の命令にいた時間）。呼び出し先の時間は含まない（PC サンプリングでは分からない）。
仕様: docs/superpowers/specs/2026-10-17-f5-profile-design.md の 3 節。
割合の信頼区間は付けない: 独立な二項の試行という仮定の上の推定で、周期の抽出では成り立たない（点検 3 回目の H1。
使う側が仮定を承知で付けるなら plugins.heuristics.sampling_intervals）。
"""

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from ..memview import MemoryReadError
from ..symbols import Location, Symbol
from .exceptions import exception_name
from .samples import PcSamples

__all__ = ["CATEGORIES", "Profile", "Profiler", "SymbolSource"]

CATEGORIES = ("code", "sleep", "wfi", "unknown")   # sleep = コアが送った眠りのサンプル、wfi = PC が WFI / WFE の上
_SLEEP_MNEMONICS = ("wfi", "wfe")
_BY = ("function", "line", "instruction", "exception")


class SymbolSource(Protocol):
    """番地 → 関数・ファイル:行（fprobe.symbols.Symbols が満たす）。"""
    def describe(self, address: int) -> Location: ...
    def symbol(self, name: str) -> Symbol: ...


@dataclass
class _Function:
    name: str
    address: int
    size: int
    instructions: dict[int, object] = field(default_factory=dict)    # 番地 → disasm.Instruction（逆アセンブラがあれば）
    wfi: frozenset[int] = frozenset()                                # WFI / WFE の番地


class Profiler:
    def __init__(self, symbols: SymbolSource, disassembler=None):
        """disassembler（fprobe.disasm.Disassembler）があれば、命令の文字列と WFI / WFE の場所を使う。PC が WFI / WFE の
        上のサンプルは wfi に数える（眠っていたとはみなさない。直後の命令に当たったサンプルも code のまま）。"""
        self.symbols = symbols
        self.disassembler = disassembler
        self._functions: dict[str, _Function | None] = {}

    def function_at(self, location: Location) -> _Function | None:
        if location.function is None:
            return None
        name = location.function
        if name not in self._functions:
            self._functions[name] = self._load(name, location.address - location.offset)
        return self._functions[name]

    def _load(self, name: str, address: int) -> _Function:
        try:
            size = self.symbols.symbol(name).size
        except KeyError:
            size = 0
        f = _Function(name, address, size)
        if self.disassembler is None or size <= 0:
            return f
        try:
            listing = self.disassembler.range(address, address + size)
        except (MemoryReadError, ValueError):                # 中身が読めない（ROM の外など）: 命令の情報なしで続ける
            return f
        f.instructions = {i.address: i for i in listing.instructions}
        f.wfi = frozenset(i.address for i in listing.instructions
                          if i.kind == "code" and i.mnemonic in _SLEEP_MNEMONICS)
        return f

    def profile(self, samples: PcSamples, *, names: Mapping[int, str] | None = None) -> "Profile":
        prof = Profile(source=samples.source, unavailable=samples.unavailable,
                       overflows=samples.overflows, exception_trace=samples.exception_trace, names=names)
        places: dict[int, tuple[Location, _Function | None]] = {}     # 番地 → 割り当て（同じ番地は 1 回だけ引く）
        for s in samples.samples:
            prof.total += 1
            if s.exception is not None:
                prof.exceptions_counts[s.exception] += 1
            if s.pc is None:
                prof.categories["sleep"] += 1
                continue
            place = places.get(s.pc)
            if place is None:
                where = self.symbols.describe(s.pc)
                place = places[s.pc] = (where, self.function_at(where))
            where, f = place
            if f is None:
                prof.categories["unknown"] += 1
                prof.unknown_pcs[s.pc] += 1
                continue
            if s.pc in f.wfi:
                prof.categories["wfi"] += 1
                prof.wfi_pcs[s.pc] += 1
                continue
            prof.categories["code"] += 1
            prof.function_counts[f.name] += 1
            prof.function_meta[f.name] = (f.address, f.size)
            prof.line_counts[(f.name, where.file, where.line)] += 1
            prof.instruction_counts[s.pc] += 1
            if s.pc not in prof.instruction_meta:
                insn = f.instructions.get(s.pc)
                text = f"{insn.mnemonic} {insn.operands}".strip() if insn is not None else None
                prof.instruction_meta[s.pc] = (f.name, where.offset, where.file, where.line, text)
        return prof


@dataclass
class Profile:
    source: str
    unavailable: int = 0
    overflows: int = 0
    exception_trace: bool = False
    names: Mapping[int, str] | None = None
    total: int = 0
    categories: Counter = field(default_factory=lambda: Counter({c: 0 for c in CATEGORIES}))
    function_counts: Counter = field(default_factory=Counter)
    function_meta: dict[str, tuple[int, int]] = field(default_factory=dict)
    line_counts: Counter = field(default_factory=Counter)             # (関数, ファイル, 行) → 数
    instruction_counts: Counter = field(default_factory=Counter)      # 番地 → 数
    instruction_meta: dict[int, tuple] = field(default_factory=dict)  # 番地 → (関数, オフセット, ファイル, 行, 命令)
    exceptions_counts: Counter = field(default_factory=Counter)       # 例外番号 → 数（例外トレースがあるとき）
    unknown_pcs: Counter = field(default_factory=Counter)
    wfi_pcs: Counter = field(default_factory=Counter)                  # WFI / WFE の番地 → 数

    def hit_addresses(self) -> set[int]:
        """サンプルが当たった命令の番地（WFI / WFE を含む。関数の外は含まない）。カバレッジに使う。"""
        return set(self.instruction_counts) | set(self.wfi_pcs)

    # --- 割合 ---------------------------------------------------------------------

    @property
    def active(self) -> int:
        """眠り以外のサンプル数。"""
        return self.total - self.categories["sleep"]

    def _stat(self, n: int, total: int | None = None) -> dict:
        total = self.total if total is None else total
        return {"samples": n, "fraction": n / total if total else 0.0}

    def _active(self, n: int) -> dict:
        return {"active_fraction": n / self.active if self.active else 0.0}

    # --- 表 -----------------------------------------------------------------------

    def functions(self) -> list[dict]:
        rows = [{"function": name, "address": self.function_meta[name][0], "size": self.function_meta[name][1],
                 **self._stat(n), **self._active(n)} for name, n in self.function_counts.items()]
        return sorted(rows, key=lambda r: (-r["samples"], r["address"]))

    def lines(self, function: str | None = None) -> list[dict]:
        rows = [{"function": f, "file": file, "line": line, **self._stat(n), **self._active(n)}
                for (f, file, line), n in self.line_counts.items() if function is None or f == function]
        return sorted(rows, key=lambda r: (-r["samples"], r["file"] or "", r["line"] or 0))

    def instructions(self, function: str | None = None) -> list[dict]:
        rows = []
        for address, n in self.instruction_counts.items():
            f, offset, file, line, text = self.instruction_meta[address]
            if function is None or f == function:
                rows.append({"address": address, "function": f, "offset": offset, "file": file, "line": line,
                             "text": text, **self._stat(n), **self._active(n)})
        return sorted(rows, key=lambda r: (-r["samples"], r["address"]))

    def exceptions(self) -> list[dict]:
        """例外ごとのサンプル（例外トレースがあるとき）。割合の分母は処理中の例外が分かったサンプル。"""
        known = sum(self.exceptions_counts.values())
        rows = [{"exception": n, "name": exception_name(n, self.names), **self._stat(c, known)}
                for n, c in self.exceptions_counts.items()]
        return sorted(rows, key=lambda r: (-r["samples"], r["exception"]))

    def category_table(self) -> dict:
        return {c: self._stat(self.categories[c]) for c in CATEGORIES}

    def top(self, n: int = 20, by: str = "function", function: str | None = None) -> list[dict]:
        """by の表のサンプルの多い順の上位 n（line / instruction は function で絞れる）。"""
        return self.table(by, function)[:n]

    def table(self, by: str = "function", function: str | None = None) -> list[dict]:
        """by の表の全部（サンプルの多い順。line / instruction は function で絞れる）。"""
        if by not in _BY:
            raise ValueError(f"by は {', '.join(_BY)} のどれか: {by!r}")
        if by == "function":
            rows = self.functions()
        elif by == "line":
            rows = self.lines(function)
        elif by == "instruction":
            rows = self.instructions(function)
        else:
            rows = self.exceptions()
        return rows

    def notes(self) -> list[str]:
        out = ["割合はサンプルの回数の割合（自分の時間。呼び出し先の時間は含まない）。サンプルの周期と同期した処理は"
               "実際の時間の割合から偏りうる（エイリアシング）。"]
        if self.overflows:
            out.append(f"SWO のオーバーフローが {self.overflows} 回: サンプルが欠け、パケットの多い区間が少なく見える。")
        if self.unavailable:
            out.append(f"DWT_PCSR が 0xFFFFFFFF（止まっていた・サンプルできない）の {self.unavailable} 回は数えていない。")
        if self.source == "pcsr":
            out.append("DWT_PCSR は DAP の命令の境界で読むので、周期がゆらぐ。眠りのサンプルは無い（PC が WFI / WFE の上の"
                       "サンプルは wfi に数える。眠っていたとはみなさない）。")
        if not self.exception_trace:
            out.append("例外トレースが無いので、割り込みの中かどうかは関数の名前でしか分からない。")
        return out

    def as_dict(self, top: int | None = None) -> dict:
        def cut(rows):
            return rows if top is None else rows[:top]
        return {"source": self.source, "total": self.total, "active": self.active, "unavailable": self.unavailable,
                "overflows": self.overflows, "categories": self.category_table(),
                "functions": cut(self.functions()), "lines": cut(self.lines()),
                "instructions": cut(self.instructions()), "exceptions": cut(self.exceptions()),
                "notes": self.notes()}
