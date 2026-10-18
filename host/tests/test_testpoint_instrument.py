"""テストポイント: 自動挿入（fprobe.testpoint.instrument）。写しの形・対応表・コンパイル・実行の順・差分。"""

import shutil

import pytest
from testpoint_helpers import BACKEND_DEFINES, DATA, needs_zig, parse_log, run_instrumented, zig_syntax

from fprobe.testpoint.instrument import Options, WatchVar, instrument_source, instrument_tree
from fprobe.testpoint.probes import ProbeTable


def inst(src, **kw):
    out, probes, infos, result, _ = instrument_source(src, "a.c", **kw)
    return out, {p.id: p for p in probes}, infos, result


def body_of(out):
    """先頭の #include と #line を除いた写し。"""
    lines = out.split("\n")
    assert lines[0].startswith('#include "') and lines[1].startswith("#line 1 ")
    return "\n".join(lines[2:])


def test_entry_exit_and_line_numbers_are_kept():
    src = "int f(int x)\n{\n    return x + 1;\n}\n"
    out, probes, _, _ = inst(src)
    assert body_of(out) == ("int f(int x)\n{ FPROBE_TP(1);\n    { int fprobe_tp_r = (x + 1); FPROBE_TP(2); "
                            "return fprobe_tp_r; }\n}\n")
    assert [(p.kind, p.line, p.function) for p in probes.values()] == [("entry", 1, "f"), ("exit", 3, "f")]
    assert out.count("\n") == src.count("\n") + 2


def test_void_function_end_and_bare_return():
    out, probes, _, _ = inst("void g(int a) { if (a) return; h(); }")
    assert body_of(out) == ("void g(int a) { FPROBE_TP(1); if (a) { FPROBE_TP(2); { FPROBE_TP(4); return; } } "
                            "else { FPROBE_TP(3); } h(); FPROBE_TP(5); }")
    assert [p.kind for p in probes.values()] == ["entry", "then", "else", "exit", "exit"]
    assert probes[3].implicit and probes[3].decision == 2 and probes[5].implicit


def test_switch_labels_fallthrough_and_implicit_default():
    out, probes, _, _ = inst("void s(int v) { switch (v) { case 1: case 2: x(); break; case 3: y(); } }")
    assert "case 1: FPROBE_TP(2); case 2: FPROBE_TP(3); x();" in out
    assert "y(); break; default: FPROBE_TP(5); break; }" in out
    sw = [p for p in probes.values() if p.kind in ("case", "default")]
    assert [(p.label, p.decision, p.implicit) for p in sw] == [("case 1", 2, False), ("case 2", 2, False),
                                                                ("case 3", 2, False), ("default", 2, True)]


def test_nested_switch_labels_belong_to_their_own_switch():
    _, probes, _, _ = inst("void s(int a, int b) { switch (a) { case 1: switch (b) { case 9: ; default: ; } "
                           "default: ; } }")
    groups = {}
    for p in probes.values():
        if p.decision is not None:
            groups.setdefault(p.decision, []).append(p.label)
    assert sorted(groups.values()) == [["case 1", "default"], ["case 9", "default"]]


def test_loops_body_and_exit():
    out, probes, _, _ = inst("void l(int n) { while (n) n--; do { n++; } while (n < 3); }")
    assert "while (n) { FPROBE_TP(2); n--; } FPROBE_TP(3);" in out
    assert "do { FPROBE_TP(4); n++; } while (n < 3); FPROBE_TP(5);" in out
    assert [(p.kind, p.label, p.decision) for p in probes.values()][1:5] == [
        ("loop_body", "while", 2), ("loop_exit", "while", 2), ("loop_body", "do", 4), ("loop_exit", "do", 4)]


def test_dangling_else_wrapping_keeps_meaning():
    out, _, _, _ = inst("void d(int a, int b) { if (a) if (b) x(); else y(); }")
    # 外の if に足す else は、包んだ内側の if の後ろ（内側の else は内側の if のまま）
    assert "if (a) { FPROBE_TP(2); if (b) { FPROBE_TP(4); x(); } else { FPROBE_TP(5); y(); } } " \
           "else { FPROBE_TP(3); }" in out


def test_return_of_unknown_type_uses_auto_type():
    out, _, _, _ = inst("int (*getter(void))(int) { return sq; }\nSTATIC_T u8 m(void) { return 1; }")
    assert "{ __auto_type fprobe_tp_r = (sq);" in out
    assert out.count("__auto_type") == 2


def test_variable_writes():
    src = "void w(int k) { state = 1; table[k] = 2; s.x = 3; ++state; state++; other = 4; *pstate = 5; " \
          "int state2 = 0; if (state == 1) {} }"
    out, probes, _, _ = inst(src, options=Options(variables=(WatchVar("state"), WatchVar("table"),
                                                             WatchVar("s", value=False), WatchVar("pstate"))))
    vars_ = [(p.variable, p.label) for p in probes.values() if p.kind == "var"]
    assert vars_ == [("state", "state"), ("table", "table [ k ]"), ("s", "s . x"), ("state", "state"),
                     ("state", "state")]
    assert "state = 1; FPROBE_TP_VALUE(2, (state));" in out
    assert "table[k] = 2; FPROBE_TP_VALUE(3, (table [ k ]));" in out
    assert "s.x = 3; FPROBE_TP(4);" in out                  # 値なしの指定


def test_static_local_initializer_is_not_a_write():
    _, probes, _, _ = inst("void f(void) { static int state = 0; int state2; state = 2; }",
                           options=Options(variables=(WatchVar("state"),)))
    assert [p.line for p in probes.values() if p.kind == "var"] == [1]
    assert len([p for p in probes.values() if p.kind == "var"]) == 1


def test_naked_and_excluded_functions_are_skipped():
    src = "__attribute__((naked)) void n(void) { __asm(\"bx lr\"); }\nvoid a(void) { }\nvoid b(void) { }"
    out, probes, _, result = inst(src, options=Options(exclude=("b",)))
    assert {p.function for p in probes.values()} == {"a"}
    assert result.skipped[0].name == "n" and "naked" in result.skipped[0].reason


def test_unparsable_file_is_copied_unchanged():
    src = "void f(void) {\n#if A\n if (a) {\n#else\n if (b) {\n#endif\n }\n"
    out, probes, _, result = inst(src)
    assert out == src and not probes and result.error


def test_file_without_functions_is_unchanged():
    out, probes, _, _ = inst("int x = 1;\n")
    assert out == "int x = 1;\n" and not probes


def test_watchvar_parse():
    assert WatchVar.parse("state") == WatchVar("state", True)
    assert WatchVar.parse("cfg:novalue") == WatchVar("cfg", False)
    with pytest.raises(ValueError):
        WatchVar.parse("a b")


def test_only_branches_or_only_entry_exit():
    out, probes, _, _ = inst("int f(int a) { if (a) return 1; return 2; }", options=Options(branches=False))
    assert {p.kind for p in probes.values()} == {"entry", "exit"}
    out, probes, _, _ = inst("int f(int a) { if (a) return 1; return 2; }", options=Options(entry_exit=False))
    assert {p.kind for p in probes.values()} == {"then", "else"}


# --- コンパイル ---

@needs_zig
@pytest.mark.parametrize("backend", sorted(BACKEND_DEFINES))
def test_tricky_copy_compiles_for_every_backend(tmp_path, backend):
    shutil.copy(DATA / "tricky.c", tmp_path / "tricky.c")
    result = instrument_tree(tmp_path, tmp_path / "tp",
                             options=Options(variables=(WatchVar("state"), WatchVar("table"), WatchVar("pair"))))
    assert not result.files[0].error and not result.files[0].skipped
    for extra in ([], ["-DFEATURE"]):
        r = zig_syntax(tmp_path / "tp" / "tricky.c", backend, extra)
        assert r.returncode == 0, r.stderr


@needs_zig
def test_tricky_map_is_consistent(tmp_path):
    shutil.copy(DATA / "tricky.c", tmp_path / "tricky.c")
    result = instrument_tree(tmp_path, tmp_path / "tp", options=Options(variables=(WatchVar("state"),)))
    table = ProbeTable.load(tmp_path / "tp" / "testpoints.json")
    assert table.to_dict() == result.table.to_dict()
    src_lines = (DATA / "tricky.c").read_text(encoding="utf-8").split("\n")
    copy_lines = (tmp_path / "tp" / "tricky.c").read_text(encoding="utf-8").split("\n")
    assert len(copy_lines) == len(src_lines) + 2
    ends = {f.name: f.end_line for f in table.functions}
    for p in table.probes.values():                  # 印は写しの p.line〜関数の終わりの行（+2）に入っている
        if p.kind in ("then", "else", "loop_body", "loop_exit"):
            lines = copy_lines[p.line + 1:ends[p.function] + 2]
        else:
            lines = [copy_lines[p.line + 1]]
        assert any(f"({p.id})" in line or f"({p.id}," in line for line in lines), p
    names = {f.name for f in table.functions}
    assert names == {"early", "with_goto", "loops", "make", "bitf", "str", "sq", "getter", "nothing", "ret_void",
                     "call_void", "combined"}
    assert sorted(table.probes) == list(range(1, table.next_id))
    early_ret = table.find(function="early", kind="exit")
    assert [p.line for p in early_ret] == [18, 20, 21, 23]


# --- 実行の順 ---

def _seq(table, log):
    return [(table[i].function, table[i].kind, table[i].label) for i, _ in log]


@needs_zig
def test_execution_order_matches_probes(tmp_path):
    result, out = run_instrumented(tmp_path, [DATA / "run.c"], Options(variables=(WatchVar("marks"),
                                                                                   WatchVar("depth"))))
    assert out[0] == "result 70"
    log = parse_log(out)
    table = result.table
    seq = _seq(table, log)
    # fact(3): 入口 → else（暗黙）→ 子の呼び出しが出口より先（式を先に評価する）
    assert seq[:9] == [("fact", "entry", ""), ("fact", "else", "else"), ("fact", "entry", ""),
                       ("fact", "else", "else"), ("fact", "entry", ""), ("fact", "then", "if"),
                       ("fact", "exit", "return"), ("fact", "exit", "return"), ("fact", "exit", "return")]
    rest = seq[9:]
    # classify(0): case 0 で return、classify(1): case 1 → case 2（fallthrough で depth++）→ case 3 で return
    assert rest[:3] == [("classify", "entry", ""), ("classify", "case", "case 0"), ("classify", "exit", "return")]
    assert rest[3:9] == [("classify", "entry", ""), ("classify", "case", "case 1"), ("classify", "case", "case 2"),
                         ("classify", "var", "depth"), ("classify", "case", "case 3"), ("classify", "exit", "return")]
    # classify(5): default → break → 関数の最後の return
    assert rest[9:13] == [("classify", "entry", ""), ("classify", "default", "default"),
                          ("classify", "exit", "return"), ("find", "entry", "")]
    # cleanup(4) は marks[1] = 4 を記録、cleanup(-1) は goto で bad へ
    values = [(table[i].label, v) for i, v in log if table[i].kind == "var"]
    assert ("marks [ x % 3 ]", 4) in values
    cleanup = [k for f, k, _ in seq if f == "cleanup"]
    assert cleanup == ["entry", "else", "var", "exit", "entry", "then", "exit"]
    # touch(2): ループの本体 2 回 → 出口 → depth = 0 → 関数の終わり
    assert [k for f, k, _ in seq if f == "touch"] == ["entry", "loop_body", "loop_body", "loop_exit", "var", "exit"]
    assert log[-2][1] == 0


@needs_zig
def test_uninstrumented_build_gives_same_result(tmp_path):
    import subprocess

    from testpoint_helpers import ZIG
    exe = tmp_path / "plain.exe"
    stub = tmp_path / "stub.c"
    stub.write_text("#include <stdint.h>\nvoid fprobe_tp_user_emit(uint32_t i){(void)i;}\n", encoding="utf-8")
    subprocess.run([ZIG, "cc", "-O1", "-o", str(exe), str(DATA / "run.c"), str(DATA / "run_main.c")], check=True,
                   capture_output=True)
    assert subprocess.run([str(exe)], capture_output=True, text=True).stdout.splitlines()[0] == "result 70"


# --- 差分の再挿入 ---

def _tree(tmp_path, text):
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    (src / "m.c").write_text(text, encoding="utf-8")
    (src / "m.h").write_text("#define X 1\n", encoding="utf-8")
    return src


def test_unchanged_functions_keep_ids_and_changed_get_new(tmp_path):
    v1 = "int a(int x) { return x; }\nint b(int x) { if (x) return 1; return 0; }\n"
    src = _tree(tmp_path, v1)
    r1 = instrument_tree(src, tmp_path / "out")
    assert (tmp_path / "out" / "m.h").exists() and (tmp_path / "out" / "fprobe_tp.h").exists()
    ids_a = r1.table.find(function="a")
    v2 = "// 行を足した\nint a(int x) { return x; }\nint b(int x) { if (x > 1) return 1; return 0; }\nvoid c(void) {}\n"
    _tree(tmp_path, v2)
    r2 = instrument_tree(src, tmp_path / "out", previous=r1.table)
    assert [p.id for p in r2.table.find(function="a")] == [p.id for p in ids_a]
    assert [p.line for p in r2.table.find(function="a")] == [2, 2]          # 行が動いても ID は同じ
    assert min(p.id for p in r2.table.find(function="b")) >= r1.table.next_id
    assert r2.files[0].reused == ["a"] and set(r2.files[0].changed) == {"b", "c"}
    assert not r2.removed
    _tree(tmp_path, "int a(int x) { return x; }\n")
    r3 = instrument_tree(src, tmp_path / "out", previous=r2.table)
    assert sorted(r3.removed) == ["m.c:b", "m.c:c"]


def test_only_changed_instruments_only_changed_functions(tmp_path):
    src = _tree(tmp_path, "int a(int x) { return x; }\nint b(int x) { return x + 1; }\n")
    r1 = instrument_tree(src, tmp_path / "out")
    _tree(tmp_path, "int a(int x) { return x; }\nint b(int x) { return x + 2; }\n")
    r2 = instrument_tree(src, tmp_path / "out", previous=r1.table, options=Options(only_changed=True))
    assert {p.function for p in r2.table.probes.values()} == {"b"}
    assert {f.name: f.instrumented for f in r2.table.functions} == {"a": False, "b": True}


def test_unchanged_copy_is_not_rewritten(tmp_path):
    src = _tree(tmp_path, "int a(int x) { return x; }\n")
    r1 = instrument_tree(src, tmp_path / "out")
    assert tmp_path / "out" / "m.c" in [p.resolve() for p in r1.written] or r1.written
    r2 = instrument_tree(src, tmp_path / "out", previous=r1.table)
    assert r2.written == []


def test_command_line(tmp_path, capsys):
    import json

    from fprobe.testpoint.__main__ import main
    src = _tree(tmp_path, "int a(int x) { state = x; return x; }\n")
    assert main(["instrument", str(src), str(tmp_path / "out"), "--var", "state"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["probes"] == 3 and first["files"][0]["instrumented"] == ["a"]
    assert main(["instrument", str(src), str(tmp_path / "out"), "--var", "state"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["files"][0]["reused"] == ["a"] and second["written"] == []
    assert main(["header", str(tmp_path / "h")]) == 0
    assert (tmp_path / "h" / "fprobe_tp.h").exists()


def test_renumber(tmp_path):
    src = _tree(tmp_path, "int a(int x) { return x; }\n")
    r1 = instrument_tree(src, tmp_path / "out")
    _tree(tmp_path, "int a(int x) { return x + 1; }\n")
    r2 = instrument_tree(src, tmp_path / "out", previous=r1.table)
    assert min(r2.table.probes) == 3
    r3 = instrument_tree(src, tmp_path / "out", previous=r2.table, renumber=True)
    assert sorted(r3.table.probes) == [1, 2]
