"""テストポイントの ID の対応表（ID → ファイル・行・関数・種類）と、その JSON（testpoints.json）。

仕様: docs/superpowers/specs/2026-10-17-f7-testpoint-design.md の 2.5 節。
"""

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path

__all__ = ["BLOCK_KINDS", "BRANCH_KINDS", "FORMAT", "FunctionInfo", "KINDS", "MAX_ID", "Probe", "ProbeTable"]

FORMAT = 1
MAX_ID = 0xFFFF
KINDS = ("entry", "exit", "then", "else", "case", "default", "loop_body", "loop_exit", "var")
BRANCH_KINDS = frozenset({"then", "else", "case", "default", "loop_body", "loop_exit"})
BLOCK_KINDS = frozenset(KINDS) - {"var"}          # C0 の対象（ソースにある塊の印。足した枝は除く）


@dataclass(frozen=True)
class Probe:
    id: int
    file: str                       # 挿入の根からの相対パス（/ 区切り）
    line: int
    function: str
    kind: str
    decision: int | None = None     # 分岐の組（その組の最初の ID）
    label: str = ""                 # "case 3"・"while" など人が読む説明
    variable: str | None = None     # kind = "var" の変数
    implicit: bool = False          # ソースに無い枝（足した else・default）

    @property
    def where(self) -> str:
        return f"{self.file}:{self.line}"


@dataclass
class FunctionInfo:
    file: str
    name: str
    index: int                      # 同じファイルの同じ名前の何番目か（#if の枝ごとの定義）
    line: int
    end_line: int
    fingerprint: str
    instrumented: bool
    ids: list[int] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str, int]:
        return self.file, self.name, self.index


@dataclass
class ProbeTable:
    probes: dict[int, Probe] = field(default_factory=dict)
    functions: list[FunctionInfo] = field(default_factory=list)
    next_id: int = 1
    skipped: list[dict] = field(default_factory=list)
    header: str = "fprobe_tp.h"

    def __getitem__(self, probe_id: int) -> Probe:
        return self.probes[probe_id]

    def __contains__(self, probe_id: int) -> bool:
        return probe_id in self.probes

    def __len__(self) -> int:
        return len(self.probes)

    def get(self, probe_id: int) -> Probe | None:
        return self.probes.get(probe_id)

    def find(self, *, function: str | None = None, kind: str | None = None, file: str | None = None,
             line: int | None = None) -> list[Probe]:
        return [p for p in self.probes.values()
                if (function is None or p.function == function) and (kind is None or p.kind == kind)
                and (file is None or p.file == file) and (line is None or p.line == line)]

    def one(self, **query) -> Probe:
        found = self.find(**query)
        if len(found) != 1:
            raise KeyError(f"テストポイントが 1 つに決まらない（{query}: {len(found)} 個）")
        return found[0]

    def function_names(self) -> dict[tuple[str, str], str]:
        """(ファイル, 関数) → 表示の名前。名前が他のファイルと重なるときだけ "ファイル:関数"。"""
        keys = {(p.file, p.function) for p in self.probes.values()}
        count: dict[str, int] = {}
        for _, name in keys:
            count[name] = count.get(name, 0) + 1
        return {(f, n): (n if count[n] == 1 else f"{f}:{n}") for f, n in keys}

    def decisions(self) -> dict[int, list[Probe]]:
        out: dict[int, list[Probe]] = {}
        for p in sorted(self.probes.values(), key=lambda p: p.id):
            if p.decision is not None:
                out.setdefault(p.decision, []).append(p)
        return out

    # --- JSON ---

    def to_dict(self) -> dict:
        return {"format": FORMAT, "next_id": self.next_id, "header": self.header,
                "probes": [asdict(p) for p in sorted(self.probes.values(), key=lambda p: p.id)],
                "functions": [asdict(f) for f in self.functions], "skipped": self.skipped}

    @classmethod
    def from_dict(cls, d: dict) -> "ProbeTable":
        if d.get("format") != FORMAT:
            raise ValueError(f"対応表の形式が違う（{d.get('format')}）")
        return cls({p["id"]: Probe(**p) for p in d["probes"]}, [FunctionInfo(**f) for f in d["functions"]],
                   d["next_id"], list(d.get("skipped", [])), d.get("header", "fprobe_tp.h"))

    def save(self, path: Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=1) + "\n", encoding="utf-8",
                              newline="\n")

    @classmethod
    def load(cls, path: Path) -> "ProbeTable":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    @classmethod
    def of(cls, probes: Iterable[Probe]) -> "ProbeTable":
        """試験・手書きの表。"""
        table = cls({p.id: p for p in probes})
        table.next_id = max(table.probes, default=0) + 1
        return table
