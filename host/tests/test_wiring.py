"""fprobe.wiring.find_swd_pins: 読めた組を全部返す（最初の 1 つで決めない。3 回目の点検 ?74）。"""

from fprobe import wiring
from fprobe.wiring import SwdPins, find_swd_pins


def test_every_pair_that_answers_is_returned(monkeypatch):
    answers = {(0, 1): 0x2BA01477, (2, 3): 0x0BC12477}
    monkeypatch.setattr(wiring, "read_dpidr", lambda dev, swclk, swdio: answers.get((swclk, swdio)))
    assert find_swd_pins(None, pins=4) == [SwdPins(0, 1, 0x2BA01477), SwdPins(2, 3, 0x0BC12477)]
    monkeypatch.setattr(wiring, "read_dpidr", lambda dev, swclk, swdio: None)
    assert find_swd_pins(None, pins=4) == []
