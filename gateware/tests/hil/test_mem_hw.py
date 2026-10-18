"""Primer 20K の DDR3（M8 第 1 段）: 初期化・較正が終わり、全容量を誤りなく読み書きでき、帯域が足りること。"""


import pytest

from tests.hil.program import program  # noqa: E402
from fprobe import Device
from fprobe.memory import Memory
from fprobe_gw.boards.primer20k import PRIMER20K
from fprobe_gw.usb.fprobe_device import STRINGS, STR_SERIAL

pytestmark = pytest.mark.hil

MEM_HZ = PRIMER20K.ddr3.user_clock_hz
LA_BYTES_PER_S = 100e6 * 2                     # ロジアナ 16 ch × 100 MS/s


@pytest.fixture(scope="module")
def memory():
    program("memtest")
    with Device.open(STRINGS[STR_SERIAL - 1]) as dev:
        m = Memory(dev)
        m.wait_ready()
        yield m


def test_capacity_is_announced(memory):
    assert memory.size_bytes == PRIMER20K.ddr3.size_bytes == 128 * 1024 * 1024


@pytest.mark.parametrize("seed", [0x5EED1234, 0xFFFFFFFF])
def test_whole_memory_round_trips(memory, seed):
    result = memory.test(seed=seed, mem_clock_hz=MEM_HZ)
    assert (result.errors, result.first_error) == (0, None)
    assert result.write_bytes_per_s > 2 * LA_BYTES_PER_S and result.read_bytes_per_s > 2 * LA_BYTES_PER_S
