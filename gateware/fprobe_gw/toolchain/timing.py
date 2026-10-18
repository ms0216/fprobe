"""Gowin のタイミングレポート（impl/pnr/project_tr_content.html）を解析する。"""

import html
import re
from dataclasses import dataclass

__all__ = ["ClockTiming", "TimingReportError", "TimingSummary", "parse_timing_report"]


class TimingReportError(ValueError):
    """レポートの書式が想定外。"""


@dataclass(frozen=True)
class ClockTiming:
    name: str
    constraint_mhz: float | None   # Fmax 表に載らないクロックは None
    fmax_mhz: float | None
    setup_tns: float
    hold_tns: float

    @property
    def met(self) -> bool:
        return self.setup_tns >= 0.0 and self.hold_tns >= 0.0


@dataclass(frozen=True)
class TimingSummary:
    clocks: tuple[ClockTiming, ...]

    @property
    def met(self) -> bool:
        return all(c.met for c in self.clocks)

    def describe(self) -> str:
        def mhz(v):
            return "-" if v is None else f"{v:.3f} MHz"

        return "\n".join(
            f"{'OK' if c.met else 'NG'} {c.name}: 制約 {mhz(c.constraint_mhz)}, "
            f"Fmax {mhz(c.fmax_mhz)}, Setup TNS {c.setup_tns:.3f}, Hold TNS {c.hold_tns:.3f}"
            for c in self.clocks)


def _table_after(report: str, title: str) -> list[list[str]]:
    """見出し `title` の直後にある表のデータ行（<td> を持つ行）をセル文字列で返す。"""
    m = re.search(re.escape(title) + r".*?<table[^>]*>(.*?)</table>", report, re.S)
    if not m:
        raise TimingReportError(f"'{title}' 節が見つからない")
    rows = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(1), re.S):
        cells = [html.unescape(re.sub(r"<[^>]+>", "", c)).strip()
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if cells:
            rows.append(cells)
    return rows


def _mhz(cell: str) -> float:
    m = re.fullmatch(r"([\d.]+)\(MHz\)", cell)
    if not m:
        raise TimingReportError(f"周波数の書式が想定外: {cell!r}")
    return float(m.group(1))


def parse_timing_report(report: str) -> TimingSummary:
    order: list[str] = []
    fmax: dict[str, tuple[float, float]] = {}
    for cells in _table_after(report, "Max Frequency Summary:"):
        if len(cells) < 4:
            raise TimingReportError(f"Fmax 行の書式が想定外: {cells}")
        name = cells[1]
        fmax[name] = (_mhz(cells[2]), _mhz(cells[3]))
        order.append(name)

    tns: dict[tuple[str, str], float] = {}
    for cells in _table_after(report, "Total Negative Slack Summary:"):
        if len(cells) < 3:
            raise TimingReportError(f"TNS 行の書式が想定外: {cells}")
        name, kind, value = cells[:3]
        tns[(name, kind)] = float(value)
        if name not in order:
            order.append(name)

    if not order:
        raise TimingReportError("クロックが 1 つも報告されていない")
    return TimingSummary(tuple(
        ClockTiming(name=n,
                    constraint_mhz=fmax.get(n, (None, None))[0],
                    fmax_mhz=fmax.get(n, (None, None))[1],
                    setup_tns=tns.get((n, "Setup"), 0.0),
                    hold_tns=tns.get((n, "Hold"), 0.0))
        for n in order))
