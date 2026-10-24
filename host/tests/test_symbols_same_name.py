"""同じ名前のソースが 2 つ（a/util.c と b/util.c）あるとき、"util.c:10" を先に出たほうと決めない（点検 2 回目 ?:
symbols.py:206）。ディレクトリを添えれば選べる。どちらとも決められなければ候補を挙げて誤り。"""

from pathlib import Path

import pytest

from fprobe.symbols import Symbols, _normal_path

A, B = Path("/w/a/util.c"), Path("/w/b/util.c")


@pytest.fixture
def syms():
    s = Symbols.__new__(Symbols)
    s._set_lines([(0x100, "util.c", 10, A), (0x200, "util.c", 10, B), (0x300, "main.c", 5, Path("/w/main.c"))],
                 {"util.c": [A, B], "main.c": [Path("/w/main.c")]})
    return s


def test_a_bare_name_shared_by_two_files_is_refused_with_the_candidates(syms):
    with pytest.raises(KeyError) as e:
        syms.resolve("util.c:10")
    assert str(A) in e.value.args[0] and str(B) in e.value.args[0]


def test_a_directory_picks_the_file(syms):
    assert syms.resolve("b/util.c:10") == 0x200
    assert syms.resolve("a/util.c:10") == 0x100
    assert syms.resolve("main.c:5") == 0x300                    # 名前が 1 つなら名前だけで


def test_same_name_sources_are_listed(syms):
    assert syms.source_conflicts() == {"util.c": [A, B]}


def test_the_same_file_written_two_ways_is_one_file():
    assert _normal_path(Path("/w/a/../a/util.c")) == _normal_path(Path("/w/a/util.c"))
