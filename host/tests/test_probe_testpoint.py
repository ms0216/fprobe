"""Probe のテストポイントの道具（fprobe.probe_testpoint）: 挿入、ロジアナ（深いバッファ・流し続ける捕捉）・SWO（ITM）・
RTT での受け取り、途中と終わりの要約、呼び出しの時間軸、カバレッジ、記録への書き出し。
機器は使わない（偽のデバイス・深いバッファ・ストリーム・イベント記録のリング・ターゲット・RTT）。"""

import dataclasses
import time
from array import array
from types import SimpleNamespace

import pytest
import test_la_stream as las
from probe_fakes import DictMemory, FakeDevice, FakeTarget, use_fake_ring

from fprobe import collector, probe_testpoint, probe_window
from fprobe.decode.synth import Wave
from fprobe.mcp_server import Probe
from fprobe.profile import dwt
from fprobe.testpoint import Probe as TP
from fprobe.testpoint import ProbeTable, synth

HZ = 100_000_000
TABLE = ProbeTable.of([
    TP(1, "app.c", 10, "main", "entry"), TP(2, "app.c", 20, "main", "exit"),
    TP(3, "app.c", 30, "work", "entry"), TP(4, "app.c", 40, "work", "exit"),
    TP(5, "app.c", 33, "work", "then", decision=5, label="if"),
    TP(6, "app.c", 33, "work", "else", decision=5, label="else", implicit=True),
    TP(7, "app.c", 35, "work", "var", variable="state", label="state"),
])


def program(n: int, start: float = 1e-6, step: float = 10e-6) -> list[tuple[float, int, int | None]]:
    """main の中で work を n 回（then の枝・変数 state の書き込みつき）。(秒, ID, 値)。"""
    out = [(start, 1, None)]
    t = start + step
    for k in range(n):
        out += [(t, 3, None), (t + step, 5, None), (t + 2 * step, 7, k), (t + 3 * step, 4, None)]
        t += 4 * step
    return out + [(t, 2, None)]


@pytest.fixture
def table_path(tmp_path):
    path = tmp_path / "testpoints.json"
    TABLE.save(path)
    return path


@pytest.fixture
def probe(tmp_path, monkeypatch):
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path / "home"))
    use_fake_ring(monkeypatch, probe_window, collector)
    p = Probe("unused")
    p._session = SimpleNamespace(device=FakeDevice())
    yield p
    run = p._tp_run
    if run is not None and run.pump is not None:
        p._close_testpoint()
    if p._recorder is not None:
        p.recording_stop()


# --- 挿入 --------------------------------------------------------------------------------------

def test_instrument_writes_copies_and_the_map_and_keeps_ids(probe, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "app.c").write_text("int g;\nint work(int x) {\n  if (x) g = x;\n  return g;\n}\n"
                               "int main(void) { return work(1); }\n", encoding="utf-8")
    first = probe.testpoint_instrument(str(src), str(tmp_path / "tp"), variables=["g"])
    assert first["probes"] > 4 and first["map"] == str((tmp_path / "tp" / "testpoints.json").resolve())
    assert first["files"][0]["instrumented"] == ["work", "main"] and not first["files"][0]["error"]
    assert "FPROBE_TP" in (tmp_path / "tp" / "app.c").read_text(encoding="utf-8")
    again = probe.testpoint_instrument(str(src), str(tmp_path / "tp"), variables=["g"])
    assert again["files"][0]["reused"] == ["work", "main"] and again["probes"] == first["probes"]
    assert probe._tp_map == (tmp_path / "tp" / "testpoints.json").resolve()


# --- ロジアナ: 深いバッファ ------------------------------------------------------------------------

class FakeDeep:
    """DeepAnalyzer の偽物: start の引数を残し、読むと用意した Transitions を返す。"""
    last = None
    tr = None

    def __init__(self, dev):
        self.dev = dev
        FakeDeep.last = self

    def start(self, *, rate_hz=None, mask=0, value=0, rise=0, fall=0, rle=False, post_beats=None, pretrigger=0):
        self.started = {"rate_hz": rate_hz, "rise": rise, "rle": rle, "post_beats": post_beats}
        return FakeDeep.tr.sample_period_s

    def wait(self, timeout_s=10.0):
        self.waited = timeout_s

    def read_transitions(self, before_beats=None, after_beats=None):
        self.before_beats = before_beats
        return FakeDeep.tr


def serial_capture(records, rate=50e6, trigger_time=7_000_000):
    wave = Wave(8, rate, idle={4: 1, 5: 1})
    end = synth.gpio_serial(wave, records, clock_channel=4, data_channel=5)
    tr = wave.build(end_s=end + 1e-6, trigger_s=records[0][0])
    return dataclasses.replace(tr, trigger_time=trigger_time)


def test_gpio_serial_deep_capture_is_decoded_analyzed_and_recorded(probe, monkeypatch, table_path):
    monkeypatch.setattr(probe_testpoint, "DeepAnalyzer", FakeDeep)
    FakeDeep.tr = serial_capture(program(5))
    probe.recording_start(name="tp")
    started = probe.testpoint_start("gpio_serial", map_path=str(table_path), clock_channel=4, data_channel=5, guard_samples=1,
                                    rate_hz=50e6, after_beats=1000)
    assert started["running"] and started["capture"] == "deep" and started["hz"] == pytest.approx(50e6)
    assert FakeDeep.last.started == {"rate_hz": 50e6, "rise": 1 << 4, "rle": True, "post_beats": 1000}
    stopped = probe.testpoint_stop()
    assert not stopped["running"] and stopped["hits"] == 22 and stopped["losses"] == 0
    assert FakeDeep.last.before_beats == 0
    work = next(f for f in stopped["functions"] if f["name"] == "work")
    assert work["calls"] == 5 and work["inclusive"]["mean_s"] == pytest.approx(30e-6, rel=0.01)
    assert stopped["coverage"]["c0"] == [5, 5] and stopped["coverage"]["c1"] == [1, 2]
    assert probe._capture is FakeDeep.tr                                  # la_view・記録の波形にも
    # 時刻の写し: トリガのサンプルがトリガ時刻（タイムベース 100 MHz、サンプル 50 MHz）
    tr = FakeDeep.tr
    offset = tr.trigger_time - tr.trigger * 2
    assert stopped["time_map"] == {"timebase_hz": HZ, "offset": offset, "uncertainty_ticks": 0}
    calls = probe.recording_find({"stream": "testpoint", "kind": "call", "body": {"function": "work"}})
    assert calls["count"] == 5
    first_work = probe.testpoint_calls(function="work", limit=1)["calls"][0]
    assert calls["events"][0]["t"] == first_work["t"]
    report = probe.testpoint_report()
    assert report["variables"][0]["last"] == 4 and not report["running"]


def test_gpio_parallel_deep_without_trigger_starts_at_once(probe, monkeypatch, table_path):
    monkeypatch.setattr(probe_testpoint, "DeepAnalyzer", FakeDeep)
    wave = Wave(8, 100e6)
    end = synth.gpio_parallel(wave, program(2), [1, 5, 6], strobe_channel=4)
    FakeDeep.tr = wave.build(end_s=end + 1e-6)
    probe.testpoint_start("gpio_parallel", map_path=str(table_path), data_channels=[1, 5, 6], strobe_channel=4,
                          trigger=False)
    assert FakeDeep.last.started["rise"] == 0
    assert probe.testpoint_stop()["hits"] == 10


# --- ロジアナ: 流し続ける捕捉 -----------------------------------------------------------------------

class StreamDevice(las.FakeStreamDevice):
    def __init__(self, source, **kw):
        super().__init__(source, **kw)
        self.ticks = 5000

    def capabilities(self):
        return SimpleNamespace(mem_bytes=128 << 20, la_channels=16, la_sample_hz=100_000_000, timebase_hz=HZ)

    def time(self):
        self.ticks += 2
        return self.ticks


@pytest.fixture
def fast_stream(monkeypatch):
    monkeypatch.setattr("fprobe.la.LaStream.READ_MIN", 0)                 # 偽の機器は短いパケットで少しずつ返す


def raw_beats(tr) -> bytes:
    samples = array("H")
    bounds = list(tr.starts[1:]) + [tr.length]
    for v, s, e in zip(tr.values, tr.starts, bounds):
        samples.extend(array("H", [v]) * (e - s))
    samples.extend(array("H", [samples[-1]]) * (-len(samples) % 8))
    return samples.tobytes()


def test_gpio_parallel_stream_runs_in_the_background_until_stopped(probe, table_path, fast_stream):
    import random
    wave = Wave(8, 100e6)
    records = program(30)
    end = synth.gpio_parallel(wave, records, [1, 5, 6], strobe_channel=4)
    dev = StreamDevice(raw_beats(wave.build(end_s=end + 2e-6)), rng=random.Random(1), per_poll=5000)
    probe._session = SimpleNamespace(device=dev)
    started = probe.testpoint_start("gpio_parallel", map_path=str(table_path), data_channels=[1, 5, 6],
                                    strobe_channel=4, capture="stream", rle=False)
    assert started["capture"] == "stream" and started["time_map"] == {"timebase_hz": HZ, "offset": 5003,
                                                                      "uncertainty_ticks": 1.0}
    deadline = time.monotonic() + 5
    while probe.testpoint_status()["hits"] < len(records) - 1 and time.monotonic() < deadline:   # 最後は flush で
        time.sleep(0.01)
    middle = probe.testpoint_report()                                    # 流している最中の要約
    assert middle["running"] and middle["functions_total"] == 2
    stopped = probe.testpoint_stop()
    assert stopped["hits"] == len(records) and stopped["losses"] == 0 and "error" not in stopped
    assert not dev.running and stopped["stream"]["samples"] > 0
    calls = probe.testpoint_calls(function="main")["calls"]
    # 時刻: 流し始め（offset）+ サンプル番号（100 MS/s = タイムベース）
    assert calls[0]["t"] == 5003 + round((records[0][0] + 50e-9) * 100e6)


def test_stream_with_a_duration_ends_by_itself(probe, table_path, fast_stream):
    import random
    wave = Wave(8, 100e6)
    end = synth.gpio_parallel(wave, program(1), [1, 5, 6], strobe_channel=4)
    dev = StreamDevice(raw_beats(wave.build(end_s=end + 1e-6)), rng=random.Random(2), per_poll=5000)
    probe._session = SimpleNamespace(device=dev)
    probe.testpoint_start("gpio_parallel", map_path=str(table_path), data_channels=[1, 5, 6], strobe_channel=4,
                          capture="stream", rle=False, duration_s=0.05)
    probe._tp_run.pump.join(60)                   # 終わらないことの見張り（時間の判定ではない）
    assert not probe._tp_run.pump.is_alive() and not dev.running
    assert probe.testpoint_stop()["hits"] == 6


# --- SWO（ITM） --------------------------------------------------------------------------------

class FakeSwo:
    def __init__(self, target):
        self.target = target
        self.started: list[tuple[int, int]] = []           # (速度, その時点のターゲットへの書き込みの数)

    def swo_stop(self):
        pass

    def swo_start(self, baud):
        self.started.append((baud, len(self.target.write_log)))

    def swo_read(self):
        return b""


@pytest.fixture
def itm_probe(probe):
    memory = DictMemory({dwt.ITM_TER: 0x1})                    # ファームが printf（ポート 0）を有効にしている
    target = FakeTarget(memory, halted=False)
    probe._debug = SimpleNamespace(target=target, probe=FakeSwo(target), close=lambda: None)
    return probe


def itm_records(records, port=1):
    return synth.itm_bytes([(0, i, v) for _, i, v in records], port=port, timestamps=False, sync=False)


def test_itm_enables_the_port_only_after_the_receiver_and_restores_it(itm_probe, table_path):
    p = itm_probe
    target, dev = p.target, p.session.device
    started = p.testpoint_start("itm", map_path=str(table_path), swo_baud=2_000_000, core_clock_hz=16e6)
    log = target.write_log
    (baud, at_start), = p._debug.probe.started
    assert baud == 2_000_000
    ter = [(k, v) for k, (a, v) in enumerate(log) if a == dwt.ITM_TER]
    assert ter[0][1] == 0 and ter[0][0] < at_start                      # 全ポートを止めてから受信を始め
    assert ter[1] == (len(log) - 1, 0x3) and ter[1][0] >= at_start      # その後でポート 1（と元のポート 0）を有効に
    assert any(a == dwt.TPIU_ACPR for a, _ in log)                       # core_clock_hz があれば TPIU も
    t0 = started["window"]["t0"]
    dev.swo(t0 + 1000, itm_records(program(3)), spacing=50)
    middle = p.testpoint_report()
    assert middle["running"] and middle["hits"] == 14
    dev.swo(dev.ticks + 100, itm_records([(0, 3, None), (0, 4, None)]), spacing=50)
    stopped = p.testpoint_stop()
    assert stopped["hits"] == 16 and stopped["losses"] == 0
    assert log[-1] == (dwt.ITM_TER, 0x1)                                 # 元に戻す
    calls = p.testpoint_calls(function="main")["calls"]
    assert calls[0]["t"] == t0 + 1000                                    # FPGA がバイトに付けた時刻
    assert stopped["time_map"]["uncertainty_ticks"] == pytest.approx(10 / 2e6 * HZ)


def test_itm_needs_a_swo_speed_and_a_valid_port(itm_probe, table_path):
    with pytest.raises(RuntimeError, match="swo_baud"):
        itm_probe.testpoint_start("itm", map_path=str(table_path))
    assert itm_probe.target.write_log == []                              # 断るときは何も書かない
    with pytest.raises(ValueError):
        itm_probe.testpoint_start("itm", map_path=str(table_path), port=40, swo_baud=2_000_000)


def test_itm_reads_from_the_recording_and_writes_testpoint_events(itm_probe, table_path):
    p = itm_probe
    p.recording_start(name="itm")
    started = p.testpoint_start("itm", map_path=str(table_path), swo_baud=2_000_000)
    assert started["window"]["source"] == "recording"
    dev = p.session.device
    dev.swo(started["window"]["t0"] + 500, itm_records(program(2)), spacing=50)
    assert p.testpoint_report()["hits"] == 10                            # 記録から新しい分だけ読む
    dev.swo(dev.ticks + 100, itm_records([(0, 3, None), (0, 4, None)]), spacing=50)
    stopped = p.testpoint_stop()
    assert stopped["hits"] == 12 and stopped["recorded"]
    found = p.recording_find({"stream": "testpoint", "kind": "call", "body": {"function": "work"}})
    assert found["count"] == 3


def test_itm_is_pulled_in_the_background_so_a_small_ring_does_not_lose(itm_probe, table_path, monkeypatch):
    # 記録していないときはイベント記録のリングから読む。背景で周期的に新しい分だけ読むので、リングが何周しても欠けない
    monkeypatch.setattr(probe_testpoint, "ITM_PULL_S", 0.01)
    p = itm_probe
    dev = p.session.device
    dev.ring_capacity = 64
    p.testpoint_start("itm", map_path=str(table_path), swo_baud=2_000_000)
    run = p._tp_run
    for n in range(6):
        hits_before = run.live.hits
        dev.swo(dev.ticks + 100, itm_records(program(1)), spacing=50)        # 6 記録・28 バイト
        deadline = time.monotonic() + 5
        while run.live.hits < hits_before + 5 and time.monotonic() < deadline:
            time.sleep(0.005)
    stopped = p.testpoint_stop()
    assert stopped["hits"] == 36 and stopped["losses"] == 0 and stopped["window"]["lost_events"] == 0


def test_itm_start_failure_puts_the_ports_back(itm_probe, table_path):
    p = itm_probe
    def broken(baud):
        raise OSError("USB")
    p._debug.probe.swo_start = broken
    with pytest.raises(OSError):
        p.testpoint_start("itm", map_path=str(table_path), swo_baud=2_000_000)
    assert p.target.write_log[-1] == (dwt.ITM_TER, 0x1) and p._tp_run is None


def test_deep_capture_that_never_fills_says_what_to_check(probe, monkeypatch, table_path):
    class NeverDone(FakeDeep):
        def wait(self, timeout_s=10.0):
            raise TimeoutError("トリガが成立しない")
    monkeypatch.setattr(probe_testpoint, "DeepAnalyzer", NeverDone)
    FakeDeep.tr = serial_capture(program(1))
    probe.testpoint_start("gpio_serial", map_path=str(table_path), clock_channel=4, data_channel=5, guard_samples=1)
    with pytest.raises(TimeoutError, match="after_beats"):
        probe.testpoint_stop(timeout_s=0.01)
    assert probe._tp_run is None and probe.testpoint_status()["running"] is False


def test_itm_duration_makes_stop_wait(itm_probe, table_path):
    p = itm_probe
    p.testpoint_start("itm", map_path=str(table_path), swo_baud=2_000_000, duration_s=0.05)
    begun = time.monotonic()
    assert p.testpoint_stop()["duration_s"] == 0.05
    assert time.monotonic() - begun >= 0.04


# --- RTT ---------------------------------------------------------------------------------------

def test_rtt_hits_from_the_receiver_with_a_host_clock_anchor(probe, table_path):
    p = probe
    p._rtt = SimpleNamespace(control_block=SimpleNamespace(up=[SimpleNamespace(index=0), SimpleNamespace(index=1)]))
    p._debug = SimpleNamespace(target=None, close=lambda: None)
    with pytest.raises(ValueError, match="core_clock_hz"):
        p.testpoint_start("rtt", map_path=str(table_path))
    with pytest.raises(ValueError, match="チャンネル 3"):
        p.testpoint_start("rtt", map_path=str(table_path), core_clock_hz=16e6, rtt_channel=3)
    p.testpoint_start("rtt", map_path=str(table_path), core_clock_hz=16e6)
    records = [(round(t * 16e6), i, v) for t, i, v in program(3)]
    data = synth.rtt_bytes(records)
    host_s = time.monotonic()
    p._received(1, b"\x01\x02\x03\x04\x05" + data[:20], host_s)           # 受け始めは記録の途中
    p._received(0, data, host_s)                                         # 別のチャンネルは見ない
    p._received(1, data[20:], host_s + 0.01)
    stopped = p.testpoint_stop()
    assert stopped["hits"] == len(records) and stopped["losses"] == 0
    assert stopped["cyccnt_wraps"] == 0                                  # CYCCNT の回り込みを仮定した回数
    assert stopped["skipped_before_sync"] >= 1                           # 受け始めの途中で捨てた項目の数（?51）
    tm = p._tp_last.live.time_map
    assert tm.hz == 16e6 and tm.uncertainty >= p.poll_s * HZ
    h, ticks, _ = p._tp_last.host_clock
    last_of_first = records[1][0]                                        # 最初のかたまり（20 バイト）の最後の記録
    assert tm.offset == ticks + round((host_s - h) * HZ) - round(last_of_first * HZ / 16e6)
    assert p._rtt_listeners == []                                        # 止めたら受け取りをやめる


# --- 誤りと状態 ----------------------------------------------------------------------------------

def test_start_errors_and_status(probe, monkeypatch, table_path):
    assert probe.testpoint_status() == {"running": False}
    with pytest.raises(RuntimeError, match="testpoint_instrument"):
        probe.testpoint_start("gpio_serial", clock_channel=4, data_channel=5)
    with pytest.raises(ValueError, match="method"):
        probe.testpoint_start("swd", map_path=str(table_path))
    with pytest.raises(ValueError, match="clock_channel"):
        probe.testpoint_start("gpio_serial", map_path=str(table_path))
    with pytest.raises(ValueError, match="capture"):
        probe.testpoint_start("gpio_serial", map_path=str(table_path), clock_channel=4, data_channel=5, guard_samples=1,
                              capture="forever")
    with pytest.raises(RuntimeError, match="testpoint_start"):
        probe.testpoint_report()
    with pytest.raises(RuntimeError, match="testpoint_start"):
        probe.testpoint_stop()
    monkeypatch.setattr(probe_testpoint, "DeepAnalyzer", FakeDeep)
    FakeDeep.tr = serial_capture(program(1))
    probe.testpoint_start("gpio_serial", map_path=str(table_path), clock_channel=4, data_channel=5, guard_samples=1)
    with pytest.raises(RuntimeError, match="testpoint_stop"):
        probe.testpoint_start("gpio_serial", map_path=str(table_path), clock_channel=4, data_channel=5, guard_samples=1)
    status = probe.testpoint_status()
    assert status["running"] and status["settings"]["clock_channel"] == 4 and status["hits"] == 0
    probe.testpoint_stop()
    cov = probe.testpoint_coverage(function="work")
    assert cov["uncovered_total"] == 1 and cov["uncovered"][0]["kind"] == "else"


def test_the_api_accepts_the_usual_calls():
    from fprobe.api.spec import InvalidParams, load_spec
    spec = load_spec()
    spec.validate_params("testpoint.instrument", {"source_dir": "src", "out_dir": "tp", "variables": ["state"]})
    spec.validate_params("testpoint.start", {"method": "gpio_serial", "clock_channel": 4, "data_channel": 5, "guard_samples": 1,
                                             "rate_hz": 50e6, "after_beats": 100000})
    spec.validate_params("testpoint.start", {"method": "gpio_parallel", "data_channels": [1, 5, 6],
                                             "strobe_channel": 4, "capture": "stream", "duration_s": 3600})
    spec.validate_params("testpoint.start", {"method": "itm", "swo_baud": 2000000, "core_clock_hz": 16e6})
    spec.validate_params("testpoint.start", {"method": "rtt", "core_clock_hz": 84e6, "rtt_channel": 1})
    spec.validate_params("testpoint.calls", {"t0_s": 0.5, "function": "fib", "limit": 50})
    for bad in ({"method": "swd"}, {"method": "gpio_parallel", "data_channels": [1]}, {"method": "itm", "port": 32}):
        with pytest.raises(InvalidParams):
            spec.validate_params("testpoint.start", bad)
    assert spec.methods["testpoint.report"].observes and spec.methods["testpoint.instrument"].kind == "read"


def test_the_mixin_does_not_shadow_other_tools():
    # mixin の名前が他の mixin と重なると、MRO で黙って片方が隠れる（_start_itm で一度起きた）
    own = {n for n in vars(probe_testpoint.TestpointTools) if not n.startswith("__")}
    for base in Probe.__mro__:
        if base not in (probe_testpoint.TestpointTools, object):
            assert not own & set(vars(base)), (base.__name__, own & set(vars(base)))
