"""formats_demo.elf を作る（zig cc で Cortex-M4 向け、デバッグ情報付き。U14・U15 の実機試験用）。試験は生成済みの ELF を使う。

    python host/firmware/formats_demo/build.py
"""

import shutil
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> None:
    zig = shutil.which("zig")
    if zig is None:
        raise SystemExit("zig が見つからない（https://ziglang.org/、winget install zig.zig）")
    subprocess.run([zig, "cc", "-target", "thumb-freestanding-eabi", "-mcpu=cortex_m4", "-g", "-O1",
                    "-ffreestanding", "-nostdlib", "-fno-builtin", "-Wl,-T,link.ld", "-Wl,-e,reset_handler", "-Wl,--no-gc-sections",
                    "-o", "formats_demo.elf", "formats_demo.c"], cwd=HERE, check=True)
    print(HERE / "formats_demo.elf")


if __name__ == "__main__":
    main()
