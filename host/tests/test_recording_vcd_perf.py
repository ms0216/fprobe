"""記録の VCD の書き出し（Recording.export_vcd）の性能の約束（測り方は perf_budget。経過時間: 描くのは並列）。

場面は test_la_vcd_perf と同じ（PYNQ の RLE・800 MS/s・周期 300 の方形波 = 21.4 MB/s の流れ）を記録に入れたもの: 記録の
書き出しも、その流れが届く速さより速く書く（流しの VCD と同じ部品）。前の変化点ごとの実装は同じ量に約 20 倍かかった。
"""

import pytest
from perf_budget import within

from fprobe.la import Transitions
from fprobe.recording import Recorder, Recording

np = pytest.importorskip("numpy")

SQUARE_RLE_MB_S = 21.4             # 流れの量（test_la_vcd_perf と同じ）
RECORD_BYTES = 4                   # RLE の 1 レコード = 1 変化点


@pytest.mark.perf
def test_the_recorded_square_wave_is_exported_faster_than_it_arrived(tmp_path):
    transitions, half = 4_000_000, 150
    values = np.where(np.arange(transitions) & 1, 1, 0).astype(np.uint16)
    starts = np.arange(transitions, dtype=np.uint64) * np.uint64(half)
    rec = Recorder(tmp_path / "r", timebase_hz=8e8)
    per = 1_000_000                                          # 1 つのかたまり = 100 万変化点（記録の流しの 1 秒の桁）
    for k in range(0, transitions, per):
        tr = Transitions(values[k:k + per], starts[k:k + per] - starts[k], per * half, 0, 0, 1.25e-9, 16)
        rec.add_waveform(tr, int(starts[k]))
    rec.close()
    r = Recording.open(tmp_path / "r")
    try:
        within(transitions * RECORD_BYTES / (SQUARE_RLE_MB_S * 1e6), lambda: r.export_vcd(tmp_path / "out.vcd"),
               clock="wall")
    finally:
        r.close()
    assert (tmp_path / "out.vcd").stat().st_size > transitions * 4
