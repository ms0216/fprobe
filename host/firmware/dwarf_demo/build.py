"""dwarf_demo の ELF を作る（zig cc で Cortex-M4 向け、デバッグ情報付き）。試験は生成済みの ELF を使う。

最適化と DWARF の版の違い（-O0: 変数はスタック上 / -O1: レジスタ・ロケーションリスト。DWARF 5: .debug_loclists /
DWARF 4: .debug_loc）で、型・位置式・CFI の読み方を確かめる。

    python host/firmware/dwarf_demo/build.py
"""

import shutil
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
VARIANTS = {                                   # ELF の名前 → 最適化と DWARF の版
    "dwarf_demo_O0.elf": ["-O0", "-gdwarf-5"],
    "dwarf_demo_O1.elf": ["-O1", "-gdwarf-5"],
    "dwarf_demo_O1_dwarf4.elf": ["-O1", "-gdwarf-4"],
}


def main() -> None:
    zig = shutil.which("zig")
    if zig is None:
        raise SystemExit("zig が見つからない（https://ziglang.org/、winget install zig.zig）")
    for name, flags in VARIANTS.items():
        subprocess.run([zig, "cc", "-target", "thumb-freestanding-eabihf", "-mcpu=cortex_m4+vfp4d16sp", *flags, "-fno-sanitize=all",
                        "-ffreestanding", "-nostdlib", "-fno-builtin", "-Wl,-T,link.ld", "-Wl,-e,reset_handler",
                        "-Wl,--no-gc-sections", "-o", name, "dwarf_demo.c"], cwd=HERE, check=True)
        print(HERE / name)


if __name__ == "__main__":
    main()
