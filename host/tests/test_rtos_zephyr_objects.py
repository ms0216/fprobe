"""fprobe.rtos の Zephyr のカーネルオブジェクト（U08）: zephyr_shape.elf の .data の初期値の「止めたときの様子」から。

k_sem・k_mutex（継承の無い逆転）・k_msgq・k_timer（timeout_list の差分から残り）・k_event・k_fifo・k_heap。
"""

from pathlib import Path

import pytest

from fprobe.dwarf.access import ImageAccess
from fprobe.dwarf.info import DebugInfo
from fprobe.rtos.contention import contention
from fprobe.rtos.zephyr import Zephyr
from fprobe.symbols import Symbols

ELF = Path(__file__).resolve().parents[1] / "firmware" / "zephyr_shape" / "zephyr_shape.elf"


@pytest.fixture(scope="module")
def image():
    return DebugInfo(ELF), Symbols(ELF)


@pytest.fixture
def rtos(image):
    info, symbols = image
    return Zephyr(info, ImageAccess.from_symbols(symbols), symbols)


def by_name(objects):
    return {o.name: o for o in objects}


def test_objects_by_type(rtos, image):
    _, symbols = image
    objects = by_name(rtos.objects())
    assert {n: o.kind for n, o in objects.items()} == {
        "sem_data": "k_sem", "bus_lock": "k_mutex", "cmd_msgq": "k_msgq", "sys_events": "k_event",
        "watchdog_timer": "k_timer", "blink_timer": "k_timer", "stopped_timer": "k_timer", "rx_fifo": "k_fifo",
        "_system_heap": "k_heap"}
    assert all(o.source == "variable" for o in objects.values())
    assert objects["sem_data"].handle == symbols.variable("sem_data").address


def test_semaphore_and_its_waiter(rtos):
    sem = by_name(rtos.objects())["sem_data"]
    assert sem.fields == {"count": 0, "max": 1}
    assert [(w.task, w.reason, w.priority) for w in sem.waiters] == [("sensor", "pend", 5)]


def test_mutex_without_inheritance_is_an_inversion(rtos):
    tasks = rtos.tasks()
    objects = rtos.objects(tasks=tasks)
    mutex = by_name(objects)["bus_lock"]
    assert mutex.fields["holder"] == {"task": "sleeper", "handle": mutex.holder, "priority": 7, "base_priority": 7,
                                      "inherited": False}
    assert mutex.fields["lock_count"] == 1 and not mutex.fields["available"]
    (inv,) = contention(tasks, objects, rtos.priority_order)["inversions"]
    assert rtos.priority_order == -1                                    # Zephyr は小さい方が高い
    assert inv["waiter"]["task"] == "ctl" and not inv["inherited"]
    assert [t["task"] for t in inv["interferers"]] == ["logger"]        # 4: 2 と 7 の間で実行可能


def test_message_queue_event_and_fifo(rtos):
    objects = by_name(rtos.objects())
    assert objects["cmd_msgq"].fields == {"msg_size": 4, "max_msgs": 8, "used_msgs": 3, "free": 5}
    assert objects["sys_events"].fields == {"events": 0x5}
    assert objects["rx_fifo"].fields == {"items": 2}


def test_timers_remaining_from_the_timeout_list(rtos):
    objects = by_name(rtos.objects())
    blink = objects["blink_timer"].fields
    assert blink == {"period_ticks": 100, "active": True, "remaining_ticks": 50, "expiry_count": 4,
                     "callback": "blink_expired", "user_data": 0x1234}
    assert objects["watchdog_timer"].fields["remaining_ticks"] == 20
    stopped = objects["stopped_timer"].fields
    assert not stopped["active"] and "remaining_ticks" not in stopped


def test_heap_runtime_stats(rtos):
    heap = rtos.heap().to_dict()
    assert heap["scheme"] == "sys_heap" and heap["total"] == 4000
    assert (heap["free"], heap["min_free"], heap["used"], heap["peak_used"]) == (3000, 2500, 1000, 1500)
    assert by_name(rtos.objects())["_system_heap"].fields == {"free_bytes": 3000, "allocated_bytes": 1000,
                                                              "max_allocated_bytes": 1500}


def test_kinds_filter(rtos):
    assert [o.name for o in rtos.objects(kinds=["k_timer"])] == ["blink_timer", "stopped_timer", "watchdog_timer"]


def test_words_for_the_sampler(rtos, image):
    _, symbols = image
    assert rtos.idle_handles() == {symbols.variable("idle_thread").address}
    mutex = by_name(rtos.objects())["bus_lock"]
    holder_word, head_word, _ = rtos.mutex_words(mutex)
    assert holder_word == mutex.handle + 8 and head_word == mutex.handle    # owner、wait_q.waitq.head
    assert rtos.mutex_empty_head(mutex) == mutex.handle
    sleeper = symbols.variable("sleeper_thread").address
    assert rtos.saved_sp_address(sleeper) > sleeper
    assert rtos.handle_priority(sleeper) == 7
    address, total = rtos.heap_free_word()
    assert address == symbols.variable("system_heap_state").address + 24 and total == 4000   # free_bytes（chunk0_hdr 16 + 4 + 4）
