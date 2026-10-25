"""MCP はデーモンの利用者: 道具は仕様から生成し、呼び出しはデーモンへ転送する。"""

import asyncio
import inspect

from fprobe.api.spec import load_spec
from fprobe.mcp_client import build_client_server, tool_function


class FakeClient:
    def __init__(self):
        self.calls = []

    def call(self, method, **params):
        self.calls.append((method, params))
        return {"ok": method}


def test_tools_are_generated_from_the_spec():
    spec = load_spec()
    server = build_client_server(FakeClient(), spec)
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert "debug_run_until_halt" in names and "target_halt" in names
    assert not any(n.startswith("session_") for n in names)            # セッションの操作は道具にしない


def test_binary_methods_are_not_tools():
    # バイナリの応答（timeline.tiles）は画面用。AI は同じ内容を la_view / recording_waveform / recording_density で読む
    spec = load_spec()
    assert spec.methods["timeline.tiles"].binary
    names = {t.name for t in asyncio.run(build_client_server(FakeClient(), spec).list_tools())}
    assert "timeline_tiles" not in names and "la_view" in names


def test_a_tool_forwards_to_the_daemon_with_typed_arguments():
    spec = load_spec()
    client = FakeClient()
    fn = tool_function(client, spec.methods["debug.run_until_halt"])
    sig = inspect.signature(fn)
    assert sig.parameters["timeout_s"].default == 5.0
    assert fn(timeout_s=1.5) == {"ok": "debug.run_until_halt"}
    assert client.calls == [("debug.run_until_halt", {"timeout_s": 1.5})]


def test_object_and_untyped_params_are_not_forced_to_strings():
    # 辞書を取る引数（xtrig.set_sequence の stages = 段の辞書の並び、pattern_a）は辞書として、型の無いものは Any として渡す
    import typing
    spec = load_spec()
    fn = tool_function(FakeClient(), spec.methods["xtrig.set_sequence"])
    assert fn.__annotations__["stages"] == list[dict[str, typing.Any]]
    assert fn.__annotations__["pattern_a"] == dict[str, typing.Any] | None


def test_mcp_refuses_a_daemon_that_holds_a_different_device():
    # 既に動いているデーモンが別の機器を握っていたら、黙ってそれを操作しない（機器はシリアル番号等で必ず指定する）
    import argparse
    import pytest
    from fprobe.mcp_client import requested_device, check_device
    args = argparse.Namespace(serial="0001", port=None, baud=2_000_000, eth=None)
    assert requested_device(args) == {"link": "usb", "address": "0001"}
    check_device(requested_device(args), {"link": "usb", "address": "0001"})
    with pytest.raises(RuntimeError, match="別の機器"):
        check_device(requested_device(args), {"link": "eth", "address": "192.168.77.2"})


def test_a_lost_daemon_connection_is_reopened_on_the_next_call():
    # デーモンが再起動・切断しても、AI の側（MCP）を起動し直さずに次の呼び出しでつなぎ直す。切れた呼び出し自体は
    # 二重に実行しないよう、やり直さずに誤りを返す
    import pytest
    from fprobe.mcp_client import ReconnectingClient

    class Flaky:
        def __init__(self, n):
            self.n, self.closed = n, False

        def call(self, method, **params):
            if self.n == 0:
                raise ConnectionError("lost")
            return {"client": self.n}

        def close(self):
            self.closed = True

    made = []

    def factory():
        made.append(Flaky(len(made)))
        return made[-1]

    client = ReconnectingClient(factory)
    with pytest.raises(ConnectionError, match="つなぎ直す"):
        client.call("target.status")                       # 1 つ目の接続は切れている
    assert client.call("target.status") == {"client": 1}   # 次の呼び出しで新しい接続
    assert made[0].closed and len(made) == 2


def test_the_spawned_daemon_logs_to_a_file(tmp_path, monkeypatch):
    import argparse
    import fprobe.mcp_client as m
    started = {}
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path))
    monkeypatch.setattr(m, "_listening", lambda port: bool(started))
    monkeypatch.setattr(m._subprocess, "Popen", lambda cmd, **kw: started.update(cmd=cmd, **kw))
    args = argparse.Namespace(serial="0001", port=None, baud=2_000_000, eth=None, listen=7999, token=None)
    assert m.ensure_daemon(args) == "ws://127.0.0.1:7999/api"
    assert "--listen" in started["cmd"] and "7999" in started["cmd"]
    assert started["stdout"].name == str(tmp_path / "daemon.log")


def test_tool_names_in_the_instructions_and_summaries_exist():
    # AI は案内文に書かれた道具の名前で呼ぶ。名前は仕様から生成するので、案内文の `名前` がすべて実在すること
    import asyncio
    import re
    from fprobe.mcp_instructions import INSTRUCTIONS
    spec = load_spec()
    tools = {t.name for t in asyncio.run(build_client_server(FakeClient(), spec).list_tools())}
    texts = [INSTRUCTIONS] + [m.summary for m in spec.methods.values()]
    named = {n for text in texts for n in re.findall(r"`([a-z][a-z0-9_]*)`", text)}
    assert named, "案内文に道具の名前が無い"
    assert named <= tools, sorted(named - tools)


def test_a_type_list_with_null_becomes_optional():
    import typing
    from fprobe.mcp_client import _annotation
    assert _annotation({"type": ["integer", "null"]}) == typing.Optional[int]
    assert _annotation({"type": ["string"]}) is str


def test_alternative_types_and_enums_keep_every_form():
    # 「整数か "semihosting"」「整数・小数・文字列」「8 / 16 / 32」を MCP が 1 つの型に狭めて断らない
    from fprobe.mcp_client import _annotation
    assert _annotation({"anyOf": [{"type": "integer"}, {"enum": ["semihosting"]}]}) == int | str
    assert _annotation({"type": ["integer", "number", "string"]}) == int | float | str
    assert _annotation({"enum": [8, 16, 32]}) is int and _annotation({"enum": ["a", "b"]}) is str
    assert _annotation({"anyOf": [{"type": "string"}, {"type": "null"}]}) == str | None


def test_parameters_marked_not_for_mcp_are_hidden_from_the_ai():
    # セミホスティングのファイルの root は API（人間・スクリプト）でだけ指定できる。AI の道具には出さない
    spec = load_spec()
    method = spec.methods["debug.semihosting.configure"]
    assert {"file_root", "writable"} <= set(method.params["properties"])
    fn = tool_function(FakeClient(), method)
    assert set(inspect.signature(fn).parameters) == {"enabled", "cmdline"}
    tools = {t.name: t for t in asyncio.run(build_client_server(FakeClient(), spec).list_tools())}
    assert "file_root" not in tools["debug_semihosting_configure"].input_schema["properties"]
