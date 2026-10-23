"""ビルドの報告から、階層（モジュール）ごとの資源の使用量を読む（モジュール化の段階 0。仕様
docs/superpowers/specs/2026-10-22-modular-design.md）。

- Gowin: 合成の報告 impl/gwsynthesis/project_syn_rsc.xml（`<Module>`・`<SubModule>` の T_* = 総計（自分の分））
- Vivado: `report_utilization -hierarchical` の表（util_hier.rpt）

どれも測った値をそのまま返す（どこまでを残すか・出すかは使う人が決める）。
"""

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

__all__ = ["ModuleUse", "read_gowin_hierarchy", "read_vivado_hierarchy", "table"]


@dataclass(frozen=True)
class ModuleUse:
    path: str           # "top/core_i/la_i"（インスタンスの名前を / でつなぐ）
    depth: int          # 最上位 = 0
    lut: int            # その下の総計
    ff: int
    alu: int            # Gowin の ALU（Vivado は 0）
    bsram: int          # ブロック RAM（Gowin の BSRAM、Vivado は RAMB36 + RAMB18）


def _total(element: ET.Element, key: str) -> int:
    text = element.get(f"T_{key}")
    return int(text.split("(", 1)[0]) if text else 0


def read_gowin_hierarchy(rsc_xml: Path) -> list[ModuleUse]:
    """Gowin の合成の報告（project_syn_rsc.xml）を、上から順に。"""
    out: list[ModuleUse] = []

    def walk(element: ET.Element, prefix: str, depth: int) -> None:
        path = f"{prefix}/{element.get('name')}" if prefix else element.get("name", "")
        out.append(ModuleUse(path, depth, lut=_total(element, "Lut"), ff=_total(element, "Register"),
                             alu=_total(element, "Alu"), bsram=_total(element, "Bsram")))
        for child in element.findall("SubModule"):
            walk(child, path, depth + 1)

    walk(ET.parse(rsc_xml).getroot(), "", 0)
    return out


_ROW = re.compile(r"^\|( *)(\S.*?)\s*\|")


def read_vivado_hierarchy(rpt: Path) -> list[ModuleUse]:
    """Vivado の階層別の使用量の表を、上から順に（自分の分の行「(top)」等は数えない）。"""
    out: list[ModuleUse] = []
    stack: list[str] = []
    header: list[str] | None = None
    for line in Path(rpt).read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if header is None:
            if cells and cells[0] == "Instance":
                header = cells
            continue
        indent = len(line[1:]) - len(line[1:].lstrip(" ")) - 1
        depth = max(0, indent // 2)
        name = cells[0]
        if name.startswith("("):
            continue
        col = {h: cells[i] for i, h in enumerate(header)}
        number = lambda key: int(float(col.get(key, "0") or 0))                    # noqa: E731
        del stack[depth:]
        stack.append(name)
        out.append(ModuleUse("/".join(stack), depth, lut=number("Total LUTs"), ff=number("FFs"), alu=0,
                             bsram=number("RAMB36") + number("RAMB18")))
    return out


def table(modules: list[ModuleUse], depth: int = 2) -> str:
    """depth までの段の表（LUT・FF・ALU・BSRAM と、最上位の LUT に対する割合）。"""
    top = modules[0].lut or 1
    lines = [f"{'モジュール':40} {'LUT':>8} {'割合':>7} {'FF':>8} {'ALU':>6} {'BSRAM':>6}"]
    for m in modules:
        if m.depth > depth:
            continue
        name = "  " * m.depth + m.path.rsplit("/", 1)[-1]
        lines.append(f"{name:40} {m.lut:>8} {m.lut / top:>7.1%} {m.ff:>8} {m.alu:>6} {m.bsram:>6}")
    return "\n".join(lines)
