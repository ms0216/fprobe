"""カーネルの出来事の解読（fprobe.rtos.kernel_trace）と解析（fprobe.rtos.flow）: fprobe_trace.h の記録（ポート 30・29）と
ポート 31 の切り替えの語から、だれが・何に・何をしたか。Communication Flow・ブロック時間・キュー使用率・逆転の区間・
割り込みの区間・応答時間・フックの負荷。機器は使わない。"""

import re
from pathlib import Path

import pytest

from fprobe.itm import DecodeError, Instrumentation, Overflow
from fprobe.rtos.analysis import find_jobs, job_summary
from fprobe.rtos.flow import (blocking, communication_flow, inversions_from_ops, isr_activations, job_events,
                              overhead, queue_usage)
from fprobe.rtos.kernel_trace import (PAYLOAD_CYCLES, CLASSES, EVENTS, KERNEL_PORT, PAYLOAD_PORT, QUEUE_TYPES, WAITS, KernelOp,
                                      kernel_trace, object_table)
from fprobe.rtos.timeline import task_events, timeline_from_events

HEADER = Path(__file__).resolve().parents[1] / "firmware" / "freertos_demo" / "fprobe_trace.h"

HI, MID, LO, IDLE = 0x2000_1000, 0x2000_2000, 0x2000_3000, 0x2000_4000
NAMES = {HI: "hi", MID: "mid", LO: "lo", IDLE: "IDLE"}
PRIO = {HI: 3, MID: 2, LO: 1, IDLE: 0}
QUEUE, SEM, MUTEX = 0x2000_8000, 0x2000_8100, 0x2000_8200
CODES = {name: code for code, name in EVENTS.items()}


class Trace:
    """時刻付きの ITM のパケットを組み立てる（fprobe_trace.h と同じ語の形）。"""

    def __init__(self):
        self.packets: list = []
        self.t = 0

    def at(self, t: int) -> "Trace":
        self.t = t
        return self

    def word(self, port: int, value: int) -> None:
        self.packets.append((self.t, Instrumentation(port, value & 0xFFFF_FFFF, 4)))

    def switch_in(self, tcb: int) -> "Trace":
        self.word(31, tcb | 0)
        return self

    def switch_out(self, tcb: int) -> "Trace":
        self.word(31, tcb | 1)
        return self

    def record(self, kind: str, *payload: int, arg: int = 0, ipsr: int = 0) -> "Trace":
        self.word(KERNEL_PORT, CODES[kind] | len(payload) << 8 | ipsr << 10 | min(arg, 0x1FFF) << 19)
        for w in payload:
            self.word(PAYLOAD_PORT, w)
        return self

    def obj(self, kind: str, handle: int, cls: str, arg: int = 0, ipsr: int = 0) -> "Trace":
        return self.record(kind, handle | CLASSES.index(cls), arg=arg, ipsr=ipsr)

    def run(self, tcb: int, t0: int, t1: int) -> "Trace":
        self.at(t0).switch_in(tcb)
        self.at(t1).switch_out(tcb)
        return self


# --- 語の形がファームと同じ --------------------------------------------------------------------------

def test_event_numbers_ports_and_classes_match_the_firmware_header():
    text = HEADER.read_text(encoding="utf-8")
    defines = {m[1]: int(m[2]) for m in re.finditer(r"#define FPROBE_(\w+)\s+(\d+)u\b", text)}
    assert defines["KERNEL_PORT"] == KERNEL_PORT and defines["PAYLOAD_PORT"] == PAYLOAD_PORT
    events = {k[3:].lower(): v for k, v in defines.items() if k.startswith("EV_")}
    events["object_create"] = events.pop("create")
    assert events == {name: code for code, name in EVENTS.items()}
    assert [defines[f"CLASS_{c.upper()}"] for c in CLASSES] == [0, 1, 2, 3]
    assert [defines[f"WAIT_{w.upper()}"] for w in WAITS] == [0, 1, 2, 3]
    assert QUEUE_TYPES[1] == "mutex" and QUEUE_TYPES[4] == "recursive_mutex"


# --- 解読 -----------------------------------------------------------------------------------------

def test_records_are_decoded_and_the_actor_is_the_running_task():
    tr = Trace()
    tr.at(10).switch_in(HI)
    tr.at(20).obj("send", QUEUE, "queue", arg=2)
    tr.at(30).record("ready", LO, ipsr=15)                        # SysTick が LO を起こした
    tr.at(40).obj("block", MUTEX, "mutex", arg=0)
    tr.at(50).record("object_create", SEM | 1, 3, arg=3)
    trace = kernel_trace(tr.packets)
    send, ready, block, create = trace.ops
    assert (send.kind, send.actor, send.obj, send.obj_class, send.arg) == ("send", HI, QUEUE, "queue", 2)
    assert (ready.kind, ready.isr, ready.task, ready.actor) == ("ready", 15, LO, None)
    assert (block.kind, block.wait, block.obj_class) == ("block", "receive", "mutex")
    assert (create.kind, create.obj, create.length, create.queue_type) == ("object_create", SEM, 3, "binary_semaphore")
    assert trace.lost == 0 and trace.words == {"task": 1, "kernel": 4, "payload": 5}
    assert object_table(trace, {QUEUE: {"name": "cmd", "kind": "queue", "length": 4}}) == {
        QUEUE: {"name": "cmd", "kind": "queue", "length": 4},
        SEM: {"name": f"0x{SEM:08X}", "kind": "binary_semaphore", "length": 3}}


def test_incomplete_records_and_overflow_are_counted_and_forget_the_running_task():
    tr = Trace().at(1).switch_in(HI)
    tr.at(2).word(KERNEL_PORT, CODES["send"] | 1 << 8)            # 中身が来ないまま次の見出し
    tr.at(3).obj("receive", QUEUE, "queue", arg=1)
    tr.at(4).word(PAYLOAD_PORT, 0x1234)                             # 見出しの無い中身
    tr.packets.append((5, Overflow()))
    tr.at(6).obj("send", QUEUE, "queue")
    tr.packets.append((7, DecodeError("bad header")))
    trace = kernel_trace(tr.packets)
    assert [(o.kind, o.actor) for o in trace.ops] == [("receive", HI), ("lost", None), ("send", None), ("lost", None)]
    assert trace.lost == 2


def test_unknown_event_numbers_are_kept_with_their_payload():
    tr = Trace()
    tr.word(KERNEL_PORT, 99 | 1 << 8)
    tr.word(PAYLOAD_PORT, 7)
    (op,) = kernel_trace(tr.packets).ops
    assert op.kind == "event_99" and op.words == (7,)


# --- 解析 -----------------------------------------------------------------------------------------

def producer_consumer() -> Trace:
    """hi が queue を受けて待ち、lo が送ると hi が起きて受ける。mid は sem を give、hi は take。SysTick が出入りする。"""
    tr = Trace()
    tr.at(0).switch_in(HI)
    tr.at(10).obj("receive_failed", QUEUE, "queue", arg=0)
    tr.at(11).obj("block", QUEUE, "queue", arg=0)                  # hi は queue を待つ
    tr.at(12).switch_out(HI)
    tr.at(20).switch_in(LO)
    tr.at(30).record("isr_enter", ipsr=15)
    tr.at(35).record("isr_exit", ipsr=15)
    tr.at(40).obj("send", QUEUE, "queue", arg=0)                   # lo → queue（1 個に）
    tr.at(41).record("ready", HI)
    tr.at(42).switch_out(LO)
    tr.at(43).switch_in(HI)
    tr.at(50).obj("receive", QUEUE, "queue", arg=1)                # queue → hi（0 個に）: 待った結果は ok
    tr.at(60).record("delay", HI)
    tr.at(61).switch_out(HI)
    tr.at(62).switch_in(MID)
    tr.at(70).obj("send", SEM, "semaphore", arg=0)
    tr.at(71).record("notify", LO, arg=0)
    tr.at(80).obj("send_failed", QUEUE, "queue", arg=4)            # 満杯
    tr.at(90).switch_out(MID)
    tr.at(100).record("ready", HI, ipsr=15)
    tr.at(101).switch_in(HI)
    tr.at(110).obj("receive", SEM, "semaphore", arg=1)
    return tr


def test_communication_flow_has_directed_edges_between_tasks_isrs_and_objects():
    trace = kernel_trace(producer_consumer().packets)
    objects = {QUEUE: {"name": "cmd_queue", "kind": "queue", "length": 4},
               SEM: {"name": "done_sem", "kind": "binary_semaphore", "length": 1}}
    flow = communication_flow(trace, NAMES, objects)
    edges = {(e["from"], e["to"], e["op"]): e for e in flow["edges"]}
    assert edges[("lo", "cmd_queue", "send")]["count"] == 1
    assert edges[("cmd_queue", "hi", "receive")]["count"] == 1 and edges[("cmd_queue", "hi", "receive")]["failed"] == 1
    assert edges[("mid", "cmd_queue", "send")]["failed"] == 1 and edges[("mid", "cmd_queue", "send")]["count"] == 0
    assert edges[("mid", "done_sem", "give")]["count"] == 1 and edges[("done_sem", "hi", "take")]["count"] == 1
    assert edges[("hi", "cmd_queue", "wait")]["count"] == 1 and edges[("mid", "lo", "notify")]["count"] == 1
    kinds = {n["name"]: n["type"] for n in flow["nodes"]}
    assert kinds["cmd_queue"] == "object" and kinds["hi"] == "task"
    assert flow["dot"].startswith("digraph") and '"lo" -> "cmd_queue"' in flow["dot"]


def test_blocking_times_by_task_and_object_with_the_outcome():
    trace = kernel_trace(producer_consumer().packets)
    result = blocking(trace, NAMES, {QUEUE: {"name": "cmd_queue", "kind": "queue", "length": 4}}, t_end=200)
    waits = result["waits"]
    assert [(w["task"], w["on"], w["t0"], w["t1"], w["outcome"]) for w in waits] == [
        ("hi", "cmd_queue", 11, 41, "ok"), ("hi", "delay", 60, 100, None)]
    hi = result["tasks"]["hi"]
    assert hi["count"] == 2 and hi["total"] == 70 and hi["max"]["duration"] == 40 and hi["max"]["t0"] == 60
    assert result["objects"]["cmd_queue"]["total"] == 30 and result["open"] == 0


def test_blocking_open_at_the_end_is_truncated_and_lost_forgets_waits():
    tr = Trace().at(0).switch_in(HI)
    tr.at(5).obj("block", QUEUE, "queue")
    tr.at(6).switch_out(HI).at(7).switch_in(MID)
    tr.at(8).obj("block", SEM, "semaphore")
    tr.packets.append((9, Overflow()))
    result = blocking(kernel_trace(tr.packets), NAMES, {}, t_end=50)
    assert result["open"] == 0 and result["forgotten"] == 2 and result["waits"] == []
    tr = Trace().at(0).switch_in(HI).at(5).obj("block", QUEUE, "queue")
    result = blocking(kernel_trace(tr.packets), NAMES, {}, t_end=50)
    assert result["open"] == 1 and result["waits"][0]["truncated"] and result["waits"][0]["t1"] == 50


def test_queue_usage_follows_the_messages_and_counts_full_and_empty():
    trace = kernel_trace(producer_consumer().packets)
    usage = queue_usage(trace, NAMES, {QUEUE: {"name": "cmd_queue", "kind": "queue", "length": 4}}, t1=110)
    q = usage["objects"]["cmd_queue"]
    assert q["length"] == 4 and q["max"] == 4 and q["max_usage"] == 1.0
    assert q["points"][:3] == [[10, 0], [40, 1], [50, 0]] and q["sends"] == 1 and q["receives"] == 1
    assert (q["send_failed"], q["receive_failed"], q["waits_send"], q["waits_receive"]) == (1, 1, 0, 1)
    # 時間で重みを付けた平均: 10〜40 は 0、40〜50 は 1、50〜80 は 0、80〜110 は 4 → (10 + 120) / 100
    assert q["mean"] == pytest.approx(130 / 100)
    sem = usage["objects"][f"0x{SEM:08X}"]
    assert sem["length"] is None and sem["max_usage"] is None and sem["max"] == 1


def test_inversion_from_events_is_exact_and_tells_the_inheritance_and_interference():
    """lo が mutex を取り、hi が待つ（継承で lo が 3 に）。その間に mid が動けば際限の無い逆転。lo が give して終わる。"""
    tr = Trace()
    tr.at(0).switch_in(LO)
    tr.at(5).obj("receive", MUTEX, "mutex", arg=1)                  # lo が取る
    tr.at(10).switch_out(LO)
    tr.at(11).switch_in(HI)
    tr.at(20).obj("block", MUTEX, "mutex")                          # hi が待つ = 逆転の始め
    tr.at(21).record("prio_inherit", LO, arg=3)
    tr.at(22).switch_out(HI)
    tr.at(23).switch_in(MID)                                        # mid が割り込む（中間の優先度）
    tr.at(33).switch_out(MID)
    tr.at(34).switch_in(LO)
    tr.at(40).obj("send", MUTEX, "mutex", arg=0)                    # lo が返す = 逆転の終わり
    tr.at(41).record("prio_disinherit", LO, arg=1)
    tr.at(42).record("ready", HI)
    tr.at(43).switch_out(LO)
    tr.at(44).switch_in(HI)
    tr.at(45).obj("receive", MUTEX, "mutex", arg=1)
    trace = kernel_trace(tr.packets)
    tl = timeline_from_events(task_events(tr.packets), NAMES, t_end=60)
    found = inversions_from_ops(tl, trace, PRIO, names=NAMES, objects={MUTEX: {"name": "log_mutex", "kind": "mutex"}})
    assert found["available"] and found["source"] == "trace" and found["count"] == 1
    (ep,) = found["episodes"]
    assert (ep["mutex"], ep["holder"], ep["waiter"], ep["t0"], ep["t1"]) == ("log_mutex", "lo", "hi", 20, 40)
    assert ep["interference"] == {"mid": 10} and ep["unbounded"] and ep["holder_time"] == 6
    assert ep["inherited"] == {"t": 21, "priority": 3, "latency": 1}


def test_isr_activations_pair_enter_and_exit_by_ipsr_with_nesting():
    tr = Trace()
    tr.at(10).record("isr_enter", ipsr=20)
    tr.at(12).record("isr_enter", ipsr=15)
    tr.at(15).record("isr_exit", ipsr=15)
    tr.at(18).record("isr_exit", ipsr=20)
    tr.at(30).record("isr_exit", ipsr=16)                           # 入りの無い出（portEND_SWITCHING_ISR）は数えない
    acts = isr_activations(kernel_trace(tr.packets))
    assert [(a.number, a.start, a.end, a.inclusive, a.self_time) for a in acts] == [(15, 12, 15, 3, 3), (20, 10, 18, 8, 5)]


def test_job_events_give_response_times_to_find_jobs():
    tr = producer_consumer()
    trace = kernel_trace(tr.packets)
    events = task_events(tr.packets)
    tl = timeline_from_events(events, NAMES, t_end=120)
    jobs = find_jobs(tl, PRIO, events=job_events(trace, events))
    hi = [j for j in jobs if j.task == "hi"]
    assert [(j.release, j.end, j.end_reason) for j in hi][:1] == [(41, 60, "blocked")]
    assert job_summary(jobs)["hi"]["response"]["max"] == 19


def test_overhead_counts_words_bandwidth_and_cpu_estimate():
    trace = kernel_trace(producer_consumer().packets)
    out = overhead(trace, duration=1000, timebase_hz=1000, baud=2_000_000, core_clock_hz=16e6)
    words = trace.words["task"] + trace.words["kernel"] + trace.words["payload"]
    assert out["words"] == words and out["bytes"] == 5 * words
    assert out["swo_share"] == pytest.approx(5 * words * 10 / 2e6)
    assert 0 < out["cpu_share"] < 0.001 and out["by_kind"]["send"] == 2
    assert overhead(trace, duration=1000, timebase_hz=1000)["swo_share"] is None


def test_overhead_counts_the_wait_for_the_fifo_behind_the_header():
    """記録の中身の語は見出しのすぐ後に書くので、見出しが SWO に出る（1 語 = 5 バイト × 10 ビット）まで FIFO の空きを
    待つ。2026-10-18 の実機（4 Mbaud・16 MHz）で、待ちを数えない見積もり 1.5 % に対し実測 6.2 % だった。"""
    trace = kernel_trace(producer_consumer().packets)
    out = overhead(trace, duration=1000, timebase_hz=1000, baud=4_000_000, core_clock_hz=16e6)
    tx = 50 / 4e6 * 16e6                                                  # 1 語を送る間のコアのクロック数 = 200
    assert out["fifo_wait_cycles"] == pytest.approx(trace.words["payload"] * (tx - PAYLOAD_CYCLES))
    assert out["cycles"] == pytest.approx(out["instruction_cycles"] + out["fifo_wait_cycles"])
    assert overhead(trace, duration=1000, timebase_hz=1000)["fifo_wait_cycles"] is None


def test_kernel_op_dict_names_things():
    op = KernelOp(5, 0, "send", 0, HI, arg=1, obj=QUEUE, obj_class="queue")
    d = op.to_dict(NAMES, {QUEUE: {"name": "cmd", "kind": "queue"}})
    assert d == {"t": 5, "kind": "send", "actor": "hi", "isr": None, "object": "cmd", "object_handle": QUEUE,
                 "class": "queue", "arg": 1}


# --- 作り直した freertos_demo.elf ---------------------------------------------------------------------

def test_freertos_demo_calls_the_record_from_the_kernel_and_the_record_code_is_small():
    """カーネルの関数がフックの記録を呼び、記録の関数は小さい（負荷の見積もり WORD_CYCLES 等の前提）。"""
    from fprobe.disasm import Disassembler
    elf = HEADER.parent / "freertos_demo.elf"
    d = Disassembler.from_elf(elf)
    record = [i for i in d.function("fprobe_trace_record").instructions if i.kind == "code"]
    assert len(record) <= 80
    assert sum(1 for i in record if i.mnemonic.startswith("cpsid")) == 1         # 割り込みを禁止して書く
    for caller in ("xQueueGenericSend", "xQueueReceive", "xQueueSemaphoreTake", "xTaskPriorityInherit",
                   "vTaskDelay", "xPortSysTickHandler"):
        calls = {i.target_name for i in d.function(caller).instructions if i.flow == "call"}
        assert "fprobe_trace_record" in calls, caller
