"""fprobe.dwarf.inspector / cexpr: 呼び出し履歴と各フレームの変数、C の式の評価と書き込み（偽のスタックと ELF の初期値）。"""

from pathlib import Path

import pytest
from dwarf_stack import StackBuilder
from value_kinds import ints, varray, venum, vfloat, vint, vptr, vstruct

from fprobe.dwarf.cexpr import ExpressionError
from fprobe.dwarf.info import DebugInfo
from fprobe.dwarf.inspector import Inspector
from fprobe.symbols import Symbols

FIRMWARE = Path(__file__).resolve().parents[1] / "firmware" / "dwarf_demo"
SOURCE = (FIRMWARE / "dwarf_demo.c").read_text(encoding="utf-8").splitlines()


def line_of(text: str) -> int:
    return next(i for i, line in enumerate(SOURCE, start=1) if text in line)


def values(variables):
    return {v["name"]: v["value"] for v in variables}


@pytest.fixture
def o0():
    """-O0: reset_handler → walk(3) → walk(2) → walk(1) → walk(0) → leaf_sum(beta, 0) → probe_point(1234) で止まる。"""
    info, syms = DebugInfo(FIRMWARE / "dwarf_demo_O0.elf"), Symbols(FIRMWARE / "dwarf_demo_O0.elf")
    sensors = syms.variable("sensors").address
    b = StackBuilder(syms)
    b.enter("reset_handler", lr=0xFFFFFFFF)
    b.call("reset_handler", "walk", sensors, 3)
    for depth in (2, 1, 0):
        b.call("walk", "walk", sensors + 80, depth)
    b.call("walk", "leaf_sum", sensors + 80, 0)
    pc = b.call("leaf_sum", "probe_point", 1234)
    return Inspector(info, b.halt_at(pc), symbols=syms), sensors


def test_backtrace_names_lines_and_arguments_of_every_frame(o0):
    insp, sensors = o0
    trace = insp.backtrace(variables=True)
    assert [f["function"] for f in trace] == ["probe_point", "leaf_sum", "walk", "walk", "walk", "walk",
                                               "reset_handler"]
    assert trace[0]["line"] == line_of("sink = marker;") and trace[0]["file"] == "dwarf_demo.c"
    assert trace[1]["line"] == line_of("probe_point(total + corner.x);")
    assert trace[1]["offset"] == trace[1]["pc"] - insp.symbols.symbol("leaf_sum").address
    assert values(trace[0]["arguments"]) == {"marker": vint(1234)}
    assert values(trace[1]["arguments"]) == {"s": vptr(sensors + 80, "const Sensor", symbol="sensors+80"),
                                             "depth": vint(0)}
    assert [values(f["arguments"])["depth"] for f in trace[2:6]] == ints(0, 1, 2, 3)
    assert values(trace[5]["arguments"])["s"]["symbol"] == "sensors"
    assert [v["name"] for v in trace[1]["locals"]] == ["total", "corner", "ratio"]   # i は for の外では見えない
    assert trace[2]["locals"] == []                                   # deeper のブロックの外（leaf_sum を呼ぶ所）
    assert trace[3]["locals"][0]["name"] == "deeper" and trace[3]["locals"][0]["value"]["kind"] == "unavailable"
    assert all(f["cfa"] for f in trace) and trace[0]["note"] == ""


def test_locals_can_be_written_and_read_in_a_caller_frame(o0):
    insp, _ = o0
    insp.write("total", 600, frame=1)
    insp.write("ratio", 1.5, frame=1)
    insp.write("corner.x", -3, frame=1)
    insp.write("corner.y", 4, frame=1)
    local = values(insp.frame_variables(1)["locals"])
    assert local == {"total": vint(600), "corner": vstruct(x=vint(-3), y=vint(4)), "ratio": vfloat(1.5)}
    assert insp.read("total + corner.x", frame=1)["value"] == vint(597)
    assert insp.read("s->name", frame=1)["value"]["string"] == "beta"
    assert insp.read("depth + 1", frame=5)["value"] == vint(4)
    assert insp.read("visits", frame=2)["type"] == "int"                              # 関数の中の static
    assert insp.read("visits", frame=2)["value"]["kind"] == "unavailable"             # .bss は ELF に無い


@pytest.mark.parametrize("expression, expected", [
    ("head->samples[1][2]", vint(-6)),
    ("sensors[1].name", varray(ints(98, 101, 116, 97, 0, 0, 0, 0), string="beta")),
    ("head->next->pos.y", vint(2)),
    ("sensors[0].flags.level", vint(-3)),
    ("sensors[0].flags.wide", vint(0x123)),
    ("(unsigned char)sensors[0].pos.y", vint(236)),
    ("*(struct Point *)&sensors[1].pos", vstruct(x=vint(-1), y=vint(2))),
    ("matrix[2][3]", vint(23)),
    ("matrix[1]", varray(ints(10, 11, 12, 13))),
    ("greeting[4]", vint(ord("o"))),
    ("*message", vint(ord("h"))),
    ("message[7]", vint(ord("f"))),
    ("&sensors[1] - &sensors[0]", vint(1)),
    ("-negative", vint(5)),
    ("~0", vint(-1)),
    ("!enabled", vint(0)),
    ("(int)sensors[0].gain", vint(1)),
    ("(float)3", vfloat(3.0)),                                       # 浮動小数は型で float（JSON の 3 でも）
    ("sensors[0].mode", venum("MODE_RUN", 1)),
    ("sensors[0].raw16", vint(0xBEEF)),
    ("sensors[0].pair[1]", vint(-66)),
    ("sensors[0].word.f", vfloat(1.0)),
    ("s_ticks", vint(7)),
    ("'A' + 1", vint(66)),
    ("0x10 - 0b11", vint(13)),
])
def test_expressions(o0, expression, expected):
    insp, _ = o0
    assert insp.read(expression)["value"] == expected


def test_expression_types_addresses_and_pointers(o0):
    insp, sensors = o0
    head = insp.read("*head")
    assert head["type"] == "Sensor" and head["address"] == sensors
    assert head["value"]["members"]["name"]["string"] == "alpha"
    pointer = insp.read("&sensors[1]")
    assert pointer["type"] == "Sensor *" and pointer["value"]["address"] == sensors + 80
    assert "address" not in pointer
    assert insp.read("head + 1")["value"] == vptr(sensors + 80, "Sensor", symbol="sensors+80")
    assert insp.read("callback")["value"]["symbol"] == "square"
    assert insp.read("message")["value"]["string"] == "hello, fprobe"
    assert insp.read("walk::visits")["type"] == "int"
    assert insp.read("sensors[0].samples")["type"] == "int32_t[2][3]"
    assert insp.read("ramp", max_elements=3)["value"] == varray(ints(1, 2, 0), length=100)


@pytest.mark.parametrize("expression", [
    "nothing", "head.pos", "sensors[2]", "*sensors[0].gain", "&1", "sensors[", "1 +", "sensors[0].nope",
    "(struct Point)sensors[0]", "head->", "1 $ 2",
])
def test_bad_expressions_say_why(o0, expression):
    insp, _ = o0
    with pytest.raises((ExpressionError, KeyError)):
        insp.read(expression)


def test_writes_to_globals_including_enums_and_bitfields(o0):
    insp, _ = o0
    insp.write("sensors[0].gain", 2.5)
    insp.write("sensors[0].mode", "MODE_FAULT")
    insp.write("sensors[0].flags.level", -8)
    insp.write("head->samples[0][1]", 42)
    assert insp.read("sensors[0].gain")["value"] == vfloat(2.5)
    assert insp.read("sensors[0].mode")["value"] == venum("MODE_FAULT", -2)
    assert insp.read("sensors[0].flags")["value"] == vstruct(ready=vint(1), mode=vint(5), level=vint(-8),
                                                             wide=vint(0x123), raw=vint(0xAB))
    assert insp.read("sensors[0].samples[0]")["value"] == varray(ints(1, 42, 3))
    with pytest.raises(ValueError):
        insp.write("sensors[0].flags.level", 8)                     # 4 ビットの符号付きは -8〜7
    with pytest.raises(ValueError):
        insp.write("negative", 200)
    with pytest.raises(ExpressionError):
        insp.write("1 + 1", 3)


def test_type_info(o0):
    insp, _ = o0
    point = insp.type_info("struct Point")
    assert point == {"name": "struct Point", "size": 4, "kind": "struct", "members": [
        {"name": "x", "type": "int16_t", "offset": 0}, {"name": "y", "type": "int16_t", "offset": 2}]}
    flags = insp.type_info("struct Flags")["members"]
    assert flags[2] == {"name": "level", "type": "int", "offset": 0, "bit_size": 4, "bit_offset": 4}
    assert insp.type_info("Mode")["enumerators"] == {"MODE_IDLE": 0, "MODE_RUN": 1, "MODE_FAULT": -2}


@pytest.mark.parametrize("name", ["dwarf_demo_O1.elf", "dwarf_demo_O1_dwarf4.elf"])
def test_o1_variables_in_registers_pieces_and_entry_values(name):
    info, syms = DebugInfo(FIRMWARE / name), Symbols(FIRMWARE / name)
    sensors = syms.variable("sensors").address
    b = StackBuilder(syms)
    b.enter("reset_handler", lr=0xFFFFFFFF)
    b.call("reset_handler", "walk", sensors, 2)
    b.call("walk", "leaf_sum", sensors + 80, 2)
    b.set(r4=2, r5=600, s16=0x40400000)                              # depth → r4、total → r5、ratio → s16（3.0）
    insp = Inspector(info, b.halt_at(b.call("leaf_sum", "probe_point", 602)), symbols=syms)
    frame = insp.frame_variables(1)
    assert frame["function"] == "leaf_sum"
    arguments, local = values(frame["arguments"]), values(frame["locals"])
    assert arguments["depth"] == vint(2) and "entry value" in arguments["s"]["reason"]
    assert local == {"total": vint(600), "corner": vstruct(x=vint(2), y=vint(-2)), "ratio": vfloat(3.0)}
    assert values(insp.frame_variables(0)["arguments"]) == {"marker": vint(602)}
    assert insp.read("depth", frame=1)["value"] == vint(2)
    with pytest.raises(ExpressionError):
        insp.read("&depth", frame=1)                                 # レジスタの値に番地は無い
