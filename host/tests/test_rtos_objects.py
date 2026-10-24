"""fprobe.rtos のカーネルオブジェクトの一覧（U08）: FreeRTOS のキュー・セマフォ・ミューテックス・タイマ・イベントグループ・
ストリーム / メッセージバッファとヒープ、止めた瞬間の優先度の逆転・デッドロック。

偽のメモリは rtos_kernel.FreeRTOSBuilder（freertos_demo.elf の像の上に組み立てる）。
"""

import pytest
from rtos_kernel import FreeRTOSBuilder, shared

from fprobe.rtos.contention import contention
from fprobe.rtos.freertos import FreeRTOS
from fprobe.rtos.objects import typed_variables

TICK = 1000


@pytest.fixture
def kernel():
    """ctrl（優先度 3）が log_mutex を待ち、持ち主は worker（1、継承で 3）。sensor（2）が実行中。fpu はイベントを待つ。"""
    k = FreeRTOSBuilder()
    k.set_tick(TICK)
    k.timer_lists()
    h = {name: k.task(name, prio) for name, prio in
         [("ctrl", 3), ("sensor", 2), ("worker", 1), ("fpu", 1), ("IDLE", 0)]}
    q = {"cmd_queue": k.queue("cmd_queue", "queue", 4, 4), "done_sem": k.queue("done_sem", "binary_semaphore", 1, 0),
         "log_mutex": k.queue("log_mutex", "mutex", 1, 0, holder=h["worker"])}
    q["slots"] = k.queue(None, "counting_semaphore", 3, 0)              # 登録簿に無い: 変数の名前で
    q["hidden"] = k.queue(None, "queue", 2, 8)                          # 登録簿にも変数にも無い: 待っているタスクから
    k.set_handle("cmd_queue", q["cmd_queue"])
    k.set_handle("slots", q["slots"])
    k.take_count(q["cmd_queue"], 3)
    k.take_count(q["slots"], 2)
    k.u32(h["worker"] + k.tcb.offset("uxPriority"), 3)                  # 継承で上がっている
    k.put_state(h["ctrl"], k.var("xSuspendedTaskList"))
    k.put_event(h["ctrl"], k.receivers(q["log_mutex"]))
    k.put_state(h["sensor"], k.ready(2))
    k.set_current(h["sensor"])
    k.put_state(h["worker"], k.ready(3))
    k.put_state(h["fpu"], k.var("xDelayedTaskList1"), TICK + 2)
    timers = {"tick_timer": k.timer("tick_timer", 10, True, TICK + 7, timer_id=1),
              "oneshot": k.timer("oneshot", 100, False, None, "oneshot_callback", 2)}
    k.set_handle("tick_timer", timers["tick_timer"])
    k.set_handle("oneshot_timer", timers["oneshot"])
    group = k.event_group(0x0000_0004)
    k.set_handle("sync_events", group)
    k.wait_bits(h["fpu"], group, 0x3, clear=True)
    stream = k.stream_buffer(64, bytes(range(10)), trigger=1, tail=60)  # 折り返して 10 バイト
    k.set_handle("log_stream", stream)
    message = k.stream_buffer(64, (4).to_bytes(4, "little") + b"abcd" + (2).to_bytes(4, "little") + b"xy",
                              message=True)
    k.set_handle("msg_buffer", message)
    k.stream_waiter(stream, h["IDLE"], receive=False)
    k.put_state(h["IDLE"], k.ready(0))
    k.heap_state(free=9000, minimum=8000, allocations=20, frees=2, blocks=[(18000, 1024), (19000, 512)])
    k.handles, k.queues, k.timers, k.group, k.stream, k.message = h, q, timers, group, stream, message
    return k


def rtos_of(k: FreeRTOSBuilder) -> FreeRTOS:
    info, symbols = shared()
    return FreeRTOS(info, k.memory, symbols)


def by_name(objects):
    return {o.name: o for o in objects}


def test_variables_holding_handles_are_found_through_types(kernel):
    info, _ = shared()
    found = {f.expression: f for f in typed_variables(info, rtos_of(kernel).mem, {"QueueDefinition": "queue"}).found}
    assert found["cmd_queue"].address == kernel.queues["cmd_queue"] and found["cmd_queue"].pointer
    assert "done_sem" not in found                                    # ハンドルの変数が 0（作る前）は出さない


def test_the_type_search_reports_what_its_limits_left_unread(kernel):
    # 黙って数え止めない（総点検）: 配列の要素・入れ子の深さ・読むハンドルの上限で見ていない所を事実として返す
    info, _ = shared()
    mem = rtos_of(kernel).mem
    search = typed_variables(info, mem, {"xMINI_LIST_ITEM": "mini"})
    assert len([f for f in search.found if f.expression.startswith("pxReadyTasksLists")]) == 5
    assert search.elements_cut == [] and search.depth_cut == [] and search.notes() == []
    cut = typed_variables(info, mem, {"xMINI_LIST_ITEM": "mini"}, max_elements=2)
    assert [f.expression for f in cut.found if f.expression.startswith("pxReadyTasksLists")] == [
        "pxReadyTasksLists[0].xListEnd", "pxReadyTasksLists[1].xListEnd"]
    assert cut.elements_cut == [("pxReadyTasksLists", 5)] and cut.limits["max_elements"] == 2
    assert any("pxReadyTasksLists（5 要素）" in n and "2" in n for n in cut.notes())
    shallow = typed_variables(info, mem, {"xMINI_LIST_ITEM": "mini"}, max_depth=1)
    assert shallow.depth_cut == [f"pxReadyTasksLists[{i}]" for i in range(5)]
    assert "xDelayedTaskList1.xListEnd" in [f.expression for f in shallow.found]
    handles = typed_variables(info, mem, {"xLIST": "list"}, max_pointers=1)
    assert handles.pointers_unread >= 1 and any("上限 1" in n for n in handles.notes())


def test_queues_semaphores_and_mutexes(kernel):
    objects = by_name(rtos_of(kernel).objects())
    cmd = objects["cmd_queue"]
    assert (cmd.kind, cmd.source, cmd.handle) == ("queue", "registry", kernel.queues["cmd_queue"])
    assert cmd.fields["length"] == 4 and cmd.fields["item_size"] == 4 and cmd.fields["messages"] == 3
    assert cmd.fields["free"] == 1 and cmd.aliases == []               # 変数 cmd_queue も同じ名前
    slots = objects["slots"]
    assert (slots.kind, slots.source, slots.fields["count"], slots.fields["max"]) == ("counting_semaphore", "variable",
                                                                                     2, 3)
    mutex = objects["log_mutex"]
    assert mutex.kind == "mutex" and not mutex.fields["available"]
    assert mutex.fields["holder"] == {"task": "worker", "handle": kernel.handles["worker"], "priority": 3,
                                      "base_priority": 1, "inherited": True}
    assert [(w.task, w.reason, w.priority) for w in mutex.waiters] == [("ctrl", "receive", 3)]


def test_objects_found_only_through_a_waiting_task_are_named_by_address(kernel):
    k = kernel
    producer = k.task("producer", 1)                                    # 名前の無いキューに送ろうとして待っている
    k.put_state(producer, k.var("xSuspendedTaskList"))
    k.put_event(producer, k.senders(k.queues["hidden"]))
    # 総点検 T9: 登録簿にも変数にも無い待ちリストの持ち主を、「キューらしさ」の閾値で推定しない。待ちリストの番地と
    # 待っているタスクの事実だけを返す（持ち主の種類は unknown）
    objects = by_name(rtos_of(k).objects())
    lst = k.senders(k.queues["hidden"])
    hidden = objects[f"0x{lst:08X}"]
    assert hidden.source == "waiter" and hidden.kind == "unknown" and hidden.wait_lists == (lst,)
    assert [(w.task, w.reason) for w in hidden.waiters] == [("producer", "event")]          # 待ちの理由も持ち主から推さない


def test_timers(kernel):
    objects = by_name(rtos_of(kernel).objects())
    tick = objects["tick_timer"]
    assert tick.kind == "timer" and tick.source == "kernel"
    assert tick.fields == {"period_ticks": 10, "auto_reload": True, "active": True, "expiry_tick": TICK + 7,
                           "remaining_ticks": 7, "callback": "tick_timer_callback", "id": 1}
    assert tick.aliases == []                                           # 変数 tick_timer も同じ名前
    oneshot = objects["oneshot"]
    assert not oneshot.fields["active"] and not oneshot.fields["auto_reload"] and "expiry_tick" not in oneshot.fields
    assert oneshot.fields["callback"] == "oneshot_callback" and oneshot.source == "variable"
    assert oneshot.aliases == ["oneshot_timer"]                         # 名前はタイマ自身の名前、変数は別名


def test_event_groups_with_waiting_bits(kernel):
    group = by_name(rtos_of(kernel).objects())["sync_events"]
    assert group.kind == "event_group" and group.fields["bits"] == 0x4
    (w,) = group.waiters
    assert (w.task, w.reason, w.detail) == ("fpu", "bits", {"bits": 0x3, "all": False, "clear_on_exit": True})


def test_stream_and_message_buffers(kernel):
    objects = by_name(rtos_of(kernel).objects())
    stream = objects["log_stream"]
    assert stream.kind == "stream_buffer"
    assert {k: stream.fields[k] for k in ("capacity", "used", "free", "trigger_level")} == {
        "capacity": 64, "used": 10, "free": 54, "trigger_level": 1}
    assert [(w.task, w.reason) for w in stream.waiters] == [("IDLE", "send")]
    message = objects["msg_buffer"]
    assert message.kind == "message_buffer" and message.fields["used"] == 14
    assert message.fields["messages"] == 2 and message.fields["next_message"] == 4
    # 長さの語の大きさは 4 バイトと仮定せず、FreeRTOS の stream_buffer.c の configMESSAGE_BUFFER_LENGTH_TYPE の変数の
    # DWARF の型から決める（事実）
    assert message.fields["message_length_bytes"] == 4
    assert "configMESSAGE_BUFFER_LENGTH_TYPE" in message.fields["message_length_from"]


def test_messages_are_not_counted_when_the_length_type_is_unknown(kernel, monkeypatch):
    import fprobe.rtos.freertos_objects as fo
    monkeypatch.setattr(fo, "MESSAGE_LENGTH_VARIABLES", {"no_such_function": "x"})
    message = by_name(rtos_of(kernel).objects())["msg_buffer"]
    assert message.fields["messages"] is None and message.fields["message_length_bytes"] is None
    assert any("configMESSAGE_BUFFER_LENGTH_TYPE" in n for n in message.notes)


def test_messages_counted_up_to_the_limit_say_so(kernel, monkeypatch):
    # 黙って数え止めない（総点検）: メッセージを数える上限で止めたら、止めた印と上限を返す
    import fprobe.rtos.freertos_objects as fo
    monkeypatch.setattr(fo, "MAX_MESSAGES", 1)
    message = by_name(rtos_of(kernel).objects())["msg_buffer"]
    assert message.fields["messages"] == 1 and message.fields["messages_truncated"] is True
    assert any("上限 1" in n for n in message.notes)
    monkeypatch.setattr(fo, "MAX_MESSAGES", 1024)
    assert "messages_truncated" not in by_name(rtos_of(kernel).objects())["msg_buffer"].fields


def test_kinds_filter_and_sorting(kernel):
    objects = rtos_of(kernel).objects(kinds=["timer"])
    assert [o.name for o in objects] == ["oneshot", "tick_timer"]


def test_heap_4(kernel):
    """方式は変数の有無からの推定でなく、ヒープの変数を定めた源のファイル（DWARF の decl_file）。総量は ucHeap の
    大きさ（configTOTAL_HEAP_SIZE）そのもの（port で違う portBYTE_ALIGNMENT を決め打ちで引かない）。"""
    heap = rtos_of(kernel).heap()
    d = heap.to_dict()
    assert d["scheme"] == "heap_4" and d["source"] == "heap_4.c" and d["total"] == 20 * 1024
    assert (d["free"], d["min_free"], d["allocations"], d["frees"]) == (9000, 8000, 20, 2)
    assert d["used"] == 20 * 1024 - 9000 and d["peak_used"] == 20 * 1024 - 8000
    assert d["free_blocks"] == 2 and d["largest_free_block"] == 1024


def test_heap_before_the_first_allocation(kernel):
    kernel.u32(kernel.var("xStart"), 0)
    heap = rtos_of(kernel).heap().to_dict()
    assert heap["free_blocks"] is None and any("初期化" in n for n in heap["notes"])


def test_snapshot_contention_finds_the_inversion(kernel):
    rtos = rtos_of(kernel)
    tasks = rtos.tasks()
    result = contention(tasks, rtos.objects(tasks=tasks), rtos.priority_order)
    (inv,) = result["inversions"]
    assert inv["mutex"] == "log_mutex" and inv["waiter"]["task"] == "ctrl" and inv["holder"]["task"] == "worker"
    assert inv["inherited"] and inv["interferers"] == []                  # 継承で 3 に上がり、間の sensor（2）は割り込めない
    assert result["deadlocks"] == []


def test_snapshot_contention_without_inheritance_lists_interferers(kernel):
    k = kernel
    k.u32(k.handles["worker"] + k.tcb.offset("uxPriority"), 1)             # 継承していない
    rtos = rtos_of(k)
    tasks = rtos.tasks()
    (inv,) = contention(tasks, rtos.objects(tasks=tasks), rtos.priority_order)["inversions"]
    assert not inv["inherited"] and [t["task"] for t in inv["interferers"]] == ["sensor"]


def test_snapshot_contention_finds_a_deadlock(kernel):
    k = kernel
    a, b = k.task("a", 1), k.task("b", 1)
    m1 = k.queue("m1", "mutex", 1, 0, holder=a)
    m2 = k.queue("m2", "mutex", 1, 0, holder=b)
    for task, waits in ((a, m2), (b, m1)):                                # a は b の持つ m2 を、b は a の持つ m1 を待つ
        k.put_state(task, k.var("xSuspendedTaskList"))
        k.put_event(task, k.receivers(waits))
    rtos = rtos_of(k)
    tasks = rtos.tasks()
    (cycle,) = contention(tasks, rtos.objects(tasks=tasks), rtos.priority_order)["deadlocks"]
    assert sorted((step["task"], step["waits_for"], step["held_by"]) for step in cycle) == [
        ("a", "m2", "b"), ("b", "m1", "a")]


def test_words_for_the_sampler(kernel):
    """時間軸（U09）でサンプラが読む語: ミューテックスの持ち主と待ちの先頭、タスクの保存された SP、アイドルの TCB。"""
    k = kernel
    rtos = rtos_of(k)
    mutex = by_name(rtos.objects())["log_mutex"]
    holder_word, head_word, delta = rtos.mutex_words(mutex)
    assert k.read32(holder_word) == k.handles["worker"]
    assert k.read32(head_word) - delta == k.handles["ctrl"]                 # 最も優先度の高い待ち手
    assert rtos.mutex_empty_head(mutex) != k.read32(head_word)
    assert rtos.mutex_words(by_name(rtos.objects())["cmd_queue"]) is None   # ミューテックスでない
    assert rtos.saved_sp_address(k.handles["fpu"]) == k.handles["fpu"]       # pxTopOfStack は TCB の先頭
    k.u32(k.var("xIdleTaskHandles"), k.handles["IDLE"])
    assert rtos.idle_handles() == {k.handles["IDLE"]}
    assert rtos.handle_priority(k.handles["worker"]) == 1                    # 継承の前（uxBasePriority）
    assert rtos.heap_free_word() == (k.var("xFreeBytesRemaining"), 20 * 1024)
