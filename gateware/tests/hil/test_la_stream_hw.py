"""ロジアナの連続ストリーミング（M8 第 3 段）: 外部メモリを FIFO にして止めずに PC へ流す。

- RAW 12.5 MS/s（25 MB/s）を 10 秒、欠けなく受け取る（成功基準: 16 ch @ ≥ 10 MS/s 連続）。受け取りだけを測る
  （Python での復号は 25 MB/s に追いつかない）
- RLE を VCD に少しずつ書く
"""

import pytest

from fprobe import generated as g
from fprobe.la import DeepAnalyzer
from fprobe.session import Session
from tests.hil.program import SERIAL, program

pytestmark = pytest.mark.hil


@pytest.fixture(scope="module")
def device():
    program("fprobe")
    with Session.open(SERIAL) as s:
        yield s.device


def test_raw_stream_at_12_5_msps_without_loss(device):
    la = DeepAnalyzer(device)
    stream = la.stream(rate_hz=12.5e6, rle=False)
    received = 0
    for chunk in stream.chunks(duration_s=10.0):
        received += len(chunk.data)
    stats = stream.stats()
    print(f"RAW ストリーム: {stats.bytes / 1e6:.0f} MB / {stats.elapsed_s:.1f} s = {stats.mb_per_s:.1f} MB/s")
    assert not stats.overrun
    assert received == stats.bytes == 16 * device.read32(g.LA_DEEP_STREAM_BEATS)      # 書いた拍を全部受け取った
    assert stats.samples * stats.sample_period_s >= 9.5                                # 10 秒分のサンプル
    assert stats.mb_per_s >= 20


def test_rle_stream_to_vcd(device, tmp_path):
    path = tmp_path / "stream.vcd"
    stats = DeepAnalyzer(device).stream_to_vcd(path, duration_s=2.0, rle=True)
    assert not stats.overrun and stats.samples * stats.sample_period_s >= 1.9
    text = path.read_text()
    assert "$enddefinitions" in text and text.count("\n#") >= 1
