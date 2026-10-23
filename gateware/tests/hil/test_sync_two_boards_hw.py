"""2 枚のメインボードの時刻の同期（モジュール化の段階 1。docs/superpowers/specs/2026-10-22-modular-design.md §3）の実機の試験。

親 = PYNQ-Z2（fprobe_pynq_deep、Ethernet）、子 = Primer（fprobe_la、USB）。配線（どれも 3.3 V 同士、電源の線はつながない）:
- PYNQ の RPi ヘッダ 38 番（GPIO20、親の sync_out）→ Primer J6 の 5 番（B12、子の sync_in。ヘッダの物理の番号。1〜4 番は 3.3V/GND）、
  GND 同士（RPi ヘッダ 39 番 ↔ J6 の GND）
- 同じ信号（試験の印。例: Shrike の GPIO26）を、両方のロジアナの最初の専用の入力（PYNQ の PMOD JB の 1 番、Primer J14 の 7 番）へ

両方のロジアナで印の同じ立ち上がりを取り、子の時刻を同期の組で親の時刻に換算して、立ち上がりの時刻の差を出す（±2 刻みが目標）。
環境変数 FPROBE_PYNQ_HOST（PYNQ の IP）と FPROBE_SYNC_WIRED=1（上の配線がある）のときだけ実行。PYNQ の回路は書いてある前提
（test_pynq_deep_hw.py と同じ）、Primer は fprobe_la を SRAM に書く。
"""

import os
import statistics
import threading
import time

import pytest

from fprobe import generated as g
from fprobe.collector import Collector, add_capture
from fprobe.la import DeepAnalyzer
from fprobe.merge import merge
from fprobe.recording import Recorder, Recording
from fprobe.session import Session
from fprobe.timesync import fit_parent, interpolator
from tests.hil.program import SERIAL, program

HOST = os.environ.get("FPROBE_PYNQ_HOST")
pytestmark = [pytest.mark.hil,
              pytest.mark.skipif(not HOST or not os.environ.get("FPROBE_SYNC_WIRED"),
                                 reason="FPROBE_PYNQ_HOST と FPROBE_SYNC_WIRED（2 枚の間の同期の線と印の線）が要る")]

PERIOD_TICKS = 100_000                    # 親のパルスの間隔（1 ms）
EDGES = 20                                # 比べる立ち上がりの数（印が少なければ取れた分）


@pytest.fixture(scope="module")
def boards():
    program("fprobe_la")
    with Session.open_eth(HOST) as parent, Session.open(SERIAL) as child:
        assert parent.device.capabilities().sync and child.device.capabilities().sync
        yield parent.device, child.device


def first_input(caps) -> int:
    return next(c.channel for c in caps.la_layout if c.kind == "input")


def rising_edges(recording: Recording, channel: int, delay_ticks: float) -> list[float]:
    """記録の波形の ch の立ち上がりの時刻（タイムベースの刻み。ロジアナの申告の遅れを引く）。"""
    out = []
    for t0, tps, tr in recording.waveforms():
        previous = None
        for value, start in zip(tr.values, tr.starts):
            bit = value >> channel & 1
            if previous == 0 and bit == 1:
                out.append(t0 + start * tps - delay_ticks)
            previous = bit
    return out


def record(device, directory, channel, post_beats, lock):
    """記録を始め、印の立ち上がりで深いバッファの取り込みを始める（機器の読み書きは lock の中で: 取り込みのスレッドと混ぜない）。"""
    rec = Recorder(directory, timebase_hz=device.capabilities().timebase_hz)
    collector = Collector(device, rec, lock=lock)
    collector.start()
    analyzer = DeepAnalyzer(device)
    with lock:
        analyzer.start(rle=True, rise=1 << channel, post_beats=post_beats)
    return rec, collector, analyzer


def test_two_boards_share_one_time(boards, tmp_path):
    parent, child = boards
    pc, cc = parent.capabilities(), child.capabilities()
    parent.write32(g.SYNC_CONTROL, 0)
    child.write32(g.SYNC_CONTROL, 1 << g.SYNC_CONTROL_ENABLE[0])
    parent.write32(g.SYNC_PERIOD, PERIOD_TICKS)
    parent.write32(g.SYNC_CONTROL, 1 << g.SYNC_CONTROL_MASTER[0])
    time.sleep(0.5)
    p_ch, c_ch = first_input(pc), first_input(cc)
    p_lock, c_lock = threading.Lock(), threading.Lock()
    p_rec, p_col, p_la = record(parent, tmp_path / "parent", p_ch, 4096, p_lock)
    c_rec, c_col, c_la = record(child, tmp_path / "child", c_ch, 4096, c_lock)
    for la, rec, lock in ((p_la, p_rec, p_lock), (c_la, c_rec, c_lock)):
        with lock:
            la.wait(10)
            add_capture(rec, la.read_transitions())
    time.sleep(1.0)                                            # 取り込みの後ろにも組を
    for col, rec in ((p_col, p_rec), (c_col, c_rec)):
        col.stop()
        rec.close()
    status = child.read32(g.SYNC_STATUS)
    pairs_seen, crc_errors = status & 0xFFFF, status >> 16
    parent.write32(g.SYNC_CONTROL, 0)
    child.write32(g.SYNC_CONTROL, 0)

    merged = merge(tmp_path / "parent", tmp_path / "child", tmp_path / "merged", child_name="primer")
    c = Recording.open(tmp_path / "child")
    pairs = [(e.t, e.body["parent"]) for e in c.events(streams=["sync"])]
    fit = fit_parent(pairs)
    assert fit is not None, f"組が足りない（CSR の pairs {pairs_seen}・crc_errors {crc_errors}）"
    locals_ = [p[0] for p in pairs]
    p_edges = rising_edges(Recording.open(tmp_path / "parent"), p_ch, pc.la_delay_deep)
    to = interpolator(pairs)                                      # 両側の組の間の直線（merge と同じ換算）
    c_edges = [to(t) for t in rising_edges(c, c_ch, cc.la_delay_deep)]
    assert p_edges and c_edges, "印の立ち上がりが無い（印の線と信号を確かめる）"
    # 2 枚の取り込みの窓は違う: 両方に写っている所（子の立ち上がりの範囲の中の親の立ち上がり）だけを比べる
    lo, hi = min(c_edges), max(c_edges)
    common = [pe for pe in p_edges if lo - 100 <= pe <= hi + 100][:EDGES]
    assert common, f"窓が重ならない（親 {p_edges[0]:.0f}〜{p_edges[-1]:.0f}、子 {lo:.0f}〜{hi:.0f}）"
    diffs = [min((ce - pe for ce in c_edges), key=abs) for pe in common]
    print(f"\n組 {len(pairs)}（CSR {pairs_seen}、CRC 異常 {crc_errors}）、残差の最大 {fit.residual_max:.2f} 刻み、"
          f"傾き {fit.ratio:.9f}、合成 {merged}")
    print(f"比べた立ち上がり {len(diffs)}、差（子 - 親、刻み）: {diffs}")
    print(f"立ち上がりの差（子 - 親、刻み）: 最小 {min(diffs):.1f}、最大 {max(diffs):.1f}、中央値 {statistics.median(diffs):.1f}")
    assert crc_errors == 0
    assert all(abs(d) <= 2 for d in diffs), diffs
