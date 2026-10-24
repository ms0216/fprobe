"""fprobe.rtos.stackcheck: 塗りの残りを、底の何バイトが塗りか（閾値の幅）で分けず、底から続く塗りのバイト数（事実）で返す
（点検 3 回目の ?11）。"""

from fprobe.rtos import stackcheck
from fprobe.rtos.stackcheck import check_stack

BASE = 0x2000_0000


def reader(data: bytes):
    def read(address: int, n: int) -> bytes:
        return data[address - BASE:address - BASE + n]
    return read


def test_no_guard_width_threshold():
    assert not hasattr(stackcheck, "GUARD")


def test_a_short_fill_at_the_bottom_is_reported_as_it_is():
    # 底の 4 バイトだけ塗りのまま（16 バイト等の幅で「塗られていない」に分けない）
    data = b"\xa5" * 4 + b"\x00" * 60
    info = check_stack(reader(data), BASE, BASE + 64, BASE + 32, 0xA5)
    assert info.fill == "intact" and info.untouched == 4


def test_a_bottom_that_is_not_fill_is_none_with_the_run_above():
    data = b"\x11" * 8 + b"\xa5" * 24 + b"\x00" * 32
    info = check_stack(reader(data), BASE, BASE + 64, BASE + 48, 0xA5)
    assert info.fill == "none" and info.untouched is None
    assert info.fill_run_above == {"offset": 8, "length": 24, "searched": 48}


def test_all_fill_up_to_the_pointer():
    data = b"\xa5" * 64
    info = check_stack(reader(data), BASE, BASE + 64, BASE + 40, 0xA5)
    assert info.fill == "intact" and info.untouched == 40
