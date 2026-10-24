"""rtos.timeline.interval の task: 同じ名前のタスクが複数あれば最初のものと決めない（点検 2 回目 ?:
probe_rtos_analysis.py:123-126）。番地（"0x..."）で選べる。"""

import pytest

from fprobe.probe_rtos_analysis import task_handle

NAMES = {0x2000_0100: "worker", 0x2000_0200: "worker", 0x2000_0300: "sensor"}


def test_a_unique_name_gives_its_handle():
    assert task_handle(NAMES, "sensor") == 0x2000_0300


def test_a_shared_name_is_refused_with_the_handles():
    with pytest.raises(KeyError) as e:
        task_handle(NAMES, "worker")
    assert "0x20000100" in e.value.args[0] and "0x20000200" in e.value.args[0]


def test_a_handle_picks_the_task():
    assert task_handle(NAMES, "0x20000200") == 0x2000_0200
    with pytest.raises(KeyError):
        task_handle(NAMES, "0x20000400")
    with pytest.raises(KeyError):
        task_handle(NAMES, "nobody")
