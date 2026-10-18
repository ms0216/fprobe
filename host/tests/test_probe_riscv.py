"""RISC-V（U33）: Probe の target_connect で RP2350 の Hazard3 につなぎ、いつもの道具（状態・停止・再開・レジスタ・
メモリ・ブレークポイント・DP の読み）を使う。機器は使わない（偽の CMSIS-DAP の層の ADIv6 の DP と、APB-AP の向こうの
DM の偽物）。"""

from types import SimpleNamespace

import pytest
from dap_fakes import FakeDapLink, FakeMemAp, FakeTarget
from probe_fakes import FakeDevice
from riscv_fakes import CSR_DCSR, FakeHazard3Dm

from fprobe.mcp_server import Probe
from fprobe.riscv_dm import DCSR_EBREAKM, MC_EXECUTE

RP2350_DPIDR = 0x4C013477                                   # DPv3（ADIv6）


class DmAp(FakeMemAp):
    """APB-AP: DRW の読み書きを DM のレジスタ（番地 / 4）へ。"""

    def __init__(self, dm: FakeHazard3Dm):
        super().__init__(idr=0x34770002, on_write=self._write)
        self.dm = dm

    def read(self, address):
        return self.dm.read(address >> 2)

    def _write(self, offset, value):
        if offset == 0xD0C:
            self.dm.write(self.tar >> 2, value)
            return True
        return False


def make_probe(dm: FakeHazard3Dm):
    p = Probe("unused")
    links = []

    def dap_access():
        link = FakeDapLink(FakeTarget(dpidr=RP2350_DPIDR, adi_v6=True, dormant_only=True, aps={0xA000: DmAp(dm)}))
        links.append(link)
        return link

    def debug(**options):
        raise AssertionError("RISC-V の接続は pyOCD を使わない")
    p._session = SimpleNamespace(device=FakeDevice(), dap_access=dap_access, debug=debug)
    return p, links


def test_connect_halts_the_selected_hart_and_reports_it():
    dm = FakeHazard3Dm()
    p, links = make_probe(dm)
    status = p.target_connect("rp2350_riscv", frequency_hz=2_000_000, core=1)
    assert status["state"] == "HALTED" and status["core"] == 1 and status["arch"] == "riscv"
    assert status["debug_module"]["harts"] == [0, 1] and status["pc"] == 0x20000000
    assert dm.harts[1].halted and not dm.harts[0].halted
    assert dm.harts[1].csrs[CSR_DCSR] & DCSR_EBREAKM                        # ebreak でも止まる
    assert links[0].clock == 2_000_000
    assert p.dp_read("DPIDR")["value"] == RP2350_DPIDR
    p.target_disconnect()
    assert links[0].closed


def test_registers_memory_and_breakpoints_through_the_usual_tools():
    dm = FakeHazard3Dm(memory={0x20000040 + i: i for i in range(8)})
    p, _ = make_probe(dm)
    p.target_connect("rp2350_riscv")
    regs = p.read_registers()
    assert list(regs)[:3] == ["pc", "ra", "sp"] and len(regs) == 32
    p.write_register("a0", 0x1234)
    assert dm.harts[0].gprs[10] == 0x1234 and p.read_registers(["a0"]) == {"a0": 0x1234}
    assert p.read_memory(0x20000040, 6) == "000102030405"
    p.write_memory(0x20000041, "aabb")
    assert p.read_memory(0x20000040, 4) == "00aabb03"
    assert p.breakpoint_set("0x20000080")["address"] == 0x20000080
    assert dm.harts[0].triggers[0][1] == 0x20000080 and dm.harts[0].triggers[0][0] & MC_EXECUTE
    assert p.target_resume()["state"] == "RUNNING"
    assert dm.reach(0x20000080)
    status = p.target_status()
    assert status["state"] == "HALTED" and status["pc"] == 0x20000080 and "fatal" not in status
    assert p.target_step()["pc"] == 0x20000084
    p.breakpoint_clear()
    assert not dm.harts[0].triggers[0][0] & MC_EXECUTE
    assert p.target_halt()["state"] == "HALTED"


def test_unsupported_things_are_refused_clearly():
    dm = FakeHazard3Dm()
    p, links = make_probe(dm)
    with pytest.raises(ValueError, match="script"):
        p.target_connect("rp2350_riscv", script="builtin:reset_with_nreset")
    with pytest.raises(ValueError, match="connect_mode"):
        p.target_connect("rp2350_riscv", connect_mode="under-reset")
    assert all(link.closed for link in links)                                # 断っても接続口は閉じる
    p.target_connect("rp2350_riscv", connect_mode="attach")
    assert not dm.harts[0].halted                                            # attach は止めない
    with pytest.raises(RuntimeError, match="リセット"):
        p.target_reset()
    with pytest.raises(RuntimeError, match="RISC-V"):
        p.pins_read()
