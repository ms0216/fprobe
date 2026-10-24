"""補助マイコンの電圧の live: 「待った間に計測値の数が増えたか」の事実に、待った時間 window_s と前後の数を添える
（点検 2 回目 ?: aux_mcu.py:42-47。live の判定の窓が暗黙にならないように）。"""

from types import SimpleNamespace

import pytest

from fprobe.aux_mcu import Aux


class FakeDevice:
    def __init__(self, before: int, after: int):
        self.before, self.after = before, after

    def capabilities(self):
        return SimpleNamespace(aux=True)

    def read32(self, address):
        return self.before

    def read(self, address, count):
        return [3300, self.after, 0]


def test_reading_carries_the_window_and_both_counts():
    r = Aux(FakeDevice(10, 12)).read(settle_s=0.002)
    d = r.as_dict()
    assert d["live"] is True and d["window_s"] == 0.002 and (d["samples_before"], d["samples"]) == (10, 12)
    assert d["volts"] == pytest.approx(3.3)
    assert Aux(FakeDevice(10, 10)).read(settle_s=0.002).as_dict()["live"] is False
