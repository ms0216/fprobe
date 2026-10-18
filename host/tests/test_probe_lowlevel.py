"""Probe の接続の診断と低レベル操作の道具（fprobe.probe_lowlevel）: ピン・DP / AP（未接続は RawDap、接続中は pyOCD の
dp）・JTAG・速度試験・診断・target_connect の auto。機器は使わない（偽の CMSIS-DAP の層・JTAG の模型）。"""

from types import SimpleNamespace

import pytest
from coresight_fakes import stm32f401_words
from dap_fakes import FakeDapLink, FakeMemAp, FakeTarget
from jtag_fakes import FakeChain, Tap
from probe_fakes import DictMemory, FakeDevice
from probe_fakes import FakeTarget as FakeCore
from pyocd.core.target import Target

from fprobe.mcp_server import Probe
from fprobe.probe_lowlevel import NotDisconnected


def make_probe(target=None, pins=0b1000_0011):
    p = Probe("unused")
    links = []

    def dap_access():
        link = FakeDapLink(target or FakeTarget(aps={0: FakeMemAp(words=stm32f401_words())}), pins=pins)
        links.append(link)
        return link
    p._session = SimpleNamespace(device=FakeDevice(), dap_access=dap_access)
    return p, links


class PyocdDp:
    """pyOCD の DebugPort の偽物（接続中の経路）。"""

    def __init__(self):
        from pyocd.coresight.dap import ADIVersion
        self.adi_version = ADIVersion.ADIv5
        self.log = []

    def read_dp(self, address):
        self.log.append(("read_dp", address))
        return 0x2BA01477 if address == 0 else 0xF0000040

    def write_dp(self, address, value):
        self.log.append(("write_dp", address, value))

    def read_ap(self, address):
        self.log.append(("read_ap", address))
        return 0x24770011

    def write_ap(self, address, value):
        self.log.append(("write_ap", address, value))

    def flush(self):
        pass


class BlockCore(FakeCore):
    def read_memory_block32(self, address, words):
        return [int.from_bytes(self.memory.read_memory(address + 4 * i, 4), "little") for i in range(words)]

    def write_memory_block32(self, address, data):
        for i, w in enumerate(data):
            self.memory.write_memory(address + 4 * i, w.to_bytes(4, "little"))


def connected(p):
    dp = PyocdDp()
    core = BlockCore(DictMemory({0x20000000: 0x11111111, 0x20000004: 0x22222222}))
    core.dp = dp
    pins_probe = SimpleNamespace(read_pins=lambda group, mask: 0b10011,
                                 session=SimpleNamespace(options={"frequency": 4_000_000}))
    p._debug = SimpleNamespace(target=core, probe=pins_probe, close=lambda: None)
    return dp, core


def test_pins_are_read_and_written_while_disconnected():
    p, links = make_probe()
    assert p.pins_read() == {"swclk": 1, "swdio": 1, "nreset": 1, "not_reported": ["tdi", "tdo", "ntrst"]}
    after = p.pins_write(nreset=0)
    assert after["nreset"] == 0 and links[0].pin_writes == [(0x80, 0)]
    after = p.pins_write(nreset=0, hold_us=10)                                  # 脈: 元に戻す
    assert after["nreset"] == 0 and links[0].pin_writes[-1] == (0x80, 0)
    p.pins_write(nreset=1)
    p.pins_write(nreset=0, hold_us=10)
    assert links[0].pin_writes[-1] == (0x80, 0x80) and p.pins_read()["nreset"] == 1
    with pytest.raises(ValueError):
        p.pins_write()
    with pytest.raises(ValueError):
        p.pins_write(swclk=2)


def test_pins_are_read_through_pyocd_when_connected_and_writes_are_refused():
    p, links = make_probe()
    connected(p)
    assert p.pins_read()["nreset"] == 1                                       # pyOCD の nRESET（bit 4）→ bit 7
    with pytest.raises(NotDisconnected):
        p.pins_write(nreset=0)
    with pytest.raises(NotDisconnected):
        p.jtag_scan()
    with pytest.raises(NotDisconnected):
        p.target_diagnose()
    assert not links


def test_dp_and_ap_without_a_target_connection_use_the_raw_path():
    p, links = make_probe()
    assert p.dp_read("DPIDR")["value"] == 0x2BA01477
    out = p.ap_read(0, 0xFC)
    assert out["value"] == 0x24770011 and out["path"] == "raw swd 1000000 Hz"
    p.ap_write(0, 0x04, 0xE000ED00)
    assert p.ap_read(0, 0x0C)["value"] == 0x410FC241
    p.dp_write("ABORT", 0x1E)
    assert len(links) == 1                                                    # 開いた接続口は使い回す


def test_dp_and_ap_while_connected_go_through_pyocd():
    p, links = make_probe()
    dp, _ = connected(p)
    assert p.dp_read("TARGETID")["path"] == "pyocd" and dp.log[-1] == ("read_dp", 0x24)
    p.ap_read(1, 0xFC)
    assert dp.log[-1] == ("read_ap", 0x010000FC)
    p.ap_write(0, 0x04, 5)
    assert dp.log[-1] == ("write_ap", 0x04, 5)
    p.dp_write(0x8, 0)
    with pytest.raises(ValueError):
        p.ap_read(300, 0)
    assert not links


def test_jtag_scan_shift_and_raw_clock():
    taps = [Tap(4, 0x4BA00477), Tap(5, 0x06433041, user={0b00010: (8, 0x5A)})]
    p, links = make_probe(FakeTarget(swd=False, jtag_chain=FakeChain(taps)))
    scan = p.jtag_scan()
    assert scan["jtag_chain"] == [4, 5] and scan["count"] == 2
    out = p.jtag_shift(device=1, ir=0b00010, dr_value=0x33, dr_length=8)
    assert out["dr_capture"] == 0x5A and out["chain"] == [4, 5]
    assert taps[1].updates[-1] == ("DR", 0x33)
    assert p.jtag_reset() == {"state": "run-test/idle"}
    raw = p.jtag_clock("100" + "0" * 31 + "1" + "10")
    assert raw["clocks"] == 37 and int(raw["tdo"][3:35][::-1], 2) == 0x4BA00477
    assert links[0].log[0] == "connect JTAG"


def test_jtag_shift_needs_a_chain():
    p, _ = make_probe(FakeTarget(swd=False))
    with pytest.raises(ValueError):
        p.jtag_shift(ir=1)


def test_speed_test_reads_writes_verifies_and_restores():
    p, _ = make_probe()
    _, core = connected(p)
    out = p.target_speed_test(0x20000000, 8, write=True)
    assert out["read"]["seconds"] >= 0 and out["write"]["verified"] and out["write"]["restored"]
    assert out["frequency_hz"] == 4_000_000
    assert core.read_memory(0x20000000) == 0x11111111                         # 元に戻った
    core.state = Target.State.RUNNING
    with pytest.raises(RuntimeError):
        p.target_speed_test(0x20000000, 8, write=True)
    with pytest.raises(ValueError):
        p.target_speed_test(0x20000001, 8)


def test_diagnose_runs_on_the_raw_path_and_leaves_the_dp_ready():
    p, links = make_probe()
    d = p.target_diagnose()
    assert d["ok"] and d["suggested_connect"]["target"] == "stm32f401xe"
    assert [s["name"] for s in d["steps"]][:2] == ["voltage", "pins"]
    assert d["steps"][0]["status"] == "skip"                                  # 偽の補助マイコンは計測値を送らない
    assert p.dp_read("CTRL/STAT")["value"] & (1 << 29)
    assert len(links) == 1


def test_auto_connect_identifies_the_part_and_closes_the_raw_link(monkeypatch):
    p, links = make_probe()
    opened = {}

    class FakeSession:
        def open(self):
            pass

        def close(self):
            pass

    def debug(**options):
        opened.update(options)
        s = FakeSession()
        s.target = BlockCore(DictMemory(), halted=True)
        s.target.read_core_register = lambda name: 0x08000000
        return s
    p._session.debug = debug
    status = p.target_connect("auto", frequency_hz=4_000_000)
    assert opened["target_override"] == "stm32f401xe" and opened["frequency"] == 4_000_000
    assert status["auto"]["part"]["name"] == "STM32F401xD/E" and status["auto"]["target"] == "stm32f401xe"
    assert links[0].closed and p._raw_link is None
