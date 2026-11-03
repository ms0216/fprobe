"""ロジアナの流れ → VCD の性能の約束（numpy の道。2026-11-03 の実機の場面: PYNQ の RLE・16 ch・800 MS/s・周期 300 の方形波
= 21.4 MB/s、RAW 1 ch 800 MS/s = 100 MB/s）。測り方は perf_budget（経過時間: VcdWriter は描くのを並列にする。上限を超えた
回だけ測り直し、3 回の最良）。細かい段ごとの数は tools/bench/la_vcd_bench.py。"""

import pytest
from perf_budget import within

from fprobe.la import StreamChunk, VcdWriter, _samples_of

np = pytest.importorskip("numpy")

LINE_MB_S = 117.7              # 線の理論値（D）
SQUARE_RLE_MB_S = 21.4         # 実機の場面の流れの量


def _rle_square(total: int, period: int = 300, mask: int = 1) -> bytes:
    values = np.where(np.arange(total // 4) & 1, mask, 0).astype("<u4")
    return (values | np.uint32(period // 2 - 1) << 16).astype("<u4").tobytes()


def _chunks(data: bytes, rle: bool, pack: int, size: int = 4_000_000):
    out, first, previous = [], 0, None
    for i in range(0, len(data), size):
        part = data[i:i + size]
        out.append(StreamChunk(part, rle, first, previous, pack=pack))
        samples, previous = _samples_of(part, rle, pack)
        first += samples
    return out


@pytest.mark.perf
def test_rle_transitions_keep_up_with_the_line():
    data = _rle_square(16_000_000)
    chunks = _chunks(data, True, 0)
    within(len(data) / (LINE_MB_S * 1e6), lambda: [c.changes() for c in chunks], clock="wall")


@pytest.mark.perf
def test_the_square_wave_of_the_rle_stream_is_written_to_vcd_faster_than_it_arrives(tmp_path):
    data = _rle_square(16_000_000)
    chunks = _chunks(data, True, 0)

    def run():
        with VcdWriter(tmp_path / "s.vcd", 16, 1.25e-9) as vcd:
            for c in chunks:
                vcd.write(*c.changes())
    within(len(data) / (SQUARE_RLE_MB_S * 1e6), run, clock="wall")
