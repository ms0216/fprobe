"""Primer 20K 実機での bringup 確認。`pytest gateware --hil` で実行する。

安全な書き込み経路（CLI → GowinProgrammer）で SRAM に書き込み、BL702 の仮想 COM で検証する。
"""

import subprocess
import sys
import threading
import time

import pytest

from fprobe_gw.boards.primer20k import PRIMER20K
from tests.hil.console import primer_console

pytestmark = pytest.mark.hil

@pytest.fixture(scope="module")
def console():
    subprocess.run([sys.executable, "-m", "fprobe_gw", "build", "bringup", "--program"],
                   check=True)
    with primer_console() as port:
        yield port


def test_echo(console):
    console.write(b"fprobe!")
    assert console.read(7) == b"fprobe!"


def test_reported_frequency_matches_pll_solution(console):
    console.reset_input_buffer()
    console.write(b"?")
    line = console.readline().decode()
    assert line == (f"fprobe-bringup sync_hz={PRIMER20K.sync_pll.fout_hz} "
                    f"rev={PRIMER20K.revision}\r\n")


def echo_roundtrip(port, payload: bytes, *, timeout_s: float) -> bytes:
    """送信と並行して受信する。書き終えてから読むと PC 側の受信バッファ（数 KB）が溢れて欠ける。"""
    received = bytearray()
    done = threading.Event()

    def reader():
        deadline = time.monotonic() + timeout_s
        while not done.is_set() and len(received) < len(payload) and time.monotonic() < deadline:
            received.extend(port.read(4096))

    port.reset_input_buffer()
    port.timeout = 0.1
    thread = threading.Thread(target=reader)
    thread.start()
    port.write(payload)
    port.flush()
    thread.join(timeout_s)
    done.set()
    thread.join()
    return bytes(received)


PLAIN = bytes(b for b in range(32, 127) if b != ord("?"))   # '?' はバナー要求なので除く


def test_sustained_echo_is_lossless(console):
    payload = PLAIN * 8
    assert echo_roundtrip(console, payload, timeout_s=5) == payload


def test_long_sustained_echo_is_lossless(console):
    # 約 3 秒の連続送信。ホストとのボーレート差で FIFO が溜まり続ける設計なら、ここで欠ける
    payload = PLAIN * 350   # 32,900 バイト
    assert echo_roundtrip(console, payload, timeout_s=10) == payload
