"""Renesas ForgeFPGA（SLG47910。Shrike-Lite の FPGA）を GUI なしでビルドする。

ForgeFPGA Workshop（Go Configure Software Hub）が裏で呼ぶ 2 つの道具を、Workshop と同じ引数で呼ぶ:
  1. 合成: 付属の yosys（v37）で synth_xilinx → netlist.edif（Workshop が build/ に書く synth_script.ys と同じ）
  2. 配置配線とビットストリーム: 付属の eda-placer（v22。Flex Logix の EFLX）。引数は「FPGA_DATA <束> <口>」で渡す
     （束 = base64(be32 長さ + zlib(be32 数 + be32 長さの表 + 文字列の連結))。口は進み具合の TCP。聞くだけでよい）
そのうえで RP2040 が FPGA に書く形（Workshop の bitstream/FPGA_bitstream_MCU.bin）を AXI の語から組む。
どれも Workshop v6.55 の出力と照らして決め、同じネットリストからバイト単位で同じものができることを確かめた
（docs/boards/shrike-lite.md）。

ピンは Workshop の I/O Planner の記録の名前（IOB_t[0:0]_xy[X:Y]_in0 等）で指定する。基板の GPIO の名前からの対応は
Board（SHRIKE_LITE）が持つ。出力は値の口と出力の有効の口（*_oe）の 2 つで 1 本（Workshop の作法）。
"""

import base64
import os
import re
import socket
import struct
import subprocess
import threading
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .gowin import ToolchainNotFound

__all__ = ["SHRIKE_LITE", "Board", "BuildResult", "ForgeFpgaToolchain", "Resources", "io_spec", "mcu_image",
           "pack_arguments", "parse_axi", "parse_resources", "placer_arguments"]

ENV_VAR = "FPROBE_FORGEFPGA_HOME"
DEFAULT_HOMES: tuple[Path, ...] = (Path("C:/Program Files/Renesas Electronics/Go Configure Software Hub"),)

# Workshop v6.55 が書く合成のスクリプト（読み込みの行の後）
SYNTH_SCRIPT = """synth_xilinx -nobram -noiopad -flatten -nodsp -abc9
clean
autoname
write_verilog "post_synth_results.v"
write_edif "netlist.edif"
tee -q -o post_synth_report.txt stat
"""
FLOORPLAN = "1K\n\nM"                     # Workshop の floorplanspec.fp（1K のタイル 1 枚）
PLACER_OPTIONS = ("-ENABLE_BITSTREAM_OUTPUT_AXI", "1", "-ENABLE_BITSTREAM_OUTPUT_AXI_CRC", "0",
                  "-ENABLE_HIGH_DENSITY_PACKING", "1", "-ENABLE_HIGH_DENSITY_IO_PACKING", "0",
                  "-CLK_CONCURRENT_OPT", "1", "-PLACE_AND_TRIAL_ROUTE", "0", "-PNR_TRIAL_ITER_TOTAL", "20",
                  "-MAX_ROUTE_ITER", "300", "-MAX_CPU", "16", "-TIMING_DRIVEN_PACKING_THR", "0.70")

# MCU 用の形: 0 の 320 語 + 頭の 10 語 + AXI の語（下位バイトから）+ 0 の 8 語（語は上位バイトから並べる）。
# 頭の 3 語目は Workshop が 0x22002222 と書き、見本の blink_all.bin は 0x22222222（どちらでも動いた。意味は未解読）
MCU_PAD_WORDS = 320
MCU_HEAD = (0xAA22FF11, 0, 0x22002222, 0x22222222, 0x22220000, 0x03000000, 0, 0, 0, 0)
MCU_TAIL_WORDS = 8

IOB = re.compile(r"IOB_t\[0:0\]_xy\[(\d+):(\d+)\]_(in|out)(\d)$")
CLK = re.compile(r"CLK_t\[0:0\]_([NSEW])_in(\d)$")


@dataclass(frozen=True)
class Board:
    """基板の GPIO の名前 → FPGA の IOB の座標。"""
    name: str
    gpio: Mapping[str, tuple[int, int]]

    def input(self, pin: str) -> str:
        x, y = self.gpio[pin]
        return f"IOB_t[0:0]_xy[{x}:{y}]_in0"

    def output(self, pin: str) -> tuple[str, str]:
        """(値の口, 出力の有効の口)。"""
        x, y = self.gpio[pin]
        return f"IOB_t[0:0]_xy[{x}:{y}]_out0", f"IOB_t[0:0]_xy[{x}:{y}]_out1"


# Shrike-Lite の F_GPIOn = SLG47910 の GPIOn。座標は Workshop の見本（application-notes）の I/O Planner の記録から。
# GPIO3〜6 は FPGA を書く SPI（RP2040 の GPIO0〜3 とつながる。書いた後は利用者の口）、16 は LED、17・18 は抵抗を通って
# RP2040 の GPIO15・14（抵抗の有無は未確認）。ここに無い番号は見本に無く、確かめていない。
SHRIKE_LITE = Board("shrike_lite", {
    "F_GPIO0": (0, 6), "F_GPIO1": (0, 7), "F_GPIO2": (0, 8), "F_GPIO3": (0, 9), "F_GPIO4": (0, 10),
    "F_GPIO5": (0, 22), "F_GPIO6": (0, 23), "F_GPIO7": (0, 24),
    "F_GPIO8": (31, 27), "F_GPIO9": (31, 26), "F_GPIO10": (31, 25),
    "F_GPIO18": (31, 4),                       # 見本 AN-FG-007（nreset を GPIO18 から）。実機で確かめた（2026-10-21）
})

# 内部の口（Workshop の見本 AN-FG-019・AN-FG-001 と同じ記録）
NRESET = "IOB_t[0:0]_xy[31:11]_in0"             # 電源投入のリセット（0 でリセット。見本の nreset・i_por）
OSC_EN = "IOB_t[0:0]_xy[0:25]_out0"             # 1 で内蔵の発振器（50 MHz）を動かす
OSC_CLK = "CLK_t[0:0]_W_in0"                    # 発振器のクロック
LAC0_SOURCE = "IOB_t[0:0]_xy[31:11]_out0"       # ロジックをクロックに（LaC0）: 源の信号
LAC0_EN = "IOB_t[0:0]_xy[31:12]_out0"           # 1 で LaC0 を有効に
LAC0_CLK = "CLK_t[0:0]_E_in0"                   # LaC0 から戻るクロック


def io_spec(pins: Mapping[str, str]) -> str:
    """ピン割り当て（ポート名 → 記録の名前）を eda-placer の io_spec_in.txt にする（使うピンだけ。クロックが先、
    IOB は座標の順。Workshop の書き方と同じ）。"""
    clocks: dict[str, list[tuple[int, str]]] = {}
    iobs: dict[tuple[int, int], list[tuple[int, int, str]]] = {}
    for port, rid in pins.items():
        if m := CLK.match(rid):
            clocks.setdefault(m[1], []).append((int(m[2]), port))
        elif m := IOB.match(rid):
            iobs.setdefault((int(m[1]), int(m[2])), []).append((m[3] == "out", int(m[4]), port))
        else:
            raise ValueError(f"{port}: 記録の名前 {rid!r} は IOB_t[0:0]_xy[X:Y]_in0/out0/out1 か CLK_t[0:0]_<辺>_in<n>")
    out = []
    for side, ins in sorted(clocks.items()):
        out.append(f"EFLX_CLK chip_tile_x=0, chip_tile_y=0, clk_side={side}\n")
        out += [f"    Input={n}, pin={port}\n" for n, port in sorted(ins)]
    for (x, y), ports in sorted(iobs.items()):
        out.append(f"EFLX_IOB chip_tile_x=0, chip_tile_y=0, chip_x={x}, chip_y={y}\n")
        for is_out, n, port in sorted(ports):
            out.append(f"   Output={n}, pin={port}, output_delay=-1\n" if is_out
                       else f"    Input={n}, pin={port},  input_delay=-1\n")
    return "".join(out)


def placer_arguments(work: str) -> list[str]:
    """eda-placer の引数（work の下の config/ に入力。出力は work に出る）。"""
    return ["-edif", f"{work}/config/netlist.edif", "-fp", f"{work}/config/floorplanspec.fp", *PLACER_OPTIONS,
            "-io", f"{work}/config/io_spec_in.txt"]


def pack_arguments(args: Sequence[str]) -> str:
    raw = [a.encode() for a in args]
    data = struct.pack(">I", len(raw)) + struct.pack(f">{len(raw)}I", *map(len, raw)) + b"".join(raw)
    return base64.b64encode(struct.pack(">I", len(data)) + zlib.compress(data)).decode()


def parse_axi(text: str) -> list[int]:
    """eda-placer の EFLX_bitstream_AXI.log（16 進の語を 1 行ずつ。# は注記）。"""
    return [int(s, 16) for s in (line.strip() for line in text.splitlines()) if s and not s.startswith("#")]


def mcu_image(words: Sequence[int]) -> bytes:
    """AXI の語から RP2040 が FPGA に書く形（Workshop の FPGA_bitstream_MCU.bin）を作る。"""
    head = [0] * MCU_PAD_WORDS + list(MCU_HEAD)
    return (b"".join(w.to_bytes(4, "big") for w in head) + b"".join(w.to_bytes(4, "little") for w in words)
            + bytes(4 * MCU_TAIL_WORDS))


@dataclass(frozen=True)
class Resources:
    """配置配線の資源の報告（PNR_RESOURCE.log と STDOUT から）。rbbs = 使ったロジックのブロック（LUT 4 組ずつ）。"""
    luts: int
    ffs: int
    rbbs: int
    rbbs_total: int
    fmax_mhz: dict[str, float] = field(default_factory=dict)

    @property
    def usage(self) -> float:
        return self.rbbs / self.rbbs_total


def parse_resources(text: str) -> Resources:
    """論理の無い回路は LUT・FF・使ったブロックの行が無いので 0。床の広さ（Total EFLX resources の節）は必ずある。"""
    total_at = text.find("Total EFLX resources")
    if total_at < 0:
        raise ValueError("配置配線の報告に Total EFLX resources が無い")
    used, total = text[:total_at], text[total_at:]

    def num(pattern: str, s: str) -> int:
        m = re.search(pattern, s)
        return int(m[1]) if m else 0
    fmax = {m[1]: float(m[2]) for m in re.finditer(r"^\s+(\S+)\s+<UDEF_\S+>\s+([\d.]+)\s*$", text, re.M)}
    return Resources(num(r"(\d+) LUTs total", text), num(r"(\d+) FFs total", text),
                     num(r"(\d+) Logic 6-LUT RBBs", used), num(r"(\d+) Logic 6-LUT RBBs", total), fmax)


@dataclass(frozen=True)
class BuildResult:
    image: Path                 # RP2040 が FPGA に書く .bin
    resources: Resources
    log: Path                   # 配置配線の標準出力


@dataclass(frozen=True)
class ForgeFpgaToolchain:
    home: Path

    @property
    def yosys(self) -> Path:
        return self.home / "external/yosys/v37/yosys.exe"

    @property
    def placer(self) -> Path:
        return self.home / "external/eda-placer/v22/eda-placer.exe"

    @classmethod
    def locate(cls, env: Mapping[str, str] = os.environ) -> "ForgeFpgaToolchain":
        homes = [Path(env[ENV_VAR])] if ENV_VAR in env else list(DEFAULT_HOMES)
        for home in homes:
            tc = cls(home)
            if tc.yosys.is_file() and tc.placer.is_file():
                return tc
        raise ToolchainNotFound(f"ForgeFPGA Workshop（Go Configure Software Hub）が見つからない（{ENV_VAR} で指定可能）: "
                                + ", ".join(map(str, homes)))

    def synthesize(self, verilog: Sequence[Path], build: Path) -> Path:
        """Verilog（top に (* top *)）を合成して build/netlist.edif を作る。"""
        build.mkdir(parents=True, exist_ok=True)
        files = " ".join(f'"{Path(v).resolve().as_posix()}"' for v in verilog)
        (build / "synth_script.ys").write_text(f"read -sv {files}\n{SYNTH_SCRIPT}", encoding="utf-8")
        r = subprocess.run([str(self.yosys), "-e", "(.*)is implicitly declared\\.", "-Q", "-s", "synth_script.ys"],
                           cwd=build, capture_output=True, text=True, errors="replace")
        (build / "synth.log").write_text(r.stdout + r.stderr, encoding="utf-8")
        if r.returncode != 0:
            raise RuntimeError(f"合成に失敗（{build / 'synth.log'}）: {(r.stdout + r.stderr)[-1500:]}")
        return build / "netlist.edif"

    def place_and_route(self, build: Path, pins: Mapping[str, str], timeout_s: float = 1800) -> BuildResult:
        """build/netlist.edif を配置配線し、build/fpga_mcu.bin（RP2040 が書く形）を作る。"""
        work = build / "pnr"
        (work / "config").mkdir(parents=True, exist_ok=True)
        (work / "config/netlist.edif").write_bytes((build / "netlist.edif").read_bytes())
        (work / "config/floorplanspec.fp").write_text(FLOORPLAN, encoding="ascii")
        (work / "config/io_spec_in.txt").write_text(io_spec(pins), encoding="ascii")
        for old in ("EFLX_bitstream_AXI.log", "PNR_RESOURCE.log"):
            (work / old).unlink(missing_ok=True)
        with socket.socket() as srv:                     # 進み具合の口（聞いて捨てる）
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
            threading.Thread(target=_drain, args=(srv,), daemon=True).start()
            blob = pack_arguments(placer_arguments(work.resolve().as_posix()))
            r = subprocess.run([str(self.placer), "FPGA_DATA", blob, str(srv.getsockname()[1])], cwd=work,
                               capture_output=True, text=True, errors="replace", timeout=timeout_s, stdin=subprocess.DEVNULL)
        log = work / "PNR_STDOUT.txt"
        log.write_text(r.stdout + r.stderr, encoding="utf-8")
        axi = work / "EFLX_bitstream_AXI.log"
        if "Bitstream generated." not in r.stdout or not axi.is_file():
            errors = [line for line in r.stdout.splitlines() if "ERROR" in line.upper()]
            raise RuntimeError(f"配置配線に失敗（{log}）: " + ("\n".join(errors[-5:]) or r.stdout[-1500:]))
        image = build / "fpga_mcu.bin"
        image.write_bytes(mcu_image(parse_axi(axi.read_text(encoding="ascii", errors="replace"))))
        report = (work / "PNR_RESOURCE.log").read_text(errors="replace") + r.stdout
        return BuildResult(image, parse_resources(report), log)

    def build(self, verilog: Sequence[Path], pins: Mapping[str, str], build: Path) -> BuildResult:
        self.synthesize(verilog, build)
        return self.place_and_route(build, pins)


def _drain(srv: socket.socket) -> None:
    try:
        conn, _ = srv.accept()
        with conn:
            while conn.recv(65536):
                pass
    except OSError:
        pass
