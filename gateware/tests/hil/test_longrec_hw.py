"""長期の記録（U23）の実機試験: 分割した記録・ロジアナの流し続けを記録へ・全体の縮図。

確かめること:
- 流し始めの時刻の基準（anchor）: 条件なしのトリガは最初のサンプルで成立し、深いバッファのトリガ時刻が前後の
  タイムベースの読みの間に入る（method = "trigger"）。成立しなければ "host"（往復の半分の不確かさ）になる。
- 流している間もデバッガの道具は動く（機器の呼び出しごとにロックを取る）。
- 分割（segment_s = 1）が回り、縮図（recording_overview）と記録の件数が合う。
"""

import time

import pytest

from fprobe.mcp_server import Probe
from tests.hil.program import SERIAL, program

pytestmark = pytest.mark.hil


@pytest.fixture(scope="module")
def probe():
    program("fprobe")
    p = Probe(SERIAL)
    p.target_connect("stm32f401xe", frequency_hz=4_000_000)
    yield p
    if p._recorder is not None:
        p.recording_stop()
    p.close()


def test_la_stream_into_a_segmented_recording(probe):
    probe.recording_start(name="longrec-hw", segment_s=1.0, max_bytes=2 << 30)
    started = probe.recording_la_stream_start(rate_hz=1e6, rle=True, chunk_s=0.5)
    print("時刻の基準:", started["anchor"])
    assert started["anchor"]["method"] == "trigger", started          # 違えば F3b の課題（host でも記録は正しい）
    for _ in range(10):                                               # 流している間もデバッガは動く
        probe.target_halt()
        probe.read_registers()
        probe.target_resume()
        probe.recording_mark(text="操作")
        time.sleep(0.3)
    stopped = probe.recording_la_stream_stop()
    assert not stopped["overrun"] and stopped["error"] is None and stopped["duration_s"] >= 3, stopped
    status = probe.recording_status()
    segments = probe.recording_segments()
    assert len(segments["segments"]) >= 3, segments
    overview = probe.recording_overview(width=100)
    assert sum(overview["streams"]["annotation"]) == 10
    assert any(ch.strip("-") for ch in overview["waveform"]), overview["waveform"]    # 波形がある
    span = status["span"]
    assert sum(probe.recording_density(t0=span[0], t1=span[1] + 1, width=50)["counts"]) == status["events"]
    probe.recording_stop()
