"""Zynq DUT のアプリ（host/pynq/dut）のレジスタ表が、PL の回路（dut_stream_core.vhd）の表と一致する。
アプリは PYNQ の gcc でビルドする（ホストの Windows に mmap 等の POSIX が無いので、ここでは表の一致だけを見る）。"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HEADER = ROOT / "host" / "pynq" / "dut" / "dut_regs.h"
VHDL = ROOT / "gateware" / "rtl" / "designs" / "dut_stream_core.vhd"
NAMES = ("CTRL", "PERIOD", "DATA", "LEVEL", "OVERFLOWS", "GENERATED", "FAULT", "ID")


def header_words():
    text = HEADER.read_text(encoding="utf-8")
    return {n: int(re.search(rf"#define\s+DUT_REG_{n}\s+(\d+)", text).group(1)) for n in NAMES}


def vhdl_words():
    text = VHDL.read_text(encoding="utf-8")
    return {n: int(re.search(rf"constant\s+A_{n}\s*:\s*natural\s*:=\s*(\d+)", text).group(1)) for n in NAMES}


def test_register_word_addresses_match_the_vhdl():
    assert header_words() == vhdl_words() == dict(zip(NAMES, range(8)))


def test_id_value_matches_the_vhdl():
    header = int(re.search(r"#define\s+DUT_ID_VALUE\s+0x([0-9A-Fa-f]+)", HEADER.read_text(encoding="utf-8")).group(1), 16)
    vhdl = int(re.search(r'ID_VALUE\s*:\s*std_logic_vector\(31 downto 0\)\s*:=\s*x"([0-9A-Fa-f]+)"', VHDL.read_text(encoding="utf-8")).group(1), 16)
    assert header == vhdl == 0x44555401


def test_app_exposes_the_function_the_linux_probe_attaches_to():
    source = (HEADER.parent / "dut_app.c").read_text(encoding="utf-8")
    assert re.search(r"__attribute__\(\(noinline\)\)\s+(?:void|int)\s+dut_consume\s*\(\s*uint32_t\s+\w+\s*\)", source)
    assert "--period" in source and "--delay-us" in source and "--fault" in source and "--seconds" in source


def test_app_compiles_for_the_target_arm_linux(tmp_path):
    import shutil
    import subprocess
    zig = shutil.which("zig")
    assert zig
    r = subprocess.run([zig, "cc", "-target", "arm-linux-gnueabihf", "-O2", "-marm", "-Wall", "-Wextra", "-Werror",
                        "-o", str(tmp_path / "dut_app"), str(HEADER.parent / "dut_app.c")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_pins_set_refuses_a_bad_pin_before_touching_the_registers():
    import importlib.util
    spec = importlib.util.spec_from_file_location("dut_pins", HEADER.parent / "pins.py")
    pins = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pins)
    assert pins.parse_set(["pins.py", "set", "10", "1"]) == (10, 1)
    for bad in (["set"], ["set", "16", "1"], ["set", "-1", "0"], ["set", "3", "2"], ["set", "x", "1"]):
        assert pins.parse_set(["pins.py", *bad]) is None
    assert pins.main(["pins.py", "set", "99", "1"]) == 2        # /dev/mem を開く前に終わる（Windows でも通る）
