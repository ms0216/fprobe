"""disasm_demo.elf を作る（zig cc で Cortex-M0+ 向け、デバッグ情報付き）。試験は生成済みの ELF を使う。

リンカスクリプトは sram_demo のものを使う（SRAM に置く。実行はしない）。

    python host/firmware/disasm_demo/build.py
"""

import shutil
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
LINK = HERE.parent / "sram_demo" / "link.ld"


def main() -> None:
    zig = shutil.which("zig")
    if zig is None:
        raise SystemExit("zig が見つからない（https://ziglang.org/、winget install zig.zig）")
    subprocess.run([zig, "cc", "-target", "thumb-freestanding-eabi", "-mcpu=cortex_m0plus", "-g", "-O1",
                    "-ffreestanding", "-nostdlib", "-fno-builtin", f"-Wl,-T,{LINK}", "-Wl,-e,reset_handler",
                    "-Wl,--no-gc-sections", "-o", "disasm_demo.elf", "disasm_demo.c"], cwd=HERE, check=True)
    print(HERE / "disasm_demo.elf")


if __name__ == "__main__":
    main()
