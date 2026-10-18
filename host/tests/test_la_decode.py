"""深いバッファの拍の復号（RAW / RLE）、変化点表現、VCD の検査（ハードウェア不要）。"""

from array import array

from fprobe.la import Capture, Transitions, decode_beats, decode_transitions, trigger_sample, write_vcd


def rle_beats(records):
    data = b"".join((count << 16 | value).to_bytes(4, "little") for value, count in records)
    return data + bytes(-len(data) % 16)


def test_raw_and_rle_decode_agree():
    samples = [1, 1, 1, 2, 2, 7, 7, 7, 7, 7, 7, 7, 3, 3, 3, 3]
    raw = array("H", samples).tobytes()
    rle = rle_beats([(1, 2), (2, 1), (7, 6), (3, 3)])
    assert list(decode_beats(raw, False)) == samples == list(decode_beats(rle, True))
    for data, is_rle in ((raw, False), (rle, True)):
        values, starts, length = decode_transitions(data, is_rle)
        assert (list(values), list(starts), length) == ([1, 2, 7, 3], [0, 3, 5, 12], 16)
    assert trigger_sample(rle, True, 0, 2) == 5 and trigger_sample(raw, False, 1, 3) == 11


def _changes(path):
    return sorted(line for line in path.read_text().splitlines() if line.startswith("#") or line[:1] in "01")


def test_transitions_edges_and_vcd(tmp_path):
    cap = Capture([0, 0, 1, 1, 3, 3, 2, 0], trigger=2, trigger_time=0, sample_period_s=1e-8, channels=2)
    tr = Transitions.from_capture(cap)
    assert tr.edges(0) == [2, 6] and tr.edges(1) == [4, 7]
    assert tr.level_at(1, 5) == 1 and tr.level_at(0, 7) == 0
    write_vcd(tr, tmp_path / "t.vcd")
    write_vcd(cap, tmp_path / "c.vcd")
    assert _changes(tmp_path / "t.vcd") == _changes(tmp_path / "c.vcd")    # 同じ時刻に同じ変化
