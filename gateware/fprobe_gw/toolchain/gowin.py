"""Gowin EDA のインストール場所を解決する。"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

__all__ = ["DEFAULT_GOWIN_HOMES", "ENV_VAR", "GowinToolchain", "ToolchainNotFound"]

ENV_VAR = "FPROBE_GOWIN_HOME"

DEFAULT_GOWIN_HOMES: tuple[Path, ...] = (
    Path(r"C:\Gowin\Gowin_V1.9.11.03_Education_x64"),
)


class ToolchainNotFound(RuntimeError):
    """必要な Gowin ツールが見つからない。"""


@dataclass(frozen=True)
class GowinToolchain:
    home: Path

    @property
    def gw_sh(self) -> Path:
        return self.home / "IDE" / "bin" / "gw_sh.exe"

    @property
    def programmer_cli(self) -> Path:
        return self.home / "Programmer" / "bin" / "programmer_cli.exe"

    def missing_tools(self) -> list[Path]:
        return [p for p in (self.gw_sh, self.programmer_cli) if not p.is_file()]

    @classmethod
    def locate(cls, env: Mapping[str, str] = os.environ) -> "GowinToolchain":
        # 既定値はモジュール属性を実行時に参照する（テストや利用側で差し替え可能にするため）
        candidates = (Path(env[ENV_VAR]),) if ENV_VAR in env else DEFAULT_GOWIN_HOMES
        problems = []
        for home in candidates:
            toolchain = cls(home)
            missing = toolchain.missing_tools()
            if not missing:
                return toolchain
            problems.append(f"{home}: " + ", ".join(p.name for p in missing) + " が無い")
        raise ToolchainNotFound(
            f"Gowin EDA が見つからない（{ENV_VAR} で指定可能）: " + "; ".join(problems))
