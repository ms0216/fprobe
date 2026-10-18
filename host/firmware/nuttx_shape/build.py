"""nuttx_shape.elf を作る（arm-none-eabi 系の GCC で Cortex-M4F 向け、デバッグ情報付き）。試験は生成済みの ELF を使う。

Apache NuttX の本物のヘッダ（nuttx/include。出典は nuttx/SOURCE.md）の構造体で、止めたときのカーネルの様子を置いた試験用の
ELF（NuttX のカーネルそのものではない）。NuttX の libc のヘッダを使うので、コンパイラの標準のヘッダは外す（-nostdinc。
stdarg.h だけはコンパイラのもの。NuttX の build と同じ）。設定は config/nuttx/config.h（手で書いた）。
配置は sram_demo と同じ（link.ld を共有）。

    python host/firmware/nuttx_shape/build.py [GCC]

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
    gcc = _gcc()
    compiler_include = subprocess.run([gcc, "-print-file-name=include"], capture_output=True, text=True,
                                      check=True).stdout.strip()
    subprocess.run([gcc, "-mcpu=cortex-m4", "-mthumb", "-mfloat-abi=hard", "-mfpu=fpv4-sp-d16", "-O1", "-g3",
                    "-gdwarf-4", f"-ffile-prefix-map={HERE}=.", "-ffreestanding", "-nostdlib", "-fno-builtin",
                    "-nostdinc", "-D__NuttX__", "-isystem", "config", "-isystem", "nuttx/include",
                    "-isystem", compiler_include, "-Wl,-T,../sram_demo/link.ld", "-Wl,-e,reset_handler",
                    "-Wl,--no-gc-sections", "-o", "nuttx_shape.elf", "nuttx_shape.c"], cwd=HERE, check=True)
    print(HERE / "nuttx_shape.elf")


if __name__ == "__main__":
    main()
