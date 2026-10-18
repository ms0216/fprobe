"""プロファイル・カバレッジの書き出し（fprobe.export.profile_formats、U18）: gmon.out（gprof）・callgrind（KCacheGrind）・
lcov・gcov のテキスト・dot。PC サンプリング（sram_demo.elf）とテストポイント（手で組んだ対応表と印）から。"""

import struct
from pathlib import Path

import pytest

from fprobe.callgraph import CallGraph
from fprobe.disasm import Disassembler
from fprobe.export.profile_formats import (FileCoverage, callgrind_from_profile, callgrind_from_tree,
                                           coverage_from_profile, coverage_from_testpoint, dot_from_profile,
                                           dot_from_tree, gmon_bytes, render_gcov, render_lcov, tree_edges,
                                           write_gcov)
from fprobe.profile import PcSamples, Profiler, coverage
from fprobe.symbols import Symbols
from fprobe.testpoint import FunctionInfo, Hit, Probe, ProbeTable, analyze

SRAM = Path(__file__).resolve().parents[1] / "firmware" / "sram_demo" / "sram_demo.elf"


@pytest.fixture(scope="module")
def sram():
    symbols = Symbols(SRAM)
    dis = Disassembler.from_elf(SRAM)
    tick = symbols.symbol("tick").address
    milestone = symbols.symbol("reached_milestone").address
    pcs = [tick, tick, tick + 2, tick + 4, milestone, None, 0x1000_0000]
    prof = Profiler(symbols, dis).profile(PcSamples.from_pcs(pcs))
    return symbols, dis, prof


# --- gmon.out -----------------------------------------------------------------------------------

def parse_gmon(data: bytes):
    assert data[:4] == b"gmon" and struct.unpack("<I", data[4:8])[0] == 1 and data[8:20] == bytes(12)
    at, hist, arcs = 20, None, []
    while at < len(data):
        tag = data[at]
        at += 1
        if tag == 0:
            low, high, size, rate = struct.unpack("<IIII", data[at:at + 16])
            dimen, abbrev = data[at + 16:at + 31], data[at + 31:at + 32]
            at += 32
            bins = struct.unpack(f"<{size}H", data[at:at + 2 * size])
            at += 2 * size
            hist = (low, high, rate, dimen.rstrip(b"\0"), abbrev, bins)
        elif tag == 1:
            arcs.append(struct.unpack("<III", data[at:at + 12]))
            at += 12
        else:
            raise AssertionError(tag)
    return hist, arcs


def test_gmon_histogram_like_openocd():
    data, info = gmon_bytes([0x100, 0x100, 0x102, 0x1FE, 0x400], rate_hz=1000, low=0x100, high=0x200)
    (low, high, rate, dimen, abbrev, bins), arcs = parse_gmon(data)
    assert (low, high, rate, dimen, abbrev) == (0x100, 0x200, 1000, b"seconds", b"s") and arcs == []
    assert len(bins) == 128 and bins[0] == 2 and bins[1] == 1 and bins[127] == 1 and sum(bins) == 4
    assert info["outside"] == 1 and info["samples"] == 4 and info["scale"] == 1


def test_gmon_scales_counts_that_do_not_fit_16_bits():
    data, info = gmon_bytes([0x100] * 70000 + [0x104] * 3, rate_hz=10_000)
    (low, high, rate, _, _, bins), _ = parse_gmon(data)
    assert info["scale"] == 2 and bins[0] == 35000 and bins[2] == 2 and rate == 5000      # 時間 = 数 / 率 は保つ
    assert low == 0x100 and high > 0x104


def test_gmon_arcs():
    data, _ = gmon_bytes([], rate_hz=1, arcs=[(0x200, 0x100, 7)])
    hist, arcs = parse_gmon(data)
    assert hist is None and arcs == [(0x200, 0x100, 7)]


# --- callgrind ----------------------------------------------------------------------------------

def test_callgrind_from_pc_samples(sram):
    _, _, prof = sram
    text = callgrind_from_profile(prof, cmd="sram_demo.elf")
    lines = text.splitlines()
    assert lines[0] == "# callgrind format" and "events: Samples" in lines and "cmd: sram_demo.elf" in lines
    assert "fn=tick" in lines and "fn=reached_milestone" in lines
    costs = [int(line.split()[1]) for line in lines if line[:1].isdigit()]
    assert sum(costs) == prof.categories["code"] == 5
    assert "calls=" not in text


def test_callgrind_from_the_call_tree():
    report = _testpoint_report()
    text = callgrind_from_tree(report.tree, report.hz, file_of={"main": "src/a.c", "helper": "src/b.c"}.get,
                               line_of={"main": 10, "helper": 5}.get)
    lines = text.splitlines()
    assert "events: Ticks" in lines
    main = lines.index("fn=main")
    block = lines[main:lines.index("fn=helper")] if lines.index("fn=helper") > main else lines[main:]
    assert "cfn=helper" in block and "calls=2 5" in block
    i = block.index("calls=2 5")
    assert block[i + 1] == "10 30"                                          # helper の含む時間（10 + 20）


# --- lcov / gcov --------------------------------------------------------------------------------

def test_lcov_from_pc_samples(sram):
    symbols, dis, prof = sram
    files = coverage_from_profile(coverage(prof, dis, all_functions=True), prof)
    text = render_lcov(files, "pc", source_root="/work")
    assert text.startswith("TN:pc\nSF:/work/") and text.rstrip().endswith("end_of_record")
    assert "FN:" in text and "FNDA:4,tick" in text and "FNDA:0,default_handler" in text
    da = [line for line in text.splitlines() if line.startswith("DA:")]
    assert any(line.endswith(",0") for line in da) and sum(int(x.split(",")[1]) for x in da) == 5
    assert "LF:" in text and "LH:" in text


def _table() -> ProbeTable:
    probes = [Probe(1, "src/a.c", 10, "main", "entry"), Probe(2, "src/a.c", 20, "main", "exit"),
              Probe(3, "src/a.c", 12, "main", "then", decision=3, label="if"),
              Probe(4, "src/a.c", 12, "main", "else", decision=3, label="else", implicit=True),
              Probe(5, "src/b.c", 5, "helper", "entry"), Probe(6, "src/b.c", 8, "helper", "exit"),
              Probe(7, "src/b.c", 6, "helper", "then", decision=7), Probe(8, "src/b.c", 7, "helper", "else",
                                                                          decision=7)]
    functions = [FunctionInfo("src/a.c", "main", 0, 10, 21, "x", True, [1, 2, 3, 4]),
                 FunctionInfo("src/b.c", "helper", 0, 5, 9, "y", True, [5, 6, 7, 8])]
    return ProbeTable({p.id: p for p in probes}, functions, 9)


def _testpoint_report():
    hits = [Hit(0, 1), Hit(5, 3), Hit(10, 5), Hit(20, 6), Hit(30, 5), Hit(50, 6), Hit(60, 2)]
    return analyze(hits, _table(), hz=1e6)


def test_lcov_and_gcov_from_testpoints(tmp_path):
    report = _testpoint_report()
    files = coverage_from_testpoint(report, _table())
    a = next(f for f in files if f.path == "src/a.c")
    assert a.lines == {10: 1, 12: 1, 20: 1} and a.functions["main"].count == 1
    assert a.branches == {12: [(3, 0, 1), (3, 1, 0)]}
    b = next(f for f in files if f.path == "src/b.c")
    assert b.branches == {6: [(7, 0, None), (7, 1, None)]}                   # 判定まで来なかった
    assert b.functions["helper"].returned == 2 and b.functions["helper"].blocks == (2, 4)
    text = render_lcov(files, "tp")
    assert "SF:src/a.c" in text and "BRDA:12,3,0,1" in text and "BRDA:12,3,1,0" in text and "BRDA:6,7,0,-" in text
    assert "BRF:2\nBRH:1" in text and "FNDA:2,helper" in text
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.c").write_text("".join(f"line {n}\n" for n in range(1, 22)), encoding="utf-8")
    gcov = render_gcov(a, (source / "a.c").read_text(encoding="utf-8").splitlines())
    rows = gcov.splitlines()
    assert rows[0] == "        -:    0:Source:src/a.c"
    assert "        1:   10:line 10" in rows and "        -:   11:line 11" in rows
    assert rows[rows.index("        1:   12:line 12") + 1] == "branch  0 taken 1"
    assert "branch  1 taken 0" in rows and "function main called 1 returned 100% blocks executed 100%" in rows
    written = write_gcov(files, tmp_path / "out", source_root=tmp_path)
    assert [p.name for p in written] == ["a.c.gcov", "b.c.gcov"]
    assert "        2:    5:" in (tmp_path / "out" / "b.c.gcov").read_text(encoding="utf-8")


def test_gcov_without_the_source_lists_only_known_lines():
    fc = FileCoverage("x.c", {3: 0, 5: 2})
    rows = render_gcov(fc, None).splitlines()
    assert rows == ["        -:    0:Source:x.c", "    #####:    3:", "        2:    5:"]


# --- dot ----------------------------------------------------------------------------------------

def test_dot_from_the_call_tree():
    report = _testpoint_report()
    assert tree_edges(report.tree) == {("main", "helper"): (2, 30)}
    text = dot_from_tree(report.tree, report.functions, report.hz)
    assert text.startswith("digraph") and '"main" -> "helper"' in text and "×2" in text
    assert text.count("fillcolor") == 2


def test_dot_from_pc_samples_with_the_static_graph(sram):
    _, dis, prof = sram
    text = dot_from_profile(prof, CallGraph.build(dis))
    assert '"tick" -> "reached_milestone"' in text and "dashed" in text and '"default_handler"' not in text
