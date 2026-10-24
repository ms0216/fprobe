"""接続の診断と低レベル操作（U29）・接続の手順の上書きとオプションバイト（U30）の実機試験: Primer 20K + Nucleo F401RE。

配線は SWD・JTAG の試験と同じ J12（TCK = SWCLK、TMS = SWDIO、TDI = PA15、TDO = SWO のピン PB3、nRESET）。
STM32F401 の JTAG チェーンは TDO 側から JTAG-DP（IR 4、IDCODE 0x4BA00477）、バウンダリスキャン（IR 5、0x06433041）。

**オプションバイトは読むだけ。RDP の解除（target_unlock）・オプションバイトの書き込み（option_bytes_write）は
この試験では絶対に行わない**（全消去・保護の変更を伴う。偽の FLASH の模型の試験で確かめる）。

OpenOCD の SVF（OOCD-12）は、fprobe の CMSIS-DAP の DAP_JTAG_Sequence で IDCODE を読む SVF を流して確かめる。
IPDBG（OOCD-11）・PLD の書き込み（OOCD-13）は相手の FPGA（IPDBG のハブを入れた Tang Nano 等）の配線が要るので、
手順だけを skip の理由に書いておく。
"""

import os
import shutil
import subprocess

import pytest

from fprobe.mcp_server import Probe
from tests.hil.program import SERIAL, program

pytestmark = pytest.mark.hil

STM32_CHAIN = [4, 5]
JTAG_DP_IDCODE = 0x4BA00477
BSC_IDCODE = 0x06433041
SRAM = 0x20004000


@pytest.fixture(scope="module")
def probe():
    program("fprobe")
    p = Probe(SERIAL)
    yield p
    p.close()


@pytest.fixture
def disconnected(probe):
    probe.target_disconnect()
    yield probe
    probe._close_lowlevel()


def test_diagnose_finds_the_nucleo_and_its_target_name(disconnected):
    d = disconnected.target_diagnose(frequencies_hz=[4_000_000])
    assert d["reached"], [s for s in d["steps"] if s["status"] == "fail"]
    assert d["protocol"] == "swd" and d["dp"]["dpidr"]["value"] == 0x2BA01477
    assert d["part"]["name"] == "STM32F401xD/E" and d["part"]["suggested_target"] == "stm32f401xe"
    assert d["core"]["core"] == "Cortex-M4"
    names = [c["name"] for c in d["aps"][0]["rom_table"]["children"]]
    assert "SCS (Cortex-M4)" in names and "DWT" in names
    prot = next(s for s in d["steps"] if s["name"] == "protection")
    assert prot["data"]["rdp_level"] == 0, prot                     # Nucleo は保護なし（読むだけ）


def test_raw_dp_and_ap_access(disconnected):
    assert disconnected.dp_read("DPIDR")["value"] == 0x2BA01477
    idr = disconnected.ap_read(0, 0xFC)
    assert idr["value"] == 0x24770011 and idr["path"].startswith("raw swd")
    assert disconnected.dp_read("CTRL/STAT")["value"] & (1 << 29)   # CDBGPWRUPACK


def test_jtag_scan_shift_and_idcodes(disconnected):
    scan = disconnected.jtag_scan(frequency_hz=1_000_000)
    assert scan["jtag_chain"] == STM32_CHAIN, scan
    assert [d["idcode"] for d in scan["devices"]] == [JTAG_DP_IDCODE, BSC_IDCODE]
    assert disconnected.jtag_shift(device=0, ir=0b1110, dr_length=32)["dr_capture"] == JTAG_DP_IDCODE
    assert disconnected.jtag_shift(device=1, ir=0b00001, dr_length=32)["dr_capture"] == BSC_IDCODE
    disconnected.jtag_reset()
    tdo = disconnected.jtag_clock("100" + "0" * 31 + "1" + "10")["tdo"]           # TLR 後の DR = DP の IDCODE
    assert int(tdo[3:35][::-1], 2) == JTAG_DP_IDCODE


def test_pins_and_a_reset_pulse(disconnected):
    pins = disconnected.pins_read()
    assert pins["nreset"] == 1 and pins["swdio"] == 1
    after = disconnected.pins_write(nreset=0, hold_us=2000)
    assert after["nreset"] == 1


def test_auto_connect_speed_and_dp_through_pyocd(probe):
    status = probe.target_connect("auto", frequency_hz=8_000_000)
    assert status["auto"]["target"] == "stm32f401xe"
    assert probe.dp_read("DPIDR")["path"] == "pyocd"
    read = probe.target_speed_test(0x08000000, 65536)
    assert read["read"]["bytes_per_s"] > 300_000, read
    probe.target_halt()
    out = probe.target_speed_test(SRAM, 16384, write=True)
    assert out["write"]["verified"] and out["write"]["restored"], out
    probe.target_resume()


def test_option_bytes_are_only_read(probe):
    """読むだけ。書き込み・解除はしない（この関数の外でも呼ばない）。"""
    probe.target_connect("stm32f401xe")
    ob = probe.option_bytes_read()
    assert ob["family"] == "f4" and ob["rdp_level"] == 0 and ob["fields"]["RDP"] == 0xAA, ob
    assert ob["part"]["name"] == "STM32F401xD/E" and ob["fields"]["nWRP"] == 0xFF


def test_connect_with_a_builtin_script(probe):
    status = probe.target_connect("stm32f401xe", script="builtin:reset_with_nreset")
    assert status["script"]["hooks"][0]["name"] == "will_reset"
    after = probe.target_reset(halt=True)
    assert after["state"] == "HALTED", after
    probe.target_connect("stm32f401xe", script="builtin:stm32_debug_low_power")
    assert probe.read_memory(0xE0042004, 4) != "00000000"            # DBGMCU_CR の DBG_SLEEP 等
    probe.target_reset()


def _openocd() -> str | None:
    import glob
    return shutil.which("openocd") or next(iter(glob.glob(os.path.expanduser(
        "~/AppData/Local/Microsoft/WinGet/Packages/xpack-dev-tools.openocd-xpack*/*/bin/openocd.exe"))), None)


def test_openocd_svf_reads_the_idcode_over_fprobe_jtag(probe, tmp_path):
    openocd = _openocd()
    if openocd is None:
        pytest.skip("OpenOCD が無い")
    probe.target_disconnect()
    probe.jtag_reset()                                                # 前の試験の SWD から JTAG へ切り替えておく
    probe._close_lowlevel()
    probe.close()                                                     # OpenOCD が USB を開けるように
    svf = tmp_path / "idcode.svf"
    # IR: DP = IDCODE（1110）、BSC = BYPASS（11111）。DR: DP の IDCODE 32 ビット + BSC の BYPASS 1 ビット
    svf.write_text("TRST OFF;\nENDIR IDLE;\nENDDR IDLE;\nSTATE RESET;\nSTATE IDLE;\n"
                   "SIR 9 TDI (1FE);\n"
                   f"SDR 33 TDI (000000000) TDO (0{JTAG_DP_IDCODE:08X}) MASK (00FFFFFFF);\n", encoding="ascii")
    scripts = os.path.join(os.path.dirname(openocd), "..", "openocd", "scripts")
    adapter = (f"adapter driver cmsis-dap; cmsis-dap vid_pid 0x1209 0x0001; adapter serial {SERIAL}; "
               "cmsis-dap backend usb_bulk; transport select jtag; adapter speed 1000")
    taps = ("jtag newtap stm32 cpu -irlen 4 -expected-id 0x4ba00477; "
            "jtag newtap stm32 bs -irlen 5 -expected-id 0x06433041")
    result = subprocess.run([openocd, "-s", scripts, "-c", adapter, "-c", taps, "-c",
                             f"init; svf -quiet {svf.as_posix()}; shutdown"], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=60)
    text = result.stdout + result.stderr
    # -quiet では成功の行を出さない版がある: 処理したこと・TDO の不一致や誤りが無いことで判定する
    lower = text.lower()
    assert "svf processing file" in lower and f"found: 0x{JTAG_DP_IDCODE:08x}" in lower, text[-2000:]
    assert "mismatch" not in lower and "error" not in lower, text[-2000:]


@pytest.mark.skip(reason="相手の FPGA が要る: Tang Nano 9K に IPDBG の JTAG ハブ（ipdbg.org の jtag_hub + la）を入れ、"
                         "その JTAG を fprobe の J12 につなぎ、OpenOCD で `ipdbg create-hub` → `ipdbg/hub start -port 4242`、"
                         "IPDBG の PC 側（ipdbg-la）で波形が取れることを見る。PLD（OOCD-13）は同じ配線で "
                         "`pld create gowin.pld gowin -chain-position gw1n.tap` → `pld load gowin.pld <bit>`")
def test_openocd_ipdbg_and_pld():
    pass
