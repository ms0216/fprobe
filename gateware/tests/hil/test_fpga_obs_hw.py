"""対象の FPGA の観測（O1・O2）の実機の試験: PYNQ-Z2（fprobe、観測の口 = Raspberry Pi のヘッダ）→ Primer
（観測される側の代役。構成 obs_target_primer = 27 MHz のカウンタ + obs_soft_tap + obs_core）。

配線（docs/boards/pynq-z2.md「対象の FPGA の観測の口」）: PYNQ のヘッダ 15 TCK → Primer の J12 の印刷 J14、16 TMS → J16、
18 TDI → M14、22 TDO ← M15、Primer の J12 の T12（カウンタの 12 ビット）→ PYNQ の PMOD JB の 1 番（ロジアナ ch 10）。
XIAO は PMOD JA のまま（MCU と並べる試験で SWD で読む。止めない）。

環境変数 FPROBE_PYNQ_HOST（PYNQ の IP）と FPROBE_OBS_TARGET=1（上の配線と Primer の回路がある）のときだけ実行。
PYNQ の回路は fprobe_pynq_deep（DDR の予約の後）が書いてある前提（test_pynq_deep_hw.py と同じ）。
"""

import bisect
import os
import statistics
import time

import pytest

from fprobe.events import KIND_OBS_RECORD, KIND_SAMPLE, EventLog
from fprobe.fpga_obs import FpgaObs
from fprobe.la import DeepAnalyzer
from fprobe.session import Session

HOST = os.environ.get("FPROBE_PYNQ_HOST")
pytestmark = [pytest.mark.hil,
              pytest.mark.skipif(not HOST or not os.environ.get("FPROBE_OBS_TARGET"),
                                 reason="FPROBE_PYNQ_HOST と FPROBE_OBS_TARGET（Primer の代役と配線）が要る")]

COUNTER_HZ = 27_000_000 / 4096           # 観測する組 0 = カウンタの 27〜12 ビットが変わる頻度
MARK_CH = 10                             # Primer のカウンタの 12 ビット（JB の 1 番）
FICR_PART = 0x10000100                   # XIAO（nRF52840）の部品の番号（変わらない番地）


@pytest.fixture(scope="module")
def session():
    with Session.open_eth(HOST) as s:
        assert s.device.capabilities().fpga_obs
        yield s


def observe(session, half_period, seconds, *, la=False):
    """観測を始め、seconds 秒の値の並びと（la なら途中で取ったロジアナの取り込みと）状態を返す。"""
    d = session.device
    obs = FpgaObs(d)
    obs.open(chain={"ir_len": 4}, user_ir=2, half_period=half_period, words=64, sync_period=20)
    log = EventLog(d)
    log.start()
    obs.start()
    cap = None
    if la:
        time.sleep(seconds / 2)
        analyzer = DeepAnalyzer(d)
        analyzer.start(rle=False, post_beats=200_000)
        analyzer.wait(5)
        time.sleep(seconds / 2)
        cap = analyzer.read(before_beats=0)
    else:
        time.sleep(seconds)
    obs.stop()
    status = obs.status()
    log.stop()
    written, lost = log.counts()
    events = log.read(written, end=written)
    obs.close()
    return obs.samples(events), cap, status, lost, events


def test_the_id_and_the_declared_delays(session):
    info = FpgaObs(session.device).open(chain={"ir_len": 4}, user_ir=2, half_period=5)
    FpgaObs(session.device).close()
    assert info["id"] == 0x464F4253 and info["groups"] == 2 and info["clock_hz"] == 27_000_000
    assert info["mode"] == "stream" and (info["sync_delay"], info["sample_delay"]) == (3.5, 1.0)   # 観測の回路 v2


@pytest.mark.parametrize("half_period", [50, 5, 4])           # TCK 1・10・12.5 MHz（16.7 MHz からは今の配線で化ける）
def test_o1_the_counter_arrives_without_gaps(session, half_period):
    samples, _, status, lost, _ = observe(session, half_period, 10.0)
    values = [s for s in samples if s.kind == "value"]
    assert lost == 0 and not status["overflow_seen"] and not status["stuck"]
    assert not any(s.kind == "gap" for s in samples)                                   # 溢れの切れ目が無い
    assert all((b.value - a.value) & 0xFFFF == 1 for a, b in zip(values, values[1:]))   # 1 ずつ増え、抜けが無い
    assert {b.target_cycles - a.target_cycles for a, b in zip(values[1:], values[2:])} == {4096}
    assert len(values) == pytest.approx(10.0 * COUNTER_HZ, rel=0.02)


def test_o2_the_same_edge_on_the_analyzer_and_the_observation(session):
    # 同じカウンタのビットを、fprobe のロジアナ（JB）と観測の回路で見る。どちらも申告の遅れで直した後の差
    samples, cap, _, _, _ = observe(session, 5, 0.6, la=True)
    hz = session.device.capabilities().timebase_hz
    x = cap.channel(MARK_CH)
    la_edges = [cap.trigger_time + (i + 1 - cap.trigger) * cap.sample_period_s * hz
                for i, (a, b) in enumerate(zip(x, x[1:])) if a != b]
    values = [s for s in samples if s.kind == "value"]
    obs_edges = [b.time_fprobe for a, b in zip(values, values[1:]) if (a.value ^ b.value) & 1]
    diffs = []
    for t in la_edges:
        k = bisect.bisect_left(obs_edges, t)
        near = min((obs_edges[j] for j in (k - 1, k) if 0 <= j < len(obs_edges)), key=lambda o: abs(o - t))
        diffs.append((near - t) / hz * 1e9)
    print(f"観測 - ロジアナ: 平均 {statistics.mean(diffs):.1f} ns、{min(diffs):.1f}〜{max(diffs):.1f} ns")
    assert len(diffs) > 50 and max(abs(v) for v in diffs) < 100


def test_the_mcu_and_the_fpga_in_one_record(session):
    # XIAO のサンプラ（FPGA が SWD で周期的に読む）と FPGA の観測が、同じイベントの記録に時刻の順で入る
    from fprobe.sampler import Sampler
    with session.debug(target_override="nrf52840", frequency=4_000_000, connect_mode="attach"):
        sampler = Sampler(session.device)
        sampler.configure([(FICR_PART, 1)], period_s=1e-3)
        sampler.start()
        try:
            _, _, status, lost, events = observe(session, 5, 1.0)
        finally:
            sampler.stop()
    kinds = [e.kind for e in events]
    assert kinds.count(KIND_SAMPLE) > 500 and kinds.count(KIND_OBS_RECORD) > 5000 and lost == 0
    times = [e.time for e in events if e.kind in (KIND_SAMPLE, KIND_OBS_RECORD)]
    first_obs = kinds.index(KIND_OBS_RECORD)
    assert any(k == KIND_SAMPLE for k in kinds[first_obs:])                              # 交じって並ぶ
    assert times == sorted(times)


def test_the_timeline_shows_named_fpga_signals_while_recording(session, tmp_path):
    # 記録しながら観測（Collector の取り込み）→ 時間軸のタイルに、記録の信号の定義の名前で段が出る。
    # 組 0 = カウンタの 27〜12 ビット: bit 0 = mark（照合のビット）、bits 3:0 を列挙で（0 と 8 に名前）
    from fprobe.collector import Collector
    from fprobe.recording import Recorder, Recording
    from fprobe.timeline import recording_tile
    defs = {"groups": {"0": [{"name": "mark", "lsb": 0, "width": 1},
                             {"name": "phase", "lsb": 1, "width": 3, "format": "enum", "enum": {"0": "ZERO", "4": "HALF"}},
                             {"name": "upper", "lsb": 4, "width": 12, "format": "dec"}]}}
    d = session.device
    rec = Recorder(tmp_path / "r", timebase_hz=d.capabilities().timebase_hz, meta={"fpga.signals": defs})
    obs = FpgaObs(d)
    info = obs.open(chain={"ir_len": 4}, user_ir=2, half_period=5, words=64, sync_period=20)
    collector = Collector(d, rec)
    collector.obs.sync_delay, collector.obs.sample_delay = info["sync_delay"], info["sample_delay"]
    collector.obs.nominal_ratio = d.capabilities().timebase_hz / info["clock_hz"]
    collector.start()
    try:
        obs.start()
        time.sleep(1.0)
        obs.stop()
        time.sleep(0.5)
    finally:
        collector.stop()
        obs.close()
    rec.flush()
    r = Recording.open(rec.directory)
    t0, t1 = r.span()
    tile = recording_tile(r, t0, t1)
    [lane] = [ln for ln in tile.result["lanes"] if ln["stream"] == "fpga"]
    titles = {row["title"] for row in lane["rows"]}
    assert {"mark", "phase", "upper"} <= titles
    values = list(r.iter_events(streams=["fpga"], kinds=["value"]))
    assert len(values) == pytest.approx(1.0 * COUNTER_HZ, rel=0.15)
    assert all((b.b - a.b) & 0xFFFF == 1 for a, b in zip(values, values[1:]))      # 記録の値も 1 ずつ（抜けなし）
