"""テストポイント: C の字句解析（fprobe.testpoint.clex）。"""

import pytest

from fprobe.testpoint.clex import CLexError, tokenize


def texts(src):
    return [t.text for t in tokenize(src)]


def test_comments_and_whitespace_are_dropped():
    assert texts("a /* x { */ b // y }\n c") == ["a", "b", "c"]


def test_line_comment_continues_after_backslash():
    assert texts("a // x \\\n still comment\nb") == ["a", "b"]


def test_strings_and_chars_keep_braces_inside():
    toks = tokenize('s = "a{\\"}" ; c = \'}\'; d = L"w"; e = u8"x";')
    assert [t.text for t in toks] == ["s", "=", '"a{\\"}"', ";", "c", "=", "'}'", ";", "d", "=", 'L"w"', ";",
                                      "e", "=", 'u8"x"', ";"]
    assert [t.kind for t in toks][:3] == ["id", "punct", "str"]


def test_longest_punctuators():
    assert texts("a<<=b->c...d++ ##") == ["a", "<<=", "b", "->", "c", "...", "d", "++", "##"]


def test_pp_number():
    assert texts("x = 0x1Fu + 1.5e+3f + .5;") == ["x", "=", "0x1Fu", "+", "1.5e+3f", "+", ".5", ";"]


def test_directive_is_one_token_with_continuation_and_comment():
    src = "#define F(x) \\\n  { x; } /* c\n */ \nint a;\n  #  ifdef X\n#endif\n"
    toks = tokenize(src)
    assert [(t.kind, t.directive) for t in toks if t.kind == "pp"] == [("pp", "define"), ("pp", "ifdef"),
                                                                        ("pp", "endif")]
    assert [t.text for t in toks if t.kind != "pp"] == ["int", "a", ";"]
    assert toks[0].line == 1 and toks[1].line == 4


def test_hash_not_at_line_start_is_punct():
    assert [t.kind for t in tokenize("a # b")] == ["id", "punct", "id"]


def test_directive_with_apostrophe_in_error_text():
    toks = tokenize("#error don't do this\nint x;")
    assert toks[0].kind == "pp" and [t.text for t in toks[1:]] == ["int", "x", ";"]


def test_conditional_flag():
    toks = tokenize("#if A\n#elif B\n#else\n#endif\n#pragma once\n")
    assert [t.conditional for t in toks] == [True, True, True, True, False]


def test_positions_and_lines():
    src = "int\n  foo;\n"
    toks = tokenize(src)
    assert [(t.start, t.end, t.line) for t in toks] == [(0, 3, 1), (6, 9, 2), (9, 10, 2)]


def test_line_splice_between_tokens():
    assert texts("a \\\n b") == ["a", "b"]


def test_unterminated_comment_raises():
    with pytest.raises(CLexError):
        tokenize("a /* never")


def test_unterminated_string_raises():
    with pytest.raises(CLexError):
        tokenize('a = "abc\n";')
