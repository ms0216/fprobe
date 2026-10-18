"""CMSIS-DAP の性能（成功基準）: DAP_Info の往復 ≤ 250 µs（中央値）、DAP_TransferBlock の読み出し ≥ 1.5 MB/s
（SWCLK 25 MHz、コマンドを 2 つ先送り）。pyOCD で接続して SWD と MEM-AP を準備し、同じ USB でコマンドを直接送る。
pyOCD の読み出し（read_memory_block32）は、fprobe.session がページをまたいで先送りする版に置き換える（元は 1.12 MB/s）。"""

import statistics
import struct
import time

import pytest

pytest.importorskip("pyocd")
from fprobe.session import Session  # noqa: E402
from pyocd.core.exceptions import TransferFaultError  # noqa: E402
from tests.hil.program import SERIAL, program  # noqa: E402

pytestmark = pytest.mark.hil

SRAM = 0x20000000
WORDS = 126                                         # 応答 = 4 + 504 バイト（パケット 512 バイトに収まる）


@pytest.fixture(scope="module")
def target():
    program("fprobe")
    with Session.open(SERIAL) as s, s.debug(target_override="stm32f401xe", frequency=25_000_000,
                                            connect_mode="attach") as debug:
        target = debug.target
        target.halt()
        target.read32(SRAM)                         # CSW（32 ビット・自動増加）と SELECT を pyOCD に設定させる
        yield target
        target.resume()


@pytest.fixture(scope="module")
def dap(target):
    return target.session.probe._link._interface   # pyOCD の USB インターフェース（受信はキュー経由）


def test_command_round_trip(dap):
    times = []
    for _ in range(200):
        start = time.perf_counter()
        dap.write([0x00, 0x01])                     # DAP_Info（ベンダ）
        dap.read()
        times.append(time.perf_counter() - start)
    median = statistics.median(times)
    print(f"DAP_Info 往復: 中央値 {median * 1e6:.0f} µs")
    assert median < 250e-6


def test_transfer_block_read_throughput(dap):
    dap.write([0x05, 0, 1, 0x05, *struct.pack("<I", SRAM)])     # AP TAR ← SRAM
    assert dap.read()[2] == 1
    command = [0x06, 0, *struct.pack("<H", WORDS), 0x0F]          # AP DRW を WORDS 回読む
    rounds, depth = 64, 2
    start = time.perf_counter()
    sent = received = 0
    while received < rounds:
        while sent - received < depth and sent < rounds:
            dap.write(command)
            sent += 1
        response = dap.read()
        assert response[0] == 0x06 and struct.unpack_from("<H", bytes(response), 1)[0] == WORDS and response[3] == 1
        received += 1
    rate = rounds * WORDS * 4 / (time.perf_counter() - start)
    print(f"DAP_TransferBlock 読み出し: {rate / 1e6:.2f} MB/s")
    assert rate >= 1.5e6


def test_pyocd_block_read_throughput(target):
    # fprobe.session の先送り版（ページ = 自動増加の境界ごとに応答を待たない）
    # 停止中のターゲットの SRAM を読む（書き換えない）。中身は 1 ページずつ読んだもの（応答を毎回待つ）と比べる
    size, page = 64 * 1024, 4096
    expected = [w for a in range(SRAM, SRAM + size, page) for w in target.read_memory_block32(a, page // 4)]
    times = []
    for _ in range(5):                              # PC の負荷でぶれるので最良で見る
        start = time.perf_counter()
        assert target.read_memory_block32(SRAM, size // 4) == expected
        times.append(time.perf_counter() - start)
    rate = size / min(times)
    print(f"pyOCD read_memory_block32: {rate / 1e6:.2f} MB/s")
    assert rate >= 1.4e6


def test_pyocd_block_read_reports_the_faulting_page(target):
    # SRAM（96 KB）の終わりをまたぐ読み: 2 ページ目（0x20018000〜）でフォールトし、その番地が付く
    with pytest.raises(TransferFaultError) as fault:
        target.read_memory_block32(0x20017000, 0x2000 // 4)
    assert fault.value.fault_address == 0x20018000
    assert target.read_memory_block32(SRAM, 4) == target.read_memory_block32(SRAM, 4)   # その後も使える
