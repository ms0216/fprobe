"""不具合の再現試験（修正済み。台帳: docs/reports/bugs/correlate-recording.md）。領域: obs_condition / obs_value の条件の意味。"""

from fprobe.obs_condition import compile_match, count_condition
from fprobe.fpga_signals import FpgaSignals

SIGNALS = FpgaSignals.from_dict({"groups": {"0": [
    {"name": "state", "lsb": 0, "width": 2, "format": "enum", "enum": {"0": "IDLE", "1": "BUSY", "2": "DONE"}}]}})


def _items(*states):
    return [{"kind": "value", "group": 0, "time_us": 10 * i, "fields": {"state": s}} for i, s in enumerate(states)]


def test_rose_on_enum_signal_from_zero_value():
    """BUG-OBS-01: 列挙の名前は信号の定義の表で数に直して比べる（値 0 の名前 IDLE は「オフ」）。"""
    assert count_condition(_items("IDLE", "BUSY"), "state rose", SIGNALS)["count"] == 1
    assert count_condition(_items("BUSY", "IDLE"), "state fell", SIGNALS)["count"] == 1
    assert count_condition(_items("BUSY", "DONE"), "state rose", SIGNALS)["count"] == 0


def test_equals_number_matches_enum_name_like_compile_match():
    """BUG-OBS-02: state=1 も state=BUSY も同じ値に一致（compile_match と同じ）。"""
    mask, value, change = compile_match("state changed and state=1", SIGNALS)    # FPGA 側は 1 = BUSY として一致する
    assert value == 1
    assert count_condition(_items("IDLE", "BUSY"), "state changed and state=1", SIGNALS)["count"] == 1
    assert count_condition(_items("IDLE", "BUSY"), "state changed and state=BUSY", SIGNALS)["count"] == 1
    assert count_condition(_items("IDLE", "BUSY"), "state changed and state=2", SIGNALS)["count"] == 0
