"""Zephyr のアプリのビルド（west build）。ターゲットのファームウェア（例: Shrike-Lite の C のデモ）用。

この PC では、別の作業（ZMK）のために入った Zephyr の作業場所・Zephyr SDK・west をそのまま使う。作業場所は
書き換えない（ビルドの出力は呼び手が渡す out の下だけ）。場所は環境変数で差し替えられる（ほかの PC・CI）:
FPROBE_ZEPHYR_WS（zephyr/ と modules/ のある作業場所）・FPROBE_ZEPHYR_SDK（Zephyr SDK）・FPROBE_WEST（west の実行ファイル）。
"""

import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .gowin import ToolchainNotFound

_HOME = Path.home() / "zmk"
DEFAULTS = {"FPROBE_ZEPHYR_WS": _HOME / "ws", "FPROBE_ZEPHYR_SDK": _HOME / "sdk" / "zephyr-sdk-0.17.0",
            "FPROBE_WEST": _HOME / "zmkenv" / "Scripts" / "west.exe"}


@dataclass(frozen=True)
class ZephyrToolchain:
    ws: Path
    sdk: Path
    west: Path

    @classmethod
    def locate(cls, env: Mapping[str, str] = os.environ) -> "ZephyrToolchain":
        ws, sdk, west = (Path(env[k]) if k in env else v for k, v in DEFAULTS.items())
        tc = cls(ws, sdk, west)
        if (ws / "zephyr" / "VERSION").is_file() and sdk.is_dir() and west.is_file():
            return tc
        raise ToolchainNotFound("Zephyr（作業場所・SDK・west）が見つからない（" + "・".join(DEFAULTS) + " で指定可能）: "
                                f"{ws}, {sdk}, {west}")

    def version(self) -> str:
        """作業場所の Zephyr の版（例 "4.1.0"）。"""
        text = (self.ws / "zephyr" / "VERSION").read_text(encoding="utf-8")
        part = {k: v for k, v in re.findall(r"^(\w+)\s*=\s*(\S*)", text, re.M)}
        return f"{part['VERSION_MAJOR']}.{part['VERSION_MINOR']}.{part['PATCHLEVEL']}"

    def build(self, app: Path, board: str, out: Path, extra: Sequence[str] = ()) -> Path:
        """app を board 向けに out へ作り、out/zephyr/zephyr.elf を返す（-p auto: 設定が変われば作り直す）。"""
        app, out = Path(app).resolve(), Path(out).resolve()   # west は作業場所で走る: 相対だとその中にできる
        env = {**os.environ, "ZEPHYR_SDK_INSTALL_DIR": str(self.sdk), "ZEPHYR_TOOLCHAIN_VARIANT": "zephyr",
               "ZEPHYR_BASE": str(self.ws / "zephyr"), "PYTHONUTF8": "1"}   # version() が読む Zephyr と同じもの
        args = [str(self.west), "build", "-p", "auto", "-s", str(app), "-d", str(out), "-b", board]
        if extra:
            args += ["--", *extra]
        r = subprocess.run(args, cwd=self.ws, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            raise RuntimeError(f"west build が失敗した（{r.returncode}）:\n" + (r.stdout + r.stderr)[-4000:])
        return out / "zephyr" / "zephyr.elf"
