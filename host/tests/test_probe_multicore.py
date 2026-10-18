"""マルチコア（U31）: 複数ドロップの SWD の部品（RP2040）のコアの選択と、両方のコアの状態・もう一方のコアの停止と再開
（fprobe.multidrop、fprobe.probe_multicore）。機器は使わない（偽の CMSIS-DAP の線に DP を 2 つ並べる）。"""

from types import SimpleNamespace

import pytest
from dap_fakes import FakeDapLink, FakeMemAp, FakeMultidropBus, FakeTarget
from probe_fakes import FakeDevice
from pyocd.core.target import Target

from fprobe import generated as g
from fprobe.mcp_server import Probe
from fprobe.multidrop import (DBGKEY, DCRDR, DCRSR, DHCSR, C_HALT, S_HALT, S_REGRDY, S_SLEEP, core_state, part_of,
                              resolve_target)

CORE0, CORE1 = 0x01002927, 0x11002927


class CortexM0:
    """DP の AHB-AP の向こうのコアのデバッグのレジスタ（DHCSR の停止・再開、DCRSR / DCRDR のレジスタの読み）。"""

    def __init__(self, halted=False, pc=0x10000100):
        self.ap = FakeMemAp(on_write=self._write)
        self.regs = {15: pc}
        self.ap.words[DHCSR] = S_HALT if halted else S_SLEEP
        self.writes: list[tuple[int, int]] = []

    def _write(self, register, value):
        if register != 0x0C:
            return False
        address = self.ap.tar
        self.writes.append((address, value))
        if address == DHCSR:
            assert value >> 16 == DBGKEY >> 16
            self.ap.words[DHCSR] = S_HALT if value & C_HALT else 0
            return True
        if address == DCRSR:
            self.ap.words[DCRDR] = self.regs[value & 0x7F]
            self.ap.words[DHCSR] |= S_REGRDY
            return True
        return False


def rp2040(core0: CortexM0, core1: CortexM0, home=CORE0) -> FakeMultidropBus:
    dps = []
    for sel, core in ((CORE0, core0), (CORE1, core1)):
        link = FakeDapLink(FakeTarget(dpidr=0x0BC12477, targetid=sel, multidrop=True, aps={0: core.ap}))
        link.swd_active = True
        link.selected = sel == home                    # pyOCD が home のコアに接続した後
        link.ctrl_stat = (1 << 28 | 1 << 29) if sel == home else 0
        dps.append(link)
    return FakeMultidropBus(dps)


def connected(bus, target="rp2040_core0", core=0, pc=0x10000200, state=Target.State.HALTED):
    p = Probe("unused")
    flushed = []
    pyocd_core = SimpleNamespace(get_state=lambda: state, read_core_register=lambda name: pc)
    probe = SimpleNamespace(_link=bus, flush=lambda: flushed.append(True))
    p._debug = SimpleNamespace(target=pyocd_core, probe=probe, close=lambda: None)
    p._session = SimpleNamespace(device=FakeDevice())
    p._link = {"target": target, "frequency_hz": 4_000_000, "wire": "swd", "core": core}
    return p, flushed


def test_parts_and_target_names():
    assert part_of("RP2040")[1] == 0 and part_of("rp2040_core1")[1] == 1 and part_of("stm32f401xe") is None
    assert resolve_target("rp2040", 1)[0] == "rp2040_core1"
    assert resolve_target("rp2040", None)[0] == "rp2040_core0"
    assert resolve_target("rp2040_core1", None)[2] == 1
    assert resolve_target("cortex_m", None) == ("cortex_m", None, None)
    with pytest.raises(ValueError, match="core を選べる"):
        resolve_target("stm32f401xe", 1)
    with pytest.raises(ValueError, match="食い違う"):
        resolve_target("rp2040_core0", 1)
    with pytest.raises(ValueError, match="コアは 0, 1"):
        resolve_target("rp2040", 2)
    assert [core_state(v) for v in (S_HALT, S_SLEEP, 0, 1 << 19, 1 << 25)] == \
        ["HALTED", "SLEEPING", "RUNNING", "LOCKUP", "RESET"]


def test_target_connect_picks_the_pyocd_target_of_the_core():
    p = Probe("unused")
    seen = {}

    class S:
        target = SimpleNamespace(get_state=lambda: Target.State.RUNNING)

        def open(self):
            pass

        def close(self):
            pass

    def debug(**options):
        seen.update(options)
        return S()
    p._session = SimpleNamespace(device=FakeDevice(), debug=debug)
    p._multicore_status = lambda status: status                 # 覗くのは別の試験で（ここは接続の名前だけ）
    status = p.target_connect("rp2040", core=1)
    assert seen["target_override"] == "rp2040_core1"
    assert status["target"] == "rp2040_core1" and status["core"] == 1
    with pytest.raises(ValueError, match="core を選べる"):
        p.target_connect("stm32f401xe", core=1)


def test_status_shows_both_cores_and_returns_to_the_connected_dp():
    core0, core1 = CortexM0(halted=True), CortexM0(halted=True, pc=0x20000040)
    bus = rp2040(core0, core1)
    p, flushed = connected(bus)
    status = p.target_status()
    assert status["core"] == 0 and status["state"] == "HALTED"
    cores = {c["core"]: c for c in status["cores"]}
    assert cores[0] == {"core": 0, "selected": True, "state": "HALTED", "pc": 0x10000200, "where": "0x10000200"}
    assert cores[1]["selected"] is False and cores[1]["state"] == "HALTED" and cores[1]["pc"] == 0x20000040
    assert flushed                                              # pyOCD の溜めた転送を先に出す
    # 最後に元の DP（core0）を選び直している。core1 の DP にはデバッグ電源を求めた
    assert bus.log[-1] == f"targetsel 0x{CORE0:08X}" and bus.dps[0].selected and not bus.dps[1].selected
    assert bus.dps[1].ctrl_stat & (1 << 29)


def test_the_other_core_can_be_halted_and_resumed():
    core0, core1 = CortexM0(halted=True), CortexM0(halted=False)
    p, _ = connected(rp2040(core0, core1))
    halted = p.target_halt(core=1)
    assert halted["core"] == 1 and halted["state"] == "HALTED" and halted["pc"] == 0x10000100
    resumed = p.target_resume(core=1)
    assert resumed["state"] == "RUNNING"
    assert core0.writes == []                                   # 接続中のコアの DHCSR には触らない
    with pytest.raises(ValueError, match="コアは 0, 1"):
        p.target_halt(core=3)


def test_status_without_multidrop_has_no_cores_and_core_needs_a_multidrop_target():
    p, _ = connected(None, target="stm32f401xe", core=None)
    assert "cores" not in p.target_status()
    with pytest.raises(ValueError, match="複数ドロップ"):
        p.target_halt(core=1)


def test_the_sampler_on_the_same_wire_stops_peeking():
    p, _ = connected(rp2040(CortexM0(halted=True), CortexM0()))
    p._session.device.csr[g.SAMPLER_CONTROL] = 1
    cores = {c["core"]: c for c in p.target_status()["cores"]}
    assert "サンプラ" in cores[1]["error"]
    with pytest.raises(RuntimeError, match="サンプラ"):
        p.target_halt(core=1)


def test_core_select_reconnects_to_the_other_core_without_halting():
    p = Probe("unused")
    seen = []

    class S:
        target = SimpleNamespace(get_state=lambda: Target.State.RUNNING)

        def open(self):
            pass

        def close(self):
            pass

    def debug(**options):
        seen.append(options)
        return S()
    p._session = SimpleNamespace(device=FakeDevice(), debug=debug)
    p._multicore_status = lambda status: status
    p.target_connect("rp2040", frequency_hz=2_000_000)
    status = p.target_core_select(1)
    assert seen[-1]["target_override"] == "rp2040_core1" and seen[-1]["connect_mode"] == "attach"
    assert seen[-1]["frequency"] == 2_000_000 and status["core"] == 1
    p.target_connect("cortex_m")
    with pytest.raises(ValueError, match="複数ドロップ"):
        p.target_core_select(1)
