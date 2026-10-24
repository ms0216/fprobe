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


def test_a_pin_that_does_not_return_within_the_wait_is_reported_as_not_settled():
    # 黙って打ち切らない（総点検）: 戻した線が settle_timeout_us の内に戻らなければ settled = false と待った上限を返す
    p, links = make_probe()
    p.pins_read()
    out = p.pins_write(nreset=0, hold_us=10)
    assert out["settled"] is True and out["settle_timeout_us"] == 100_000
    link = links[0]
    access = link.pin_access
    written = []

    def stuck_low(mask, value):                                               # 書いた後は nRESET が 0 のまま戻らない
        written.extend([mask] if mask else [])
        return access(mask, value) & (~0x80 if written else ~0)
    link.pin_access = stuck_low
    out = p.pins_write(nreset=0, hold_us=10, settle_timeout_us=2000)
    assert out["settled"] is False and out["settle_timeout_us"] == 2000 and out["settle_us"] >= 2000


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
    assert d["reached"] and d["part"]["suggested_target"] == "stm32f401xe" and d["frequencies_hz"] == [1_000_000]
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
    assert status["auto"]["target_source"] == "part" and status["auto"]["frequency_hz"] == 4_000_000
    assert links[0].closed and p._raw_link is None


class LateTdo:
    """TDO が 1 ビット遅れて届く線（fprobe の取り込みの遅れが csr の値 need より小さい間）。Pi 2B の 14.3 MHz 以上の形。"""

    def __init__(self, wire, device, need):
        self.wire, self.device, self.need, self.last = wire, device, need, 1

    def sequence(self, cycles, tms, tdi, capture):
        from fprobe import generated as g
        tdo = self.wire.sequence(cycles, tms, tdi, True) or 0
        delay = self.device.csr.get(g.DEBUG_SAMPLE_DELAY, 0) >> g.DEBUG_SAMPLE_DELAY_JTAG[0] & 7
        if delay < self.need:
            late = (tdo << 1 | self.last) & ((1 << cycles) - 1)
            self.last = tdo >> (cycles - 1) & 1
            tdo = late
        return tdo if capture else None


def test_jtag_sample_delay_scan_reports_facts_per_delay_and_restores(monkeypatch):
    # R16: 遅れ 0〜7 ごとに、チェーンの IDCODE を reads 回読んで、期待値（呼び手が明示）との一致の数と見えた値を並べる。
    # どれを使うかは決めない（終わったら元の遅れに戻す）
    from fprobe import generated as g
    taps = [Tap(4, 0x4BA00477)]
    p, _ = make_probe(FakeTarget(swd=False, jtag_chain=FakeChain(taps)))
    dev = p.session.device
    dev.csr[g.DEBUG_SAMPLE_DELAY] = 1 | 5 << 8                          # SWD 1・JTAG 5（戻す値）
    real = type(p)._jtag_wire
    monkeypatch.setattr(type(p), "_jtag_wire", lambda self, f=None: LateTdo(real(self, f), dev, need=2))
    out = p.jtag_sample_delay_scan(frequency_hz=16_700_000, expected_idcodes=[0x4BA00477], reads=3)
    rows = {r["delay"]: r for r in out["delays"]}
    assert sorted(rows) == list(range(8))
    assert rows[0]["matches"] == 0 and rows[1]["matches"] == 0 and rows[2]["matches"] == 3 and rows[7]["matches"] == 3
    assert rows[0]["observed"] == [{"idcodes": [0x974008EF], "count": 3}]
    assert all(r["applied"] for r in rows.values())
    assert dev.csr[g.DEBUG_SAMPLE_DELAY] == 1 | 5 << 8                  # 元に戻した（SWD の欄も触らない）
    assert out["frequency_hz"] == 16_700_000 and out["restored_delay"] == 5
    assert out["half_period"] == 3 and out["tck_hz"] == 100_000_000 / 7    # 実際の TCK（1 ビット = 2 × 3 + 1 周期）


def test_jtag_sample_delay_set_keeps_the_swd_field_and_reports_readback():
    from fprobe import generated as g
    p, _ = make_probe()
    dev = p.session.device
    dev.csr[g.DEBUG_SAMPLE_DELAY] = 1
    assert p.jtag_sample_delay_set(cycles=3) == {"cycles": 3, "readback": 3}
    assert dev.csr[g.DEBUG_SAMPLE_DELAY] == 1 | 3 << 8
    with pytest.raises(ValueError):
        p.jtag_sample_delay_set(cycles=8)


def test_jtag_sample_delay_scan_refuses_while_connected_without_touching_the_delay():
    from fprobe import generated as g
    p, _ = make_probe()
    connected(p)
    dev = p.session.device
    dev.csr[g.DEBUG_SAMPLE_DELAY] = 1 | 5 << 8
    writes = []
    real = dev.write32
    dev.write32 = lambda a, v: (writes.append(a), real(a, v))
    with pytest.raises(NotDisconnected):
        p.jtag_sample_delay_scan(frequency_hz=1_000_000, expected_idcodes=[1], reads=1)
    assert g.DEBUG_SAMPLE_DELAY not in writes


def test_jtag_sample_delay_set_switches_to_the_jtag_design_first(monkeypatch):
    # 横断レビュー: 標準の構成（JTAG なし）で書くと、後で JTAG の構成に書き替えたときに 0 に戻る。先に切り替える
    p, _ = make_probe()
    calls = []
    monkeypatch.setattr(type(p), "_ensure_design", lambda self, protocol: calls.append(protocol))
    p.jtag_sample_delay_set(cycles=2)
    assert calls == ["jtag"]
