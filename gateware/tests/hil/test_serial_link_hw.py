"""M11: SerialLink（UART のホストリンク）で、USB 版と同じ fprobe_core を同じホストソフトから使う（移植性の確認）。

Primer 20K に fprobe_serial（BL702 の USB-UART、外部メモリなし、内蔵ロジアナ）を書き込み、COM ポート越しに
ケイパビリティ・CSR・pyOCD のデバッグ・ロジアナを使う。COM ポートは Primer の BL702 のシリアル番号で選ぶ
（他の機器のポートは開かない）。
"""

import random
import subprocess
import sys
import time

import pytest

pytest.importorskip("pyocd")
from serial.tools import list_ports  # noqa: E402

from fprobe import generated as g  # noqa: E402
from fprobe.la import LogicAnalyzer  # noqa: E402
from fprobe.session import Session  # noqa: E402
from fprobe_gw.boards.primer20k import PRIMER20K  # noqa: E402

pytestmark = pytest.mark.hil

SRAM = 0x20004000


def primer_com_port() -> str:
    identity = PRIMER20K.jtag_identity
    ports = [p.device for p in list_ports.comports() if p.serial_number == identity.usb_serial]
    assert len(ports) == 1, ports
    return ports[0]


@pytest.fixture(scope="module")
def session():
    subprocess.run([sys.executable, "-m", "fprobe_gw", "build", "fprobe_serial", "--program"], check=True)
    time.sleep(0.5)
    with Session.open_serial(primer_com_port(), PRIMER20K.serial_baud) as s:
        yield s


def test_same_core_answers_over_the_serial_link(session):
    caps = session.device.capabilities()
    assert (caps.design, caps.host_link) == ("fprobe_serial", g.LINK_SERIAL)
    assert caps.la_depth == 8192 and not caps.mem_bytes
    session.device.write32(g.SYSTEM_SCRATCH, 0x5A5AC3C3)
    assert session.device.read32(g.SYSTEM_SCRATCH) == 0x5A5AC3C3


def test_pyocd_debugs_the_target_over_the_serial_link(session):
    with session.debug(target_override="stm32f401xe", frequency=4_000_000, connect_mode="attach") as debug:
        t = debug.target
        assert t.read32(0xE000ED00) == 0x410FC241
        words = [random.Random(9).getrandbits(32) for _ in range(256)]
        t.write_memory_block32(SRAM, words)
        assert t.read_memory_block32(SRAM, len(words)) == words


def test_internal_logic_analyzer_over_the_serial_link(session):
    la = LogicAnalyzer(session.device)
    period = la.start(rate_hz=1e6, pretrigger=100)                 # トリガ条件なし = すぐ成立
    la.wait(timeout_s=2)
    cap = la.read(period)
    assert len(cap.samples) == 8192 and cap.trigger == 100
