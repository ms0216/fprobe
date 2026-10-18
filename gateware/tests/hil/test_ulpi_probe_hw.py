"""Primer 20K 実機で USB3317 との ULPI レジスタアクセスを確認する。`pytest gateware --hil`。

OTG ケーブルは不要（PHY は基板から給電され、CLKOUT は水晶から生成される）。
F10（RESETB）は Ethernet PHY と共用だが、本デザインは起動時に一度パルスするだけ。
"""

import subprocess
import sys

import pytest

from tests.hil.console import primer_console

pytestmark = pytest.mark.hil

EXPECTED = b"ulpi vid=0424 pid=0006 scratch=A5/5A fc=41\r\n"


@pytest.fixture(scope="module")
def console():
    subprocess.run([sys.executable, "-m", "fprobe_gw", "build", "ulpi_probe", "--program"],
                   check=True)
    with primer_console() as port:
        yield port


def test_register_access_is_correct_and_stable(console):
    # NXT の扱いが 1 バイトずれていれば Scratch の値や ID が崩れる。繰り返して安定性も見る
    for i in range(200):
        console.write(b"?")
        assert console.readline() == EXPECTED, f"iteration {i}"
