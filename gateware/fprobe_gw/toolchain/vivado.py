"""Vivado（AMD/Xilinx）の場所・プロジェクト記述・TCL / XDC の生成。ログ・タイミング報告の解析は vivado_report、
MMCM の分周は pll。

Gowin の流れ（project.py・build.py）と同じ考え: 制約（.xdc）はデザインが使うピンだけからビルド時に生成し、
ビルドは vivado -mode batch で 1 本の TCL を流す。ブロック設計（PS7 等）は、それを作る TCL をプロジェクトに渡し、
メモリ上のプロジェクトで生成して包み（<bd>_wrapper.vhd）をソースに足す。
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .locate import ToolchainNotFound
from .pll import MmcmSolution
from .project import BuildStamp, ClockConstraint, Pin, check_pins, tcl_path
from .vivado_report import parse_vivado_timing, vivado_undriven, vivado_unmatched_constraints

# MmcmSolution・解析の関数は従来の import 先として再公開する
__all__ = ["DEFAULT_VIVADO_HOME", "ENV_VAR", "MmcmSolution", "VivadoProject", "VivadoToolchain", "module_constraints",
           "parse_vivado_timing", "render_vivado_tcl", "render_xdc", "vivado_undriven", "vivado_unmatched_constraints"]

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
        check_pins(self.pins)
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
        if pin.slew is not None:
            lines.append(f"set_property SLEW {pin.slew} {port}")
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


def module_constraints(sources) -> tuple[Path, ...]:
    """ソースの隣にあるモジュールの制約（<ソースの幹>.xdc）。"""
    return tuple(x for x in (Path(s).with_suffix(".xdc") for s in sources) if x.is_file())


def render_vivado_tcl(project: VivadoProject, build_dir: Path, directive: str) -> str:
    lines = [f"create_project -in_memory -part {project.part}",
             "set_property target_language VHDL [current_project]"]
    for bd in project.block_designs:
        name = Path(bd).stem
        lines += [f"source {tcl_path(bd)}",
                  f"set bdf [get_files {name}.bd]",
                  "generate_target all $bdf",
                  "read_vhdl [make_wrapper -files $bdf -top]"]
    lines += [f"read_vhdl -vhdl2008 {tcl_path(src)}" for src in project.build_sources(build_dir)]
    # モジュールの制約: ソースの隣の <entity>.xdc（ファイル名の幹 = entity 名）を、そのモジュールの全部の実体に効かせる（UG903 の -ref）。
    # -unmanaged: Tcl として読む（generic で FF が無い実体を if で飛ばし、当たらない制約を正当な場合でも出さない）
    lines += [f"read_xdc -unmanaged -ref {xdc.stem} {tcl_path(xdc)}" for xdc in module_constraints(project.sources)]
    lines += [f"read_xdc {project.name}.xdc",
              f"synth_design -top {project.top} -part {project.part}",
              "opt_design",
              f"place_design -directive {directive}",
              "phys_opt_design -directive AggressiveExplore",
              f"route_design -directive {directive}",
              "phys_opt_design -directive AggressiveExplore",     # 配線の後: 配線で分かった遅い経路を複製・再配置で直す
              "report_timing_summary -file timing.rpt",
              "report_utilization -file util.rpt",
              "report_utilization -hierarchical -file util_hier.rpt",   # モジュールごと（fprobe_gw resources）
              "report_io -file io.rpt",
              "write_checkpoint -force routed.dcp",
              f"write_bitstream -force {project.name}.bit"]
    return "\n".join(lines) + "\n"
