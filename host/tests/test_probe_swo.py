"""Probe の SWO の DWT・ITM の設定の道具（fprobe.probe_swo）: swo_configure → swo_trace → swo_unconfigure。
機器は使わない（sram_demo.elf、偽のターゲット・SWO・デバイス・イベント記録のリング）。"""

from pathlib import Path
from types import SimpleNamespace

import pytest
from probe_fakes import DictMemory, FakeDevice, FakeTarget, use_fake_ring

from fprobe import collector, probe_window
from fprobe import swo_trace as st
from fprobe.mcp_server import Probe
from fprobe.profile import dwt
from fprobe.symbols import Symbols

SRAM_DEMO = Path(__file__).resolve().parents[1] / "firmware" / "sram_demo" / "sram_demo.elf"
M4 = 0x410FC241
SYNC = b"\x00" * 5 + b"\x80"


class FakeSwo:
    def __init__(self):
        self.started: list[int] = []
        self.target = None
        self.writes_before_start: int | None = None

    def swo_stop(self):
        pass

    def swo_start(self, baud):
        self.started.append(baud)
        if self.target is not None:
            self.writes_before_start = len(self.target.write_log)

    def swo_read(self):
        return b""


class OrderedMemory(DictMemory):
    """DWT の比較器の値の比較（DATAVMATCH）は 1 番だけ・LNK1ENA も 1 番だけ・MASK は 15 まで、の Cortex-M4 の振る舞い。"""

    def write_memory(self, address, data):
        value = int.from_bytes(data, "little") if len(data) == 4 else None
        if value is not None:
            for n in range(4):
                if address == st.dwt_function(n):
                    value &= ~(1 << 9)
                    if n != 1:
                        value &= ~(1 << 8)
                    elif value & 1 << 8:
                        value |= 1 << 9
                    data = value.to_bytes(4, "little")
                elif address == st.dwt_mask(n):
                    data = (value & 0xF).to_bytes(4, "little")
        super().write_memory(address, data)


@pytest.fixture
def probe(tmp_path, monkeypatch):
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path))
    use_fake_ring(monkeypatch, probe_window, collector)
    p = Probe("unused")
    p._session = SimpleNamespace(device=FakeDevice())
    p._symbols = Symbols(SRAM_DEMO)
    memory = OrderedMemory({dwt.DWT_CTRL: 0x4000_0000, st.CPUID: M4})
    target = FakeTarget(memory, halted=False)
    swo = FakeSwo()
    swo.target = target
    p._debug = SimpleNamespace(target=target, probe=swo, close=lambda: None)
    yield p
    if p._recorder is not None:
        p.recording_stop()
    p._stop_observing()


def address_of(probe, name):
    return probe._symbols.symbol(name).address


def pc_packet(c, pc):
    return bytes([0x47 | c << 4]) + pc.to_bytes(4, "little")


def value_packet(c, value, write=True):
    return bytes([0x87 | c << 4 | write << 3]) + value.to_bytes(4, "little")


def test_data_trace_writes_in_order_and_assembles_accesses(probe):
    target, dev = probe.target, probe.session.device
    out = probe.swo_configure(data_trace=[{"expression": "counter"}], exception_trace=True, swo_baud=2_000_000,
                              core_clock_hz=16e6, trace_pin=False)
    assert out["core"] == {"name": "Cortex-M4", "arch": "v7m", "cpuid": M4}
    (use,) = [c for c in out["plan"]["comparators"] if c["name"] == "counter"]
    assert use["index"] == 3 and use["function"] == 0b1111
    assert probe._debug.probe.started == [2_000_000]
    log = [a for a, _ in target.write_log]
    last = {a: i for i, a in enumerate(log)}                             # 比較器を調べる書き込みの後の、設定の書き込み
    # 受信の窓を開く前に書くのは鍵・TPIU まで。ITM・DWT・比較器の FUNCTION は後
    assert last[dwt.TPIU_ACPR] < last[dwt.ITM_TCR] < last[dwt.DWT_CTRL] < last[st.dwt_function(3)]
    assert last[st.dwt_mask(3)] < last[st.dwt_function(3)]
    assert last[dwt.TPIU_ACPR] < probe._debug.probe.writes_before_start <= last[dwt.ITM_TCR]   # 受信を始めてから出力を有効に
    # ITM が前から動いていなかったので、窓は最初から同期している
    assert out["synced"]
    counter = address_of(probe, "counter")
    pc = probe._symbols.symbol("tick").address + 4
    dev.swo(dev.ticks + 100, SYNC + pc_packet(3, pc) + value_packet(3, 41) + bytes([0x0E, 15, 0x10]))
    dev.swo(dev.ticks + 100, pc_packet(3, pc) + value_packet(3, 42))
    trace = probe.swo_trace()
    assert trace["total"] == 2 and [a["value"] for a in trace["accesses"]] == [41, 42]
    a = trace["accesses"][0]
    assert a["name"] == "counter" and a["address"] == counter and a["write"] and a["function"] == "tick"
    assert a["complete"] and a["t"] > 0 and a["time_us"] >= 0
    assert trace["exceptions"]["counts"] == {"SysTick": 1}
    assert probe.swo_trace(offset=1)["accesses"][0]["value"] == 42
    stopped = probe.swo_unconfigure()
    assert stopped["total"] == 2 and not stopped["running"]
    assert target.memory.u32(st.dwt_function(3)) == 0
    assert target.memory.u32(dwt.DWT_CTRL) & (1 << 16) == 0             # 例外トレースは元に戻した
    assert probe.swo_trace()["total"] == 2                               # 止めた後も最後の結果を読める
    target.write_log.clear()
    probe.swo_configure(ports=[0], swo_baud=2_000_000, trace_pin=False)  # 戻した後の設定は、もう一度戻さない
    assert (st.dwt_function(3), 0) not in target.write_log[:1]
    probe.swo_unconfigure()


def test_value_watch_halt_uses_the_value_comparator_and_a_link(probe):
    out = probe.swo_configure(value_watch=[{"expression": "milestone", "value": 512}], trace_pin=False)
    uses = {c["role"]: c for c in out["plan"]["comparators"]}
    assert uses["value_watch"]["index"] == 1 and uses["link"]["index"] == 3
    memory = probe.target.memory
    assert memory.u32(st.dwt_comp(1)) == 512
    assert memory.u32(st.dwt_function(1)) & 0xF == 0b0110 and memory.u32(st.dwt_function(1)) >> 12 & 0xF == 3
    assert memory.u32(st.dwt_comp(3)) == address_of(probe, "milestone")
    assert probe._debug.probe.started == []                              # 止めるだけなら SWO は要らない
    probe.swo_unconfigure()
    assert memory.u32(st.dwt_function(1)) == 0


def test_busy_comparators_and_pyocd_reservations(probe):
    memory = probe.target.memory
    memory.write_memory(st.dwt_function(3), (5).to_bytes(4, "little"))     # ファーム・ほかの道具が使っている
    watchpoints = [SimpleNamespace(func=0, addr=0, size=0) for _ in range(4)]
    watchpoints[2].func = 6                                               # pyOCD のウォッチポイント
    initialized = []
    fake_dwt = SimpleNamespace(watchpoints=watchpoints, watchpoint_used=1, dwt_configured=False,
                               init=lambda: initialized.append(True))
    probe.target.selected_core = SimpleNamespace(dwt=fake_dwt)
    out = probe.swo_configure(data_trace=[{"expression": "counter"}], swo_baud=2_000_000, trace_pin=False)
    assert initialized == [True]                                          # pyOCD の DWT を先に初期化（後で消されない）
    (use,) = out["plan"]["comparators"]
    assert use["index"] == 0                                              # 3（使用中）・2（pyOCD）・1（値の比較用）を避ける
    assert watchpoints[0].func != 0 and watchpoints[0].addr is None and fake_dwt.watchpoint_used == 2
    probe.swo_unconfigure()
    assert watchpoints[0].func == 0 and fake_dwt.watchpoint_used == 1


def test_swo_configure_needs_a_baud_and_refuses_while_profiling(probe):
    with pytest.raises(ValueError, match="swo_baud"):
        probe.swo_configure(ports=[0], trace_pin=False)
    probe.target.memory.write_memory(st.CPUID, (0x410FD213).to_bytes(4, "little"))
    with pytest.raises(ValueError, match="ARMv8-M"):
        probe.swo_configure(data_trace=[{"expression": "counter"}], swo_baud=2_000_000, trace_pin=False)
    probe._profile_run = object()
    with pytest.raises(RuntimeError, match="profile_stop"):
        probe.swo_configure(ports=[0], swo_baud=2_000_000, trace_pin=False)
    probe._profile_run = None
    with pytest.raises(RuntimeError, match="swo_configure"):
        probe.swo_unconfigure()


def test_counters_and_reconfigure_replace_the_previous(probe):
    dev = probe.session.device
    probe.swo_configure(ports=[0, 1], counters=["cpi", "lsu"], swo_baud=2_000_000, core_clock_hz=16e6,
                        trace_pin=False)
    assert probe.target.memory.u32(dwt.ITM_TER) == 0b11
    dev.swo(dev.ticks + 100, SYNC + bytes([0x05, 0x09, 0x05, 0x01]))       # 溢れ: CPI+LSU、CPI
    trace = probe.swo_trace()
    assert trace["counters"]["cpi"] == 512 and trace["counters"]["lsu"] == 256
    probe.swo_configure(ports=[2], swo_baud=2_000_000, trace_pin=False)
    assert probe.target.memory.u32(dwt.ITM_TER) == 0b100                 # 前の設定（ポート 0・1）は戻してから
    assert probe.target.memory.u32(dwt.DWT_CTRL) & (1 << 17) == 0
    probe.swo_unconfigure()


def test_trace_pin_uses_the_svd_field(probe):
    from fprobe.svd import Device
    svd = Device.parse(b"""<device><name>STM32F401</name><width>32</width><peripherals><peripheral><name>DBG</name>
        <baseAddress>0xE0042000</baseAddress><registers><register><name>DBGMCU_CR</name><addressOffset>0x4</addressOffset>
        <size>32</size><resetValue>0</resetValue><fields>
        <field><name>TRACE_IOEN</name><bitOffset>5</bitOffset><bitWidth>1</bitWidth></field>
        <field><name>TRACE_MODE</name><bitOffset>6</bitOffset><bitWidth>2</bitWidth></field>
        </fields></register></registers></peripheral></peripherals></device>""")
    probe._svd, probe._svd_info = svd, {"source": "test", "origin": "test"}
    probe.target.memory.write_memory(0xE0042004, (0b11 << 6).to_bytes(4, "little"))
    out = probe.swo_configure(ports=[0], swo_baud=2_000_000)
    assert probe.target.memory.u32(0xE0042004) == 1 << 5                  # TRACE_IOEN=1・非同期（TRACE_MODE=0）
    assert out["trace_pin"] == "DBG.DBGMCU_CR"
    probe.swo_unconfigure()


def test_recording_gets_the_accesses(probe):
    dev = probe.session.device
    probe.recording_start()
    probe.swo_configure(data_trace=[{"expression": "counter", "pc": False}], swo_baud=2_000_000, trace_pin=False)
    dev.swo(dev.ticks + 100, SYNC + value_packet(3, 7))
    dev.advance(1000)
    probe.swo_unconfigure()
    events = [e for e in probe.recording_events(streams=["swo"])["events"] if e["kind"] == "data_access"]
    assert len(events) == 1 and events[0]["b"] == 7 and events[0]["body"]["name"] == "counter"
    assert events[0]["a"] == address_of(probe, "counter")
