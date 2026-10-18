"""実機テスト共通: Primer 20K の BL702 仮想 COM を開く。"""

import time
from contextlib import contextmanager

import pytest
import serial
from serial.tools import list_ports

PRIMER_USB_SERIAL = "FACTORYAIOT_PRO"


def find_primer_console() -> str:
    ports = [p.device for p in list_ports.comports() if p.serial_number == PRIMER_USB_SERIAL]
    if len(ports) != 1:
        pytest.fail(f"Primer の COM ポートを一意に特定できない: {ports}")
    return ports[0]


@contextmanager
def primer_console(baud: int = 115200, *, drain_s: float = 0.3):
    """COM を開き、前のデザインが送った残りのデータを読み捨ててから渡す。

    BL702 は書き込み前のデザインが送ったバイトを内部に溜めていることがあり、
    reset_input_buffer だけでは後から届く分を消せない。
    """
    with serial.Serial(find_primer_console(), baud, timeout=0.05) as port:
        deadline = time.monotonic() + drain_s
        while time.monotonic() < deadline:
            if port.read(4096):
                deadline = time.monotonic() + drain_s   # 届き続ける間は待つ
        port.timeout = 2
        yield port
