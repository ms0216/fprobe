"""etm_demo.elf を作る（arm-none-eabi 系の GCC で Cortex-M33 向け。RP2350 の XIP の番地 0x10000000 に置く）。
試験は生成済みの ELF を使う。

    python host/firmware/etm_demo/build.py [GCC]

GCC を省くと PATH の arm-none-eabi-gcc、無ければ Zephyr SDK の arm-zephyr-eabi-gcc（環境変数 ZEPHYR_SDK_INSTALL_DIR、
無ければ ~/zmk/sdk/zephyr-sdk-*）。
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _gcc() -> str:
    if len(sys.argv) > 1:
        return sys.argv[1]
    found = shutil.which("arm-none-eabi-gcc")
    if found:
        return found
    roots = [Path(os.environ["ZEPHYR_SDK_INSTALL_DIR"])] if "ZEPHYR_SDK_INSTALL_DIR" in os.environ else \
        sorted((Path.home() / "zmk" / "sdk").glob("zephyr-sdk-*"))
    for root in roots:
        for name in ("arm-zephyr-eabi-gcc.exe", "arm-zephyr-eabi-gcc"):
            path = root / "arm-zephyr-eabi" / "bin" / name
            if path.exists():
                return str(path)
    raise SystemExit("arm-none-eabi-gcc も Zephyr SDK の arm-zephyr-eabi-gcc も見つからない（引数で渡す）")


def main() -> None:
    subprocess.run([_gcc(), "-mcpu=cortex-m33", "-mthumb", "-g", f"-ffile-prefix-map={HERE}=.", "-nostdlib",
                    "-Wl,-Ttext=0x10000000", "-Wl,-e,reset_handler", "-o", "etm_demo.elf", "etm_demo.S"],
                   cwd=HERE, check=True)
    print(HERE / "etm_demo.elf")


if __name__ == "__main__":
    main()
