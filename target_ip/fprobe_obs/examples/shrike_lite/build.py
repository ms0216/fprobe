"""Shrike-Lite のデモの FPGA をビルドする（GUI なし）: VHDL → GHDL で Verilog → ForgeFPGA の yosys・eda-placer。

    python target_ip/fprobe_obs/examples/shrike_lite/build.py [--fixed] [--obs snap|stream|none] [--out DIR] [--firmware]

--firmware: FPGA の回路を埋め込んだ RP2040 のファームウェア（Zephyr。firmware_c/）も作る（<DIR>/fw/zephyr/zephyr.elf・
zephyr.uf2。fprobe の SWD の flash で書く）。Zephyr の場所は fprobe_gw.toolchain.zephyr（環境変数で差し替え）。

できる build/<名前>/fpga_mcu.bin は --firmware でファームウェアに埋め込まれ、RP2040 が起動のたびに FPGA に送る（README）。
資源の使用（LUT・FF・ロジックのブロック）を表示する。--obs none と比べると観測の回路の分が分かる。
"""

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OBS_RTL = HERE.parent.parent / "rtl"
VHDL = [OBS_RTL / "obs_async_fifo.vhd", OBS_RTL / "obs_core.vhd", OBS_RTL / "obs_tck_sampler.vhd",
        OBS_RTL / "obs_soft_tap.vhd",
        OBS_RTL / "obs_soft_core.vhd", OBS_RTL / "obs_snap_core.vhd", HERE / "rtl/demo_spi_slave.vhd", HERE / "rtl/demo_accel.vhd",
        HERE / "rtl/shrike_demo_core.vhd"]


def pins():
    from fprobe_gw.toolchain import forgefpga as ff
    b = ff.SHRIKE_LITE
    out = {"osc_clk": ff.OSC_CLK, "osc_en": ff.OSC_EN, "nreset": ff.NRESET,
           "clk": ff.LAC0_CLK, "lac0_clk": ff.LAC0_SOURCE, "lac0_en": ff.LAC0_EN,
           # 観測の JTAG: TCK = F_GPIO0、TMS = F_GPIO18（手元の Shrike は F_GPIO2 の入力が働かないので使わない。
           # 2026-10-21。docs/boards/shrike-lite.md）
           "tck": b.input("F_GPIO0"), "tms": b.input("F_GPIO18"), "tdi": b.input("F_GPIO1"),
           "spi_sck": b.input("F_GPIO3"), "spi_cs_n": b.input("F_GPIO4"), "spi_mosi": b.input("F_GPIO5")}
    out["tdo"], out["tdo_oe"] = b.output("F_GPIO7")
    out["spi_miso"], out["spi_miso_oe"] = b.output("F_GPIO6")
    return out


OBS_KINDS = {"none": 0, "snap": 1, "stream": 2}


def build(fixed: bool = False, obs: str = "snap", fifo: int = 4, out: Path | None = None):
    from fprobe_gw.toolchain.forgefpga import ForgeFpgaToolchain
    from fprobe_gw.toolchain.ghdl import GhdlToolchain
    name = ("fixed" if fixed else "bug") + ("" if obs == "snap" else f"_{obs}") + (f"_fifo{fifo}" if fifo != 4 else "")
    out = out or HERE / "build" / name
    out.mkdir(parents=True, exist_ok=True)
    generics = {"CLEAR_ON_RESTART": str(fixed).lower(), "OBS": OBS_KINDS[obs], "FIFO_DEPTH": fifo}
    core = GhdlToolchain.locate().to_verilog(VHDL, "shrike_demo_core", out / "shrike_demo_core.v", generics)
    return ForgeFpgaToolchain.locate().build([HERE / "shrike_demo_top.v", core], pins(), out)


def build_firmware(fpga_image: Path, out: Path) -> dict:
    """FPGA の回路 fpga_image を埋め込んだファームウェア（firmware_c/）を out に作る。ELF・UF2 と、使った Zephyr の版・SDK。"""
    from fprobe_gw.toolchain.zephyr import ZephyrToolchain
    tc = ZephyrToolchain.locate()
    elf = tc.build(HERE / "firmware_c", "rpi_pico", Path(out), [f"-DFPGA_IMAGE={Path(fpga_image).resolve().as_posix()}"])
    return {"elf": str(elf), "uf2": str(elf.with_suffix(".uf2")), "zephyr": tc.version(), "sdk": str(tc.sdk)}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--fixed", action="store_true", help="計算器の不具合を直した版（CLEAR_ON_RESTART）")
    p.add_argument("--obs", choices=sorted(OBS_KINDS), default="snap",
                   help="観測の回路: snap = 標本型（既定。SLG47910 に入る）、stream = 記録型、none = なし（規模の比較）")
    p.add_argument("--fifo", type=int, default=4, help="観測の FIFO の深さ（2 の累乗）")
    p.add_argument("--out", type=Path)
    p.add_argument("--firmware", action="store_true", help="FPGA の回路を埋め込んだ RP2040 のファームウェアも作る")
    a = p.parse_args(argv)
    r = build(a.fixed, a.obs, a.fifo, a.out)
    res = r.resources
    report = {"image": str(r.image), "luts": res.luts, "ffs": res.ffs, "rbbs": res.rbbs,
              "rbbs_total": res.rbbs_total, "usage": round(res.usage, 3), "fmax_mhz": res.fmax_mhz}
    if a.firmware:
        report["firmware"] = build_firmware(r.image, Path(r.image).parent / "fw")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
