"""cxx_demo の ELF を作る（zig c++ で Cortex-M4 向け、デバッグ情報付き）。試験は生成済みの ELF を使う。

C++ の名前（ELF のシンボルはマングルされた名前、DWARF は名前空間・クラスの入れ子と DW_AT_linkage_name）から、
場所を "motor::scale"・"motor::Pid::step" で指定でき、止まった場所が C++ の名前で出ることを確かめる。

    python host/firmware/cxx_demo/build.py
"""

import shutil
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
LINK = HERE.parent / "dwarf_demo" / "link.ld"


def main() -> None:
    zig = shutil.which("zig")
    if zig is None:
        raise SystemExit("zig が見つからない（https://ziglang.org/、winget install zig.zig）")
    subprocess.run([zig, "c++", "-target", "thumb-freestanding-eabihf", "-mcpu=cortex_m4+vfp4d16sp", "-O0",
                    "-gdwarf-5", "-fno-sanitize=all", "-ffreestanding", "-nostdlib", "-fno-exceptions", "-fno-rtti",
                    f"-Wl,-T,{LINK}", "-Wl,-e,reset_handler", "-Wl,--no-gc-sections", "-o", "cxx_demo.elf",
                    "cxx_demo.cpp"], cwd=HERE, check=True)
    print(HERE / "cxx_demo.elf")


if __name__ == "__main__":
    main()
