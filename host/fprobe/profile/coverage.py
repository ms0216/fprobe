"""カバレッジ（近似）: PC サンプルが当たった命令・行から、関数ごとの「実行された行の割合」。

サンプリングによる近似: 当たった命令は実行された確かな証拠だが、当たらなかった命令が実行されなかったとは限らない
（短い時間しか使わない行はサンプルされにくい）。正確な C0 には命令トレース（ETM）かテストポイントが要る。

    cov = coverage(profile, Disassembler.from_elf("app.elf"), all_functions=True)
    cov.as_dict()

仕様: docs/superpowers/specs/2026-10-17-f5-profile-design.md の 6 節。
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .pcprofile import Profile
from .samples import PcSamples

__all__ = ["Coverage", "FunctionCoverage", "coverage", "elf_functions"]

NOTE = ("PC サンプリングによる近似: 当たった行は実行されたが、当たらなかった行が実行されなかったとは限らない"
        "（短い時間しか使わない行はサンプルされにくい）。正確な値には命令トレース（ETM）かテストポイントが要る。")


@dataclass(frozen=True)
class FunctionCoverage:
    function: str
    address: int
    size: int
    instructions: int
    instructions_hit: int
    lines: tuple[tuple[str, int], ...]              # この関数の命令がある (ファイル, 行)
    hit_lines: tuple[tuple[str, int], ...]

    @property
    def line_fraction(self) -> float:
        return len(self.hit_lines) / len(self.lines) if self.lines else 0.0

    @property
    def instruction_fraction(self) -> float:
        return self.instructions_hit / self.instructions if self.instructions else 0.0

    def as_dict(self) -> dict:
        hit = set(self.hit_lines)
        return {"function": self.function, "address": self.address, "size": self.size,
                "instructions": self.instructions, "instructions_hit": self.instructions_hit,
                "instruction_fraction": self.instruction_fraction, "lines": len(self.lines),
                "lines_hit": len(self.hit_lines), "line_fraction": self.line_fraction,
                "hit_lines": [list(x) for x in self.hit_lines],
                "missed_lines": [list(x) for x in self.lines if x not in hit]}


@dataclass
class Coverage:
    functions: list[FunctionCoverage]
    samples: int                                    # 当たった命令の番地の数
    notes: list[str] = field(default_factory=lambda: [NOTE])

    def function(self, name: str) -> FunctionCoverage:
        for f in self.functions:
            if f.function == name:
                return f
        raise KeyError(f"関数 {name!r} の結果が無い")

    def as_dict(self) -> dict:
        lines = sum(len(f.lines) for f in self.functions)
        hit = sum(len(f.hit_lines) for f in self.functions)
        return {"approximate": True, "notes": self.notes, "hit_addresses": self.samples,
                "lines": lines, "lines_hit": hit, "line_fraction": hit / lines if lines else 0.0,
                "functions": [f.as_dict() for f in self.functions]}


def elf_functions(path: str | Path) -> list[str]:
    """ELF の関数（STT_FUNC、大きさが 0 でないもの）の名前を番地の順に。"""
    from elftools.elf.elffile import ELFFile
    from elftools.elf.sections import SymbolTableSection
    found: dict[str, int] = {}
    with open(path, "rb") as f:
        for section in ELFFile(f).iter_sections():
            if isinstance(section, SymbolTableSection):
                for s in section.iter_symbols():
                    if s["st_info"]["type"] == "STT_FUNC" and s.name and s["st_size"] > 0:
                        found.setdefault(s.name, s["st_value"] & ~1)
    return sorted(found, key=lambda name: (found[name], name))


def _hits(source: Profile | PcSamples | Iterable[int]) -> set[int]:
    if isinstance(source, Profile):
        return source.hit_addresses()
    if isinstance(source, PcSamples):
        return {s.pc for s in source.samples if s.pc is not None}
    return {pc & ~1 for pc in source}


def coverage(source: Profile | PcSamples | Iterable[int], disassembler, *, functions: Iterable[str] | None = None,
             all_functions: bool = False) -> Coverage:
    """disassembler（fprobe.disasm.Disassembler、ELF のシンボル付き）で関数の命令の範囲を求めて数える。
    functions を省くと、サンプルが当たった関数（all_functions=True なら ELF の全関数。当たらない関数は 0%）。"""
    symbols = disassembler.symbols
    if symbols is None:
        raise ValueError("カバレッジには ELF のシンボルが要る（Disassembler.from_elf）")
    hits = _hits(source)
    if functions is not None:
        names = list(functions)
    elif all_functions:
        names = elf_functions(symbols.path)
    else:
        names = sorted({w.function for pc in hits if (w := symbols.describe(pc)).function is not None},
                       key=lambda name: symbols.symbol(name).address)
    rows = []
    for name in names:
        listing = disassembler.function(name)
        code = [i for i in listing.instructions if i.kind == "code"]
        lines = sorted({(i.file, i.line) for i in code if i.file is not None and i.line is not None})
        hit_code = [i for i in code if i.address in hits]
        hit_lines = sorted({(i.file, i.line) for i in hit_code if i.file is not None and i.line is not None})
        sym = symbols.symbol(name)
        rows.append(FunctionCoverage(name, sym.address, sym.size, len(code), len(hit_code), tuple(lines),
                                     tuple(hit_lines)))
    return Coverage(rows, len(hits))
