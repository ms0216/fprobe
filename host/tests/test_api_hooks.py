"""デーモンがフック（U44）を実行する: 操作の前後・止まったことを見つけた後、AI・人・GDB のどの操作でも同じに。"""

import asyncio
import time

import pytest

from fprobe.api.arbiter import Client
from fprobe.api.client import ApiError, DaemonClient
from fprobe.probe_hooks import HookTools
from test_api_daemon import FakeBackend, _serve


class HookBackend(HookTools, FakeBackend):
    """偽の機器 + 本物のフックの登録簿（Probe の HookTools）。"""

    def __init__(self):
        FakeBackend.__init__(self)
        self._init_hooks()
        self.resets = 0
        self.noted = []
        self.evaluate_error = None

    def target_reset(self, halt=False, kind="default"):
        self.resets += 1
        self.state = "HALTED" if halt else "RUNNING"
        return {"state": self.state}

    def target_status(self):
        return {"state": self.state, "pc": self.pc}

    def evaluate(self, expression, frame=0, max_elements=64):
        if self.evaluate_error:
            raise RuntimeError(self.evaluate_error)
        return {"expression": expression, "value": 42}

    def record_host_event(self, stream, kind, body):
        self.noted.append((stream, kind, body))


@pytest.fixture
def served(tmp_path):
    backend = HookBackend()
    url, app, stop = _serve(backend, tmp_path, watch_s=0.05)
    yield backend, url, app
    stop()


def _wait(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("時間内に起きない")
        time.sleep(0.01)


def test_hooks_run_before_and_after_an_operation_and_everyone_sees_them(served):
    backend, url, _ = served
    ai = DaemonClient(url, "claude", "ai")
    gui = DaemonClient(url, "gui", "human")
    seen = []
    gui.subscribe(["hook"], lambda topic, data: seen.append(data))
    ai.call("hooks.add", event="reset", phase="before", actions=[{"evaluate": "g_state"}], name="前")
    ai.call("hooks.add", event="reset", phase="after", actions=[{"mark": "{event}/{phase} by {client}"}])
    gui.call("target.reset", kind="system")
    log = gui.call("hooks.log")["entries"]
    assert [(e["phase"], e["client"]) for e in log] == [("before", "gui"), ("after", "gui")]
    assert log[0]["results"][0] == {"action": "evaluate", "target": "g_state", "ok": True,
                                    "value": {"expression": "g_state", "value": 42}}
    assert ("annotation", "mark", {"text": "reset/after by gui", "hook": 2}) in backend.noted
    assert any((s, k) == ("hook", "run") for s, k, _ in backend.noted)
    _wait(lambda: len(seen) == 2)
    assert [h["name"] for h in ai.call("hooks.list")["hooks"]] == ["前", ""]
    ai.close()
    gui.close()


def test_a_required_before_hook_that_fails_stops_the_operation(served):
    backend, url, _ = served
    ai = DaemonClient(url, "claude", "ai")
    ai.call("hooks.add", event="reset", phase="before", actions=[{"evaluate": "supply_ok"}], required=True)
    backend.evaluate_error = "読めない"
    with pytest.raises(ApiError, match="フック 1"):
        ai.call("target.reset")
    assert backend.resets == 0
    backend.evaluate_error = None
    ai.call("target.reset")
    assert backend.resets == 1
    ai.close()


def test_halt_after_runs_once_per_stop_whoever_stopped_the_core(served):
    backend, url, _ = served
    ai = DaemonClient(url, "claude", "ai")
    ai.call("hooks.add", event="halt", phase="after", actions=[{"mark": "stop at {pc}"}])
    backend.state = "RUNNING"
    time.sleep(0.2)                                     # 見張りが RUNNING を見る
    ai.call("target.halt")                              # 操作の応答の前にフックが走る
    log = ai.call("hooks.log")["entries"]
    assert len(log) == 1 and log[0]["client"] == "claude"
    time.sleep(0.2)
    assert len(ai.call("hooks.log")["entries"]) == 1    # 見張りが同じ止まりでもう一度は走らせない
    backend.state = "RUNNING"
    time.sleep(0.2)
    backend.state = "HALTED"                            # 誰か（GDB・ブレークポイント）が止めた
    _wait(lambda: len(ai.call("hooks.log")["entries"]) == 2)
    entry = ai.call("hooks.log")["entries"][-1]
    assert entry["client"] == "daemon" and entry["results"][0]["value"] == f"stop at 0x{backend.pc:08X}"
    ai.close()


def test_actions_inside_hooks_do_not_trigger_hooks(served):
    backend, url, _ = served
    ai = DaemonClient(url, "claude", "ai")
    ai.call("hooks.add", event="reset", phase="after", actions=[{"call": "target.reset", "params": {"halt": True}}])
    ai.call("target.reset")
    assert backend.resets == 2 and len(ai.call("hooks.log")["entries"]) == 1
    activity = ai.call("session.activity.list")["entries"]
    assert any(e["client"] == "hook 1" and e["method"] == "target.reset" for e in activity)
    ai.close()


def test_gdb_operations_run_the_same_hooks(served):
    backend, url, app = served
    ai = DaemonClient(url, "claude", "ai")
    ai.call("hooks.add", event="reset", phase="before", actions=[{"mark": "gdb {method}"}])
    daemon = app.state.daemon
    gdb = Client(99, "gdb 127.0.0.1:5000", "human", "gdb")
    future = asyncio.run_coroutine_threadsafe(
        daemon.run(gdb, "control", lambda: backend.target_reset(True), record=("gdb.reset", {"halt": True}),
                   moves=True), daemon.loop)
    future.result(5)
    entry = ai.call("hooks.log")["entries"][0]
    assert entry["method"] == "gdb.reset" and entry["client"] == "gdb 127.0.0.1:5000"
    assert entry["results"][0]["value"] == "gdb gdb.reset"
    ai.close()


def test_hooks_are_saved_and_loaded_through_the_api(served, tmp_path):
    _, url, _ = served
    ai = DaemonClient(url, "claude", "ai")
    ai.call("hooks.add", event="flash", phase="before", actions=[{"call": "probe.voltage"}], required=True)
    path = str(tmp_path / "hooks.json")
    ai.call("hooks.save", path=path)
    ai.call("hooks.remove", all=True)
    assert ai.call("hooks.list")["hooks"] == []
    assert ai.call("hooks.load", path=path)["loaded"] == 1
    assert ai.call("hooks.list")["hooks"][0]["required"]
    with pytest.raises(ApiError, match="無い"):
        ai.call("hooks.add", event="reset", phase="after", actions=[{"call": "no.such.method"}])
    ai.close()
