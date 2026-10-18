"""スタックの使用量（fprobe.stackusage、U26）: .su の読み、呼び出しグラフからの静的な最大（再帰・関数ポインタ・
可変長の確保は上限不明）、根（リセット・例外・呼ばれない関数）と MSP の見積もり、塗りと実測。"""

from pathlib import Path

import pytest

from fprobe.callgraph import CallGraph
from fprobe.disasm import Disassembler
from fprobe.stackusage import SuEntry, analyze_stack, load_su, measure_stack, paint_range, parse_su

DEMO = Path(__file__).resolve().parents[1] / "firmware" / "stack_demo"


@pytest.fixture(scope="module")
def graph():
    return CallGraph.build(Disassembler.from_elf(DEMO / "stack_demo.elf"))


@pytest.fixture(scope="module")
def su():
    return load_su([DEMO])


def test_parse_su_of_gcc_and_clang():
    text = ("main.c:12:5:int ns::f(int)\t16\tstatic\n"
            "stack_demo.c:51:dynamic_buf\t16\tdynamic\n"
            "C:\\src\\a.c:3:1:g\t8\tdynamic,bounded\n\n")
    assert parse_su(text, "x.su") == [SuEntry("main.c", 12, "int ns::f(int)", 16, "static", "x.su"),
                                      SuEntry("stack_demo.c", 51, "dynamic_buf", 16, "dynamic", "x.su"),
                                      SuEntry("C:\\src\\a.c", 3, "g", 8, "dynamic,bounded", "x.su")]
    with pytest.raises(ValueError, match="x.su:1"):
        parse_su("nonsense\n", "x.su")


def test_load_su_searches_directories(su):
    assert {e.function for e in su} >= {"leaf", "middle", "dynamic_buf"} and su[0].source.endswith("stack_demo.su")


def test_static_maximum_through_the_call_graph(graph, su):
    report = analyze_stack(graph, su)
    f = {r["function"]: r for r in report["functions"]}
    assert f["leaf"]["max"] == 32 and f["leaf"]["bounded"] and f["leaf"]["frame_source"] == "su"
    assert f["middle"]["max"] == 112 and f["middle"]["path"] == ["middle", "leaf"]
    assert f["tail"]["max"] == 112 and f["tail"]["path"] == ["tail", "middle", "leaf"]   # 末尾の呼び出しは枠を残さない
    assert not f["depth"]["bounded"] and any("再帰" in r for r in f["depth"]["reasons"]) and f["depth"]["max"] == 24
    assert not f["apply"]["bounded"] and any("関数ポインタ" in r for r in f["apply"]["reasons"])
    assert not f["dynamic_buf"]["bounded"] and any("可変" in r for r in f["dynamic_buf"]["reasons"])
    main = f["main"]
    assert not main["bounded"] and main["max"] == 112                                 # 分かる分の下限
    assert set(main["unknown_from"]) == {"depth", "apply", "dynamic_buf"}
    assert report["cycles"] == [["depth"]]


def test_roots_and_the_msp_estimate(graph, su):
    report = analyze_stack(graph, su)
    roots = {r["root"]: r for r in report["roots"]}
    assert roots["Reset_Handler"]["kind"] == "reset"
    systick = roots["SysTick_Handler"]
    assert systick["kind"] == "exception" and systick["exceptions"] == [15] and systick["max"] == 8 + 32
    assert systick["total"] == 40 + 36                                               # 例外の枠（FPU なし 32 + 揃え 4）
    assert roots["Default_Handler"]["exceptions"] == [2, 3, 4, 5, 6, 11, 12, 14]
    assert roots["main"]["kind"] == "uncalled" and roots["op_inc"]["kind"] == "address_taken"
    msp = report["msp"]
    assert msp["exception_frame"] == 36 and not msp["bounded"]
    assert msp["one_level"] == 112 + 76 and msp["all_nested"] == 112 + 76 + 36


def test_hints_resolve_function_pointers(graph, su):
    hinted = {r["function"]: r for r in analyze_stack(graph, su, indirect={"apply": ["op_double"]})["functions"]}
    apply = hinted["apply"]
    assert apply["bounded"] and apply["max"] == 8 and apply["path"] == ["apply", "op_double"]
    assumed = {r["function"]: r for r in analyze_stack(graph, su, assume_address_taken=True)["functions"]}
    assert assumed["apply"]["bounded"] and assumed["apply"]["max"] == 8
    with pytest.raises(KeyError, match="nothing"):
        analyze_stack(graph, su, indirect={"apply": ["nothing"]})


def test_without_su_the_disassembly_is_used(graph):
    f = {r["function"]: r for r in analyze_stack(graph)["functions"]}
    assert f["middle"]["frame_source"] == "disasm" and f["middle"]["max"] == 112
    assert not f["dynamic_buf"]["bounded"]


def test_su_frame_overrides_and_bounded_dynamic(graph):
    su = [SuEntry("stack_demo.c", 22, "middle", 96, "static"), SuEntry("stack_demo.c", 51, "dynamic_buf", 40,
                                                                     "dynamic,bounded")]
    report = analyze_stack(graph, su, exception_frame=108)
    f = {r["function"]: r for r in report["functions"]}
    assert f["middle"]["frame"] == 96 and f["middle"]["max"] == 96 + 32             # 呼び出しは .su の枠の深さで
    assert f["dynamic_buf"]["bounded"] and f["dynamic_buf"]["max"] == 40
    assert report["msp"]["exception_frame"] == 108
    assert any("食い違" in n for n in report["notes"])


# --- 塗り・実測 ----------------------------------------------------------------------------------

class Ram:
    def __init__(self, base, size, fill=0):
        self.base, self.data = base, bytearray([fill]) * size

    def read(self, address, length):
        return bytes(self.data[address - self.base:address - self.base + length])

    def write(self, address, data):
        self.data[address - self.base:address - self.base + len(data)] = data


def test_paint_range_stays_below_the_live_stack():
    assert paint_range(0x2000_0000, 0x2000_1000, sp=0x2000_0F80, margin=64) == (0x2000_0000, 0x2000_0F40)
    assert paint_range(0x2000_0000, 0x2000_1000, sp=None, margin=64) == (0x2000_0000, 0x2000_1000)
    assert paint_range(0x2000_0000, 0x2000_1000, sp=0x2000_1000, margin=64) == (0x2000_0000, 0x2000_1000)   # リセット直後
    with pytest.raises(ValueError, match="SP"):
        paint_range(0x2000_0000, 0x2000_1000, sp=0x2000_0010, margin=64)


def test_measure_painted_stack():
    ram = Ram(0x2000_0000, 0x1000, 0xCD)
    ram.write(0x2000_0C00, b"\x01" * 0x400)                     # 上の 1 KB を使った
    m = measure_stack(ram.read, 0x2000_0000, 0x2000_1000, 0xCD, sp=0x2000_0F00, threshold=0.2)
    assert m["used"] == 0x400 and m["untouched"] == 0xC00 and m["fraction"] == 0.25 and m["current"] == 0x100
    assert m["warning"] and not m["overflow"]
    ram.write(0x2000_0000, b"\x00" * 16)                        # 底まで届いた
    over = measure_stack(ram.read, 0x2000_0000, 0x2000_1000, 0xCD)
    assert over["overflow"] and over["used"] == 0x1000 and over["current"] is None
