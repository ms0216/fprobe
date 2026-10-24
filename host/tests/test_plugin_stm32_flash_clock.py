"""プラグイン stm32_flash_clock: STM32F2 / F4 / F7 の書き込みアルゴリズムの間のクロックの守り（本体から外した機器固有の処理）。"""

import pytest

from fprobe.flash import RegisterGuard
from fprobe.plugins import stm32_flash_clock as sfc


class FakeApi:
    def __init__(self):
        self.calls = []

    def call(self, method, /, **params):
        self.calls.append((method, params))
        return {"guard": params.get("guard")}


def test_the_guard_is_a_register_sequence_the_body_can_run():
    for family in ("f2", "f4", "f7"):
        RegisterGuard.from_dict(sfc.guard(family))                       # 本体の汎用の形に合う
    f7 = sfc.guard("f7")
    resets = [s for s in f7["exit"] if s["op"] == "write" and s["value"]]
    assert resets[0]["mask"] == 1 << 11                                  # F7 は ARTRST だけ
    with pytest.raises(ValueError):
        sfc.guard("h7")


def test_choosing_the_family_from_the_part_name_is_marked_as_a_convention():
    out = sfc.guard_for_part("STM32F401xE")
    assert out["heuristic"] and out["assumption"] and out["family"] == "f4" and out["guard"] == sfc.guard("f4")
    assert sfc.guard_for_part("STM32L432KC")["guard"] is None


def test_install_sets_the_guard_through_the_api():
    api = FakeApi()
    sfc.install(api, "f4")
    assert api.calls == [("flash.algorithm_guard.set", {"guard": sfc.guard("f4")})]
