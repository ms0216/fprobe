"""zephyr_shape.elf を作る（zig cc で Cortex-M4 向け、デバッグ情報付き）。試験は生成済みの ELF を使う。

Zephyr と同じ名前・形の構造体を持つ試験用の ELF（Zephyr そのものではない）。配置は sram_demo と同じ（link.ld を共有）。

    python host/firmware/zephyr_shape/build.py
"""

import shutil
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> None:
    zig = shutil.which("zig")
    if zig is None:
        raise SystemExit("zig が見つからない（https://ziglang.org/、winget install zig.zig）")
    subprocess.run([zig, "cc", "-target", "thumb-freestanding-eabi", "-mcpu=cortex_m4", "-O1", "-gdwarf-5",
                    "-fno-sanitize=all", "-ffreestanding", "-nostdlib", "-fno-builtin", "-Wl,-T,../sram_demo/link.ld",
                    "-Wl,-e,reset_handler", "-Wl,--no-gc-sections", "-o", "zephyr_shape.elf", "zephyr_shape.c"],
                   cwd=HERE, check=True)
    print(HERE / "zephyr_shape.elf")


if __name__ == "__main__":
    main()
