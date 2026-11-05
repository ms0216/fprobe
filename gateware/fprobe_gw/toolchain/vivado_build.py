"""Vivado で VivadoProject をビルドし、成果物とタイミングを検証する（GowinBuilder と同じ約束）。

- 入力（TCL・XDC・ソース・ブロック設計の TCL）が前回の成功したビルドと同じなら Vivado を省き、前回の成果物を再び検証する。
- 版のパッケージ（BuildStamp）があれば、作り直すときに入力の要約・時刻・git の状態を書いてから Vivado を動かす。
- 駆動されない配線の警告・タイミング未達のビットストリームは残さない。成功の印 inputs.sha256 を隣に置く。
"""

import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from .build_support import (INPUTS_STAMP, BuildError, BuildResult, force_remove, git_state, inputs_digest,
                            is_reusable, publish_files, run_tool, write_build_stamp)
from .timing import TimingReportError
from .vivado import VivadoProject, VivadoToolchain, module_constraints
from .vivado_report import parse_vivado_timing, vivado_undriven, vivado_unmatched_constraints

__all__ = ["VivadoBuilder"]

BUILD_TIMEOUT_S = 40 * 60


class VivadoBuilder:
    def __init__(self, toolchain: VivadoToolchain, *, run=subprocess.run, timeout_s: float = BUILD_TIMEOUT_S,
                 now: Callable[[], float] = time.time,
                 git: Callable[[Path], tuple[str | None, bool]] = git_state):
        self._toolchain = toolchain
        self._run = run
        self.timeout_s = timeout_s
        self._now = now
        self._git = git

    def build(self, project: VivadoProject, build_dir: Path, directive: str = "Default") -> BuildResult:
        build_dir = Path(build_dir)
        bitstream = build_dir / f"{project.name}.bit"
        log = build_dir / "vivado.log"
        tcl = project.prepare(build_dir, directive)
        digest = _inputs_digest(project, build_dir, tcl)
        stamp = build_dir / INPUTS_STAMP
        if not is_reusable(bitstream, build_dir, digest, project.stamp):
            stamp.unlink(missing_ok=True)
            force_remove(bitstream)
            (build_dir / "timing.rpt").unlink(missing_ok=True)
            if project.stamp is not None:
                write_build_stamp(project.stamp, build_dir, digest, self._git, self._now)
            out = run_tool(self._run, [str(self._toolchain.vivado), "-mode", "batch", "-nojournal", "-nolog",
                                       "-notrace", "-source", tcl.name], cwd=build_dir,
                           timeout_s=self.timeout_s, log=log, tool="Vivado")
            undriven = vivado_undriven(out)
            if undriven:
                force_remove(bitstream)
                raise BuildError("駆動されない配線がある（実機では値が定まらない）。すべて明示的に駆動すること"
                                 f"（{log}）:\n  " + "\n  ".join(undriven))
            unmatched = vivado_unmatched_constraints(out)
            if unmatched:
                force_remove(bitstream)
                raise BuildError("対象の見つからない制約がある（効いていない。名前を確かめること）"
                                 f"（{log}）:\n  " + "\n  ".join(unmatched))
            if not bitstream.is_file():
                raise BuildError(f"ビットストリーム {bitstream} が生成されていない（{log}）")
        report = build_dir / "timing.rpt"
        try:
            timing = parse_vivado_timing(report.read_text(encoding="utf-8", errors="replace"))
        except (OSError, TimingReportError) as e:
            force_remove(bitstream)
            raise BuildError(f"タイミングの報告 {report} を解析できない: {e}") from e
        reported = {c.name for c in timing.clocks}
        missing = [c.name for c in project.clocks if c.name not in reported]
        if missing or not timing.met:
            force_remove(bitstream)
            raise BuildError(f"タイミング制約未達（{report}）:\n" + timing.describe() +
                             (f"\n報告に無いクロック: {', '.join(missing)}" if missing else ""))
        stamp.write_text(digest, encoding="utf-8")
        return BuildResult(bitstream=bitstream, log=log, timing=timing)

    def build_best(self, project: VivadoProject, build_dir: Path,
                   directives=("Default", "Explore")) -> BuildResult:
        """配置・配線の directive（place_design と route_design の両方にある名前）を順に試し、タイミングを満たした最初の版を build_dir 直下に写す。
        ponytail: 並列にしない（Vivado は 1 本で数 GB 使う）。満たした最初で止める（最良の比較は要るとき足す）。"""
        build_dir = Path(build_dir)
        errors = []
        for d in directives:
            try:
                result = self.build(project, build_dir / d, d)
            except BuildError as e:
                errors.append(f"{d}: {str(e).splitlines()[0]}")
                continue
            publish_files(result, build_dir)
            return BuildResult(bitstream=build_dir / result.bitstream.name, log=result.log, timing=result.timing)
        raise BuildError("どの directive でもタイミングを満たさない:\n" + "\n".join(errors))


def _inputs_digest(project: VivadoProject, build_dir: Path, tcl: Path) -> str:
    stamped = project.stamp.source if project.stamp is not None else None
    # ブロック設計の TCL は同じ場所の TCL を source しうる（ps7_hp_bd.tcl → ps7_bd.tcl）ので、隣の TCL も入力に含める
    siblings = sorted({t for bd in project.block_designs for t in Path(bd).parent.glob("*.tcl")})
    return inputs_digest([tcl, build_dir / f"{project.name}.xdc", *siblings,
                          *sorted(Path(s) for s in project.sources if s != stamped),
                          *module_constraints(project.sources)])    # モジュールの制約（ソースの隣の .xdc）
