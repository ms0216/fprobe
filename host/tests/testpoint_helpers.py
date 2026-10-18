"""テストポイントの試験の共通: zig cc での構文の確認、ホストでの実行（FPROBE_TP_CUSTOM で ID を記録）。"""

import shutil
import subprocess
from pathlib import Path

import pytest

from fprobe.testpoint.instrument import Options, instrument_tree

DATA = Path(__file__).parent / "data" / "testpoint"
ZIG = shutil.which("zig")
needs_zig = pytest.mark.skipif(ZIG is None, reason="zig が無い")

BACKEND_DEFINES = {
    "none": [],
    "gpio_parallel": ["-DFPROBE_TP_BACKEND=FPROBE_TP_GPIO_PARALLEL", "-DFPROBE_TP_GPIO_BSRR=0x40020818u",
                      "-DFPROBE_TP_PAR_SHIFT=0", "-DFPROBE_TP_PAR_WIDTH=3", "-DFPROBE_TP_PAR_STROBE=3"],
    "gpio_serial": ["-DFPROBE_TP_BACKEND=FPROBE_TP_GPIO_SERIAL", "-DFPROBE_TP_GPIO_BSRR=0x40020018u",
                    "-DFPROBE_TP_SER_CLK=5", "-DFPROBE_TP_SER_DAT=9"],
    "itm": ["-DFPROBE_TP_BACKEND=FPROBE_TP_ITM"],
    "rtt": ["-DFPROBE_TP_BACKEND=FPROBE_TP_RTT", "-DFPROBE_TP_RTT_WRITE(p,n)=((void)(p),(void)(n))"],
    "custom": ["-DFPROBE_TP_BACKEND=FPROBE_TP_CUSTOM"],
}


def zig_syntax(path: Path, backend: str, extra: list[str] = ()) -> subprocess.CompletedProcess:
    """Cortex-M4 向けに翻訳だけする（zig の -fsyntax-only は Windows で一時ファイルの名前替えに失敗するので -c）。"""
    obj = path.with_suffix(f".{backend}.o")
    return subprocess.run([ZIG, "cc", "-target", "thumb-freestanding-eabi", "-mcpu=cortex_m4", "-ffreestanding",
                           "-c", "-o", str(obj), "-Werror=implicit-function-declaration", *BACKEND_DEFINES[backend],
                           *extra, str(path)], capture_output=True, text=True)


def run_instrumented(tmp: Path, sources: list[Path], options: Options = Options(), main: Path = DATA / "run_main.c"):
    """sources を挿入してホスト向けに作って走らせる。(InstrumentResult, 標準出力の行)。"""
    src_dir = tmp / "src"
    src_dir.mkdir(exist_ok=True)
    for s in sources:
        shutil.copy(s, src_dir / s.name)
    result = instrument_tree(src_dir, tmp / "tp", options=options)
    exe = tmp / "run.exe"
    copies = [tmp / "tp" / s.name for s in sources]
    subprocess.run([ZIG, "cc", "-O1", *BACKEND_DEFINES["custom"], "-o", str(exe), *map(str, copies), str(main)],
                   check=True, capture_output=True, text=True)
    out = subprocess.run([str(exe)], check=True, capture_output=True, text=True).stdout.splitlines()
    return result, out


def parse_log(lines: list[str]) -> list[tuple[int, int | None]]:
    out = []
    for line in lines:
        if line.startswith("result"):
            continue
        ident, _, value = line.partition("=")
        out.append((int(ident), int(value) if value else None))
    return out
