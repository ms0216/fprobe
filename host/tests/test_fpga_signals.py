"""fprobe.fpga_signals: FPGA の観測の組に付ける信号の定義（名前・ビットの範囲・書式・列挙）と、画面の状態での共有。"""

import pytest

from fprobe.fpga_signals import FpgaSignals
from fprobe.probe_ui_state import UiStateTools

DEFS = {"groups": {"0": [
    {"name": "mark", "lsb": 0, "width": 1},
    {"name": "state", "lsb": 4, "width": 3, "format": "enum", "enum": {"0": "IDLE", "1": "RUN", "5": "ERR"}},
    {"name": "count", "lsb": 8, "width": 8, "format": "dec"},
]}}


def test_fields_split_a_group_value_into_named_signals():
    s = FpgaSignals.from_dict(DEFS)
    assert s.fields(0, 0x2A11) == {"mark": 1, "state": "RUN", "count": 42}
    assert s.fields(0, 0x0070)["state"] == "0x7"                    # 名前の無い列挙の値は 16 進
    assert s.fields(1, 0xBEEF) == {"value": 0xBEEF}                  # 定義の無い組は組の値のまま


def test_text_follows_the_format():
    s = FpgaSignals.from_dict({"groups": {"2": [{"name": "h", "lsb": 0, "width": 8, "format": "hex"},
                                                {"name": "b", "lsb": 8, "width": 4, "format": "bin"}]}})
    [h, b] = s.signals(2)
    assert (s.text(h, 0x3C), s.text(b, 0b0101)) == ("0x3C", "0b0101")


@pytest.mark.parametrize("bad, message", [
    ({"groups": {"0": [{"name": "a", "lsb": 0, "width": 4}, {"name": "b", "lsb": 3, "width": 2}]}}, "重なる"),
    ({"groups": {"0": [{"name": "a", "lsb": 12, "width": 8}]}}, "16 ビット"),
    ({"groups": {"0": [{"name": "a", "lsb": 0, "width": 1}, {"name": "a", "lsb": 1, "width": 1}]}}, "重複"),
    ({"groups": {"0": [{"name": "a", "lsb": 0, "width": 2, "format": "enum", "enum": {"4": "X"}}]}}, "幅"),
    ({"groups": {"0": [{"name": "a", "lsb": 0, "width": 1, "format": "oct"}]}}, "書式"),
    ({"groups": {"x": []}}, "組"),
    ({"groups": {"0": [{"name": "", "lsb": 0, "width": 1}]}}, "名前"),
])
def test_bad_definitions_are_rejected_with_a_reason(bad, message):
    with pytest.raises(ValueError, match=message):
        FpgaSignals.from_dict(bad)


def test_round_trip_normalizes_the_defaults():
    d = FpgaSignals.from_dict(DEFS).to_dict()
    assert d["groups"]["0"][0] == {"name": "mark", "lsb": 0, "width": 1, "format": "hex", "enum": {}}
    assert FpgaSignals.from_dict(d).to_dict() == d


class Holder(UiStateTools):
    def __init__(self):
        self._init_ui_state()
        self.sent = []
        self._publish = lambda topic, data: self.sent.append((topic, data))


def test_definitions_are_shared_through_the_screen_state():
    h = Holder()
    out = h.ui_state_set(fpga_signals=DEFS, origin="w1")
    assert out["changed"] == ["fpga_signals"]
    assert h.ui_state_get()["fpga_signals"] == FpgaSignals.from_dict(DEFS).to_dict()
    assert h.sent[-1][1]["state"]["fpga_signals"]["groups"]["0"][1]["enum"] == {"0": "IDLE", "1": "RUN", "5": "ERR"}
    with pytest.raises(ValueError):
        h.ui_state_set(fpga_signals={"groups": {"0": [{"name": "a", "lsb": 20, "width": 1}]}})
    assert h.ui_state_set(clear=["fpga_signals"])["fpga_signals"] is None
