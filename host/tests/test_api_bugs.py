"""API の入口の既知の不具合の再現試験（検出専用。直るまで xfail(strict)。直ったら xfail を外す）。

台帳: docs/reports/bugs/api.md
"""

import asyncio
import threading
import time

import pytest

from fprobe.api import E_INVALID_PARAMS
from fprobe.api.client import ApiError, DaemonClient
from fprobe.api.spec import InvalidParams, load_spec
from fprobe.mcp_client import build_client_server
from test_api_daemon import FakeBackend, _serve

SPEC = load_spec()


class _Recorder:
    """MCP の道具の転送先: 呼ばれた引数を控える。"""

    def __init__(self):
        self.calls = []

    def call(self, method, /, **params):
        self.calls.append((method, params))
        return {"ok": True}


def _sample(schema):
    if "default" in schema and schema["default"] is not None:
        return schema["default"]
    if "enum" in schema:
        return schema["enum"][0]
    for alt in schema.get("anyOf", []):
        if alt.get("type") != "null":
            return _sample(alt)
    return {"integer": 1, "number": 1.0, "string": "x", "boolean": False, "array": [], "object": {}}.get(schema.get("type"))


def _mcp_forwarded(name):
    """必須の引数だけを渡して MCP の道具を呼んだとき、デーモンへ転送される引数。"""
    method = SPEC.methods[name]
    rec = _Recorder()
    server = build_client_server(rec, SPEC)
    props = method.params["properties"]
    args = {p: _sample(props[p]) for p in method.params.get("required", []) if props[p].get("x-mcp", True)}
    asyncio.run(server.call_tool(name.replace(".", "_"), args))
    return rec.calls[0][1]


# BUG-API-001: 既定値の無い省略可能な引数を省くと、MCP の道具は None を明示して転送し、仕様の検証が断る
_MCP_NULL = ["watch.xy", "watch.measure", "rtos.backtrace", "rtos.read", "testpoint.instrument", "testpoint.start"]


@pytest.mark.parametrize("name", _MCP_NULL)
def test_mcp_tool_with_only_required_args_is_valid_for_the_daemon(name):
    SPEC.validate_params(name, _mcp_forwarded(name))


# BUG-API-002: watch.set の names の既定値が null なのに、型は array（null を許さない）。仕様の既定値が自分の型に反する
def test_watch_set_default_is_valid_for_its_own_schema():
    SPEC.validate_params("watch.set", _mcp_forwarded("watch.set"))


# BUG-API-003: 孤立サロゲート（JSON の "\ud800"）を含む引数の操作は、操作の記録に書けず、記録が黙って欠ける
def test_activity_log_keeps_an_operation_with_a_lone_surrogate_argument(tmp_path):
    from fprobe.api.activity import ActivityLog
    seen = []
    log = ActivityLog(tmp_path / "act", seen.append)
    log.record({"client": "ai", "method": "debug.elf.load", "params": {"path": "a\ud800b"}})
    assert len(seen) == 1 and len(log.read()) == 1


@pytest.fixture
def daemon(tmp_path):
    url, app, stop = _serve(FakeBackend(), tmp_path)
    yield url, app
    stop()


def _raw(url, message, timeout=3):
    """hello 済みの接続で、生の要求を送って生の応答を返す（DaemonClient の _pending を借りる）。"""
    import json
    from concurrent.futures import Future
    c = DaemonClient(url, "raw", "human")
    try:
        future = Future()
        c._pending[777] = future
        c._ws.send(json.dumps({"jsonrpc": "2.0", "id": 777, **message}))
        return future.result(timeout=timeout)
    finally:
        c.close()


# BUG-API-006: method が配列・辞書だと、断らず内部の TypeError（unhashable）が E_INTERNAL で漏れる
@pytest.mark.parametrize("method", [[1], {"a": 1}])
def test_a_non_string_method_is_refused_as_invalid_request(daemon, method):
    reply = _raw(daemon[0], {"method": method, "params": {}})
    assert reply["error"]["code"] in (-32600, -32601), reply


# BUG-API-007: 偽とみなされる params（[]・0・""・false）が黙って {} 扱いになる（[1] や "x" は断るのに不揃い）
@pytest.mark.parametrize("params", [[], 0, "", False])
def test_a_falsy_non_object_params_is_refused(daemon, params):
    reply = _raw(daemon[0], {"method": "target.status", "params": params})
    assert reply["error"]["code"] == E_INVALID_PARAMS, reply


# BUG-API-005 (疑い・設計): 人 2 人が一時停止したとき、先の人の再開で後の人の一時停止まで解ける
@pytest.mark.xfail(strict=True, reason="BUG-API-005: 人 A の再開が、人 B の一時停止まで解く（paused_by は 1 人分しか持たない）")
def test_one_humans_resume_does_not_release_another_humans_pause(daemon):
    a = DaemonClient(daemon[0], "a", "human")
    b = DaemonClient(daemon[0], "b", "human")
    try:
        a.call("session.control.pause")
        b.call("session.control.pause")
        assert a.call("session.control.resume")["paused"] is True
    finally:
        a.close()
        b.close()


def _inproc(tmp_path, method, params, probe=None):
    """実物の Probe（機器なし・未接続）をデーモンの中で直接呼ぶ。応答の辞書を返す。"""
    from fprobe.api.arbiter import Client
    from fprobe.api.daemon import Daemon, _Connection
    from fprobe.mcp_server import Probe
    probe = probe or Probe("unused")
    d = Daemon(probe, None, tmp_path / "act", SPEC, {})
    conn = _Connection(None)
    conn.client = Client(1, "t", "ai")
    try:
        return asyncio.run(d.handle(conn, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}))
    finally:
        d.arbiter.close()


# BUG-API-009: 整数の引数に 2.0 のような整数値の小数（JSON では整数と同じ）が通り、実装に float のまま渡って内部の TypeError が漏れる
@pytest.mark.parametrize("method,params", [("target.step", {"count": 3.0}), ("target.dp.read", {"register": 4.0})])
def test_an_integral_float_for_an_integer_argument_does_not_leak_a_typeerror(tmp_path, method, params):
    reply = _inproc(tmp_path, method, params)
    error = reply.get("error") or {}
    assert (error.get("data") or {}).get("exception") != "TypeError", reply


# BUG-API-010: swo.stop を swo.start の前（未接続・未開始）に呼ぶと、素の AttributeError（'NoneType' ... 'probe'）が漏れる
def test_swo_stop_without_a_connection_is_not_connected(tmp_path):
    reply = _inproc(tmp_path, "swo.stop", {})
    assert reply["error"]["code"] == -32021, reply


# BUG-API-011: NaN（JSON に無い字句。Python の json は出し・読みする）が number の引数に通り、rtt.read の timeout_s が NaN だと
# 作業スレッドが永久に待ち、デーモンの全操作が止まる
def test_nan_timeout_does_not_block_the_worker_forever():
    # 塞がった作業スレッドが試験の process の CPU を食うので、別の process で 4 秒だけ試す
    import os
    import subprocess
    import sys
    code = ("from fprobe.mcp_server import Probe\np = Probe('unused')\nprint('started', flush=True)\n"
            "p.rtt_read(0, timeout_s=float('nan'))")
    child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                             env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)})
    try:
        assert child.stdout.readline().strip() == b"started"       # 読み込みの時間は数えない
        try:
            child.wait(timeout=4)
        except subprocess.TimeoutExpired:
            pytest.fail("rtt_read(timeout_s=NaN) が 4 秒たっても返らない")
    finally:
        child.kill()
        child.wait()


def test_the_non_json_token_nan_is_refused_at_the_wire(daemon):
    import json
    from concurrent.futures import Future
    c = DaemonClient(daemon[0], "raw", "human")
    try:
        future = Future()
        c._pending[5] = future
        c._ws.send('{"jsonrpc":"2.0","id":5,"method":"debug.run_until_halt","params":{"timeout_s":NaN}}')
        reply = future.result(timeout=3)
    finally:
        c.close()
    assert reply["error"]["code"] in (-32700, E_INVALID_PARAMS), reply


# BUG-API-012: 長さ・個数・上限の引数に最小値・最大値が無く、負や桁外れが検証を通る（target.memory.read の length は上限無しで
# 作業スレッドを長く塞ぐ。debug.breakpoint.log の limit=-1 は末尾を黙って 1 件落とす）
_UNBOUNDED = [("target.memory.read", {"address": 0, "length": -1}), ("target.memory.read", {"address": 0, "length": 2**40}),
              ("target.step", {"count": -1}), ("debug.breakpoint.log", {"limit": -1}),
              ("probe.csr.read", {"address": 0, "count": -1}), ("events.read", {"last": -1}),
              ("probe.csr.write", {"address": -1, "words": [0]}), ("probe.csr.write", {"address": 0, "words": [2**40]}),
              ("xtrig.set_match", {"address": 2**64}), ("xtrig.set_inject", {"value": -1}),
              ("target.registers.write", {"name": "r0", "value": 2**70}),
              ("target.flash.erase", {"address": 0, "length": -1}),("debug.run_until_halt", {"timeout_s": -1.0}),
              ("uart.read", {"timeout_s": -1.0}), ("swo.read", {"timeout_s": -1.0})]


@pytest.mark.parametrize("method,params", _UNBOUNDED)
def test_nonsensical_counts_lengths_and_timeouts_are_refused_by_the_spec(method, params):
    with pytest.raises(InvalidParams):
        SPEC.validate_params(method, params)


# BUG-API-013: 「無い」の誤りが素の KeyError（文言に引用符が付く）の E_DEVICE で返る。E_NO_DATA や引数の誤りの専用の道があるのに通らない
@pytest.mark.xfail(strict=True, reason="BUG-API-013: recording.open の「記録が無い」が E_DEVICE の KeyError（引用符付きの文言）で返る")
def test_a_missing_recording_is_not_reported_as_a_device_error_with_a_raw_keyerror(tmp_path):
    reply = _inproc(tmp_path, "recording.open", {"name": "no-such-recording"})
    assert reply["error"]["data"]["exception"] != "KeyError", reply


# BUG-API-014: 孤立サロゲートを含む text の rtt.write は、素の UnicodeEncodeError が E_DEVICE で漏れる（引数の誤りとして断らない）
def test_rtt_write_with_an_unencodable_text_is_an_invalid_params_error(tmp_path):
    reply = _inproc(tmp_path, "rtt.write", {"channel": 0, "text": "a\ud800b"})
    assert reply["error"]["code"] == E_INVALID_PARAMS, reply


# BUG-API-015: 閉じた Arbiter に submit すると、実行する作業スレッドが無いので Future が永久に完了しない
def test_submit_after_close_does_not_hang():
    from fprobe.api.arbiter import Arbiter, Client
    arbiter = Arbiter(lambda state: None)
    arbiter.close()
    future = arbiter.submit(Client(1, "x", "ai"), "read", lambda: 1)
    try:
        future.result(timeout=1)
    except TimeoutError:
        pytest.fail("閉じた後の submit が完了しない")
    except Exception:
        pass                                             # 拒否（例外）なら良い


# BUG-API-004: 通知の購読の関数が例外を投げると、利用者の受信のスレッドが死に、以後の呼び出しがすべて失敗する
def test_a_failing_subscriber_does_not_kill_the_client(daemon):
    url, _ = daemon
    c = DaemonClient(url, "gui", "human")

    def bad(topic, data):
        raise RuntimeError("subscriber bug")
    c.subscribe(["activity"], bad)
    c.call("target.status")
    time.sleep(0.3)
    try:
        assert c.call("target.status")["state"] in ("HALTED", "RUNNING")
    finally:
        c.close()


def test_the_client_refuses_to_send_nan(daemon):
    c = DaemonClient(daemon[0], "gui", "human")
    try:
        with pytest.raises(ValueError):
            c.call("debug.run_until_halt", timeout_s=float("nan"))
        assert not c._pending                              # 登録した待ちを残さない
    finally:
        c.close()
