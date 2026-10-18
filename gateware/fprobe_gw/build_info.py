"""ビルドの版（U49）を VHDL に入れるパッケージ build_info_pkg。

ケイパビリティ ROM の TLV BUILD の中身（WORDS 語）は、生成したケイパビリティのパッケージが build_info_pkg の
BUILD_INFO を参照する。リポジトリには版なし（全部 0。シミュレーション用）の既定を置き（`python -m fprobe_gw gen`）、
ビルドでは GowinBuilder がその回の版を書いたものに差し替える（toolchain.project.BuildStamp）。

語の並び（ホストの fprobe.caps が読む）: 0 = ビルドした時刻（UNIX 秒）、1・2 = 入力の要約（inputs.sha256）の先頭
64 ビットの上位・下位、3 = git のコミットの先頭 32 ビット、4 = 旗（BUILD_FLAG_*）。時刻 0 は版なし。
"""

from .rtl import CORE_GENERATED
from .toolchain.project import BuildInfo, BuildStamp

__all__ = ["BUILD_FLAG_DIRTY", "BUILD_FLAG_GIT", "DEFAULT_PACKAGE", "WORDS", "render_package", "stamp", "words"]

WORDS = 5
BUILD_FLAG_GIT = 1 << 0          # git のコミットが取れた
BUILD_FLAG_DIRTY = 1 << 1        # ソースのディレクトリにコミットしていない変更があった
DEFAULT_PACKAGE = CORE_GENERATED / "build_info_pkg.vhd"


def words(info: BuildInfo | None) -> list[int]:
    if info is None:
        return [0] * WORDS
    ident = int(info.digest[:16], 16)
    commit = int(info.git[:8], 16) if info.git else 0
    flags = BUILD_FLAG_GIT * (info.git is not None) | BUILD_FLAG_DIRTY * info.dirty
    return [info.time & 0xFFFFFFFF, ident >> 32, ident & 0xFFFFFFFF, commit, flags]


def render_package(info: BuildInfo | None) -> str:
    rows = ",\n".join(f'    x"{w:08X}"' for w in words(info))
    what = ("版なし（リポジトリの既定。ビルドでは GowinBuilder が差し替える）" if info is None else
            f"digest={info.digest[:16]} time={info.time} git={info.git} dirty={info.dirty}")
    return "\n".join([
        "-- 自動生成ファイル。直接編集しないこと（python -m fprobe_gw gen / fprobe_gw build が書く）。",
        "-- 生成元: gateware/fprobe_gw/build_info.py",
        f"-- {what}",
        "library ieee;",
        "use ieee.std_logic_1164.all;",
        "use work.csr_bus_pkg.all;",
        "",
        "package build_info_pkg is",
        f"  constant BUILD_INFO : csr_word_array_t(0 to {WORDS - 1}) := (",
        rows,
        "  );",
        "end package;",
        "",
    ])


def stamp(repo) -> BuildStamp:
    """fprobe_core を含むデザインの版のパッケージ（repo は git の状態を調べるディレクトリ）。"""
    return BuildStamp(source=DEFAULT_PACKAGE, render=render_package, repo=repo)
