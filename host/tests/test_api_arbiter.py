"""fprobe.api.arbiter / activity: 操作の調停（1 本の作業スレッド、一時停止・保留・再開）と記録。"""

import threading
import time

import pytest

from fprobe.api.activity import ActivityLog, summarize
from fprobe.api.arbiter import Arbiter, Client, NotAllowed

HUMAN, AI = Client(1, "gui", "human"), Client(2, "claude", "ai")


@pytest.fixture
def arbiter():
    events = []
    a = Arbiter(on_control=events.append)
    a.events = events
    yield a
    a.close()


def test_operations_never_overlap(arbiter):
    running, overlaps = [0], []

    def op():
        running[0] += 1
        overlaps.append(running[0])
        time.sleep(0.01)
        running[0] -= 1
        return 1

    futures = [arbiter.submit(c, "control", op) for c in (HUMAN, AI) for _ in range(10)]
    assert sum(f.result(5) for f in futures) == 20 and max(overlaps) == 1


def test_pause_holds_ai_control_but_not_reads_or_human(arbiter):
    arbiter.pause(HUMAN)
    held = arbiter.submit(AI, "control", lambda: "ai")
    assert arbiter.submit(AI, "read", lambda: "read").result(2) == "read"
    assert arbiter.submit(HUMAN, "control", lambda: "human").result(2) == "human"
    time.sleep(0.05)
    assert not held.done() and arbiter.state() == {"paused": True, "paused_by": "gui", "held": 1}
    arbiter.resume(HUMAN)
    assert held.result(2) == "ai" and arbiter.state()["held"] == 0
    assert [(e["paused"], e["held"]) for e in arbiter.events] == [(True, 0), (True, 1), (False, 0)]   # 保留の数も知らせる


def test_only_humans_can_pause(arbiter):
    with pytest.raises(NotAllowed):
        arbiter.pause(AI)


def test_pause_released_when_pauser_leaves(arbiter):
    arbiter.pause(HUMAN)
    held = arbiter.submit(AI, "control", lambda: 7)
    arbiter.leave(HUMAN)
    assert held.result(2) == 7 and arbiter.state()["paused"] is False


def test_leaving_cancels_own_held_requests(arbiter):
    arbiter.pause(HUMAN)
    held = arbiter.submit(AI, "control", lambda: 7)
    arbiter.leave(AI)
    assert held.cancelled()


def test_errors_propagate_to_the_caller(arbiter):
    def boom():
        raise RuntimeError("device")
    with pytest.raises(RuntimeError, match="device"):
        arbiter.submit(AI, "control", boom).result(2)
    assert arbiter.submit(AI, "control", lambda: "still alive").result(2) == "still alive"


def test_activity_is_numbered_written_and_delivered(tmp_path):
    seen = []
    log = ActivityLog(tmp_path, on_entry=seen.append)
    a = log.record({"client": "claude", "method": "target.halt"})
    b = log.record({"client": "gui", "method": "probe.info"})
    assert (a["seq"], b["seq"]) == (1, 2) and "time" in a
    assert log.path.read_text(encoding="utf-8").count("\n") == 2 and seen == [a, b]


def test_large_results_are_summarized():
    assert summarize("ab" * 500)["length"] == 1000
    assert summarize(list(range(1000)))["length"] == 1000
    assert summarize({"x": 1}) == {"x": 1}


def test_a_request_held_too_long_is_dropped_not_run_late():
    from fprobe.api.arbiter import HoldExpired
    a = Arbiter(on_control=lambda state: None, hold_s=0.1)
    ran = []
    a.pause(HUMAN)
    stale = a.submit(AI, "control", lambda: ran.append("stale"))
    time.sleep(0.2)                                   # 呼び出し側はもう諦めているかもしれない
    fresh = a.submit(AI, "control", lambda: ran.append("fresh"))
    a.resume(HUMAN)
    assert fresh.result(2) is None and isinstance(stale.exception(2), HoldExpired) and ran == ["fresh"]
    a.close()


def test_large_values_inside_results_are_summarized_too():
    # ソースの本文（debug.source.read）のように入れ子の中の大きな値も、記録と通知には要約だけを残す
    s = summarize({"name": "a.c", "text": "x" * 5000, "lines": list(range(100)), "nested": [{"blob": "y" * 999}]})
    assert s["name"] == "a.c" and s["text"]["length"] == 5000 and s["lines"]["length"] == 100
    assert s["nested"][0]["blob"]["length"] == 999
