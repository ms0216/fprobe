"""GHDL（VHDL シミュレータ）の所在解決と、cocotb から使うための実行環境。

cocotb の VPI モジュールは GHDL の libghdlvpi.dll・cocotb の DLL・Python の DLL に依存する。
Windows ではこれらが PATH 上に無いと「モジュールが見つからない」で失敗するため、
ここで必要なディレクトリを PATH 先頭に揃える。
"""

import os
import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .gowin import ToolchainNotFound

__all__ = ["DEFAULT_GHDL_HOMES", "ENV_VAR", "GhdlToolchain"]

ENV_VAR = "FPROBE_GHDL_HOME"

DEFAULT_GHDL_HOMES: tuple[Path, ...] = (
    Path.home() / "AppData/Local/Microsoft/WinGet/Packages"
    / "ghdl.ghdl.ucrt64.mcode_Microsoft.Winget.Source_8wekyb3d8bbwe",
)


@dataclass(frozen=True)
class GhdlToolchain:
    home: Path

    @property
    def ghdl(self) -> Path:
        return self.home / "bin" / "ghdl.exe"

    @property
    def vpi_lib_dir(self) -> Path:
        return self.home / "lib"

    def missing_files(self) -> list[Path]:
        required = (self.ghdl, self.vpi_lib_dir / "libghdlvpi.dll")
        return [p for p in required if not p.is_file()]

    def environment(self, base_env: Mapping[str, str] = os.environ) -> dict[str, str]:
        """GHDL + cocotb の実行に必要なディレクトリを PATH 先頭に加えた環境変数。"""
        import cocotb_tools.config

        prepend = [self.home / "bin", self.vpi_lib_dir, Path(cocotb_tools.config.libs_dir),
                   Path(sys.base_prefix)]
        env = dict(base_env)
        rest = env.get("PATH", "")
        env["PATH"] = os.pathsep.join([*map(str, prepend), *([rest] if rest else [])])
        return env

    def to_verilog(self, sources: "list[Path]", top: str, out: Path, generics: Mapping[str, object] = {}) -> Path:
        """VHDL を GHDL の合成で 1 つの Verilog にする（Verilog だけを読むベンダーの道具向け。ForgeFPGA 等）。"""
        import subprocess
        work = out.parent / f"{out.stem}_ghdl"
        work.mkdir(parents=True, exist_ok=True)
        common = ["--std=08", f"--workdir={work}"]
        r = subprocess.run([str(self.ghdl), "-a", *common, *map(str, sources)], capture_output=True, text=True)
        if r.returncode == 0:
            gen = [f"-g{k}={v}" for k, v in generics.items()]
            r = subprocess.run([str(self.ghdl), "synth", *common, *gen, "--out=verilog", top],
                               capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"GHDL の合成に失敗: {r.stderr[-2000:]}")
        out.write_text(r.stdout, encoding="utf-8")
        return out

    @classmethod
    def locate(cls, env: Mapping[str, str] = os.environ) -> "GhdlToolchain":
        candidates: list[Path] = []
        if ENV_VAR in env:
            candidates.append(Path(env[ENV_VAR]))
        else:
            candidates.extend(DEFAULT_GHDL_HOMES)
            on_path = shutil.which("ghdl", path=env.get("PATH", ""))
            if on_path:
                candidates.append(Path(on_path).resolve().parent.parent)
        problems = []
        for home in candidates:
            toolchain = cls(home)
            missing = toolchain.missing_files()
            if not missing:
                return toolchain
            problems.append(f"{home}: " + ", ".join(p.name for p in missing) + " が無い")
        detail = "; ".join(problems) if problems else "候補なし"
        raise ToolchainNotFound(f"GHDL が見つからない（{ENV_VAR} で指定可能）: {detail}")
