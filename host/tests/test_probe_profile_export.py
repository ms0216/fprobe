"""Probe のプロファイルの集計・書き出しの道具（U18）: profile.aggregate / timeline / export、testpoint.aggregate / export。
プロファイルは止めた後の結果（pcsr の時刻付きサンプル、sram_demo.elf）、テストポイントは手で組んだ対応表と印で。"""

import struct
from pathlib import Path
from types import SimpleNamespace

import pytest
from probe_fakes import HZ, FakeDevice

from fprobe.disasm import Disassembler
from fprobe.mcp_server import Probe
from fprobe.probe_profile import _ProfileResult, _ProfileRun
from fprobe.probe_testpoint import _Run
from fprobe.profile import PcSample, PcSamples, Profiler, plan_itm_sampling, plan_pcsr_sampling
from fprobe.symbols import Symbols
from fprobe.testpoint import FunctionInfo, Hit, LiveAnalysis, Probe as TpProbe, ProbeTable

SRAM = Path(__file__).resolve().parents[1] / "firmware" / "sram_demo" / "sram_demo.elf"
PERIOD = HZ // 1000                      # 1 ms ごとのサンプル（タイムベースの刻み）
T_FIRST = 5_000_000


@pytest.fixture
def probe(tmp_path, monkeypatch):
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path))
    p = Probe("unused")
    p._session = SimpleNamespace(device=FakeDevice())
    p._symbols = Symbols(SRAM)
    yield p
    p._stop_observing()


def addr(p, name, offset=0):
    return p._symbols.symbol(name).address + offset


def set_profile(p, method="pcsr"):
    """最初の 10 ms は tick、次の 10 ms は reached_milestone と眠り（交互）。"""
    tick, milestone = addr(p, "tick"), addr(p, "reached_milestone")
    pcs = [tick] * 10 + [milestone if n % 2 else None for n in range(10)]
    if method == "pcsr":
        run = _ProfileRun("pcsr", plan_pcsr_sampling(1000))
        samples = PcSamples(tuple(PcSample(pc, T_FIRST + n * PERIOD) for n, pc in enumerate(pcs)), "pcsr")
    else:
        run = _ProfileRun("itm", plan_itm_sampling(16e6, 1000, 2_000_000), core_clock_hz=16e6, timestamps=False)
        samples = PcSamples(tuple(PcSample(pc) for pc in pcs), "itm")
    prof = Profiler(p._symbols, Disassembler.from_elf(SRAM)).profile(samples)
    p._profile_last = _ProfileResult(run, samples, prof, None)


def test_aggregate_in_a_time_window(probe):
    set_profile(probe)
    whole = probe.profile_aggregate()
    assert whole["total"] == 20 and whole["window"]["samples"] == 20 and whole["window"]["t0"] == T_FIRST
    late = probe.profile_aggregate(t0_s=0.010)
    assert late["total"] == 10 and [r["function"] for r in late["rows"]] == ["reached_milestone"]
    assert late["categories"]["sleep"]["samples"] == 5 and late["window"]["t0_s"] == 0.010
    same = probe.profile_aggregate(t0=T_FIRST + 10 * PERIOD, t1=T_FIRST + 20 * PERIOD)
    assert same["total"] == 10
    by_file = probe.profile_aggregate(by="file")
    assert by_file["rows"][0]["group"].endswith("sram_demo.c") and by_file["rows"][0]["samples"] == 15
    named = probe.profile_aggregate(by="groups", groups={"timer": ["tick"]})
    assert {r["group"]: r["samples"] for r in named["rows"]} == {"timer": 10, "(other)": 5}
    lines = probe.profile_aggregate(by="line", function="tick", t1_s=0.005)
    assert lines["by"] == "line" and sum(r["samples"] for r in lines["rows"]) == 5


def test_itm_without_timestamps_cannot_be_cut(probe):
    set_profile(probe, "itm")
    assert probe.profile_aggregate()["total"] == 20
    with pytest.raises(ValueError, match="時刻"):
        probe.profile_aggregate(t0_s=0.01)
    with pytest.raises(ValueError, match="pcsr"):
        probe.profile_aggregate(t0=1)


def test_timeline_of_the_cpu_load(probe):
    set_profile(probe)
    out = probe.profile_timeline(bucket_s=0.010, by="function", top=1)
    assert out["groups"] == ["tick"] and [b["t0_s"] for b in out["buckets"]] == [0.0, 0.010]
    assert out["buckets"][0]["load"] == 1.0 and out["buckets"][1]["load"] == 0.5
    assert out["buckets"][1]["groups"] == {"(other)": 0.5}


def test_export_gmon_callgrind_lcov_gcov_dot(probe, tmp_path):
    set_profile(probe)
    gmon = probe.profile_export("gmon", str(tmp_path / "gmon.out"))
    data = (tmp_path / "gmon.out").read_bytes()
    assert data[:4] == b"gmon" and gmon["samples"] == 15 and gmon["prof_rate"] == 1000
    assert gmon["low"] == min(addr(probe, "tick"), addr(probe, "reached_milestone"))
    assert gmon["rate_source"] == "measured"
    rate = struct.unpack("<I", data[21 + 12:21 + 16])[0]
    assert rate == 1000
    probe.profile_export("callgrind", str(tmp_path / "callgrind.out"))
    assert "fn=tick" in (tmp_path / "callgrind.out").read_text(encoding="utf-8")
    lcov = probe.profile_export("lcov", str(tmp_path / "pc.info"), source_root="/src")
    assert lcov["files"] == 1 and "SF:/src/" in (tmp_path / "pc.info").read_text(encoding="utf-8")
    gcov = probe.profile_export("gcov", str(tmp_path / "gcov"))
    assert [Path(f).name for f in gcov["written"]] == ["sram_demo.c.gcov"]
    # 既定で関数を図から落とさない（「0.5 % 未満は省く」の暗黙の既定を持たない。省くのは min_fraction を渡したとき）
    import inspect
    assert inspect.signature(probe.profile_export).parameters["min_fraction"].default == 0.0
    probe.profile_export("dot", str(tmp_path / "pc.dot"))
    assert '"tick" -> "reached_milestone"' in (tmp_path / "pc.dot").read_text(encoding="utf-8")
    window = probe.profile_export("gmon", str(tmp_path / "late.out"), t0_s=0.010)
    assert window["samples"] == 5
    with pytest.raises(ValueError, match="共有"):
        probe.profile_export("gmon", "//server/share/gmon.out")


# --- テストポイント -------------------------------------------------------------------------------

def table():
    main = "reset_handler"
    probes = [TpProbe(1, "src/a.c", 10, main, "entry"), TpProbe(2, "src/a.c", 20, main, "exit"),
              TpProbe(3, "src/a.c", 12, main, "then", decision=3), TpProbe(4, "src/a.c", 14, main, "else", decision=3),
              TpProbe(5, "src/b.c", 5, "tick", "entry"), TpProbe(6, "src/b.c", 8, "tick", "exit")]
    functions = [FunctionInfo("src/a.c", "reset_handler", 0, 10, 21, "x", True, [1, 2, 3, 4]),
                 FunctionInfo("src/b.c", "tick", 0, 5, 9, "y", True, [5, 6])]
    return ProbeTable({p.id: p for p in probes}, functions, 7)


def set_testpoint(p, hz=1e6):
    live = LiveAnalysis(table(), hz)
    live.feed([Hit(0, 1), Hit(5, 3), Hit(10, 5), Hit(20, 6), Hit(30, 5), Hit(50, 6), Hit(60, 2)])
    p._tp_last = _Run("itm", None, live, "testpoints.json", None, {}, 0.0, stopped=1.0)


def test_testpoint_aggregate_in_a_window(probe):
    set_testpoint(probe)
    whole = {r["function"]: r for r in probe.testpoint_aggregate()["rows"]}
    main = whole["reset_handler"]
    assert main["inclusive_s"] == pytest.approx(60e-6) and main["exclusive_s"] == pytest.approx(30e-6)
    window = probe.testpoint_aggregate(t0_s=25e-6, t1_s=45e-6)
    rows = {r["function"]: r for r in window["rows"]}
    assert rows["tick"]["inclusive_s"] == pytest.approx(15e-6) and rows["tick"]["started"] == 1
    assert rows["reset_handler"]["exclusive_s"] == pytest.approx(5e-6)
    grouped = probe.testpoint_aggregate(by="file")
    assert {g["group"] for g in grouped["rows"]} == {"src/a.c", "src/b.c"}


def test_testpoint_export(probe, tmp_path):
    set_testpoint(probe)
    probe.testpoint_export("callgrind", str(tmp_path / "tp.callgrind"))
    text = (tmp_path / "tp.callgrind").read_text(encoding="utf-8")
    assert "cfn=tick" in text and "calls=2 5" in text and "fl=src/a.c" in text
    out = probe.testpoint_export("lcov", str(tmp_path / "tp.info"), source_root="/proj")
    info = (tmp_path / "tp.info").read_text(encoding="utf-8")
    assert out["files"] == 2 and "SF:/proj/src/a.c" in info and "BRDA:12,3,1,0" in info
    probe.testpoint_export("dot", str(tmp_path / "tp.dot"))
    assert '"reset_handler" -> "tick"' in (tmp_path / "tp.dot").read_text(encoding="utf-8")
    gmon = probe.testpoint_export("gmon", str(tmp_path / "tp.gmon"))
    assert gmon["arcs"] == 1 and gmon["missing"] == []
    written = probe.testpoint_export("gcov", str(tmp_path / "gcov"))
    assert len(written["written"]) == 2
    with pytest.raises(ValueError, match="format"):
        probe.testpoint_export("vcd", str(tmp_path / "x"))
