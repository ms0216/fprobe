"""monitor_demo.elf を作る（zig cc で Cortex-M4F 向け、デバッグ情報付き）。試験は生成済みの ELF を使う。

    python host/firmware/monitor_demo/build.py

モニタ（DebugMon_Handler と箱）は host/firmware/monitor/fprobe_monitor.c を組み込む。
"""

import shutil
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
MONITOR = HERE.parent / "monitor"


def main() -> None:
    zig = shutil.which("zig")
    if zig is None:
        raise SystemExit("zig が見つからない（https://ziglang.org/、winget install zig.zig）")
    subprocess.run([zig, "cc", "-target", "thumb-freestanding-eabihf", "-mcpu=cortex_m4+vfp4d16sp", "-O1", "-gdwarf-5",
                    "-fno-sanitize=all", "-ffreestanding", "-nostdlib", "-fno-builtin", f"-I{MONITOR}",
                    "-Wall", "-Werror", "-Wl,-T,link.ld", "-Wl,-e,reset_handler", "-Wl,--no-gc-sections",
                    "-o", "monitor_demo.elf", "monitor_demo.c", str(MONITOR / "fprobe_monitor.c")], cwd=HERE, check=True)
    print(HERE / "monitor_demo.elf")


if __name__ == "__main__":
    main()
