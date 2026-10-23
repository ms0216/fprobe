"""Probe の変数のライブ観測（fprobe.probe_watch）: 式・名前・番地 → サンプラのか所、イベント記録の語 → 型の値。
拡充（U43）: ポインタをたどる式・条件つきの取得・走行中の書き込み・ターゲット側のバッファ。
機器は使わない（dwarf_demo の ELF、CSR とイベント記録の偽物、ELF を置いた偽の CPU）。"""

import struct
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_probe_observe import FakeCpu, connect, wait_for

from fprobe import generated as g
from fprobe import probe_watch
from fprobe.events import KIND_INJECT, KIND_SAMPLE, DapEvent
from fprobe.mcp_server import Probe
from fprobe.symbols import Symbols
from test_sampler import FakeDevice

DWARF_O0 = Path(__file__).resolve().parents[1] / "firmware" / "dwarf_demo" / "dwarf_demo_O0.elf"


class FakeEventLog:
    events: list[DapEvent] = []
    started = 0

    def __init__(self, dev):
        pass

    def ensure_started(self):
        FakeEventLog.started += 1

    def read(self, last=None, end=None):
        return FakeEventLog.events[:end][-last:]


@pytest.fixture
def probe(monkeypatch):
    monkeypatch.setattr(probe_watch, "EventLog", FakeEventLog)
    FakeEventLog.events, FakeEventLog.started = [], 0
    dev = FakeDevice()
    dev.capabilities = lambda: SimpleNamespace(sampler=True, sys_clock_hz=100_000_000, timebase_hz=100_000_000)
    p = Probe("unused")
    p._session = SimpleNamespace(device=dev)
    p._symbols = Symbols(DWARF_O0)
    return p


def sample(time, address, value):
    return DapEvent(time, KIND_SAMPLE, True, True, 0xC, 0b001, False, address, value)


def memory_words(syms, name, size):
    """ELF の初期値から、変数を含む語（番地 → 値）。"""
    address = syms.variable(name).address
    first, end = address // 4 * 4, address + size
    data = bytearray()
    for seg in syms.segments:
        lo, hi = max(first, seg.address), min((end + 3) // 4 * 4, seg.address + len(seg.data))
        if lo < hi:
            data = seg.data[lo - seg.address:hi - seg.address]
    return {first + i: int.from_bytes(data[i:i + 4], "little") for i in range(0, len(data), 4)}


def test_expressions_become_slots_and_words_become_typed_values(probe):
    syms = probe._symbols
    set_ = probe.watch_set(["sensors[1].mode", "big", "negative", "0x20000400:8"], rate_hz=10_000)
    mode = set_["watches"][0]
    assert mode["type"] == "Mode" and mode["size"] == 4
    assert set_["watches"][1]["size"] == 8 and len(set_["slots"]) <= 4 and FakeEventLog.started == 1
    dev = probe.session.device
    assert dev.csr[g.SAMPLER_PERIOD] == 10_000
    # 1 巡回ぶんの語（big は 2 語、negative は 1 バイト）
    mode_at = int(mode["address"], 16)
    words = {mode_at: 1, **memory_words(syms, "big", 8), **memory_words(syms, "negative", 1),
             0x20000400: 0x11223344, 0x20000404: 0x55667788}
    FakeEventLog.events = [sample(1000 + i, a, v) for i, (a, v) in enumerate(words.items())]
    values = {v["name"]: v["value"] for v in probe.watch_values()["values"]}
    assert values["sensors[1].mode"] == {"enum": "MODE_RUN", "value": 1}
    assert values["big"] == 0x123456789ABCDEF0
    assert values["negative"] == -5
    assert values["0x20000400:8"] == 0x5566778811223344


def test_series_follows_one_variable_over_rounds(probe):
    probe.watch_set(["0x20000000"], rate_hz=1000)
    FakeEventLog.events = [sample(100_000 * k, 0x20000000, k) for k in range(5)]
    got = probe.watch_series("0x20000000")["samples"]
    assert [s["value"] for s in got] == [0, 1, 2, 3, 4] and got[1]["time_us"] == pytest.approx(1000)
    with pytest.raises(KeyError):
        probe.watch_series("nothing")


def test_values_that_are_not_in_memory_are_refused(probe):
    with pytest.raises(ValueError):
        probe.watch_set(["1 + 2"])
    probe._symbols = None
    with pytest.raises(RuntimeError):
        probe.watch_set(["counter"])


def test_bad_expressions_are_marked_and_the_rest_are_sampled(probe):
    out = probe.watch_set(["negative", "no_such_variable", "1 + 2"], rate_hz=100)
    assert [w["name"] for w in out["watches"]] == ["negative"]
    assert [r["expression"] for r in out["rejected"]] == ["no_such_variable", "1 + 2"]
    assert all(r["error"] for r in out["rejected"])
    status = probe.watch_status()
    assert [w["name"] for w in status["watches"]] == ["negative"]
    assert [r["expression"] for r in status["rejected"]] == ["no_such_variable", "1 + 2"]
    probe.watch_set(["negative"])
    assert probe.watch_status()["rejected"] == []                           # 置き直したら消える


def test_when_every_expression_is_bad_nothing_starts_and_each_error_is_named(probe):
    with pytest.raises(ValueError) as e:
        probe.watch_set(["no_such_variable", "1 + 2"])
    assert "no_such_variable" in str(e.value) and "1 + 2" in str(e.value)
    assert probe._sampler_user is None


def test_sampler_counts_are_since_the_start(probe):
    dev = probe.session.device
    dev.write(g.SAMPLER_ROUNDS, [0xFFFFFF00, 5, 7])                          # FPGA の起動からの数（回り込む直前）
    probe.watch_set(["negative"], rate_hz=100)
    dev.write(g.SAMPLER_ROUNDS, [0x00000064, 5, 9])
    status = probe.watch_status()
    assert (status["rounds"], status["errors"], status["overruns"]) == (0x164, 0, 2)
    assert status["device"] == {"rounds": 0x64, "errors": 5, "overruns": 9}
    assert status["since_start_s"] >= 0


def test_who_holds_the_sampler_is_reported_with_the_way_to_release_it(probe):
    assert probe.sampler_status() == {"user": None, "release": None, "since": None}
    probe.watch_set(["0x20000000"])
    s = probe.sampler_status()
    assert s["user"] == "watch" and s["release"] == "watch.stop" and s["since"] > 0
    with pytest.raises(RuntimeError, match="watch_stop"):
        probe._claim_sampler("profile")
    probe.watch_stop()
    assert probe.sampler_status()["user"] is None


def test_the_sampler_is_not_taken_from_another_tool(probe):
    probe._claim_sampler("rtos")
    with pytest.raises(RuntimeError, match="rtos"):
        probe.watch_set(["0x20000000"])
    assert probe.watch_stop() == {"stopped": True} and probe._sampler_user == "rtos"   # ほかの道具のサンプラは止めない
    probe._release_sampler("rtos")
    probe.watch_set(["0x20000000"])
    assert probe._sampler_user == "watch"
    probe.watch_stop()
    assert probe._sampler_user is None


# --- 拡充（U43）: ポインタをたどる式・条件つきの取得・走行中の書き込み・ターゲット側のバッファ -----------------

class RingLog:
    """イベント記録の偽物（カーソルで続きを読める）。"""
    overwritten = 0
    events: list = []
    capacity = 1 << 20

    def __init__(self, dev):
        pass

    def ensure_started(self):
        pass

    def counts(self):
        return len(RingLog.events), 0

    def read(self, last=None, end=None):
        events = RingLog.events[:end]
        return events[-last:] if last else list(events)

    def cursor(self):
        from fprobe.events import EventCursor
        return EventCursor(self)


class ClockDevice(FakeDevice):
    def __init__(self):
        super().__init__()
        self.ticks = 0

    def time(self):
        self.ticks += 1000                                    # 呼ぶたびに 10 µs 進む
        return self.ticks

    def capabilities(self):
        return SimpleNamespace(sampler=True, sys_clock_hz=100_000_000, timebase_hz=100_000_000)


@pytest.fixture
def live(monkeypatch, tmp_path):
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path))
    monkeypatch.setattr(probe_watch, "EventLog", RingLog)
    RingLog.events = []
    p = Probe("unused")
    p._session = SimpleNamespace(device=ClockDevice())
    cpu = FakeCpu(image=DWARF_O0)
    connect(p, cpu)
    p.elf_load(str(DWARF_O0))
    yield p, cpu
    p._stop_observing()


def poke32(cpu, address, value):
    cpu.write_memory_block8(address, struct.pack("<I", value))


def test_pointer_expressions_are_resolved_again_every_period(live):
    probe, cpu = live
    syms = probe._symbols
    got = probe.watch_set(["head->pos.x", "sensors[0].gain"], chase_rate_hz=1000)
    assert [w["method"] for w in got["watches"]] == ["host", "sampler"] and got["watches"][0]["address"] is None
    assert len(got["slots"]) == 1                                              # サンプラは決まった番地の式だけ
    wait_for(lambda: probe.watch_series("head->pos.x")["samples"])
    assert probe.watch_series("head->pos.x")["samples"][-1]["value"] == 10
    sensor_size = probe._watch_target("sensors[0]").size
    poke32(cpu, syms.variable("head").address, syms.variable("sensors").address + sensor_size)   # head = &sensors[1]
    wait_for(lambda: probe.watch_series("head->pos.x")["samples"][-1]["value"] == -1)
    poke32(cpu, syms.variable("head").address, 0)                              # NULL: その周期は値なし
    wait_for(lambda: "head->pos.x" in probe.watch_status()["chase_errors"])
    probe.watch_stop()
    assert probe._sampler_user is None


def test_only_pointer_expressions_do_not_take_the_sampler(live):
    probe, _ = live
    probe._claim_sampler("rtos")
    probe.watch_set(["head->mode"])
    assert probe._sampler_user == "rtos" and probe.watch_stop() == {"stopped": True}


def test_a_value_condition_starts_and_post_ends_a_capture_with_pretrigger(live):
    probe, cpu = live
    syms = probe._symbols
    probe.watch_set(["head->pos.x"], chase_rate_hz=1000)
    wait_for(lambda: len(probe.watch_series("head->pos.x")["samples"]) >= 3)
    armed = probe.watch_trigger(start={"variable": "head->pos.x", "op": "<", "value": 0}, pre_s=1.0, post_s=0.0003)
    assert armed["state"] == "armed" and armed["start"]["op"] == "<"
    sensor_size = probe._watch_target("sensors[0]").size
    poke32(cpu, syms.variable("head").address, syms.variable("sensors").address + sensor_size)
    result = probe.watch_capture(wait_s=5)
    assert result["state"] == "done" and result["reason"] == "post", result
    values = [s["value"] for s in result["samples"]["head->pos.x"]]
    assert values[0] == 10 and values[-1] == -1                                # プリトリガの分（10）から
    assert result["t_end_us"] - result["t_start_us"] == pytest.approx(300)


def test_a_cross_trigger_event_starts_a_capture_of_sampled_words(live):
    probe, _ = live
    probe.watch_set(["0x20000400"])
    RingLog.events = [sample(100 + 10 * k, 0x20000400, k) for k in range(5)]  # arm する前の語（プリトリガ）
    probe.watch_trigger(start={"event": "xtrig"}, stop={"variable": "0x20000400", "op": ">=", "value": 7},
                        pre_s=0.0000003)
    RingLog.events += [sample(160, 0x20000400, 5),
                       DapEvent(165, KIND_INJECT, True, False, 0xC, 0b001, False, 0xE000EDF0, 0xA05F0003),
                       sample(170, 0x20000400, 6), sample(180, 0x20000400, 7), sample(190, 0x20000400, 8)]
    result = probe.watch_capture(wait_s=5)
    assert result["reason"] == "stop" and result["t_start_us"] == pytest.approx(1.65)
    assert [s["value"] for s in result["samples"]["0x20000400"]] == [4, 5, 6, 7]   # 165 − 30 tick から
    with pytest.raises(ValueError):
        probe.watch_trigger(start={"variable": "nothing", "op": ">", "value": 1})


def test_writing_while_running(live):
    probe, cpu = live
    cpu.state = "running"                                                      # 止めずに書く
    done = probe.watch_write("sensors[1].mode", "MODE_IDLE")
    assert done["value"] == {"enum": "MODE_IDLE", "value": 0} and done["atomic"]
    assert probe.watch_write("head->pos.y", 5)["value"] == 5                   # ポインタをたどって書く
    bits = probe.watch_write("sensors[0].flags.mode", 3)
    assert bits["value"] == 3 and not bits["atomic"]
    assert probe.watch_write("0x20000400:2", 0x1234)["value"] == 0x1234
    assert bytes(cpu.ram[0x400:0x402]) == b"\x34\x12"
    with pytest.raises(ValueError):
        probe.watch_write("sensors[0].flags.mode", 99)
    with pytest.raises(ValueError):
        probe.watch_write("1 + 2", 1)


def test_a_target_side_buffer_is_drained_by_its_counter(live):
    probe, cpu = live
    syms = probe._symbols
    ramp, ticks = syms.variable("ramp").address, syms.variable("s_ticks").address
    started = probe.watch_buffer_start("ramp", "s_ticks", rate_hz=1000)
    assert started["length"] == 100 and started["element"] == "uint8_t"
    count = struct.unpack("<I", bytes(cpu.read_memory_block8(ticks, 4)))[0]
    for v in (11, 12, 13):                                                     # ファームが書いて数を進める
        cpu.ram[ramp - 0x20000000 + count % 100] = v
        count += 1
        poke32(cpu, ticks, count)
    wait_for(lambda: probe.watch_buffer_read(since=0)["total"] == 3)
    got = probe.watch_buffer_read()
    assert [s["value"] for s in got["samples"]] == [11, 12, 13] and [s["seq"] for s in got["samples"]] == [0, 1, 2]
    assert probe.watch_buffer_read()["samples"] == [] and got["next"] == 3
    assert probe.watch_buffer_stop()["lost"] == 0
    with pytest.raises(ValueError):
        probe.watch_buffer_start("big", "s_ticks")


def test_stopping_an_idle_watch_while_another_tool_uses_the_sampler_is_a_no_op(probe):
    """観測していない watch を止めても、ほかの道具（profile 等）がサンプラを使っていれば何もせずに成功する（止めて
    あるものを止めるのは誤りではない。2026-10-18 に XIAO で、プロファイル中の watch_stop が誤りになった）。"""
    probe._sampler_user = "profile"
    assert probe.watch_stop() == {"stopped": True}
    assert probe._sampler_user == "profile"


def test_a_sampler_left_running_by_a_previous_owner_is_stopped_when_the_device_is_opened(monkeypatch):
    """B30: 前のデーモン（落ちた・起動し直した）が動かしたままのサンプラは、誰の物でもないのに線を借り続け、
    ターゲットへの接続が No ACK で失敗した。開いたらすぐ止める。"""
    from fprobe import mcp_server
    dev = FakeDevice()
    dev.csr[g.SAMPLER_CONTROL] = 1                                 # 前の持ち主が始めたまま
    monkeypatch.setattr(mcp_server.Session, "open", classmethod(lambda cls, serial=None: SimpleNamespace(device=dev)))
    p = Probe("0001")
    assert p.session.device is dev
    assert dev.csr[g.SAMPLER_CONTROL] & 1 == 0


def test_names_label_the_watches_and_the_sample_rows(probe):
    # 番地の式に名前を付ける（システム解析の「誤り」等）。値・段の名前に使い、名前は重ねられない・数が合わないと断る
    probe._symbols = None
    probe.watch_set(["0x20040804", "0x20040808"], names=["count", "errors"])
    FakeEventLog.events = [sample(1000, 0x20040804, 7), sample(1001, 0x20040808, 2)]
    assert {v["name"]: v["value"] for v in probe.watch_values()["values"]} == {"count": 7, "errors": 2}
    assert probe._sample_name(0x20040808) == "errors"
    with pytest.raises(ValueError):
        probe.watch_set(["0x20040804", "0x20040808"], names=["x"])
    with pytest.raises(ValueError):
        probe.watch_set(["0x20040804", "0x20040808"], names=["x", "x"])


def test_names_stay_with_the_recording(probe, tmp_path, monkeypatch):
    # 記録中に付けた名前は記録の情報に残り、観測を止めた後・後から開いた記録でも段の名前になる
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path))
    monkeypatch.setattr("fprobe.mcp_server.Collector", lambda *a, **k: SimpleNamespace(
        start=lambda: None, stop=lambda: None, poll=lambda: None, obs=None, lost=0))
    probe._symbols = None
    probe.session.device.capabilities = lambda: SimpleNamespace(sampler=True, sys_clock_hz=100_000_000,
                                                                timebase_hz=100_000_000, board="b", design="d")
    probe.recording_start(name="named")
    probe.watch_set(["0x20040808"], names=["errors"])
    probe.watch_stop()
    probe._watched = []
    assert probe._sample_name(0x20040808) == "errors"
    probe.recording_stop()
