"""PYNQ-Z2 の P3: 深いバッファ（PS の DDR の上端 32 MB、S_AXI_HP0）で、XIAO の SWD の線の取り込み・流し続け・イベント記録。

準備: `python -m fprobe_gw pynq reserve`（/boot/uEnv.txt の mem=479M と /etc/fprobe/mem を置いて再起動）の後。
予約がまだ（外部メモリの準備ができていない）なら飛ばす。環境変数 FPROBE_PYNQ_HOST（PYNQ の IP）を入れたときだけ実行。
XIAO は止めない（attach で読むだけ）。docs/superpowers/plans/2026-10-20-pynq-p3-deep-buffer.md の Task 3。
"""

import os
import subprocess
import sys
import time

import pytest

pytest.importorskip("pyocd")
from fprobe import generated as g  # noqa: E402
from fprobe.events import KIND_SAMPLE, EventLog  # noqa: E402
from fprobe.la import DeepAnalyzer  # noqa: E402
from fprobe.sampler import Sampler  # noqa: E402
from fprobe.session import Session  # noqa: E402

HOST = os.environ.get("FPROBE_PYNQ_HOST")
pytestmark = [pytest.mark.hil, pytest.mark.skipif(not HOST, reason="FPROBE_PYNQ_HOST（PYNQ の IP）が未設定")]

PROBE_DRIVES_SWDIO = 8                     # 内部の信号: プローブが SWDIO を駆動中
SWCLK = 0
FICR_PART = 0x10000100                     # nRF52840 の部品の番号（変わらない番地）


@pytest.fixture(scope="module")
def session():
    if not os.environ.get("FPROBE_PYNQ_SKIP_PROGRAM"):
        subprocess.run([sys.executable, "-m", "fprobe_gw", "build", "fprobe_pynq_deep", "--program"], check=True)
    deadline = time.monotonic() + 15
    while True:
        try:
            s = Session.open_eth(HOST)
            break
        except (TimeoutError, OSError):
            if time.monotonic() > deadline:
                raise
            time.sleep(1)
    with s:
        caps = s.device.capabilities()
        assert caps.design == "fprobe_pynq_deep" and caps.mem_bytes == 32 << 20
        if not s.device.read32(g.MEM_STATUS) >> g.MEM_STATUS_READY[0] & 1:
            pytest.skip("外部メモリの準備ができていない（DDR の予約がまだ: python -m fprobe_gw pynq reserve）")
        yield s


def swd_traffic(session, n=20):
    with session.debug(target_override="nrf52840", frequency=4_000_000, connect_mode="attach") as debug:
        for _ in range(n):
            debug.target.read32(FICR_PART)


@pytest.mark.parametrize("rle", [False, True])
def test_captures_the_xiao_swd_traffic_into_the_ps_ddr(session, rle):
    la = DeepAnalyzer(session.device)
    la.start(rle=rle, fall=1 << PROBE_DRIVES_SWDIO, post_beats=20_000)
    swd_traffic(session)
    if rle:
        # RLE は線が変わったときだけ拍を書く。XIAO の線は静かなので（Primer の Nucleo と違う）、トリガの後ろの 20,000 拍が
        # たまるまで通信を続ける（2026-10-20 に実機で: 通信を止めると 2.5 秒で 1 拍しか増えなかった）
        deadline = time.monotonic() + 30
        while not la.status() >> g.LA_DEEP_STATUS_DONE[0] & 1 and time.monotonic() < deadline:
            swd_traffic(session, 50)
    la.wait(5)
    if rle:
        tr = la.read_transitions(before_beats=2_000)
        assert tr.level_at(PROBE_DRIVES_SWDIO, tr.trigger - 1) == 1 and tr.level_at(PROBE_DRIVES_SWDIO, tr.trigger) == 0
        assert len(tr.edges(SWCLK)) > 2000
    else:
        cap = la.read(before_beats=2_000)
        assert len(cap.samples) == 22_001 * 8
        swclk = cap.channel(SWCLK)
        assert sum(1 for a, b in zip(swclk, swclk[1:]) if a and not b) >= 40
    assert session.device.read32(g.LA_DEEP_STATUS) is not None
    assert session.link.lost_frames == 0


def test_stream_through_the_relay_without_overrun(session):
    la = DeepAnalyzer(session.device)
    stream = la.stream(rate_hz=1e6, rle=False)                # 16 ch × 1 MS/s = 2 MB/s
    for _ in stream.chunks(duration_s=5.0):
        pass
    stats = stream.stats()
    print(f"RAW ストリーム（PYNQ の中継）: {stats.bytes / 1e6:.1f} MB / {stats.elapsed_s:.1f} s = {stats.mb_per_s:.2f} MB/s")
    assert not stats.overrun and session.link.lost_frames == 0
    assert stats.samples * stats.sample_period_s >= 4.5


def test_sampler_events_are_recorded_in_the_ps_ddr(session):
    with session.debug(target_override="nrf52840", frequency=4_000_000, connect_mode="attach"):
        dev = session.device
        s = Sampler(dev)
        s.configure([(FICR_PART, 1)], period_s=1e-4)
        log = EventLog(dev)
        log.start()
        s.start()
        time.sleep(0.3)
        s.stop()
        time.sleep(0.01)
        log.stop()
        samples = [e for e in log.read() if e.kind == KIND_SAMPLE]
        assert s.status()["errors"] == 0 and len(samples) > 1000
        assert all(e.data == 0x52840 for e in samples)
