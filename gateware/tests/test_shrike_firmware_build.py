"""Shrike-Lite の C のデモ: build.py --firmware が FPGA の回路を埋め込んだ Zephyr のファームウェアを作る
（Zephyr と ForgeFPGA の道具が無ければ飛ばす。機器は使わない）。"""

import importlib.util
from pathlib import Path

import pytest
from elftools.elf.elffile import ELFFile

from fprobe_gw.toolchain.gowin import ToolchainNotFound

DEMO = Path(__file__).resolve().parents[2] / "target_ip" / "fprobe_obs" / "examples" / "shrike_lite"


def load_build():
    spec = importlib.util.spec_from_file_location("shrike_build", DEMO / "build.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_firmware_builds_with_the_fpga_image_embedded(tmp_path):
    from fprobe_gw.toolchain.forgefpga import ForgeFpgaToolchain
    from fprobe_gw.toolchain.zephyr import ZephyrToolchain
    try:
        ZephyrToolchain.locate()
        ForgeFpgaToolchain.locate()
    except ToolchainNotFound as e:
        pytest.skip(str(e))
    build = load_build()
    fpga = build.build(out=tmp_path / "fpga").image
    out = build.build_firmware(fpga, tmp_path / "fw")
    elf = out["elf"]
    assert Path(out["uf2"]).is_file() and out["zephyr"].startswith("4.")
    with open(elf, "rb") as f:
        e = ELFFile(f)
        symbols = {s.name: s for s in e.get_section_by_name(".symtab").iter_symbols()}
        # 回路の全体を、シンボル fpga_image の番地と大きさで ELF から取り出して比べる（先頭は 0 が 1280 バイト続くので、
        # 先頭だけでは別の回路でも通ってしまう）
        sym = symbols["fpga_image"]
        sec = e.get_section(sym["st_shndx"])
        start = sym["st_value"] - sec["sh_addr"]
        embedded = sec.data()[start:start + sym["st_size"]]
    assert {"g_errors", "g_scenario", "g_requests", "g_fpga_id", "_SEGGER_RTT", "fpga_load"} <= set(symbols)
    assert embedded == Path(fpga).read_bytes()
