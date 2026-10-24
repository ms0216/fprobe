"""trace の RTT の上りのチャンネル: 名前（SEGGER の "SysView" 等、形式が決める名前）が無ければ 0 番と決めない（点検 2 回目 ?:
probe_formats.py:183-195）。名前の決まっていない形式で channel を省けば SEGGER RTT の規定の端末のチャンネル 0。"""

from types import SimpleNamespace

import pytest

from fprobe.probe_formats import FormatTools

UP = [SimpleNamespace(index=0, name="Terminal"), SimpleNamespace(index=1, name="SysView")]


def pick(up, channel, format):
    return FormatTools._rtt_channel_index(up, channel, format)


def test_the_name_the_format_defines_is_found():
    assert pick(UP, None, "systemview") == 1


def test_an_explicit_channel_is_used():
    assert pick(UP, 0, "ctf") == 0 and pick(UP, "Terminal", "ctf") == 0


def test_a_missing_defined_name_is_refused_not_channel_0():
    with pytest.raises(ValueError, match="SysView|channel"):
        pick([SimpleNamespace(index=0, name="Terminal")], None, "systemview")


def test_a_format_without_a_defined_name_uses_the_segger_terminal_channel():
    assert pick(UP, None, "ctf") == 0
    with pytest.raises(ValueError, match="channel"):
        pick([SimpleNamespace(index=1, name="Data")], None, "ctf")
