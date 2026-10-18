"""逆アセンブルからの静的な呼び出しグラフと関数の枠（fprobe.callgraph、U26・U18）。
stack_demo.elf（clang -O1、.su と照合できる）と、手で組んだ命令の列（端の場合）。"""

from pathlib import Path

import pytest

from fprobe.callgraph import CallGraph, analyze_function
from fprobe.disasm import Disassembler, Instruction

ELF = Path(__file__).resolve().parents[1] / "firmware" / "stack_demo" / "stack_demo.elf"


@pytest.fixture(scope="module")
def graph():
    return CallGraph.build(Disassembler.from_elf(ELF))


def test_frames_match_the_compiler_stack_usage(graph):
    # stack_demo.su（clang -fstack-usage）と同じ値
    expected = {"leaf": 32, "middle": 80, "depth": 24, "op_double": 0, "op_inc": 0, "apply": 8, "tail": 0,
                "SysTick_Handler": 8, "Default_Handler": 0, "main": 0, "Reset_Handler": 0}
    assert {name: graph.functions[name].frame for name in expected} == expected
    assert graph.functions["dynamic_buf"].dynamic and graph.functions["dynamic_buf"].frame == 16
    assert not graph.functions["middle"].dynamic and not graph.functions["middle"].uncertain


def test_call_sites_with_their_depth(graph):
    middle = graph.functions["middle"]
    assert [(c.callee, c.kind, c.depth) for c in middle.calls] == [("leaf", "call", 80), ("leaf", "call", 80)]
    assert [(c.callee, c.kind) for c in graph.functions["depth"].calls] == [("depth", "call")]
    tail = graph.functions["tail"].calls
    assert [(c.callee, c.kind, c.depth) for c in tail] == [("middle", "tail", 0)]
    apply = graph.functions["apply"].calls
    assert [(c.callee, c.kind, c.depth) for c in apply] == [(None, "indirect", 8)]
    assert graph.functions["Default_Handler"].calls == []                  # 自分の中への分岐は呼び出しでない
    assert graph.callees("main") == ["middle", "depth", "apply", "dynamic_buf", "tail"]
    assert graph.callers("leaf") == ["middle", "SysTick_Handler"]


def test_address_taken_functions(graph):
    # ops[] の関数ポインタ（ベクタ表の処理は除く）
    assert graph.address_taken == {"op_double", "op_inc"}


def ins(address, mnemonic, operands="", *, flow="", target=None, conditional=False, size=2, kind="code", value=None):
    return Instruction(address, bytes(size), mnemonic, operands, kind, target, None, None, value, None, flow,
                       conditional)


def names(address):
    return {0x100: "f", 0x200: "g", 0x300: "h"}.get(address)


def test_shrink_wrapped_paths_take_the_deepest():
    # 早い戻り（枠なし）と、枠を作る道。条件付きの戻り（IT の中の pop pc）は続きもある
    code = [ins(0x100, "cbz", "r0, #0x10c", flow="jump", target=0x10C, conditional=True),
            ins(0x102, "push", "{r4, r5, r6, lr}"),
            ins(0x104, "sub", "sp, #0x18"),
            ins(0x106, "bl", "#0x200", flow="call", target=0x200, size=4),
            ins(0x10A, "popne", "{r4, r5, r6, pc}", flow="return", conditional=True),
            ins(0x10C, "bx", "lr", flow="return")]
    f = analyze_function(code, "f", 0x100, 0x10E, names)
    assert f.frame == 40 and [(c.callee, c.depth) for c in f.calls] == [("g", 40)] and not f.uncertain


def test_frame_pointer_restore_is_not_dynamic_but_alloca_is():
    restore = [ins(0x100, "push", "{r7, lr}"), ins(0x102, "sub", "sp, #8"), ins(0x104, "add", "r7, sp, #0"),
               ins(0x106, "adds", "r7, #8"), ins(0x108, "mov", "sp, r7"), ins(0x10A, "pop", "{r7, pc}", flow="return")]
    f = analyze_function(restore, "f", 0x100, 0x10C, names)
    assert f.frame == 16 and not f.dynamic
    alloca = [ins(0x100, "push", "{r7, lr}"), ins(0x102, "sub.w", "r1, sp, r0", size=4), ins(0x106, "mov", "sp, r1"),
              ins(0x108, "pop", "{r7, pc}", flow="return")]
    g = analyze_function(alloca, "f", 0x100, 0x10A, names)
    assert g.dynamic and g.frame == 8 and "0x00000106" in g.reasons[0]


def test_pre_decrement_stores_vpush_and_post_increment_return():
    code = [ins(0x100, "str", "lr, [sp, #-4]!", size=4), ins(0x104, "vpush", "{d8, d9}", size=4),
            ins(0x108, "strd", "r4, r5, [sp, #-8]!", size=4), ins(0x10C, "ldrd", "r4, r5, [sp], #8", size=4),
            ins(0x110, "vpop", "{d8, d9}", size=4), ins(0x114, "ldr", "pc, [sp], #4", flow="jump", size=4)]
    f = analyze_function(code, "f", 0x100, 0x118, names)
    assert f.frame == 4 + 16 + 8 and f.calls == [] and f.uses_fpu


def test_table_branch_targets_are_followed():
    code = [ins(0x100, "push", "{lr}"), ins(0x102, "cmp", "r0, #1"),
            ins(0x104, "tbb", "[pc, r0]", flow="jump", size=4),
            ins(0x108, ".byte", "0x02", kind="data", size=1, value=2),
            ins(0x109, ".byte", "0x03", kind="data", size=1, value=3),
            ins(0x10A, "b", "#0x10a", flow="jump", target=0x10A),          # 表の後（届かない）
            ins(0x10C, "sub", "sp, #8"), ins(0x10E, "bl", "#0x300", flow="call", target=0x300, size=4),
            ins(0x112, "add", "sp, #8"), ins(0x114, "pop", "{pc}", flow="return")]
    f = analyze_function(code, "f", 0x100, 0x116, names)
    assert f.frame == 12 and [(c.callee, c.depth) for c in f.calls] == [("h", 12)] and not f.uncertain


def test_indirect_jumps_and_growing_loops():
    code = [ins(0x100, "push", "{r4, lr}"), ins(0x102, "pop", "{r4, lr}"), ins(0x104, "bx", "r3", flow="jump")]
    f = analyze_function(code, "f", 0x100, 0x106, names)
    assert [(c.callee, c.kind, c.depth) for c in f.calls] == [(None, "indirect_tail", 0)]
    loop = [ins(0x100, "push", "{r4}"), ins(0x102, "b", "#0x100", flow="jump", target=0x100)]
    g = analyze_function(loop, "f", 0x100, 0x104, names)
    assert g.dynamic and any("伸び" in r for r in g.reasons)


def test_unknown_table_counts_the_unreached_code():
    code = [ins(0x100, "push", "{lr}"), ins(0x102, "tbb", "[pc, r0]", flow="jump", size=4),     # 表の大きさが分からない
            ins(0x106, "sub", "sp, #16"), ins(0x108, "bl", "#0x200", flow="call", target=0x200, size=4),
            ins(0x10C, "pop", "{pc}", flow="return")]
    f = analyze_function(code, "f", 0x100, 0x10E, names)
    assert f.uncertain and f.frame == 4 + 16 and [(c.callee, c.depth) for c in f.calls] == [("g", 20)]
