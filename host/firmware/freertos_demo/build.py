"""freertos_demo.elf を作る（zig cc で Cortex-M4F 向け、FreeRTOS V11.1.0、デバッグ情報付き）。試験は生成済みの ELF を使う。

    python host/firmware/freertos_demo/build.py

FreeRTOS のカーネルは FreeRTOS-Kernel/（必要なファイルだけ。出典・版・ライセンスは FreeRTOS-Kernel/SOURCE.md）。
port は GCC/ARM_CM4F（FPU の文脈は遅延退避。タスクの文脈の積み方は fprobe.rtos.cortexm が読む）。
"""

import shutil
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
KERNEL = "FreeRTOS-Kernel"
PORT = f"{KERNEL}/portable/GCC/ARM_CM4F"
SOURCES = ["freertos_demo.c", "fprobe_trace.c", f"{KERNEL}/tasks.c", f"{KERNEL}/list.c", f"{KERNEL}/queue.c", f"{KERNEL}/timers.c",
           f"{KERNEL}/event_groups.c", f"{KERNEL}/stream_buffer.c", f"{PORT}/port.c",
           f"{KERNEL}/portable/MemMang/heap_4.c", "libc/string.c"]
INCLUDES = [".", "libc", f"{KERNEL}/include", PORT]          # libc/: freestanding の最小の stdlib.h・string.h


def main() -> None:
    zig = shutil.which("zig")
    if zig is None:
        raise SystemExit("zig が見つからない（https://ziglang.org/、winget install zig.zig）")
    subprocess.run([zig, "cc", "-target", "thumb-freestanding-eabihf", "-mcpu=cortex_m4+vfp4d16sp", "-O1", "-gdwarf-5",
                    "-fno-sanitize=all", "-ffreestanding", "-nostdlib", "-fno-builtin", *[f"-I{p}" for p in INCLUDES],
                    "-Wl,-T,link.ld", "-Wl,-e,reset_handler", "-Wl,--no-gc-sections", "-o", "freertos_demo.elf",
                    *SOURCES], cwd=HERE, check=True)
    print(HERE / "freertos_demo.elf")


if __name__ == "__main__":
    main()
