"""R21（実機、PYNQ-Z2 + XIAO nRF52840）: XIAO を止めずに、サンプラが RTC1 の COUNTER（Zephyr のシステムの時計。32.768 kHz で
常に進む）を読み、bit10 が 1 に入る瞬間（1024 刻み = 31.25 ms ごとに変わる → 62.5 ms ごとに入る）でクロストリガ（源
sampler_match）→ ロジアナの深いバッファを捕捉する。FPROBE_PYNQ_HOST・FPROBE_PYNQ_TARGET=xiao。XIAO は止めない（利用者の
キーボード）。回路は fprobe_pynq_deep を JTAG で書いた後（FPROBE_PYNQ_SKIP_PROGRAM の前提）。"""
import os
import time

import pytest

from fprobe.mcp_server import Probe
from fprobe.sampler import Sampler

HOST = os.environ.get("FPROBE_PYNQ_HOST")
pytestmark = [pytest.mark.hil, pytest.mark.skipif(not (HOST and os.environ.get("FPROBE_PYNQ_TARGET") == "xiao"),
                                                  reason="FPROBE_PYNQ_HOST・FPROBE_PYNQ_TARGET=xiao が無い")]
RTC1_COUNTER = 0x40011504
BIT = 1 << 10


def test_sampler_match_fires_on_a_running_counter_without_halting():
    p = Probe(eth=HOST)
    try:
        caps = p.session.device.capabilities()
        assert caps.sampler_match, "この回路に R21 が無い"
        p.target_connect(target="nrf52840", frequency_hz=4_000_000, connect_mode="attach")
        dev = p.session.device
        hz = caps.timebase_hz
        s = Sampler(dev)
        s.configure([(RTC1_COUNTER, 4)], period_s=1e-3)
        s.start()
        p.sampler_trigger_set(address=RTC1_COUNTER, mask=BIT, value=BIT, halt=False, la=True)
        p.la_deep_arm(external_only=True, rle=False)
        time.sleep(1.0)
        st = p.sampler_trigger_status()
        cap = p.la_deep_collect(timeout_s=10)
        s.stop()
        gap_us = (st["time"] - st["prev_time"]) * 1e6 / hz
        print("status", st, f"発火の標本と直前の標本の間 {gap_us:.0f} µs、LA のトリガ {cap.get('trigger_time')}")
        assert p.target_status().get("state") != "HALTED"           # 止めていない
        assert st["seen"] > 500 and st["count"] >= 10               # 1 秒で 1 ms ごとの標本、62.5 ms ごとに入る
        assert st["prev_time"] < st["time"] and gap_us < 10_000
        assert cap.get("trigger_time") is not None
    finally:
        try:
            p.sampler_trigger_clear()
        finally:
            p.close()
