"""Vivado（AMD/Xilinx）の場所・プロジェクト記述・TCL / XDC の生成・タイミング報告の解析。

Gowin の流れ（project.py・build.py）と同じ考え: 制約（.xdc）はデザインが使うピンだけからビルド時に生成し、
ビルドは vivado -mode batch で 1 本の TCL を流す。ブロック設計（PS7 等）は、それを作る TCL をプロジェクトに渡し、
メモリ上のプロジェクトで生成して包み（<bd>_wrapper.vhd）をソースに足す。
"""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .gowin import ToolchainNotFound
from .project import BuildStamp, ClockConstraint, Pin, _check_pins, _tcl_path
from .timing import ClockTiming, TimingReportError, TimingSummary

__all__ = ["DEFAULT_VIVADO_HOME", "ENV_VAR", "MmcmSolution", "VivadoProject", "VivadoToolchain", "parse_vivado_timing",
           "render_vivado_tcl", "render_xdc", "vivado_undriven"]

ENV_VAR = "FPROBE_VIVADO_HOME"
DEFAULT_VIVADO_HOME = Path(r"C:\AMDDesignTools\2025.2\Vivado")


@dataclass(frozen=True)
class VivadoToolchain:
    home: Path

    @property
    def vivado(self) -> Path:
        return self.home / "bin" / "vivado.bat"

    @classmethod
    def locate(cls, env: Mapping[str, str] = os.environ) -> "VivadoToolchain":
        toolchain = cls(Path(env[ENV_VAR]) if ENV_VAR in env else DEFAULT_VIVADO_HOME)
        if not toolchain.vivado.is_file():
            raise ToolchainNotFound(f"Vivado が見つからない（{ENV_VAR} で指定可能）: {toolchain.vivado} が無い")
        return toolchain


@dataclass(frozen=True)
class MmcmSolution:
    """7 系の MMCME2 の分周: fvco = fin * mult / divclk、fout = fvco / out_div（UG472。VCO は 600〜1200 MHz、-1 品）。"""
    fin_mhz: float
    mult: float
    divclk: int
    out_div: float

    def __post_init__(self):
        if not 600.0 <= self.vco_mhz <= 1200.0:
            raise ValueError(f"MMCM の VCO {self.vco_mhz} MHz が範囲外（600〜1200）")

    @property
    def vco_mhz(self) -> float:
        return self.fin_mhz * self.mult / self.divclk

    @property
    def fout_hz(self) -> int:
        return round(self.vco_mhz / self.out_div * 1e6)


@dataclass(frozen=True)
class VivadoProject:
    name: str                              # 成果物名（<name>.bit）
    part: str
    top: str
    sources: tuple[Path, ...]              # VHDL-2008 のソース
    pins: tuple[Pin, ...]
    clocks: tuple[ClockConstraint, ...] = ()
    block_designs: tuple[Path, ...] = ()   # ブロック設計を作る TCL（ファイル名の幹 = ブロック設計の名前）
    stamp: BuildStamp | None = None

    def __post_init__(self):
        if self.stamp is not None and self.stamp.source not in self.sources:
            raise ValueError(f"版のパッケージ {self.stamp.source} が sources に無い")

    def build_sources(self, build_dir: Path) -> tuple[Path, ...]:
        if self.stamp is None:
            return self.sources
        return tuple(self.stamp.target(build_dir) if s == self.stamp.source else s for s in self.sources)

    def prepare(self, build_dir: Path, directive: str = "Default") -> Path:
        """ビルドディレクトリに XDC と TCL を書き出し、TCL のパスを返す。"""
        missing = [p for p in (*self.sources, *self.block_designs) if not Path(p).is_file()]
        if missing:
            raise FileNotFoundError(", ".join(map(str, missing)))
        _check_pins(self.pins)
        build_dir = Path(build_dir)
        build_dir.mkdir(parents=True, exist_ok=True)
        (build_dir / f"{self.name}.xdc").write_text(render_xdc(self), encoding="utf-8")
        tcl = build_dir / f"{self.name}.tcl"
        tcl.write_text(render_vivado_tcl(self, build_dir, directive), encoding="utf-8")
        return tcl


_PULL = {"UP": "PULLUP", "DOWN": "PULLDOWN"}


def render_xdc(project: VivadoProject) -> str:
    lines = []
    for pin in project.pins:
        port = f"[get_ports {{{pin.port}}}]"
        lines.append(f"set_property PACKAGE_PIN {pin.location} {port}")
        lines.append(f"set_property IOSTANDARD {pin.io_type} {port}")
        if pin.pull in _PULL:
            lines.append(f"set_property {_PULL[pin.pull]} true {port}")
        if pin.drive is not None:
            lines.append(f"set_property DRIVE {pin.drive} {port}")
    for c in project.clocks:
        if c.kind == "port":
            lines.append(f"create_clock -name {c.name} -period {c.period_ns:.3f} [get_ports {{{c.target}}}]")
        elif c.kind == "pin":
            # MMCM の出力は Vivado が自動で生成クロックにする。その名前だけを付け替える（周期は Vivado が求める）
            lines.append(f"create_generated_clock -name {c.name} [get_pins {{{c.target}}}]")
        else:
            lines.append(f"create_clock -name {c.name} -period {c.period_ns:.3f} [get_nets {{{c.target}}}]")
    # 使わない端子は弱い引き下げ（ターゲットの線を浮かせない。浮いた行の線でキーが入った。2026-10-20）
    lines.append("set_property BITSTREAM.CONFIG.UNUSEDPIN PULLDOWN [current_design]")
    return "\n".join(lines) + "\n"


def render_vivado_tcl(project: VivadoProject, build_dir: Path, directive: str) -> str:
    lines = [f"create_project -in_memory -part {project.part}",
             "set_property target_language VHDL [current_project]"]
    for bd in project.block_designs:
        name = Path(bd).stem
        lines += [f"source {_tcl_path(bd)}",
                  f"set bdf [get_files {name}.bd]",
                  "generate_target all $bdf",
                  "read_vhdl [make_wrapper -files $bdf -top]"]
    lines += [f"read_vhdl -vhdl2008 {_tcl_path(src)}" for src in project.build_sources(build_dir)]
    lines += [f"read_xdc {project.name}.xdc",
              f"synth_design -top {project.top} -part {project.part}",
              "opt_design",
              f"place_design -directive {directive}",
              "phys_opt_design",
              f"route_design -directive {directive}",
              "report_timing_summary -file timing.rpt",
              "report_utilization -file util.rpt",
              "report_io -file io.rpt",
              "write_checkpoint -force routed.dcp",
              f"write_bitstream -force {project.name}.bit"]
    return "\n".join(lines) + "\n"


_UNDRIVEN = re.compile(r"^WARNING: \[Synth 8-\d+\].*(?:does not have driver|undriven pin).*$", re.M)


def vivado_undriven(log: str) -> list[str]:
    """駆動されない配線の警告の行（重複を除いて出た順に）。"""
    return list(dict.fromkeys(m.group(0).strip() for m in _UNDRIVEN.finditer(log)))


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
