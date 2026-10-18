"""stack_demo.elf と stack_demo.su を作る（zig clang で Cortex-M4 向け、デバッグ情報付き、-fstack-usage）。
試験は生成済みの ELF と .su を使う。

    python host/firmware/stack_demo/build.py
"""

import shutil
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> None:
    zig = shutil.which("zig")
    if zig is None:
        raise SystemExit("zig が見つからない（https://ziglang.org/、winget install zig.zig）")
    # zig cc は -fstack-usage の .su をキャッシュの中に書くので、コンパイルは zig clang で（.su は .o の横）
    subprocess.run([zig, "clang", "--target=thumbv7em-none-eabi", "-mcpu=cortex-m4", "-g", "-O1", "-ffreestanding",
                    "-fno-builtin", "-fstack-usage", "-c", "stack_demo.c", "-o", "stack_demo.o"], cwd=HERE, check=True)
    subprocess.run([zig, "cc", "-target", "thumb-freestanding-eabi", "-mcpu=cortex_m4", "-nostdlib",
                    "-Wl,-T,link.ld", "-Wl,-e,Reset_Handler", "-Wl,--no-gc-sections", "-o", "stack_demo.elf",
                    "stack_demo.o"], cwd=HERE, check=True)
    (HERE / "stack_demo.o").unlink()
    print(HERE / "stack_demo.elf")


if __name__ == "__main__":
    main()
