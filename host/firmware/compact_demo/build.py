"""compact_demo.elf を作る（zig cc で Cortex-M4 向け。番地のログ・Memfault の compact log の書式を持つ）。試験は生成済みの ELF を使う。

    python host/firmware/compact_demo/build.py
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
                    "-o", "compact_demo.elf", "compact_demo.c"], cwd=HERE, check=True)
    print(HERE / "compact_demo.elf")


if __name__ == "__main__":
    main()
