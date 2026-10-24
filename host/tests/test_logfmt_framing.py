"""ログの枠（fprobe.logfmt.framing）: rzCOBS（defmt）・COBS と、0x00 で区切る流れの切り出し。
rzCOBS の試験の値は rzcobs クレートの試験（github.com/Dirbaio/rzcobs src/lib.rs）から。"""

import pytest

from fprobe.logfmt.framing import (Dropped, ZeroDelimited, cobs_decode, cobs_encode, rzcobs_decode, rzcobs_encode,
                                   tcobs_decode)

RZCOBS = [
    ("", ""), ("00", "7f"), ("0000", "7f"), ("00000000000000", "7f"), ("0000000000000000", "7f7f"),
    ("01", "017e"), ("0100", "017e"), ("0001", "017d"), ("0102", "01027c"),
    ("11223344556600", "11223344556640"), ("11223344556677", "1122334455667780"),
    ("1122334455667700", "1122334455667780"), ("1122334455667788", "112233445566778881"),
    ("00000000000000" "000000000000ff", "7fff3f"), ("00000000004400" "000000000000ff", "445fff3f"),
]


@pytest.mark.parametrize("plain,encoded", RZCOBS)
def test_rzcobs_vectors(plain, encoded):
    plain, encoded = bytes.fromhex(plain), bytes.fromhex(encoded)
    assert rzcobs_encode(plain) == encoded
    decoded = rzcobs_decode(encoded)
    assert decoded[:len(plain)] == plain and not any(decoded[len(plain):])    # 末尾は 0 の詰め物


@pytest.mark.parametrize("n,tail", [(133, "fe"), (134, "ff")])
def test_rzcobs_long_runs(n, tail):
    plain = bytes(range(1, n + 1))
    assert rzcobs_encode(plain) == plain + bytes.fromhex(tail)
    assert rzcobs_decode(plain + bytes.fromhex(tail))[:n] == plain


def test_rzcobs_a_defmt_frame():
    assert rzcobs_encode(bytes.fromhex("01002a")) == bytes.fromhex("012a7a")
    assert rzcobs_decode(bytes.fromhex("012a7a"))[:3] == bytes.fromhex("01002a")


def test_rzcobs_rejects_a_zero_inside_the_frame():
    with pytest.raises(ValueError):
        rzcobs_decode(b"\x01\x00\x7e")


@pytest.mark.parametrize("plain", [b"", b"\x00", b"\x11\x00\x22", bytes(range(1, 255)), bytes(300)])
def test_cobs_round_trip(plain):
    encoded = cobs_encode(plain)
    assert 0 not in encoded
    assert cobs_decode(encoded) == plain


def test_cobs_known_value():
    assert cobs_encode(b"\x11\x22\x00\x33") == b"\x03\x11\x22\x02\x33"


def test_zero_delimited_splits_across_chunks_and_skips_empty_frames():
    z = ZeroDelimited()
    assert z.feed(b"\x00\x00ab") == []
    assert z.feed(b"c\x00d") == [b"abc"]
    assert z.feed(b"\x00\x00") == [b"d"]
    assert z.pending == 0
    z.feed(b"xyz")
    assert z.pending == 3


# --- TCOBS v1（Trice の既定の枠。試験の値は github.com/rokath/tcobs v1/data_test.go と trice の試験から） --------------

@pytest.mark.parametrize("encoded,plain", [
    ("20", "00"), ("6020", "00000000"), ("aaa1", "aa"), ("aa09", "aaaaaa"), ("aa19", "aaaaaaaaaa"),
    ("80ffa1", "ffffffffff"),
    ("818e0923c002b801a400", None),
])
def test_tcobs_vectors(encoded, plain):
    data = bytes.fromhex(encoded)
    if plain is None:                                   # trice の TestTREX（区切りの 00 付き）
        assert tcobs_decode(data[:-1]) == bytes.fromhex("818e0900c002b801")
    else:
        assert tcobs_decode(data) == bytes.fromhex(plain)


def test_tcobs_repeat_with_zero_offset_repeats_the_byte_left_of_the_n_sigil():
    assert tcobs_decode(bytes.fromhex("01a118")) == bytes.fromhex("0101010101")      # N（off 1）の後の R4（off 0）


def test_tcobs_second_trice_example():
    frame = bytes.fromhex("a7831b23c1105c638061500562084100")
    expected = bytes.fromhex("a7831b00" "c110" "5c000000" "80000000" "50050000" "00080000")   # 92, 128, 1360, 2048
    assert tcobs_decode(frame[:-1]) == expected


def test_tcobs_rejects_bad_sigils():
    with pytest.raises(ValueError):
        tcobs_decode(bytes.fromhex("0102"))             # 00000ooo は予約
    with pytest.raises(ValueError):
        tcobs_decode(bytes.fromhex("aa3f"))             # オフセットが長さを超える


def test_zero_delimited_reports_what_it_dropped_and_the_limit():
    """区切りの来ないまま上限を超えて捨てたら、捨てた量と使った上限を返す（黙って数えるだけにしない）。"""
    z = ZeroDelimited(limit=4)
    out = z.feed(b"abcdef\x00gh\x00")
    assert out[0] == Dropped(5, 4) and out[1:] == [b"f", b"gh"]
