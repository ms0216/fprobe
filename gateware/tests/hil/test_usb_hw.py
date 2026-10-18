"""Primer 20K 実機での USB HS デバイス確認（usbtest デザイン）。`pytest gateware --hil`。

前提: Primer の USB-OTG ポート（USB3317）を PC に接続し、DEVICE/HOST スイッチを DEVICE にする。
USB-JTAG ポート（BL702）は書き込みと診断表示に使う。

  EP1 OUT/IN: 制御チャンネル。メッセージ先頭 0x00 は折り返し、それ以外は破棄（sys ドメイン経由）
  EP6 IN:     連番ストリーム（IN スループット計測）
  CDC-ACM:    折り返し（Windows の COM ポートとして使う）
"""

import subprocess
import sys
import threading
import time

import pytest
import serial
import usb.core
import usb.util
from libusb_package import get_libusb1_backend
from serial.tools import list_ports

from fprobe_gw.usb.fprobe_device import CONTROL_INTERFACE, EP_CONTROL, EP_STREAM, PID, VID

pytestmark = pytest.mark.hil

THROUGHPUT_TARGET = 30e6        # バイト/秒（設計書 §1 の完了条件）
LIBUSB_SPEED_HIGH = 3


def find_device(timeout_s: float = 10.0):
    backend = get_libusb1_backend()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        dev = usb.core.find(idVendor=VID, idProduct=PID, backend=backend)
        if dev is not None:
            return dev
        time.sleep(0.2)
    pytest.fail("fprobe（1209:0001）が見つからない。USB-OTG ポートの接続と DEVICE スイッチを確認")


@pytest.fixture(scope="module")
def device():
    subprocess.run([sys.executable, "-m", "fprobe_gw", "build", "usbtest", "--program"], check=True)
    time.sleep(1.0)
    dev = find_device()
    usb.util.claim_interface(dev, CONTROL_INTERFACE)
    yield dev
    usb.util.release_interface(dev, CONTROL_INTERFACE)
    usb.util.dispose_resources(dev)


def test_enumerated_at_high_speed(device):
    assert device.speed == LIBUSB_SPEED_HIGH
    assert usb.util.get_string(device, device.iProduct) == "fprobe CMSIS-DAP"


def test_control_channel_echoes_messages(device):
    for size in (1, 2, 63, 64, 511, 512):
        message = bytes([0x00]) + bytes((i * 7) & 0xFF for i in range(size - 1))
        device.write(EP_CONTROL, message, timeout=1000)
        assert bytes(device.read(0x80 | EP_CONTROL, 4096, timeout=1000)) == message, size


def test_in_throughput(device):
    total, chunk = 64 * 1024 * 1024, 1024 * 1024
    received = 0
    start = time.perf_counter()
    while received < total:
        received += len(device.read(0x80 | EP_STREAM, chunk, timeout=2000))
    rate = received / (time.perf_counter() - start)
    print(f"IN {rate / 1e6:.1f} MB/s")
    assert rate >= THROUGHPUT_TARGET


def test_stream_is_continuous(device):
    data = bytes(device.read(0x80 | EP_STREAM, 256 * 1024, timeout=2000))
    assert all((b - a) % 256 == 1 for a, b in zip(data, data[1:]))


def test_out_throughput(device):
    message = bytes([0x01]) + bytes(511)           # 先頭 0x01: 応答器が破棄する
    payload = message * 2048                       # 1 MiB（512 バイトのメッセージの連続）
    total = 64 * 1024 * 1024
    sent = 0
    start = time.perf_counter()
    while sent < total:
        sent += device.write(EP_CONTROL, payload, timeout=2000)
    rate = sent / (time.perf_counter() - start)
    print(f"OUT {rate / 1e6:.1f} MB/s")
    assert rate >= THROUGHPUT_TARGET


def test_cdc_acm_com_port_loopback(device):
    ports = [p.device for p in list_ports.comports() if p.vid == VID and p.pid == PID]
    assert len(ports) == 1, ports
    payload = bytes(range(256)) * 64
    with serial.Serial(ports[0], 115200, timeout=0.2) as port:
        port.reset_input_buffer()
        received = bytearray()
        reader = threading.Thread(target=lambda: [received.extend(port.read(4096))
                                                  for _ in range(100) if len(received) < len(payload)])
        reader.start()
        port.write(payload)
        port.flush()
        reader.join()
    assert bytes(received) == payload
