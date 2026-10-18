"""Probe の止めずに見る道具（fprobe.probe_observe）: RTT（背景の poll と貯め場）・SWO の ITM の解読・
セミホスティング（run_until_halt の中と、再開後の背景の見張り）・通信の解読（la.decode）と記録への出来事。
機器は使わない（rtt_demo の ELF を置いた偽のメモリ、偽の CPU、偽の SWO）。"""

import struct
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from pyocd.core.target import Target
from test_probe_recording import FakeDevice

from fprobe.decode.synth import Wave, uart
from fprobe.mcp_server import Probe
from fprobe.probe_observe import ByteLog, _incomplete_utf8_tail
from fprobe.symbols import Symbols

RTT_DEMO = Path(__file__).resolve().parents[1] / "firmware" / "rtt_demo" / "rtt_demo.elf"
SRAM_DEMO = Path(__file__).resolve().parents[1] / "firmware" / "sram_demo" / "sram_demo.elf"
SRAM, SRAM_SIZE = 0x20000000, 0x8000


def wait_for(condition, timeout_s=3.0):
    deadline = time.monotonic() + timeout_s
    while not condition():
        assert time.monotonic() < deadline, "時間切れ"
        time.sleep(0.005)


class FakeCpu:
    """pyOCD の CortexM の偽物: SRAM（rtt_demo の ELF を置く）と、resume のたびに次の停止点（pc, r0, r1）まで
    「走って」BKPT で止まる CPU。停止点が尽きたら走り続ける（halt で止まる）。"""

    def __init__(self, stops=(), image: Path | None = None):
        self.ram = bytearray(SRAM_SIZE)
        if image is not None:
            for seg in Symbols(image).segments:
                self.ram[seg.address - SRAM:seg.address - SRAM + len(seg.data)] = seg.data
        self.stops = list(stops)
        self.regs = {"pc": 0x20000100, "r0": 0, "r1": 0}
        self.state, self.reason = Target.State.HALTED, Target.HaltReason.USER
        self.resumes = 0

    # メモリ
    def read_memory_block8(self, address, length):
        assert SRAM <= address and address + length <= SRAM + SRAM_SIZE, hex(address)
        return list(self.ram[address - SRAM:address - SRAM + length])

    def write_memory_block8(self, address, data):
        self.ram[address - SRAM:address - SRAM + len(data)] = bytes(data)

    def flush(self):
        pass

    # コア
    def get_state(self):
        return self.state

    def get_halt_reason(self):
        return self.reason

    def read_core_register(self, name):
        return self.regs[name]

    def write_core_register(self, name, value):
        self.regs[name] = value

    def resume(self):
        self.resumes += 1
        if self.stops:
            pc, r0, r1 = self.stops.pop(0)
            self.regs.update(pc=pc, r0=r0, r1=r1)
            self.state, self.reason = Target.State.HALTED, Target.HaltReason.BREAKPOINT
        else:
            self.state = Target.State.RUNNING

    def halt(self):
        self.state, self.reason = Target.State.HALTED, Target.HaltReason.USER


class FakeSwo:
    def __init__(self, chunks):
        self.chunks = list(chunks)

    def swo_stop(self):
        pass

    def swo_start(self, baud):
        pass

    def swo_read(self):
        return self.chunks.pop(0) if self.chunks else b""


def connect(probe: Probe, cpu: FakeCpu, swo: FakeSwo | None = None) -> Probe:
    probe._debug = SimpleNamespace(target=cpu, probe=swo, close=lambda: None)
    probe.poll_s = 0.001
    return probe


@pytest.fixture
def probe(tmp_path, monkeypatch):
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path))
    p = Probe("unused")
    p._session = SimpleNamespace(device=FakeDevice())
    yield p
    if p._recorder is not None:
        p.recording_stop()
    p._stop_observing()


# --- 貯め場 -------------------------------------------------------------------------------

def test_byte_log_keeps_a_cursor_and_explicit_positions():
    log = ByteLog(limit=8)
    log.append(b"abcdef")
    assert log.read(max_bytes=4) == (b"abcd", 0, 0)
    assert log.read(since=2) == (b"cdef", 2, 0) and log.cursor == 4           # 明示の位置は既定の位置を動かさない
    log.append(b"ghijk")                                                     # 3 バイトあふれる
    assert log.read() == (b"efghijk", 4, 0)
    assert log.read(since=0) == (b"defghijk", 3, 3)                          # 捨てた分は lost


def test_incomplete_utf8_is_left_for_the_next_read():
    assert _incomplete_utf8_tail("あ".encode()[:2]) == 2
    assert _incomplete_utf8_tail("あ".encode()) == 0 and _incomplete_utf8_tail(b"ab") == 0


# --- RTT ---------------------------------------------------------------------------------

def target_writes(cpu: FakeCpu, channel, text: bytes) -> None:
    """ターゲットが上りのリングに書く（WrOff を進める）。"""
    syms = Symbols(RTT_DEMO)
    cb = syms.variable("_SEGGER_RTT").address
    desc = cb + 24 + 24 * channel
    buf, size, wr = struct.unpack("<III", bytes(cpu.read_memory_block8(desc + 4, 12)))
    for i, b in enumerate(text):
        cpu.ram[buf - SRAM + (wr + i) % size] = b
    cpu.write_memory_block8(desc + 12, struct.pack("<I", (wr + len(text)) % size))


def test_rtt_is_polled_in_the_background_and_read_by_cursor(probe):
    cpu = FakeCpu(image=RTT_DEMO)
    connect(probe, cpu)
    probe.elf_load(str(RTT_DEMO))
    started = probe.rtt_start()
    assert started["address"] == Symbols(RTT_DEMO).variable("_SEGGER_RTT").address
    assert [c["name"] for c in started["up"]] == ["Terminal", "Data"]
    target_writes(cpu, 0, "tick 1 あ\n".encode())
    first = probe.rtt_read(timeout_s=2)
    assert first["data"] == "tick 1 あ\n" and first["start"] == 0
    target_writes(cpu, 1, b"\x01\x02")
    assert probe.rtt_read(channel=1, encoding="hex", timeout_s=2)["data"] == "0102"
    assert probe.rtt_read(timeout_s=0)["data"] == ""                           # 既定の位置は進んでいる
    assert probe.rtt_read(since=0)["data"] == "tick 1 あ\n"                     # 画面は位置を持って読み直せる
    status = probe.rtt_status()
    assert status["running"] and status["up"][0]["received"] == first["next"] and status["polls"] > 0


def test_rtt_start_says_at_once_that_the_elf_has_no_rtt(probe):
    cpu = FakeCpu(image=RTT_DEMO)
    connect(probe, cpu)
    probe.elf_load(str(SRAM_DEMO))                                           # RTT を組み込んでいない ELF
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="_SEGGER_RTT"):
        probe.rtt_start()
    assert time.monotonic() - started < 1.0                                  # RAM を走査しない
    probe.rtt_start(ranges=[[SRAM, SRAM_SIZE]])                              # 範囲を渡せば探す（ELF が古い等）
    probe.rtt_stop()


def test_rtt_write_goes_to_the_down_ring(probe):
    cpu = FakeCpu(image=RTT_DEMO)
    connect(probe, cpu)
    probe.rtt_start(ranges=[[SRAM, SRAM_SIZE]])                                # ELF なし: 走査
    assert probe.rtt_write(text="echo\n")["written"] == 5
    syms = Symbols(RTT_DEMO)
    assert bytes(cpu.ram[syms.variable("down0_buffer").address - SRAM:][:5]) == b"echo\n"
    assert probe.rtt_stop() == {"stopped": True} and not probe.rtt_status()["running"]
    with pytest.raises(RuntimeError):
        probe.rtt_write(text="x")
    with pytest.raises(ValueError):
        probe.rtt_write()


def test_rtt_lines_and_data_are_recorded(probe):
    cpu = FakeCpu(image=RTT_DEMO)
    connect(probe, cpu)
    probe.elf_load(str(RTT_DEMO))
    probe.recording_start(name="rtt")
    probe.rtt_start()
    target_writes(cpu, 0, b"one\ntw")
    target_writes(cpu, 1, b"\xff")
    probe.rtt_read(timeout_s=2)
    wait_for(lambda: probe.rtt_status()["up"][1]["received"] == 1)
    lines = probe.recording_find({"stream": "rtt", "kind": "line"})["events"]
    assert [e["body"]["text"] for e in lines] == ["one"]                       # 改行の来ていない "tw" はまだ
    data = probe.recording_find({"stream": "rtt", "kind": "data"})["events"]
    assert data[0]["body"] == {"channel": 1, "hex": "ff"}


def test_disconnect_stops_rtt(probe):
    connect(probe, FakeCpu(image=RTT_DEMO))
    probe.rtt_start(ranges=[[SRAM, SRAM_SIZE]])
    probe.target_disconnect()
    assert not probe.rtt_status()["running"]


# --- セミホスティング ----------------------------------------------------------------------

BKPT_AB = b"\xab\xbe"
SYS_WRITE0, SYS_EXIT_EXTENDED = 0x04, 0x20


def semihosting_cpu() -> FakeCpu:
    """0x20000200 の BKPT で SYS_WRITE0("hello\\n")、0x20000210 の BKPT で SYS_EXIT_EXTENDED（終了コード 3）。"""
    cpu = FakeCpu(stops=[(0x20000200, SYS_WRITE0, 0x20000400), (0x20000210, SYS_EXIT_EXTENDED, 0x20000500)])
    cpu.write_memory_block8(0x20000200, BKPT_AB)
    cpu.write_memory_block8(0x20000210, BKPT_AB)
    cpu.write_memory_block8(0x20000400, b"hello\n\0")
    cpu.write_memory_block8(0x20000500, struct.pack("<II", 0x20026, 3))
    return cpu


def test_run_until_halt_services_semihosting_until_exit(probe):
    cpu = semihosting_cpu()
    connect(probe, cpu)
    stop = probe.run_until_halt(timeout_s=2)
    assert stop["reason"] == "exit" and stop["exit_code"] == 3
    assert cpu.regs["pc"] == 0x20000210                                       # 終了の要求では進めない
    assert probe.rtt_read(channel="semihosting")["data"] == "hello\n"
    status = probe.semihosting_status()
    assert status["requests"] == 2 and status["exit"] == {"code": 3, "reason": 0x20026}


def test_disabled_semihosting_leaves_the_breakpoint_stop(probe):
    connect(probe, semihosting_cpu())
    probe.semihosting_configure(enabled=False)
    assert probe.run_until_halt(timeout_s=2)["reason"] == "breakpoint"


def test_after_resume_semihosting_is_serviced_in_the_background(probe):
    cpu = semihosting_cpu()
    connect(probe, cpu)
    probe.target_resume()
    wait_for(lambda: probe.semihosting_status()["exit"] is not None)
    assert probe.rtt_read(channel="semihosting")["data"] == "hello\n" and cpu.resumes == 2
    assert not probe.semihosting_status()["watching"]                          # 終了したら見張りをやめる


def test_files_are_off_unless_a_root_is_given(probe, tmp_path):
    assert probe.semihosting_status()["file_root"] is None
    status = probe.semihosting_configure(file_root=str(tmp_path), writable=True)
    assert status["file_root"] == str(tmp_path.resolve()) and status["writable"]


def test_semihosting_stdin_comes_from_rtt_write(probe):
    assert probe.rtt_write(channel="semihosting", text="ab")["written"] == 2
    assert probe._stdin(1) == b"a" and probe._stdin(5) == b"b" and probe._stdin(5) == b""


# --- SWO（ITM） ---------------------------------------------------------------------------

SYNC = b"\x00" * 5 + b"\x80"
STIMULUS = b"".join(b"\x01" + bytes([c]) for c in b"hi\nyo")                   # ポート 0 に 1 バイトずつ
SYSTICK_ENTER = b"\x0e\x0f\x10"                                                # 例外トレース: 15 に入った


def test_swo_decode_returns_packets_and_lines(probe):
    connect(probe, FakeCpu(), FakeSwo([SYNC + STIMULUS[:4], STIMULUS[4:] + SYSTICK_ENTER]))
    probe.swo_start()
    out = probe.swo_decode(timeout_s=0.03)
    assert out["text"] == {0: ["hi"]} and out["synced"]
    exc = next(p for p in out["packets"] if p["type"] == "ExceptionTrace")
    assert (exc["number"], exc["name"], exc["action"]) == (15, "SysTick", "enter")
    assert sum(p["type"] == "Instrumentation" for p in out["packets"]) == 5


def test_swo_text_keeps_partial_lines(probe):
    connect(probe, FakeCpu(), FakeSwo([SYNC + STIMULUS]))
    probe.swo_start()
    assert probe.swo_text(timeout_s=0.02) == {"port": 0, "lines": ["hi"], "pending": "yo"}


def test_swo_packets_are_recorded(probe):
    connect(probe, FakeCpu(), FakeSwo([SYNC + SYSTICK_ENTER + STIMULUS]))
    probe.recording_start(name="swo")
    probe.swo_start()
    probe.swo_decode(timeout_s=0.02)
    exc = probe.recording_find({"stream": "swo", "kind": "exception"})["events"]
    assert exc[0]["a"] == 15 and exc[0]["body"]["name"] == "SysTick"
    assert probe.recording_find({"stream": "swo", "kind": "line"})["events"][0]["body"] == {"port": 0, "text": "hi"}
    assert len(probe.recording_find({"stream": "swo", "kind": "stimulus"})["events"]) == 5


# --- 通信の解読 -----------------------------------------------------------------------------

BAUD = 115200


def uart_capture(data=b"OK\r\n", trigger_s=0.0):
    w = Wave(channels=10, sample_rate_hz=BAUD * 16, idle={5: 1})
    end = uart(w, 5, data, 2 / BAUD, baud=BAUD, bad_parity=())
    tr = w.build(end_s=end + 2 / BAUD, trigger_s=trigger_s)
    return tr


def test_la_decode_the_last_capture_with_paging_and_filters(probe):
    with pytest.raises(RuntimeError):
        probe.la_decode("uart", {"channel": 5})
    probe._keep_capture(uart_capture(), "la.capture")
    out = probe.la_decode("uart", {"channel": 5, "baud": BAUD})
    assert out["capture"]["id"] == 1 and out["errors"] == 0
    data = [a["value"] for a in out["annotations"] if a["kind"] == "data"]
    assert bytes(data) == b"OK\r\n"
    page = probe.la_decode("uart", {"channel": 5, "baud": BAUD}, kinds=["data"], offset=1, limit=2)
    assert [a["value"] for a in page["annotations"]] == list(b"K\r") and page["matched"] == 4
    assert probe.la_decode("uart", {"channel": 5, "baud": BAUD}, errors_only=True)["matched"] == 0
    with pytest.raises(ValueError):
        probe.la_decode("uart", {"chanel": 5})                                 # 設定の名前の誤り
    with pytest.raises(ValueError):
        probe.la_decode("no_such_protocol", {})
    with pytest.raises(KeyError):
        probe.la_decode("uart", {"channel": 5}, capture_id=7)


def test_la_decode_annotations_are_put_on_the_common_timeline(probe):
    probe.recording_start(name="decode")
    tr = uart_capture()
    probe._keep_capture(tr, "la.capture")
    out = probe.la_decode("uart", {"channel": 5, "baud": BAUD}, kinds=["data"])
    first = out["annotations"][0]
    events = probe.recording_find({"stream": "annotation", "kind": "uart.data"})["events"]
    assert [e["a"] for e in events] == list(b"OK\r\n")
    tps = tr.sample_period_s * 100_000_000
    assert events[0]["t"] == tr.trigger_time - round(tr.trigger * tps) + round(first["start"] * tps)
    probe.la_decode("uart", {"channel": 5, "baud": BAUD})                      # 同じ解読は二度入れない
    assert len(probe.recording_find({"stream": "annotation", "kind": "uart.data"})["events"]) == 4


# --- RTT をサンプラで起こす・速さの計測（U50） ------------------------------------------------

class FakeEventLog:
    """イベント記録の偽物（全インスタンスで 1 つのリング）。"""
    events: list = []
    capacity = 1 << 20

    def __init__(self, dev):
        pass

    def ensure_started(self):
        pass

    def counts(self):
        return len(FakeEventLog.events), 0

    def read(self, last=None, end=None):
        events = FakeEventLog.events[:end]
        return events[-last:] if last else list(events)

    def cursor(self):
        from fprobe.events import EventCursor
        return EventCursor(self)


class CountingCpu(FakeCpu):
    reads = 0

    def read_memory_block8(self, address, length):
        self.reads += 1
        return super().read_memory_block8(address, length)


@pytest.fixture
def sampled(tmp_path, monkeypatch):
    from fprobe import probe_observe
    from test_sampler import FakeDevice as SamplerDevice
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path))
    monkeypatch.setattr(probe_observe, "EventLog", FakeEventLog)
    monkeypatch.setattr(probe_observe, "RTT_RESYNC_S", 30.0)
    FakeEventLog.events = []
    dev = SamplerDevice()
    dev.capabilities = lambda: SimpleNamespace(sampler=True, sys_clock_hz=100_000_000, timebase_hz=100_000_000)
    p = Probe("unused")
    p._session = SimpleNamespace(device=dev)
    cpu = CountingCpu(image=RTT_DEMO)
    connect(p, cpu)
    p.elf_load(str(RTT_DEMO))
    yield p, cpu, dev
    p._stop_observing()


def wroff_sample(cpu, channel):
    """サンプラが上りの WrOff を読んだイベント（いまのターゲットの値）。"""
    from fprobe.events import KIND_SAMPLE, DapEvent
    at = Symbols(RTT_DEMO).variable("_SEGGER_RTT").address + 24 + 24 * channel + 12
    value = struct.unpack("<I", bytes(cpu.read_memory_block8(at, 4)))[0]
    return DapEvent(len(FakeEventLog.events), KIND_SAMPLE, True, True, 0xC, 0b001, False, at, value)


def test_the_sampler_wakes_rtt_only_when_a_write_offset_moves(sampled):
    from fprobe import generated as g
    probe, cpu, dev = sampled
    started = probe.rtt_start(wake="sampler", sampler_rate_hz=10_000)
    cb = Symbols(RTT_DEMO).variable("_SEGGER_RTT").address
    assert started["wake"]["mode"] == "sampler" and probe._sampler_user == "rtt"
    assert {a for a, cfg in dev.table.values() if cfg & 1} == {cb + 24 + 12, cb + 48 + 12}   # 上り 2 つの WrOff
    assert dev.csr[g.SAMPLER_PERIOD] == 10_000
    wait_for(lambda: probe.rtt_status()["wake"]["idle"] > 3)
    target_writes(cpu, 0, b"hello\n")
    reads = cpu.reads
    wait_for(lambda: probe.rtt_status()["wake"]["idle"] > 10)
    assert cpu.reads == reads and probe.rtt_read()["data"] == ""               # 変化を知らない間は読まない
    FakeEventLog.events += [wroff_sample(cpu, 1), wroff_sample(cpu, 0)]
    assert probe.rtt_read(timeout_s=2)["data"] == "hello\n"
    status = probe.rtt_status()
    assert status["wake"]["wakes"] == 1 and status["stats"]["hinted_polls"] == 1
    with pytest.raises(RuntimeError, match="rtt"):
        probe.watch_set(["0x20000000"])                                       # サンプラは 1 つ
    probe.rtt_stop()
    assert probe._sampler_user is None and dev.csr[g.SAMPLER_CONTROL] & 1 == 0


def test_lost_sampler_events_fall_back_to_reading_the_descriptors(sampled):
    probe, cpu, _ = sampled
    probe.rtt_start(wake="sampler")
    target_writes(cpu, 0, b"x")
    probe._rtt_wake.cursor.lost += 1                                          # リングが一周して読めなかった
    FakeEventLog.events.append(wroff_sample(cpu, 1))
    assert probe.rtt_read(timeout_s=2)["data"] == "x"
    assert probe.rtt_status()["wake"]["full_polls"] >= 1


def test_the_sampler_cannot_be_taken_from_watch(sampled):
    probe, _, _ = sampled
    probe._claim_sampler("watch")
    with pytest.raises(RuntimeError, match="watch"):
        probe.rtt_start(wake="sampler")
    assert probe._sampler_user == "watch" and not probe.rtt_status()["running"]
    with pytest.raises(ValueError):
        probe.rtt_start(wake="interrupt")


def test_rtt_measure_reports_rates_and_read_counts(probe):
    cpu = FakeCpu(image=RTT_DEMO)
    connect(probe, cpu)
    probe.elf_load(str(RTT_DEMO))
    with pytest.raises(RuntimeError):
        probe.rtt_measure(duration_s=0.01)
    probe.rtt_start(interval_s=0.001)
    target_writes(cpu, 0, b"a" * 100)
    m = probe.rtt_measure(duration_s=0.2)
    assert m["bytes"] == 100 and m["up"][0]["bytes"] == 100 and m["bytes_per_s"] > 0
    assert m["polls"] > 0 and 0 < m["empty_poll_ratio"] < 1 and m["reads"] >= m["polls"]
    assert 0 < m["efficiency"] < 1 and m["wake"]["mode"] == "poll"
