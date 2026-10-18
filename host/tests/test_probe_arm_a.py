"""U32: Probe の target_connect で Raspberry Pi 2（Cortex-A7）と Pi Zero（ARM1176）につなぎ、いつもの道具（状態・
停止・再開・ステップ・レジスタ・メモリ・ブレークポイント）を使う。機器は使わない（偽の CMSIS-DAP の層と JTAG）。"""

from types import SimpleNamespace

import pytest
from probe_fakes import FakeDevice
from test_arm_a import pi2, zero

from fprobe.arm_debug import REGISTER_NAMES
from fprobe.mcp_server import Probe


def make_probe(make):
    p = Probe("unused")
    links, fakes = [], []

    def dap_access():
        link, *rest = make()
        links.append(link)
        fakes.append(rest)
        return link

    def debug(**options):
        raise AssertionError("Cortex-A・ARM11 の接続は pyOCD を使わない")
    p._session = SimpleNamespace(device=FakeDevice(), dap_access=dap_access, debug=debug)
    return p, links, fakes


def test_pi2_core_selection_registers_memory_breakpoints_and_step():
    p, links, fakes = make_probe(pi2)
    status = p.target_connect("bcm2836", frequency_hz=2_000_000, core=2)
    cores = fakes[0][0]
    assert status["state"] == "HALTED" and status["core"] == 2 and status["arch"] == "armv7-a"
    assert status["wire"] == "jtag" and status["debug_base"] == 0x80014000 and "fatal" not in status
    assert cores[2].halted and not cores[0].halted and links[0].clock == 2_000_000
    cores[2].memory = {0x8000 + i: i for i in range(16)}
    regs = p.read_registers()
    assert list(regs) == REGISTER_NAMES
    p.write_register("r4", 0x44)
    assert cores[2].regs[4] == 0x44
    assert p.read_memory(0x8001, 4) == "01020304"
    p.write_memory(0x8002, "aabb")
    assert p.read_memory(0x8000, 4) == "0001aabb"
    assert p.breakpoint_set("0x8100")["address"] == 0x8100
    assert p.target_resume()["state"] == "RUNNING"
    assert cores[2].reach(0x8100)
    status = p.target_status()
    assert status["state"] == "HALTED" and status["pc"] == 0x8100 and "fatal" not in status
    assert p.target_step()["pc"] == 0x8104
    p.breakpoint_clear()
    assert not cores[2].bcr[0] & 1
    assert p.dp_read("DPIDR")["value"] == 0x4BA00477
    p.target_disconnect()
    assert links[0].closed


def test_pi_zero_through_the_arm11_tap():
    p, links, fakes = make_probe(zero)
    status = p.target_connect("bcm2835")
    core = fakes[0][0]
    assert status["state"] == "HALTED" and status["arch"] == "armv6" and status["core"] == 0
    p.write_register("pc", 0xC000)
    assert p.read_registers(["pc"]) == {"pc": 0xC000}
    assert p.target_step()["pc"] == 0xC004
    assert p.target_resume()["state"] == "RUNNING"
    assert not core.halted
    with pytest.raises(RuntimeError, match="DP"):
        p.dp_read("DPIDR")
    p.target_disconnect()
    with pytest.raises(ValueError, match="core"):
        p.target_connect("bcm2835", core=1)


def test_unsupported_things_are_refused_clearly():
    p, links, _ = make_probe(pi2)
    with pytest.raises(ValueError, match="script"):
        p.target_connect("bcm2836", script="builtin:reset_with_nreset")
    with pytest.raises(ValueError, match="connect_mode"):
        p.target_connect("bcm2836", connect_mode="under-reset")
    assert all(link.closed for link in links)                                # 断っても接続口は閉じる
    p.target_connect("bcm2836", connect_mode="attach")
    with pytest.raises(RuntimeError, match="リセット"):
        p.target_reset()
    with pytest.raises(RuntimeError, match="Cortex-A"):
        p.pins_read()
