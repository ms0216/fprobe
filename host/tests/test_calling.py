"""fprobe.calling: AAPCS の引数の置き場所・戻り値の読み方と、ターゲットの関数の呼び出し（偽の Cortex-M）。"""

import struct
from pathlib import Path

import pytest

from cpu_fake import DEMCR, VC_HARDERR, VTOR, FakeCpu
from fprobe.calling import CallError, allocate, hard_float, read_return_value, signature_of
from fprobe.debug import Debugger
from fprobe.dwarf.info import DebugInfo
from fprobe.dwarf.typesys import BaseType, PointerType, VoidType
from fprobe.symbols import Symbols

ELF = Path(__file__).resolve().parents[1] / "firmware" / "dwarf_demo" / "dwarf_demo_O0.elf"
INT = BaseType("int", 4, "signed")
CHAR = BaseType("char", 1, "signed_char")
LONG = BaseType("long long", 8, "signed")
FLOAT, DOUBLE = BaseType("float", 4, "float"), BaseType("double", 8, "float")


def f32(x: float) -> int:
    return int.from_bytes(struct.pack("<f", x), "little")


@pytest.fixture(scope="module")
def info():
    return DebugInfo(ELF)


@pytest.fixture(scope="module")
def syms():
    return Symbols(ELF)


# --- AAPCS ----------------------------------------------------------------------------------------

def test_integers_go_to_r0_to_r3_then_the_stack():
    p = allocate([INT] * 6, [1, -2, 3, 4, 5, 6], hard=False)
    assert p.core == {0: 1, 1: 0xFFFFFFFE, 2: 3, 3: 4}
    assert bytes(p.stack) == (5).to_bytes(4, "little") + (6).to_bytes(4, "little")


def test_small_signed_values_are_sign_extended():
    p = allocate([CHAR, BaseType("short", 2, "signed")], [-1, -3], hard=False)
    assert p.core == {0: 0xFFFFFFFF, 1: 0xFFFFFFFD}


def test_64_bit_values_use_an_even_register_pair_or_an_aligned_stack_slot():
    p = allocate([INT, LONG], [1, 0x1122334455667788], hard=False)
    assert p.core == {0: 1, 2: 0x55667788, 3: 0x11223344}             # r1 は飛ばす
    p = allocate([INT, INT, INT, LONG, INT], [1, 2, 3, 0x1_0000_0002, 9], hard=False)
    assert p.core == {0: 1, 1: 2, 2: 3}                                 # r3 に半分は置かない
    assert bytes(p.stack) == (0x1_0000_0002).to_bytes(8, "little") + (9).to_bytes(4, "little")


def test_hard_float_uses_vfp_registers_with_back_filling():
    p = allocate([FLOAT, DOUBLE, FLOAT, INT], [1.0, 2.0, 3.0, 7], hard=True)
    assert p.vfp[0] == f32(1.0) and p.vfp[1] == f32(3.0)               # s1 は後埋め
    assert (p.vfp[2] | p.vfp[3] << 32) == 0x4000000000000000            # d1
    assert p.core == {0: 7}


def test_soft_float_passes_floats_in_core_registers():
    p = allocate([FLOAT, DOUBLE], [1.0, 2.0], hard=False)
    assert p.core == {0: f32(1.0), 2: 0, 3: 0x40000000}


def test_variadic_arguments_use_the_base_standard_and_promote_floats():
    p = allocate([PointerType(CHAR)], [0x2000_0000], hard=True, variadic=[1.5, 2])
    assert p.core[0] == 0x2000_0000 and (p.core[2] | p.core[3] << 32) == 0x3FF8000000000000
    assert not p.vfp and bytes(p.stack) == (2).to_bytes(4, "little")


def test_a_hidden_result_pointer_takes_r0():
    p = allocate([INT], [5], hard=False, hidden_result=0x2000_0100)
    assert p.core == {0: 0x2000_0100, 1: 5}


def test_values_must_fit_their_types():
    with pytest.raises(ValueError):
        allocate([CHAR], [300], hard=False)
    with pytest.raises(TypeError):
        allocate([INT], [1.5], hard=False)


def test_the_demo_elf_is_hard_float(info):
    assert hard_float(info.elf)


def test_return_values_are_read_from_the_aapcs_places(info):
    cpu = FakeCpu({}, 0)
    cpu.regs.update(r0=0xFFFFFFFB, r1=0, s0=f32(2.5))
    assert read_return_value(cpu, info, INT, True) == {"type": "int", "value": -5, "raw": 0xFFFFFFFB}
    assert read_return_value(cpu, info, FLOAT, True)["value"] == 2.5     # ハード浮動小数: s0
    assert read_return_value(cpu, info, FLOAT, False)["value"] != 2.5    # ソフト: r0
    cpu.regs.update(r0=0xFFEC000A)
    point = read_return_value(cpu, info, info.type_named("struct Point"), True)["value"]
    assert point == {"x": 10, "y": -20}
    assert read_return_value(cpu, info, VoidType(), True) == {"type": "void", "value": None}
    sensor = info.type_named("Sensor")
    assert "unavailable" in read_return_value(cpu, info, sensor, True)["value"]   # メモリで返る: 番地が要る


def test_signatures_come_from_dwarf(info, syms):
    sig = signature_of(info, syms, "leaf_sum")
    assert sig.known and [n for n, _ in sig.parameters] == ["s", "depth"] and not sig.variadic
    assert sig.address == syms.resolve("leaf_sum")
    assert signature_of(info, syms, "square").result.name == "int"
    assert not signature_of(None, syms, "square").known
    with pytest.raises(CallError):
        signature_of(info, syms, syms.resolve("leaf_sum") + 2)                      # 関数の途中


# --- 呼び出し -------------------------------------------------------------------------------------

def demo_cpu(syms, program: dict) -> FakeCpu:
    """dwarf_demo の番地に偽の命令を置く。ベクタ表は 0x2000_0000（リセットの入口 = reset_handler = 踏み台）。
    止まっている場所は walk の先頭。"""
    entry = syms.resolve("reset_handler")
    walk = syms.resolve("walk")
    code = {entry: ("spin",), walk: ("op",), walk + 2: ("spin",), 0x700: ("spin",), **program}
    cpu = FakeCpu(code, walk, sp=0x2000_7F00, handlers={3: 0x700})
    cpu.write32(VTOR, 0x2000_0000)
    cpu.write32(0x2000_0004, entry | 1)
    for n in (0, 1, 2, 3, 12):
        cpu.regs[f"r{n}"] = 0xA0 + n
    return cpu


def program_at(address: int, *ops) -> dict:
    return {address + 2 * i: op for i, op in enumerate(ops)}


def test_call_square_returns_its_value_and_restores_everything(syms):
    square = syms.resolve("square")
    cpu = demo_cpu(syms, program_at(square, ("do", lambda c: c.regs.update(r0=c.regs["r0"] ** 2)), ("ret",)))
    before, memory = dict(cpu.regs), dict(cpu.memory)
    out = Debugger(cpu, syms).call("square", [7])
    assert out["returned"] and out["reason"] == "returned"
    assert out["return"] == {"type": "int", "value": 49, "raw": 49}
    assert out["arguments"] == [{"name": "v", "type": "int", "value": 7}]
    assert out["restored"] and cpu.regs == before and cpu.memory == memory and not cpu.breakpoints
    assert cpu.state.name == "HALTED"


def test_call_arguments_can_be_expressions_and_strings(syms):
    leaf = syms.resolve("leaf_sum")
    seen = {}

    def body(c):
        seen.update(r0=c.regs["r0"], r1=c.regs["r1"], text=bytes(c.read_memory_block8(c.regs["r0"], 6)))
        c.regs["r0"] = 3

    cpu = demo_cpu(syms, program_at(leaf, ("do", body), ("ret",)))
    out = Debugger(cpu, syms).call("leaf_sum", ["&sensors[1]", "MODE_RUN + 1"])
    assert seen["r0"] == syms.variable("sensors").address + 80 and seen["r1"] == 2
    assert out["return"]["value"] == 3
    cpu = demo_cpu(syms, program_at(leaf, ("do", body), ("ret",)))
    out = Debugger(cpu, syms).call("leaf_sum", [{"string": "hello"}, 0])
    assert seen["text"] == b"hello\0" and seen["r0"] < 0x2000_7F00 and seen["r0"] % 8 == 0
    assert out["buffers"][0]["string"] == "hello" and out["restored"]


def test_a_call_that_never_returns_is_stopped_and_undone(syms):
    square = syms.resolve("square")
    cpu = demo_cpu(syms, program_at(square, ("spin",)))
    before = dict(cpu.regs)
    out = Debugger(cpu, syms).call("square", [3], timeout_s=0.05)
    assert not out["returned"] and out["reason"] == "timeout"
    assert out["stopped_at"]["where"].startswith("square")
    assert out["restored"] and cpu.regs == before and not cpu.breakpoints


def test_a_fault_in_the_called_function_is_reported(syms):
    square = syms.resolve("square")
    cpu = demo_cpu(syms, program_at(square, ("fault",)))
    cpu.write32(DEMCR, VC_HARDERR)
    out = Debugger(cpu, syms).call("square", [3])
    assert out["reason"] == "vector_catch" and out["fault"]["exception"]["name"] == "HardFault"
    assert not out["restored"] and "target_reset" in out["note"]


def test_breakpoints_inside_the_called_function_do_not_stop_it(syms):
    square = syms.resolve("square")
    cpu = demo_cpu(syms, program_at(square, ("op",), ("do", lambda c: c.regs.update(r0=11)), ("ret",)))
    dbg = Debugger(cpu, syms)
    dbg.breakpoint(square + 2)
    out = dbg.call("square", [1])
    assert out["return"]["value"] == 11 and cpu.breakpoints == {square + 2}


def test_calls_check_their_arguments(syms):
    cpu = demo_cpu(syms, {})
    dbg = Debugger(cpu, syms)
    with pytest.raises(CallError):
        dbg.call("square", [])                                          # 引数の数
    with pytest.raises(CallError):
        dbg.call("square", [{"string": "x"}])                           # int にメモリは渡せない
    with pytest.raises(CallError):
        dbg.call("reset_handler", [])                                   # 踏み台の関数
    cpu.state = cpu.state.RUNNING
    with pytest.raises(CallError):
        dbg.call("square", [1])                                         # 走っている


def test_calls_by_address_without_dwarf_pass_integers(syms):
    square = syms.resolve("square")
    cpu = demo_cpu(syms, program_at(square, ("do", lambda c: c.regs.update(r0=c.regs["r0"] + c.regs["r1"])),
                                    ("ret",)))
    out = Debugger(cpu).call(square, [2, 40])
    assert out["return"] == {"type": None, "value": 42, "r1": 40}
    with pytest.raises(CallError):
        Debugger(cpu).call(square, [1.5])


def test_a_call_that_cannot_place_its_trampoline_leaves_the_core_as_it_was(syms):
    square = syms.resolve("square")
    cpu = demo_cpu(syms, program_at(square, ("ret",)))
    cpu.breakpoint_limit = 0                                    # 比較器が足りない
    before, memory = dict(cpu.regs), dict(cpu.memory)
    with pytest.raises(RuntimeError, match="踏み台"):
        Debugger(cpu, syms).call("square", [5])
    assert cpu.regs == before and cpu.memory == memory


def test_interrupts_can_be_disabled_during_the_call(syms):
    square = syms.resolve("square")
    seen = []
    cpu = demo_cpu(syms, program_at(square, ("do", lambda c: seen.append(c.regs["primask"])), ("ret",)))
    Debugger(cpu, syms).call("square", [1], disable_interrupts=True)
    assert seen == [1] and cpu.regs["primask"] == 0
