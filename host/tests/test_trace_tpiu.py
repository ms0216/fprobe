"""fprobe.trace.tpiu: TPIU のフォーマッタの枠を外す（16 バイトのフレーム → トレース ID ごとのバイト列）。"""

import pytest

from etm_encoder import FSYNC, HSYNC, frames
from fprobe.trace.tpiu import Deformatter, deformat


def test_a_hand_written_frame():
    # 偶数のバイト 0: ID 0x10 へ（0x21）、1: データ 0xAA（ID 0x10）、2: データ 0x55 の上 7 ビット（0x54、補助ビット 1 で 0x55）、
    # 3: 0x33、4: ID 0x11 へ・補助ビット 1 = 次の 1 バイト（5: 0x44）はまだ ID 0x10、6: 0x02（ID 0x11）、7〜13: 0x11 のデータ、
    # 14: データ 0x80（補助ビット 0）、15: 補助ビット（バイト 2 → ビット 1、バイト 4 → ビット 2）
    frame = bytes([0x21, 0xAA, 0x54, 0x33, 0x23, 0x44, 0x02, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x80,
                   0b0000_0110])
    out = deformat(FSYNC + frame)
    assert out.streams == {0x10: bytes([0xAA, 0x55, 0x33, 0x44]),
                           0x11: bytes([0x02, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x80])}
    assert out.frames == 1 and out.aligned == "fsync"


def test_the_last_even_byte_takes_its_low_bit_from_the_aux_byte():
    frame = bytes([0x21] + [0x00] * 13 + [0xFE, 0x80])
    assert deformat(FSYNC + frame).streams[0x10][-1] == 0xFF


def test_round_trip_with_several_sources_and_id_changes_at_odd_places():
    etm = bytes(range(1, 40))
    itm = bytes([0x01, 0x41, 0x42, 0x43])
    data = frames([(0x10, etm[:7]), (0x01, itm[:1]), (0x10, etm[7:]), (0x01, itm[1:])])
    out = deformat(FSYNC + data)
    assert out.streams[0x10] == etm and out.streams[0x01] == itm
    assert [ident for ident, _ in out.chunks] == [0x10, 0x01, 0x10, 0x01]          # 届いた順


def test_null_id_is_dropped():
    out = deformat(FSYNC + frames([(0x10, b"\x12\x34\x56")]))
    assert set(out.streams) == {0x10} and out.streams[0x10] == b"\x12\x34\x56"


def test_garbage_before_the_first_fsync_is_skipped():
    data = frames([(0x10, bytes(range(30)))])
    out = deformat(b"\x00\x13\x77\x01\x02" + FSYNC + data)
    assert out.streams[0x10] == bytes(range(30)) and out.skipped == 5


def test_fsync_between_frames_and_halfword_hsyncs_are_removed():
    data = frames([(0x10, bytes(range(100, 160)))])
    first, second = data[:16], data[16:]
    # 継続モード: 端子が空いているときの HSYNC（16 ビット）がフレームの中の偶数の位置に挟まる
    with_hsync = first[:6] + HSYNC + first[6:] + FSYNC + second[:2] + HSYNC + HSYNC + second[2:]
    out = deformat(FSYNC + with_hsync)
    assert out.streams[0x10] == bytes(range(100, 160))


def test_without_fsync_frames_are_taken_from_the_start_when_asked():
    data = frames([(0x10, bytes(range(20)))])
    assert deformat(data).streams == {}                                    # 既定: FSYNC を探す、無ければ何も出さない
    out = deformat(data, align="start")
    assert out.streams[0x10] == bytes(range(20)) and out.aligned == "start"
    out = deformat(data, align="auto")                                    # auto: FSYNC が無ければ先頭から
    assert out.streams[0x10] == bytes(range(20)) and out.aligned == "start"


def test_incomplete_last_frame_is_reported_not_guessed():
    data = frames([(0x10, bytes(range(20)))])
    out = deformat(FSYNC + data[:-5])
    assert out.leftover == 11 and out.streams[0x10] == bytes(range(14))         # 1 枚目のフレームだけ


def test_feeding_in_pieces_gives_the_same_result():
    data = FSYNC + frames([(0x10, bytes(range(200))), (0x02, b"\x09\x08")])
    whole = deformat(data)
    d = Deformatter()
    for n in range(0, len(data), 7):
        d.feed(data[n:n + 7])
    assert d.result().streams == whole.streams


def test_bad_align_is_refused():
    with pytest.raises(ValueError):
        deformat(b"", align="somewhere")
