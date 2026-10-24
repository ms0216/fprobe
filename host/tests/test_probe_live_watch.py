"""ライブ ウォッチ（M1）: 走らせたまま式を読む（debug.evaluate の live。大域・静的とポインタの先）と、ELF だけでの
式の検査（debug.expression.check。型と式の誤り）。機器は使わない（走っている偽のターゲットと dwarf_demo の ELF）。"""

from pathlib import Path

import pytest
from test_probe_inspect import BusTarget, connect
from value_kinds import vint

from fprobe.dwarf.cexpr import ExpressionError
from fprobe.mcp_server import Probe
from fprobe.symbols import Symbols

DWARF_O0 = Path(__file__).resolve().parents[1] / "firmware" / "dwarf_demo" / "dwarf_demo_O0.elf"


class RunningTarget(BusTarget):
    """走っているコア: メモリは読めるが、レジスタは読めない。"""

    def read_core_register(self, name):
        raise RuntimeError("core is not halted")

    def read_core_register_raw(self, name):
        raise RuntimeError("core is not halted")


@pytest.fixture
def running():
    return connect(Probe("unused"), RunningTarget.from_elf(DWARF_O0), DWARF_O0)


def test_live_evaluate_reads_globals_and_statics_while_running(running):
    assert running.evaluate("negative", live=True)["value"] == vint(-5)
    out = running.evaluate("matrix[1][2]", live=True)
    assert out["value"] == vint(12) and out["type"] == "uint16_t"
    assert running.evaluate("s_ticks", live=True)["value"] == vint(7)
    assert running.evaluate("*message", live=True)["value"] == vint(ord("h"))    # ポインタの先も読む


def test_live_evaluate_explains_that_locals_need_a_halt(running):
    with pytest.raises(ExpressionError, match="止まっているとき"):
        running.evaluate("total", live=True)


def test_check_gives_the_type_without_the_target():
    p = Probe("unused")
    p._symbols = Symbols(DWARF_O0)
    out = p.expression_check("matrix[1][2]")
    assert out["type"] == "uint16_t" and out["live"] is True and out["address"] > 0
    assert p.expression_check("enabled")["type"] == "_Bool"
    chase = p.expression_check("*message")
    assert chase["live"] is True and chase["needs_memory"] is True


def test_check_reports_expression_errors_from_the_elf_alone():
    p = Probe("unused")
    p._symbols = Symbols(DWARF_O0)
    with pytest.raises(ExpressionError, match="止まっているとき"):
        p.expression_check("no_such_variable")
    with pytest.raises(ExpressionError) as e:
        p.expression_check("matrix[1] +")
    assert "止まっているとき" not in str(e.value)                       # 文法の誤りに、ローカル変数の案内は付けない
    with pytest.raises(ExpressionError):
        p.expression_check("enabled.x")                                  # 型の誤り（構造体ではない）


def test_check_needs_an_elf():
    with pytest.raises(RuntimeError, match="ELF"):
        Probe("unused").expression_check("x")
