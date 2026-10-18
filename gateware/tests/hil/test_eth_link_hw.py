"""M11: EthernetLink（UDP のホストリンク）で、製品デザインと同じ fprobe_core・DDR3 を同じホストソフトから使う。

Primer 20K に fprobe_eth を書き込み、LAN ケーブルで直結した PC から UDP で、ケイパビリティ・CSR・pyOCD のデバッグ・
深いバッファのロジアナを使う。PC の LAN アダプタを 192.168.77.0/24（例: 192.168.77.1）に設定し、
環境変数 FPROBE_ETH_HOST にボードの IP（既定 192.168.77.2）を入れたときだけ実行する（ケーブルが無い環境では飛ばす）。
"""

import os
import random
import subprocess
import sys
import time

import pytest

pytest.importorskip("pyocd")
from fprobe import generated as g  # noqa: E402
from fprobe.la import LogicAnalyzer  # noqa: E402
from fprobe.session import Session  # noqa: E402

HOST = os.environ.get("FPROBE_ETH_HOST")
pytestmark = [pytest.mark.hil,
              pytest.mark.skipif(not HOST, reason="FPROBE_ETH_HOST（LAN で直結したボードの IP）が未設定")]

SRAM = 0x20004000


@pytest.fixture(scope="module")
def session():
    # FPROBE_HIL_FS があれば、そのビットストリーム（配置の別の候補 build/fprobe_eth/p<n>/top.fs 等）を書き込む
    fs = os.environ.get("FPROBE_HIL_FS")
    command = ["program", os.path.abspath(fs)] if fs else ["build", "fprobe_eth", "--program"]
    subprocess.run([sys.executable, "-m", "fprobe_gw", *command], check=True)
    deadline = time.monotonic() + 15                   # PHY のリンク確立（自動ネゴシエーション）を待つ
    while True:
        try:
            s = Session.open_eth(HOST)
            break
        except (TimeoutError, OSError):
            if time.monotonic() > deadline:
                raise
            time.sleep(1)
    with s:
        yield s


def test_answers_ping(session):
    result = subprocess.run(["ping", "-n", "3", HOST], capture_output=True, text=True, encoding="cp932", errors="replace")
    assert result.returncode == 0, result.stdout


def test_same_core_answers_over_ethernet(session):
    caps = session.device.capabilities()
    assert (caps.design, caps.host_link) == ("fprobe_eth", g.LINK_ETH)
    assert caps.mem_bytes == 128 * 1024 * 1024
    for value in (0x5A5AC3C3, 0x12345678):
        session.device.write32(g.SYSTEM_SCRATCH, value)
        assert session.device.read32(g.SYSTEM_SCRATCH) == value


def test_pyocd_debugs_the_target_over_ethernet(session):
    with session.debug(target_override="stm32f401xe", frequency=25_000_000, connect_mode="attach") as debug:
        t = debug.target
        assert t.read32(0xE000ED00) == 0x410FC241
        words = [random.Random(9).getrandbits(32) for _ in range(1024)]
        t.write_memory_block32(SRAM, words)
        assert t.read_memory_block32(SRAM, len(words)) == words
        t.halt()
        best = min(_timed(lambda: t.read_memory_block32(0x20000000, 16384)) for _ in range(3))
        t.resume()
    rate = 65536 / best
    print(f"pyOCD 読み出し（Ethernet、SWD 25 MHz）: {rate / 1e6:.2f} MB/s")
    assert rate >= 1.2e6                                            # USB 版と同じ水準（往復は約 200 µs）


def _timed(fn) -> float:
    start = time.perf_counter()
    fn()
    return time.perf_counter() - start


def test_deep_logic_analyzer_over_ethernet(session):
    la = LogicAnalyzer(session.device)
    period = la.start(rate_hz=1e6, pretrigger=1024)                # トリガ条件なし = プリトリガの後すぐ成立
    la.wait(timeout_s=5)
    cap = la.read(period)
    assert len(cap.samples) == la.depth and cap.trigger == 1024
    assert session.link.lost_frames == 0


def test_reopening_after_a_stream_and_a_dap_burst(session):
    # 配置によって、ストリームの読み出しの後に開き直すと CONTROL が化け・止まる版があった（2026-10-17、原因未特定。
    # docs/reports/2026-10-16-day.md）。その並びを繰り返して検出する
    from fprobe.events import EventLog
    for _ in range(3):
        with Session.open_eth(HOST) as s:
            log = EventLog(s.device)
            log.start()
            time.sleep(0.02)
            log.stop()
            assert log.read()
            with s.debug(target_override="stm32f401xe", frequency=4_000_000, connect_mode="attach") as debug:
                debug.target.read_memory_block32(0x20000000, 4096)
            s.device.write32(g.SYSTEM_SCRATCH, 0xC0FFEE)
            assert s.device.read32(g.SYSTEM_SCRATCH) == 0xC0FFEE
            assert s.link.lost_frames == 0
