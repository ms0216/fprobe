"""fprobe.code_at と recording.code_at: 記録の時刻 t → その前後の PC のサンプル・止まった出来事・動いていたタスク。

記録を合成して確かめる（機器は使わない。ELF は同梱の sram_demo.elf）。行は推定しない: 直前・直後の事実と隙間の幅
だけを返し、材料の無いもの（PC のサンプル・ELF・ターゲットの状態・RTOS の時間軸）は missing に理由を返す。
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from fprobe.code_at import code_at
from fprobe.mcp_server import Probe
from fprobe.profile.dwt import DWT_PCSR, PCSR_INVALID
from fprobe.recording import Recorder, Recording
from fprobe.recording_long import Policy, SegmentedRecorder, SegmentedRecording
from fprobe.symbols import Symbols

HZ = 100_000_000
SRAM_DEMO = Path(__file__).resolve().parents[1] / "firmware" / "sram_demo" / "sram_demo.elf"
COUNTER = 0x20000010
LINE17, LINE19 = 0x20000054, 0x2000006A            # sram_demo.c の tick の中（17 行・19 行）


@pytest.fixture(scope="module")
def symbols():
    return Symbols(SRAM_DEMO)


def make(tmp_path, rows, meta=None) -> Recording:
    rec = Recorder(tmp_path / "rec", HZ, meta=meta)
    rec.add_many(rows)
    rec.close()
    return Recording.open(tmp_path / "rec")


def pcsr(t, value, body=None):
    return ("sample", "word", t, DWT_PCSR, value, body)


def test_the_samples_just_before_and_after_with_their_lines_and_the_gap(tmp_path, symbols):
    r = make(tmp_path, [pcsr(1000, LINE17), ("sample", "word", 1400, COUNTER, 7), pcsr(2000, LINE19), pcsr(3000, LINE17)])
    out = code_at(r, 1500, symbols, str(SRAM_DEMO))
    before, after = out["pc_before"], out["pc_after"]
    assert (before["t"], before["pc"], before["function"], before["file"], before["line"]) == (1000, LINE17, "tick",
                                                                                               "sram_demo.c", 17)
    assert (after["t"], after["pc"], after["line"]) == (2000, LINE19, 19)
    assert before["dt_s"] == pytest.approx(-500 / HZ) and after["dt_s"] == pytest.approx(500 / HZ)
    assert out["pc_gap_s"] == pytest.approx(1000 / HZ)               # この間に何を実行したかは記録に無い
    assert out["t"] == 1500 and out["timebase_hz"] == HZ and out["elf"] == str(SRAM_DEMO)
    assert "pc" not in out["missing"] and "elf" not in out["missing"]


def test_a_sample_exactly_at_t_is_the_one_before(tmp_path, symbols):
    r = make(tmp_path, [pcsr(1000, LINE17), pcsr(2000, LINE19)])
    out = code_at(r, 2000, symbols, None)
    assert out["pc_before"]["t"] == 2000 and out["pc_before"]["dt_s"] == 0 and out["pc_after"] is None
    assert out["pc_gap_s"] is None


def test_invalid_and_failed_reads_are_returned_as_they_are(tmp_path, symbols):
    r = make(tmp_path, [pcsr(1000, PCSR_INVALID), pcsr(2000, 0, {"ack": 4})])
    out = code_at(r, 1500, symbols, None)
    assert out["pc_before"]["pc"] is None and out["pc_before"]["value"] == PCSR_INVALID
    assert out["pc_before"]["reason"] == "pcsr_invalid" and out["pc_before"]["line"] is None
    assert out["pc_after"]["pc"] is None and out["pc_after"]["reason"] == "read_failed" and out["pc_after"]["ack"] == 4


def test_without_pc_samples_or_elf_the_missing_material_is_named(tmp_path):
    r = make(tmp_path, [("sample", "word", 1000, COUNTER, 7)])
    out = code_at(r, 1500, None, None)
    assert out["pc_before"] is None and out["pc_after"] is None
    assert out["missing"]["pc"]["reason"] == "no_samples"
    assert out["missing"]["elf"]["reason"] == "no_elf"
    assert out["missing"]["state"]["reason"] == "no_state"
    assert out["missing"]["task"]["reason"] == "no_rtos"


def test_without_elf_the_samples_have_no_place(tmp_path):
    r = make(tmp_path, [pcsr(1000, LINE17)])
    out = code_at(r, 1500, None, None)
    assert out["pc_before"]["pc"] == LINE17 and out["pc_before"]["function"] is None and out["pc_before"]["line"] is None
    assert out["missing"]["elf"]["reason"] == "no_elf" and out["elf"] is None


HALTED = {"state": "HALTED", "pc": LINE19, "where": "tick+26 (sram_demo.c:19)", "function": "tick",
          "file": "sram_demo.c", "line": 19, "halt_reason": "breakpoint"}


def state_rows():
    return [("target", "state", 0, 0, 0, {"state": "RUNNING"}, None, 50.0),
            ("target", "state", 3000, 0, 0, HALTED, None, 50.0),
            ("target", "state", 4000, 0, 0, {"state": "RUNNING"}, None, 50.0),
            ("target", "state", 9000, 0, 0, {**HALTED, "pc": LINE17, "line": 17}, None, 50.0)]


def test_the_recorded_state_at_t_and_the_halts_around_it(tmp_path, symbols):
    r = make(tmp_path, state_rows())
    out = code_at(r, 3500, symbols, None)
    assert out["state"]["state"] == "HALTED" and out["state"]["t"] == 3000          # 記録の上で t のとき止まっていた
    assert out["state"]["dt_s"] == pytest.approx(-500 / HZ) and out["state"]["uncertainty_s"] == pytest.approx(50 / HZ)
    assert out["halt_before"]["pc"] == LINE19 and out["halt_before"]["line"] == 19
    assert out["halt_before"]["halt_reason"] == "breakpoint"
    assert out["halt_after"]["t"] == 9000 and out["halt_after"]["line"] == 17
    running = code_at(r, 5000, symbols, None)
    assert running["state"]["state"] == "RUNNING" and running["state"]["t"] == 4000
    assert running["halt_before"]["t"] == 3000 and running["halt_after"]["t"] == 9000
    assert "state" not in running["missing"]


def test_the_task_running_at_t_or_why_there_is_none(tmp_path, symbols):
    run = {"task": "sensor", "method": "sampled", "resolution": 100}
    r = make(tmp_path, [("rtos", "run", 1000, 0x20002060, 0, run, 2000),
                        ("rtos", "gap", 2000, 0, 0, {"reason": "trace buffer overflow"}, 2500),
                        ("rtos", "run", 3000, 0x20002000, 0, {"task": "main", "idle": False}, 4000)])
    out = code_at(r, 1500, symbols, None)
    assert out["task"] == {"t0": 1000, "t1": 2000, "handle": 0x20002060, **run}
    gap = code_at(r, 2200, symbols, None)
    assert gap["task"] is None
    assert gap["missing"]["task"] == {"reason": "gap", "t0": 2000, "t1": 2500, "detail": "trace buffer overflow"}
    none = code_at(r, 2700, symbols, None)
    assert none["task"] is None and none["missing"]["task"]["reason"] == "no_run"


def test_a_segmented_recording_finds_the_neighbours_across_segments(tmp_path, symbols):
    now = [1000.0]
    rec = SegmentedRecorder(tmp_path / "long", HZ, policy=Policy(segment_s=1.0), clock=lambda: now[0])
    rec.add_many([pcsr(10, LINE17)])
    rec.flush()
    now[0] += 5
    rec.add_many([("sample", "word", 2 * HZ, COUNTER, 1)])
    rec.flush()
    now[0] += 5
    rec.add_many([pcsr(5 * HZ, LINE19)])
    rec.flush()
    rec.close()
    r = SegmentedRecording(tmp_path / "long")
    assert len(r.segments()) >= 2
    out = code_at(r, 3 * HZ, symbols, None)
    assert out["pc_before"]["t"] == 10 and out["pc_after"]["t"] == 5 * HZ
    r.close()


# --- recording.code_at（Probe の道具） --------------------------------------------------------------------

@pytest.fixture
def probe(tmp_path, monkeypatch):
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path))
    p = Probe("unused")
    p._session = SimpleNamespace(device=None)
    yield p


def saved(tmp_path, rows, meta=None) -> str:
    rec = Recorder(tmp_path / "recordings" / "r1", HZ, meta=meta)
    rec.add_many(rows)
    rec.close()
    return "r1"


def test_the_tool_reads_the_opened_recording_with_the_loaded_elf(probe, tmp_path):
    probe.recording_open(saved(tmp_path, [pcsr(1000, LINE17), pcsr(2000, LINE19)], {"elf": "C:/build/app.elf"}))
    probe._symbols = Symbols(SRAM_DEMO)
    out = probe.recording_code_at(t=1500)
    assert out["pc_before"]["line"] == 17 and out["pc_after"]["line"] == 19
    assert out["elf"] == str(SRAM_DEMO) and out["recorded_elf"] == "C:/build/app.elf"   # 違えば使う側が気づける


def test_the_tool_takes_an_elf_by_path(probe, tmp_path):
    probe.recording_open(saved(tmp_path, [pcsr(1000, LINE17)]))
    out = probe.recording_code_at(t=1500, elf=str(SRAM_DEMO))
    assert out["pc_before"]["function"] == "tick" and out["elf"] == str(SRAM_DEMO) and out["recorded_elf"] is None


def test_the_tool_without_a_recording_is_an_error(probe):
    with pytest.raises(RuntimeError):
        probe.recording_code_at(t=0)
