"""ホストでの printf の整形（fprobe.logfmt.printf）: C の書式と、32 ビットの語の列からの引数の取り出し。
long・size_t・ptrdiff_t・ポインタの大きさは使う側が渡す（pointer_bits。ILP32 = 32、LP64 = 64）。"""

import struct

import pytest

from fprobe.logfmt.printf import Values, Words, conversions, format_c


def test_integers_with_flags_width_and_precision():
    assert format_c("%d|%5d|%-5d|%05d|%+d|% d", Values([-3, 42, 42, 42, 7, 7]), pointer_bits=32) == "-3|   42|42   |00042|+7| 7"
    assert format_c("%x %X %#x %#o %o %.3d %u", Values([255, 255, 255, 8, 8, 5, 4294967295]), pointer_bits=32) == \
        "ff FF 0xff 010 10 005 4294967295"
    assert format_c("%*d|%-*d|%.*d", Values([4, 1, 3, 2, 3, 9]), pointer_bits=32) == "   1|2  |009"


def test_words_are_signed_by_the_length_modifier():
    w = Words([0xFFFFFFFF, 0xFF, 0xFFFF, 0x80])
    assert format_c("%d %hhd %hd %hhu", w, pointer_bits=32) == "-1 -1 -1 128"


def test_64_bit_and_double_take_two_words():
    lo, hi = struct.unpack("<II", struct.pack("<q", -5_000_000_000))
    d0, d1 = struct.unpack("<II", struct.pack("<d", 3.25))
    assert format_c("%lld %.2f %u", Words([lo, hi, d0, d1, 9]), pointer_bits=32) == "-5000000000 3.25 9"
    assert format_c("%f", Words([d0, d1], double_words=2), pointer_bits=32) == "3.250000"
    f32 = struct.unpack("<I", struct.pack("<f", 1.5))[0]
    assert format_c("%.1f", Words([f32], double_words=1), pointer_bits=32) == "1.5"


def test_strings_chars_pointers_and_percent():
    strings = {0x1000: "hello"}
    w = Words([0x1000, ord("A"), 0x20000010], string=strings.get)
    assert format_c("%s %c %p 100%%", w, pointer_bits=32) == "hello A 0x20000010 100%"
    assert format_c("[%-7s]", Values(["ab"]), pointer_bits=32) == "[ab     ]"
    assert format_c("%.2s", Values(["abcdef"]), pointer_bits=32) == "ab"


def test_a_string_that_cannot_be_read_shows_the_address():
    assert format_c("%s", Words([0x2000], string=lambda a: None), pointer_bits=32) == "<str@0x00002000>"


def test_float_forms():
    assert format_c("%e|%g|%G|%.3g|%a", Values([1234.5, 0.0001, 1e20, 3.14159, 1.0]), pointer_bits=32) == \
        "1.234500e+03|0.0001|1E+20|3.14|0x1p+0"


def test_missing_arguments_are_marked_not_raised():
    assert format_c("%d %d", Values([1]), pointer_bits=32) == "1 <?>"


def test_conversions_lists_each_argument_with_its_size():
    specs = conversions("a %d %lld %s %*.*f %hhx %%", pointer_bits=32)
    assert [(s.conversion, s.length, s.bits) for s in specs] == [
        ("d", "", 32), ("d", "ll", 64), ("s", "", 32), ("*", "", 32), ("*", "", 32), ("f", "", 64), ("x", "hh", 8)]


@pytest.mark.parametrize("fmt", ["%", "%5", "abc %q"])
def test_bad_conversions_are_kept_as_text(fmt):
    assert format_c(fmt, Values([1]), pointer_bits=32) == fmt


def test_long_size_t_and_pointers_follow_the_given_pointer_bits():
    """long（l）・size_t（z）・ptrdiff_t（t）・ポインタ（p・s・n）の大きさを 32 ビットと決め打ちしない（ILP32 / LP64）。"""
    big = 1 << 40
    assert format_c("%ld %zu %td %p", Values([big, big, -big, big]), pointer_bits=64) ==         "1099511627776 1099511627776 -1099511627776 0x0000010000000000"
    assert format_c("%ld", Values([big + 5]), pointer_bits=32) == "5"
    assert format_c("%lu|%d", Words([1, 2, 3]), pointer_bits=64) == f"{1 | 2 << 32}|3"
    assert [c.bits for c in conversions("%ld %zu %td %p %s %d", pointer_bits=64)] == [64, 64, 64, 64, 64, 32]


def test_pointer_bits_has_no_default():
    with pytest.raises(TypeError):
        format_c("%ld", Values([1]))
    with pytest.raises(TypeError):
        conversions("%ld")
