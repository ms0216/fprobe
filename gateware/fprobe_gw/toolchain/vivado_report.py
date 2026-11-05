"""Vivado のログ・タイミング報告の解析（ファイルを読まない純粋な文字列処理）。

駆動されない配線・効いていない制約の警告の抽出と、report_timing_summary の報告の読み取り。
"""

import re

from .timing import ClockTiming, TimingReportError, TimingSummary

__all__ = ["parse_vivado_timing", "vivado_undriven", "vivado_unmatched_constraints"]


_UNDRIVEN = re.compile(r"^WARNING: \[Synth 8-\d+\].*(?:does not have driver|undriven pin).*$", re.M)


def vivado_undriven(log: str) -> list[str]:
    """駆動されない配線の警告の行（重複を除いて出た順に）。"""
    return list(dict.fromkeys(m.group(0).strip() for m in _UNDRIVEN.finditer(log)))


# 制約の対象が見つからない（12-4739）。Vivado が生成した IP の制約（.gen/ の下。PS7 の PJTAG など使わない端子）は除く
_UNMATCHED = re.compile(r"^CRITICAL WARNING: \[Vivado 12-4739\].*$", re.M)


def vivado_unmatched_constraints(log: str) -> list[str]:
    """自分の制約（モジュールの xdc・トップの xdc）のうち、対象が見つからず効いていないものの警告の行（重複を除いて出た順に）。"""
    lines = (m.group(0).strip() for m in _UNMATCHED.finditer(log))
    return list(dict.fromkeys(line for line in lines if "/.gen/" not in line.replace("\\", "/")))


def _section(report: str, title: str) -> list[str]:
    m = re.search(r"\| " + re.escape(title) + r"\n.*?\n-+\n(.*?)(?:\n-{20,}\n|\Z)", report, re.S)
    if not m:
        raise TimingReportError(f"'{title}' 節が見つからない")
    return [line for line in m.group(1).splitlines() if line.strip()]


def parse_vivado_timing(report: str) -> TimingSummary:
    """report_timing_summary の報告から、クロックごとの制約・Fmax（1000 / (周期 - WNS)）・TNS を読む。
    クロック間の経路（Inter Clock Table）の違反は、全体の要約（Design Timing Summary）で拾う。"""
    periods: dict[str, float] = {}
    for line in _section(report, "Clock Summary")[2:]:
        cells = line.split()
        if len(cells) >= 5:
            periods[cells[0]] = float(cells[-2])
    clocks = []
    for line in _section(report, "Intra Clock Table")[2:]:
        cells = line.split()
        if cells and cells[0] in periods and len(cells) == 5:
            # 経路が無くパルス幅の列だけのクロック（MMCM の入力など）
            clocks.append(ClockTiming(name=cells[0], constraint_mhz=1000.0 / periods[cells[0]], fmax_mhz=None,
                                      setup_tns=0.0, hold_tns=0.0))
            continue
        if len(cells) < 7 or cells[0] not in periods:
            continue
        name, wns, tns, whs_tns = cells[0], cells[1], cells[2], cells[6]
        period = periods[name]
        wns_v = None if wns == "inf" else float(wns)
        clocks.append(ClockTiming(name=name, constraint_mhz=1000.0 / period,
                                  fmax_mhz=None if wns_v is None else 1000.0 / (period - wns_v),
                                  setup_tns=float(tns), hold_tns=float(whs_tns)))
    total = _section(report, "Design Timing Summary")[2].split()
    if float(total[1]) < 0 or float(total[5]) < 0:
        clocks.append(ClockTiming(name="(design)", constraint_mhz=None, fmax_mhz=None,
                                  setup_tns=float(total[1]), hold_tns=float(total[5])))
    if not clocks:
        raise TimingReportError("クロックが 1 つも報告されていない")
    return TimingSummary(tuple(clocks))
