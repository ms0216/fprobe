"""PYNQ-Z2 の深いバッファのストリームを、PS が DDR のリングを直接読んで流す（TCP / UDP）の実機の確かめ。
仕様: docs/superpowers/specs/2026-10-30-la-stream-ps-direct-design.md

準備: `fprobe_pynq_deep`（ケイパビリティ la_stream_ps のある版）のビットストリームと、それに合う中継（fprobe-relay）を PYNQ に入れておく
（このテストは書かない）。環境変数 FPROBE_PYNQ_HOST（PYNQ の IP）を入れたときだけ実行する。ピンは何もつながなくてよい。

確かめること（実測の値は設計文書の 10 章）:
  1. TCP（既定）: 10 MS/s（RAW 16 ch で 20 MB/s）を 10 秒、欠け 0・overrun なし・途切れなし。
  2. TCP を使わない（UDP の PS 直読み）・窓経由（ps_read = False）・TCP を、続けて同じ PYNQ で。直前の TCP のセッションが閉じた後に、
     中継が気づかず最初の拍を捨てる不具合の回帰（2026-10-31）。
  3. ストリーム中も、制御（CSR の読み）の往復が数十 ms 以内。
"""

import os
import statistics
import time

import pytest

from fprobe.la import DeepAnalyzer, StreamFormatError
from fprobe.session import Session

HOST = os.environ.get("FPROBE_PYNQ_HOST")
pytestmark = [pytest.mark.hil, pytest.mark.skipif(not HOST, reason="FPROBE_PYNQ_HOST（PYNQ の IP）が未設定")]


def run_stream(rate_hz, duration_s, *, session_kw=None, stream_kw=None, probe_control=False, udp_may_lose=False):
    """流して、(統計, 欠けたフレーム数, 制御の往復（ミリ秒）の列) を返す。
    udp_may_lose: UDP の経路は、PC の NIC・負荷で落ちうる（落ちた回は lost_frames が 50〜125。2026-10-31 実測）。落ちたとき、
    ストリームの検査が StreamFormatError で止まり、かつ lost_frames > 0（落ちたことが検出されている）なら、統計は None で返す。
    lost_frames が 0 のまま壊れたら（検出されない欠け・壊れ）は、そのまま失敗にする。"""
    with Session.open_eth(HOST, link_up_wait_s=8, **(session_kw or {})) as session:
        la = DeepAnalyzer(session.device)
        if not getattr(la.caps, "la_stream_ps", False):
            pytest.skip("この PYNQ の回路に la_stream_ps が無い（fprobe_pynq_deep の版 2 を入れる）")
        lost0 = session.link.lost_frames
        stream = la.stream(rate_hz=rate_hz, rle=False, **(stream_kw or {}))
        latency, last = [], 0.0
        try:
            for _ in stream.chunks(duration_s=duration_s):
                if probe_control and time.perf_counter() - last > 0.05:
                    t = time.perf_counter()
                    session.device.read32(0x0)
                    latency.append((time.perf_counter() - t) * 1e3)
                    last = time.perf_counter()
        except StreamFormatError:
            lost = session.link.lost_frames - lost0
            if not (udp_may_lose and lost > 0):
                raise
            return None, lost, latency
        return stream.stats(), session.link.lost_frames - lost0, latency


def continuous(stats):
    """欠けない時間が、受けるのにかかった時間と（5% 以内で）同じ = 流れが途切れていない。"""
    return abs(stats.samples * stats.sample_period_s - stats.elapsed_s) <= 0.05 * stats.elapsed_s + 0.1


def test_tcp_stream_at_10_msps_for_10_seconds_is_lossless():
    stats, lost, _ = run_stream(10e6, 10.0)
    print(f"TCP 10 MS/s 10 s: {stats.mb_per_s:.2f} MB/s、{stats.bytes / 1e6:.0f} MB")
    assert not stats.overrun and lost == 0 and continuous(stats)
    assert stats.mb_per_s > 19.0                                       # 要求は 20 MB/s


def test_the_three_paths_work_one_after_the_other_on_the_same_board():
    # TCP を使わない（UDP の PS 直読み）→ 窓経由 → TCP。直前の TCP のセッションが閉じたことに、中継が気づいている
    run_stream(4e6, 1.0)                                                # 直前に TCP のセッションを 1 つ閉じておく
    for label, kw in (("UDP の PS 直読み", {"session_kw": {"stream_tcp": False}, "udp_may_lose": True}),
                      ("窓経由", {"session_kw": {"stream_tcp": False}, "stream_kw": {"ps_read": False}, "udp_may_lose": True}),
                      ("TCP", {})):                                                # TCP は再送するので、欠け 0 が必須
        stats, lost, _ = run_stream(4e6, 3.0, **kw)
        if stats is None:                                                          # UDP が落ち、落ちたことは検出された
            print(f"{label}: UDP が {lost} 個落ちた（検出された）")
            continue
        assert not stats.overrun and lost == 0 and continuous(stats), label
        assert stats.mb_per_s > 7.5, (label, stats.mb_per_s)


@pytest.mark.parametrize("channels, rate_mhz", [(8, 50e6), (4, 100e6), (2, 100e6)])
def test_packed_stream_keeps_every_sample_at_the_rate_the_unpacked_stream_cannot_reach(channels, rate_mhz):
    # 使うチャンネルだけ詰める（la_pack）と、データ量が 16 ch の 1/2・1/4・1/8。8 ch の 50 MS/s・4 ch の 100 MS/s は 50 MB/s で、
    # 16 ch のままなら 100 MB/s・200 MB/s で流せない。欠けなし（overrun なし・欠けたフレームなし・サンプル数がレート × 時間と一致）。
    # 入力はつないでいない（信号は一定）ので、中身の並びはここでは見ない（シミュレーションで確認）
    with Session.open_eth(HOST, link_up_wait_s=8) as session:
        if not getattr(DeepAnalyzer(session.device).caps, "la_pack", False):
            pytest.skip("この PYNQ の回路に la_pack が無い（チャンネルを詰めた fprobe_pynq_deep を入れる）")
    stats, lost, _ = run_stream(rate_mhz, 10.0, stream_kw={"pack_channels": channels})
    print(f"{channels} ch {rate_mhz / 1e6:.0f} MS/s 10 s: {stats.mb_per_s:.2f} MB/s、{stats.samples} サンプル")
    assert not stats.overrun and lost == 0 and continuous(stats)
    assert abs(stats.samples - rate_mhz * stats.elapsed_s) <= 0.02 * rate_mhz * stats.elapsed_s + rate_mhz * 0.1


def test_control_round_trips_stay_short_during_a_fast_stream():
    _, _, latency = run_stream(8e6, 5.0, probe_control=True)
    assert len(latency) > 20
    print(f"ストリーム中の制御の往復: 中央値 {statistics.median(latency):.2f} ms、最大 {max(latency):.2f} ms")
    assert statistics.median(latency) < 20.0 and max(latency) < 200.0


def test_the_capture_read_by_the_ps_equals_the_one_read_through_the_window_and_is_faster():
    """トリガ取り込みの読み出し（書き込みが止まったロジアナのリング）を、PS が DDR から直接読んで TCP で送る形と、窓経由とで、
    同じ取り込みを読んで、中身が一致し、PS 直読みの方が速い。"""
    from fprobe import generated as g
    with Session.open_eth(HOST, link_up_wait_s=8) as session:
        la = DeepAnalyzer(session.device)
        if not getattr(la.caps, "la_stream_ps", False):
            pytest.skip("この PYNQ の回路に la_stream_ps が無い")
        assert session.device.stream_tcp_open and la._read_command() == 1 << g.LA_DEEP_COMMAND_READ_PS[0]
        la.start(rate_hz=50e6, post_beats=300_000)                          # パターンなし = すぐトリガ。50 MS/s
        la.wait(10.0)
        t = time.perf_counter()
        data_ps, _, _ = la._read_raw(None, None)
        seconds_ps = time.perf_counter() - t
        la._read_command = lambda: 1 << g.LA_DEEP_COMMAND_READ[0]           # 同じ取り込みを、窓経由で
        t = time.perf_counter()
        data_window, _, _ = la._read_raw(None, None)
        seconds_window = time.perf_counter() - t
        mb = len(data_ps) / 1e6
        print(f"取り込みの読み出し {mb:.1f} MB: PS 直読み {mb / seconds_ps:.1f} MB/s、窓経由 {mb / seconds_window:.1f} MB/s")
        assert len(data_ps) == len(data_window) > 1_000_000
        assert data_ps == data_window                                           # 中身が一致
        assert mb / seconds_ps > 1.5 * (mb / seconds_window)
