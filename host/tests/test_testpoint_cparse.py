"""テストポイント: 文の水準の C の構文解析（fprobe.testpoint.cparse）。"""

import pytest

from fprobe.testpoint.clex import tokenize
from fprobe.testpoint.cparse import (CParseError, Compound, If, Label, Loop, Opaque, Return, Simple, Switch, Unsupported,
                                     parse_file)


def funcs(src):
    toks = tokenize(src)
    return toks, parse_file(toks)


def names(src):
    return [f.name for f in funcs(src)[1].functions]


def test_finds_plain_and_static_and_attributed_functions():
    src = """
    int a(void) { return 1; }
    static inline unsigned *b(int x, char **y) { return 0; }
    __attribute__((noinline)) void c(void) {}
    void d(void) __attribute__((used));
    struct s { int (*f)(void); } g = { 0 };
    int arr[] = { 1, 2, 3 };
    enum e { A, B };
    typedef struct { int x; } T;
    void (*e(int n))(int) { return 0; }
    """
    _, parsed = funcs(src)
    assert [f.name for f in parsed.functions] == ["a", "b", "c", "e"]
    a, b, c, e = parsed.functions
    assert (a.return_type, a.is_void) == ("int", False)
    assert (b.return_type, b.is_void) == ("unsigned *", False)
    assert c.is_void and c.return_type == "void"
    assert e.return_type is None                       # 複雑な宣言子: 型を取れない


def test_extern_c_block_is_transparent():
    src = '#ifdef __cplusplus\nextern "C" {\n#endif\nint f(void) { return 0; }\n#ifdef __cplusplus\n}\n#endif\n'
    assert names(src) == ["f"]


def test_naked_attribute_is_recorded():
    _, parsed = funcs("__attribute__((naked)) void h(void) { __asm volatile(\"bx lr\"); }")
    assert parsed.functions[0].attributes >= {"naked"}


def test_knr_definition_is_not_a_function():
    assert names("int f(a) int a; { return a; }\nint g(void) { return 2; }") == ["g"]


def _body(src):
    toks, parsed = funcs(src)
    if parsed.skipped:
        raise Unsupported(parsed.skipped[0].reason, parsed.skipped[0].line)
    return toks, parsed.functions[0].body


def test_if_else_chain_and_dangling_else():
    toks, body = _body("void f(int a, int b) { if (a) if (b) x(); else y(); else if (b) z(); }")
    outer = body.items[0]
    assert isinstance(outer, If)
    assert isinstance(outer.then, If) and outer.then.els is not None      # else は近い if に付く
    assert isinstance(outer.els, If) and outer.els.els is None
    assert toks[outer.else_kw].text == "else"


def test_switch_collects_labels_of_its_own_level():
    src = """void f(int a) { switch (a) { case 1: case 2: x(); break;
             case 3: switch (a) { case 9: y(); } break; default: ; } }"""
    toks, body = _body(src)
    sw = body.items[0]
    assert isinstance(sw, Switch)
    assert [toks[lab.first].text + ("" if lab.kind == "default" else toks[lab.first + 1].text) for lab in sw.labels] \
        == ["case1", "case2", "case3", "default"]
    assert sw.has_default


def test_loops_and_do_while():
    toks, body = _body("void f(void) { for (int i = 0; i < 3; i++) x(); while (1) { break; } do y(); while (0); }")
    kinds = [(type(s).__name__, getattr(s, "kind", None)) for s in body.items]
    assert kinds == [("Loop", "for"), ("Loop", "while"), ("Loop", "do")]
    assert toks[body.items[2].last].text == ";"


def test_return_goto_label_and_simple():
    toks, body = _body("int f(int a) { if (a < 0) goto fail; return a * 2; fail: return -1; }")
    assert isinstance(body.items[1], Return) and body.items[1].has_expr
    lab = body.items[2]
    assert isinstance(lab, Label) and lab.kind == "label" and isinstance(lab.stmt, Return)
    assert isinstance(body.items[0].then, Simple)


def test_expressions_with_braces_inside():
    src = """void f(void) { struct p q = (struct p){1, 2}; int v[] = {1, 2};
             int s = ({ int t = 3; t; }); g((struct p){3, 4}); }"""
    _, body = _body(src)
    assert [type(s).__name__ for s in body.items] == ["Simple"] * 4


def test_macro_blocks_are_opaque_with_body():
    _, body = _body("void f(void) { FOREACH(i, list) { x(i); } LOOP { y(); } z(); }")
    assert [type(s).__name__ for s in body.items] == ["Opaque", "Opaque", "Simple"]
    assert isinstance(body.items[0].body, Compound) and len(body.items[0].body.items) == 1


def test_missing_semicolon_before_closing_brace_is_tolerated():
    _, body = _body("void f(void) { a(); MACRO(x) }")
    assert isinstance(body.items[1], Simple) and body.items[1].no_semicolon


def test_conditionals_at_item_boundaries_are_ok():
    _, body = _body("void f(void) {\n#if A\n x = 1;\n#else\n x = 2;\n#endif\n}")
    assert len(body.items) == 2


def test_conditional_inside_statement_is_unsupported():
    with pytest.raises(Unsupported):
        _body("void f(int a) {\n if (a)\n#if A\n x();\n#else\n y();\n#endif\n}")


def test_unbalanced_conditional_branches_are_unsupported():
    # 枝ごとに括弧の数が違う: ファイル全体の括弧が合わない（写しは元のまま）
    toks = tokenize("void f(int a) {\n#if A\n if (a) {\n#else\n if (!a) {\n#endif\n x();\n }\n}\nint g(void) { return 1; }")
    with pytest.raises(CParseError):
        parse_file(toks)


def test_branches_balanced_but_groups_cross_compounds_are_unsupported():
    src = "void f(int a) {\n#if A\n if (a) {\n#else\n if (!a) {\n#endif\n x();\n }\n}\n}\nint g(void) { return 1; }"
    parsed = parse_file(tokenize(src))
    assert [f.name for f in parsed.functions] == ["g"] and parsed.skipped[0].name == "f"


def test_unsupported_function_is_reported_and_others_kept():
    src = "void f(int a) {\n if (a)\n#ifdef X\n x();\n#endif\n}\nvoid g(void) { y(); }"
    parsed = parse_file(tokenize(src))
    assert [f.name for f in parsed.functions] == ["g"]
    assert parsed.skipped[0].name == "f" and "前処理" in parsed.skipped[0].reason


def test_else_without_if_is_unsupported():
    with pytest.raises(Unsupported):
        _body("void f(void) { else x(); }")


def test_function_end_line_and_fingerprint_inputs():
    toks, parsed = funcs("int a(void)\n{\n  return 1;\n}\n")
    f = parsed.functions[0]
    assert (f.line, toks[f.body.close].line) == (1, 4)
    assert toks[f.first].text == "int"


def test_opaque_types():
    _, body = _body("void f(void) { struct { int a; } s; union u { int b; } v; enum { X } e; }")
    assert all(isinstance(s, Simple) for s in body.items)


def test_loop_body_types():
    _, body = _body("void f(void) { while (x) ; for (;;) {} }")
    assert isinstance(body.items[0], Loop) and isinstance(body.items[0].body, Simple)
    assert isinstance(body.items[1].body, Compound)


def test_case_with_ternary_and_range():
    toks, body = _body("void f(int a) { switch (a) { case A ? 1 : 2: x(); case 3 ... 5: y(); } }")
    sw = body.items[0]
    assert len(sw.labels) == 2 and toks[sw.labels[0].colon - 1].text == "2"


def test_opaque_not_for_keywords():
    _, body = _body("void f(void) { sizeof(int); }")
    assert isinstance(body.items[0], Simple)
    assert not isinstance(body.items[0], Opaque)
