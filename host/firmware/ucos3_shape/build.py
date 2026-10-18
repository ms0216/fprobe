"""ucos3_shape.elf と ucos3_shape_nodbg.elf を作る（arm-none-eabi 系の GCC で Cortex-M4F 向け、デバッグ情報付き）。
試験は生成済みの ELF を使う。

µC/OS-III の本物のヘッダ（ucos3/。出典は ucos3/SOURCE.md）の構造体で、止めたときのカーネルの様子を置いた試験用の
ELF（µC/OS-III のカーネルそのものではない）。設定（os_cfg.h。テンプレートの写し）の OS_CFG_DBG_EN を 1 と 0 で 2 つ作る
（0 ではタスク・オブジェクトの一覧と名前が無い）。cpu_cfg.h（テンプレートのまま）が
#if 0 で外している CPU_CFG_NVIC_PRIO_BITS は -D で渡す（STM32F4 と同じ 4 ビット）。配置は sram_demo と同じ（link.ld を共有）。

    python host/firmware/ucos3_shape/build.py [GCC]

GCC を省くと PATH の arm-none-eabi-gcc、無ければ Zephyr SDK の arm-zephyr-eabi-gcc（環境変数 ZEPHYR_SDK_INSTALL_DIR、
無ければ ~/zmk/sdk/zephyr-sdk-*）。
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
VARIANTS = {"ucos3_shape.elf": 1, "ucos3_shape_nodbg.elf": 0}      # 出力 → OS_CFG_DBG_EN


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
    for output, dbg in VARIANTS.items():
        subprocess.run([gcc, "-mcpu=cortex-m4", "-mthumb", "-mfloat-abi=hard", "-mfpu=fpv4-sp-d16", "-O1", "-g3",
                        "-gdwarf-4", f"-ffile-prefix-map={HERE}=.", "-ffreestanding", "-nostdlib", "-fno-builtin",
                        f"-DSHAPE_OS_CFG_DBG_EN={dbg}u", "-DCPU_CFG_NVIC_PRIO_BITS=4u","-I.", "-Iucos3", "-Wl,-T,../sram_demo/link.ld",
                        "-Wl,-e,reset_handler", "-Wl,--no-gc-sections", "-o", output, "ucos3_shape.c"],
                       cwd=HERE, check=True)
        print(HERE / output)


if __name__ == "__main__":
    main()
