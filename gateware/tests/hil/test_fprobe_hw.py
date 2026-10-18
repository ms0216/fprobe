"""Primer 20K 実機での fprobe 製品デザイン確認（M3: CSR・ケイパビリティ・タイムベース）。`pytest gateware --hil`。

前提: USB-OTG ポート（USB3317）を PC に接続し、DEVICE/HOST スイッチを DEVICE にする。
ホストライブラリ fprobe（host/）を通して操作する。
"""

import random
import time

import pytest

from tests.hil.program import program  # noqa: E402
from fprobe import ControlError, Device
from fprobe import generated as g
from fprobe.transport import DeviceNotFound
from fprobe_gw.boards.primer20k import PRIMER20K

pytestmark = pytest.mark.hil


def open_device(timeout_s: float = 10.0) -> Device:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            return Device.open()
        except DeviceNotFound:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.2)


@pytest.fixture(scope="module")
def device():
    program("fprobe")
    with open_device() as dev:
        yield dev


def test_capabilities_describe_this_design(device):
    caps = device.capabilities()
    assert caps.protocol_version == g.PROTOCOL_VERSION
    assert (caps.board, caps.design) == ("Tang Primer 20K Dock", "fprobe")
    assert caps.timebase_hz == caps.sys_clock_hz == PRIMER20K.sync_pll.fout_hz
    assert caps.host_link == g.LINK_USB_HS_ULPI and caps.channels == {g.CH_CONTROL, g.CH_DAP, g.CH_UART0}


def test_scratch_round_trips_many_values(device):
    rng = random.Random(1)
    for _ in range(500):
        value = rng.getrandbits(32)
        device.write32(g.SYSTEM_SCRATCH, value)
        assert device.read32(g.SYSTEM_SCRATCH) == value


def test_errors_are_reported(device):
    with pytest.raises(ControlError) as e:
        device.read(g.SYSTEM_SCRATCH, 2)                    # 2 ワード目は未定義
    assert e.value.status == g.STATUS_BUS_ERROR and e.value.done == 1
    with pytest.raises(ControlError):
        device.read32(0x0800_0000)                          # 未割り当て
    assert device.read32(g.SYSTEM_MAGIC) == g.SYSTEM_MAGIC_VALUE   # その後も正常


def test_timebase_tracks_real_time(device):
    hz = device.capabilities().timebase_hz
    t0, h0 = device.time(), time.perf_counter()
    time.sleep(2.0)
    t1, h1 = device.time(), time.perf_counter()
    ratio = (t1 - t0) / hz / (h1 - h0)
    print(f"timebase / host = {ratio:.5f}")
    assert abs(ratio - 1) < 0.01


def test_request_round_trip_latency(device):
    n = 2000
    start = time.perf_counter()
    for _ in range(n):
        device.read32(g.SYSTEM_SCRATCH)
    per_request = (time.perf_counter() - start) / n
    print(f"CSR 読み出し 1 回 {per_request * 1e6:.0f} us")
    assert per_request < 1e-3


def test_uart_baud_follows_com_port_settings(device):
    import serial
    from serial.tools import list_ports
    from fprobe_gw.usb.fprobe_device import PID, VID
    (port,) = [p.device for p in list_ports.comports() if p.vid == VID and p.pid == PID]
    for baud in (9_600, 921_600, 115_200):
        with serial.Serial(port, baud):
            time.sleep(0.1)
            assert device.read32(g.UART_BAUD) == baud
