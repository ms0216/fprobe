"""Shrike-Lite のデモ（target_ip/fprobe_obs/examples/shrike_lite）の実機の試験: PYNQ-Z2 の fprobe から、RP2040（MCU）と
SLG47910（FPGA）の両方を同時に見る。配線は docs/boards/shrike-lite.md・デモの README。

環境変数 FPROBE_PYNQ_HOST（PYNQ の IP）と FPROBE_SHRIKE=1（配線とデモの書き込みが済んでいる）のときだけ実行。
PYNQ の回路は fprobe_pynq_deep（v2 のエンジン）。Shrike は firmware/main.py が起動時にデモの回路を書いて動いている。
"""

import os
import time

import pytest

from fprobe.events import KIND_OBS_RECORD, EventLog
from fprobe.fpga_obs import FpgaObs, samples
from fprobe.la import DeepAnalyzer
from fprobe.mcp_server import Probe
from fprobe.session import Session

HOST = os.environ.get("FPROBE_PYNQ_HOST")
pytestmark = [pytest.mark.hil,
              pytest.mark.skipif(not HOST or not os.environ.get("FPROBE_SHRIKE"),
                                 reason="FPROBE_PYNQ_HOST と FPROBE_SHRIKE（Shrike-Lite の配線とデモ）が要る")]

MAILBOX = 0x20040800                       # マジック・requests・errors・retries・A・B・結果・期待値
MAGIC = 0x5D0B0001
MARK_CH = 10                               # MCU のマーカー（RP2040 の GPIO26 → PMOD JB の 1 番）
HALF_PERIOD = 20                           # TCK 2.5 MHz（観測の回路は 1 クロック版、25 MHz / 8 以下）
IDLE, BUSY, DONE = 0, 1, 2


def test_the_mcu_mailbox_is_read_without_halting():
    p = Probe(eth=HOST)
    try:
        p.target_connect(target="rp2040", connect_mode="attach", frequency_hz=1_000_000)
        words = [MAILBOX + 4 * k for k in range(4)]
        a = p.system_status(addresses=words)["mcu"]
        time.sleep(1.0)
        b = p.system_status(addresses=words)["mcu"]
        assert a["state"] in ("RUNNING", "SLEEPING")
        key = [f"0x{w:08X}" for w in words]
        assert b["memory"][key[0]] == MAGIC
        assert b["memory"][key[1]] - a["memory"][key[1]] > 20               # requests が増えている
    finally:
        p.target_disconnect()


def test_the_mcu_mark_is_on_the_analyzer():
    with Session.open_eth(HOST) as s:
        a = DeepAnalyzer(s.device)
        a.start(rle=False, post_beats=200_000)
        a.wait(5)
        x = a.read(before_beats=0).channel(MARK_CH)
        assert sum(1 for i in range(1, len(x)) if x[i] != x[i - 1]) >= 2


def observe(seconds: float):
    with Session.open_eth(HOST) as s:
        obs = FpgaObs(s.device)
        info = obs.open(chain={"ir_len": 4}, user_ir=2, half_period=HALF_PERIOD, words=1, sync_period=0)
        log = EventLog(s.device)
        log.start()
        try:
            time.sleep(seconds)
            written, _ = log.counts()
            events = [e for e in log.read(min(written, log.capacity), end=written) if e.kind == KIND_OBS_RECORD]
        finally:
            log.stop()
            obs.close()
        return info, [v for v in samples(events) if v.kind == "value"]


def test_the_fpga_is_seen_by_snapshots():
    info, values = observe(1.0)
    assert info["mode"] == "snapshot"
    states = {v.value & 3 for v in values}
    assert {IDLE, BUSY, DONE} & states and BUSY in states                  # 計算器が動いている


def test_restarts_arrive_while_the_fpga_is_busy():
    # 仕込んだ不具合の決め手: リトライ（グループ 0 の bit 3 のトグル）は、FPGA が演算中（状態 1）のときに来る
    _, values = observe(2.0)
    restarts = [(a, b) for a, b in zip(values, values[1:]) if (a.value ^ b.value) & 8]
    assert restarts, "リトライが見えない（ファームウェアの POLLS・配線を確認）"
    busy = sum(1 for a, _ in restarts if a.value & 3 == BUSY)
    assert busy / len(restarts) > 0.8
