"""GHDL（VHDL シミュレータ）の所在解決と、cocotb から使うための実行環境。

cocotb の VPI モジュールは GHDL の libghdlvpi・cocotb の共有ライブラリ・Python の DLL に依存する。
Windows ではこれらが PATH 上に無いと「モジュールが見つからない」で失敗するため、
ここで必要なディレクトリを PATH 先頭に揃える。

置き場所（FPROBE_GHDL_HOME・既定の場所・PATH 上の ghdl から決めた GHDL の home）:
- Windows: `<home>/bin/ghdl.exe` と `<home>/lib/libghdlvpi.dll`（winget の配置）。
- Linux: `<home>/bin/ghdl` と `<home>/lib/libghdlvpi.so`（GHDL のリリースの配置）。lib に無ければ、ghdl 自身が答える
  `ghdl --vpi-library-dir` の場所（Debian / Ubuntu のパッケージ: /usr/bin/ghdl は起動役、VPI は /usr/lib/ghdl/<backend>/）。
"""

import os
import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .locate import first_usable

__all__ = ["DEFAULT_GHDL_HOMES", "ENV_VAR", "WINDOWS", "GhdlToolchain", "ghdl_name", "vpi_name"]

ENV_VAR = "FPROBE_GHDL_HOME"

WINDOWS = os.name == "nt"

DEFAULT_GHDL_HOMES: tuple[Path, ...] = (
    Path.home() / "AppData/Local/Microsoft/WinGet/Packages"
    / "ghdl.ghdl.ucrt64.mcode_Microsoft.Winget.Source_8wekyb3d8bbwe",
) if WINDOWS else ()


def ghdl_name() -> str:
    """この OS の GHDL の実行ファイルの名前。"""
    return "ghdl.exe" if WINDOWS else "ghdl"


def vpi_name() -> str:
    """この OS の GHDL の VPI ライブラリの名前。"""
    return "libghdlvpi.dll" if WINDOWS else "libghdlvpi.so"


def _reported_vpi_dir(ghdl: Path) -> Path | None:
    """ghdl が答える VPI ライブラリの場所（`ghdl --vpi-library-dir`）。答えなければ None。"""
    import subprocess
    try:
        r = subprocess.run([str(ghdl), "--vpi-library-dir"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = r.stdout.strip()
    return Path(text) if r.returncode == 0 and text else None


@dataclass(frozen=True)
class GhdlToolchain:
    home: Path
    lib_dir: Path | None = None       # VPI ライブラリの場所（None = <home>/lib）

    @property
    def ghdl(self) -> Path:
        return self.home / "bin" / ghdl_name()

    @property
    def vpi_lib_dir(self) -> Path:
        return self.home / "lib" if self.lib_dir is None else self.lib_dir

    def missing_files(self) -> list[Path]:
        required = (self.ghdl, self.vpi_lib_dir / vpi_name())
        return [p for p in required if not p.is_file()]

    @classmethod
    def at(cls, home: Path) -> "GhdlToolchain":
        """home の GHDL。VPI ライブラリが <home>/lib に無ければ（Linux のパッケージの配置）、ghdl が答える場所を使う。"""
        toolchain = cls(home)
        if WINDOWS or (toolchain.vpi_lib_dir / vpi_name()).is_file() or not toolchain.ghdl.is_file():
            return toolchain
        reported = _reported_vpi_dir(toolchain.ghdl)
        return toolchain if reported is None else cls(home, reported)

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
        return first_usable(candidates, cls.at, cls.missing_files, what="GHDL", env_var=ENV_VAR)
