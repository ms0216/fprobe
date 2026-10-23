"""テストポイントの試験用ファーム（host/firmware/testpoint_demo）: リポジトリの ELF・対応表がソースと合うこと、
DWARF が元のソースの行を指すこと、同じソースをホストで動かした ID の列が解析で期待どおりになること。"""

from pathlib import Path

import pytest
from testpoint_helpers import DATA, needs_zig, parse_log, run_instrumented

from fprobe.symbols import Symbols
from fprobe.decode.synth import Wave
from fprobe.testpoint import Hit, Options, ProbeTable, WatchVar, analyze, instrument_source, synth
from fprobe.testpoint.decode import decode_gpio_parallel

DEMO = Path(__file__).resolve().parents[1] / "firmware" / "testpoint_demo"
OPTIONS = Options(variables=(WatchVar("state"),))
EMITTERS = {"serial": "fprobe_tp_ser_send", "parallel": "fprobe_tp_par_send", "itm": "fprobe_tp_itm_send"}


def test_map_matches_source():
    table = ProbeTable.load(DEMO / "testpoints.json")
    previous = {f.key: f for f in table.functions}
    src = (DEMO / "src" / "demo.c").read_text(encoding="utf-8")
    _, probes, infos, result, next_id = instrument_source(src, "demo.c", options=OPTIONS, next_id=table.next_id,
                                                         previous=previous)
    assert result.reused == result.instrumented                   # 指紋がすべて同じ: 対応表はソースと一致
    assert {p.id: p for p in probes} == table.probes and next_id == table.next_id
    assert {f.name for f in infos} == {"fib", "validate", "classify", "sum_until", "parse", "step"}


@pytest.mark.parametrize("variant", sorted(EMITTERS))
def test_elf_has_emitter_and_points_to_original_lines(variant):
    syms = Symbols(DEMO / f"testpoint_demo_{variant}.elf")
    assert syms.symbol(EMITTERS[variant]).size > 0
    table = ProbeTable.load(DEMO / "testpoints.json")
    for name in ("fib", "parse", "step"):
        loc = syms.describe(syms.symbol(name).address)
        assert Path(str(loc.file)).name == "demo.c"
        assert loc.line == table.one(function=name, kind="entry").line     # #line で元の行（写しの +2 ではない）


@needs_zig
def test_demo_runs_on_host_and_analyzes(tmp_path):
    result, out = run_instrumented(tmp_path, [DEMO / "src" / "demo.c"], OPTIONS, main=DATA / "demo_main.c")
    log = parse_log(out)
    table = result.table
    report = analyze([Hit(k * 100, i, v) for k, (i, v) in enumerate(log)], table, hz=1e6)
    # 経験則の「いつもより長い」の印は付けない（long_s を渡したときだけ）。ほかの異常も無い
    assert report.anomalies == []
    assert report.functions["step"].calls == 20
    fib = report.functions["fib"]
    assert fib.calls > 20 and max(c.depth for c in report.calls if c.function == "fib") >= 6   # 再帰の深さ
    lines = (DEMO / "src" / "demo.c").read_text(encoding="utf-8").splitlines()
    parse_exits = {table[i].line for i, _ in log if table[i].function == "parse" and table[i].kind == "exit"}
    assert {lines[n - 1].strip() for n in parse_exits} == {"return value;", "return -1;"}   # 正常・goto の出口
    states = {v for i, v in log if table[i].kind == "var"}
    assert {1, 1234, 0xFFFFFFFF} <= states
    cov = report.coverage
    assert cov.functions_hit == cov.functions_total == 6
    uncovered = {(p.function, p.kind, p.label) for p in cov.uncovered}
    # 20 回では validate の v > 100 と、parse の s == 0（いつも文字列を渡す）は通らない
    assert ("validate", "then", "if") in uncovered and ("parse", "then", "if") in uncovered
    assert ("classify", "then", "if") in uncovered                      # inject_delay は 0 のまま


@needs_zig
def test_demo_log_through_parallel_waveform(tmp_path):
    """同じ ID の列を、試験用ファームの並列の配線（ch1・ch5・ch6 = D0..D2、ch4 = STB）の波形にして解読する。"""
    _, out = run_instrumented(tmp_path, [DEMO / "src" / "demo.c"], OPTIONS, main=DATA / "demo_main.c")
    log = parse_log(out)[:300]
    wave = Wave(8, 100e6, jitter=2e-9, seed=5)
    records = [(1e-6 + k * 3e-6, i, v) for k, (i, v) in enumerate(log)]
    synth.gpio_parallel(wave, records, [1, 5, 6], 4, store_s=62.5e-9)        # 16 MHz で 1 書き込み 1 クロック
    dec = decode_gpio_parallel(wave.build(end_s=records[-1][0] + 5e-6), [1, 5, 6], 4)
    assert [(h.id, h.value) for h in dec.hits] == log and not dec.losses
