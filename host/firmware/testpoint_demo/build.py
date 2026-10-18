"""テストポイントの試験用ファームを作る: 挿入ツールで src/ の写しを build/tp/ に作り（testpoints.json の ID を保つ）、
zig cc で出力の方式ごとの ELF（Cortex-M4、SRAM、デバッグ情報付き）を作る。試験は生成済みの ELF と対応表を使う。

    python host/firmware/testpoint_demo/build.py

仕様: docs/superpowers/specs/2026-10-17-f7-testpoint-design.md の 6 節。
"""

import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))            # host（fprobe）

from fprobe.testpoint import Options, ProbeTable, WatchVar, instrument_tree  # noqa: E402

MAP = HERE / "testpoints.json"
OUT = HERE / "build" / "tp"
OPTIONS = Options(variables=(WatchVar("state"),))

VARIANTS = {
    # GPIO 2 線: CLK = PA5、DAT = PA9（GPIOA の BSRR）
    "serial": ["-DFPROBE_TP_BACKEND=FPROBE_TP_GPIO_SERIAL", "-DFPROBE_TP_GPIO_BSRR=0x40020018u",
               "-DFPROBE_TP_SER_CLK=5", "-DFPROBE_TP_SER_DAT=9"],
    # GPIO 並列: D0..D2 = PC0..PC2（D2 が先頭の印）、STB = PC3（GPIOC の BSRR）
    "parallel": ["-DFPROBE_TP_BACKEND=FPROBE_TP_GPIO_PARALLEL", "-DFPROBE_TP_GPIO_BSRR=0x40020818u",
                 "-DFPROBE_TP_PAR_SHIFT=0", "-DFPROBE_TP_PAR_WIDTH=3", "-DFPROBE_TP_PAR_STROBE=3"],
    # ITM: スティミュラス・ポート 1
    "itm": ["-DFPROBE_TP_BACKEND=FPROBE_TP_ITM", "-DFPROBE_TP_ITM_PORT=1"],
}


def instrument():
    previous = ProbeTable.load(MAP) if MAP.exists() else None
    return instrument_tree(HERE / "src", OUT, options=OPTIONS, previous=previous, map_path=MAP, line_root=HERE)


def main() -> None:
    zig = shutil.which("zig")
    if zig is None:
        raise SystemExit("zig が見つからない（https://ziglang.org/、winget install zig.zig）")
    result = instrument()
    for f in result.files:
        if f.error or f.skipped:
            raise SystemExit(f"挿入できなかった: {f.path}: {f.error or f.skipped}")
    print(f"テストポイント {result.probe_count} 個 → {MAP}")
    for name, defines in VARIANTS.items():
        elf = f"testpoint_demo_{name}.elf"
        subprocess.run([zig, "cc", "-target", "thumb-freestanding-eabi", "-mcpu=cortex_m4", "-g", "-O1",
                        "-ffreestanding", "-nostdlib", "-fno-builtin", *defines, "-I", "build/tp",
                        "-Wl,-T,link.ld", "-Wl,-e,reset_handler", "-Wl,--no-gc-sections",
                        "-o", elf, "build/tp/demo.c", "startup.c"], cwd=HERE, check=True)
        print(HERE / elf)


if __name__ == "__main__":
    main()
