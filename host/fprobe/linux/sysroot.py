"""ターゲットのファイルを写す PC の場所（sysroot）。"""
from __future__ import annotations

import os
from pathlib import Path


def linux_home() -> Path:
    return Path(os.environ.get("FPROBE_HOME", Path.home() / ".fprobe")) / "linux"


def sysroot_file(target: str, binary: str) -> Path:
    """ターゲットのファイル binary を写す PC の場所（linux_home()/target/sysroot の下）。target・binary の .. やパスの区切りで
    その外（既存のファイルの上書き）に出るなら ValueError。"""
    home = linux_home().resolve()
    root = (home / target / "sysroot").resolve()
    if root.parent.parent != home:
        raise ValueError(f"Linux のターゲットの名前 {target!r} は PC のフォルダの 1 段の名前にならない（.. や区切りを含む）")
    local = (root / binary.lstrip("/")).resolve()
    if local == root or not local.is_relative_to(root):
        raise ValueError(f"binary {binary!r} を写す先が sysroot（{root}）の外に出る")
    return local
