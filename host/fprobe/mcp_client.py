"""fprobe-mcp: AI エージェント向けの MCP サーバ（stdio）。デーモンの API を道具として出す薄いクライアント。

機器には触らない。デーモン（fprobe.api.daemon）が無ければ起動してつなぎ、仕様（fprobe_api.json）のメソッドを
道具にして転送する。

    fprobe-mcp [--serial <USB シリアル>] [--port <COM ポート> [--baud <ボーレート>]] [--eth <IP アドレス>]
"""

import argparse
import functools
import inspect as _inspect
import operator
import socket as _socket
import subprocess as _subprocess
import sys as _sys
import threading
import time
import typing

from mcp.server.mcpserver import MCPServer

from . import probe_names
from .api.client import DaemonClient
from .api import DEFAULT_PORT, device_of
from .api.spec import Method, Spec, load_spec
from .home import fprobe_home
from .mcp_instructions import INSTRUCTIONS
from .serial_link import DEFAULT_BAUD

__all__ = ["Notices", "ReconnectingClient", "build_client_server", "main", "tool_function"]

_PY_TYPES = {"integer": int, "number": float, "string": str, "boolean": bool}


def _union(types: list):
    """型の並びの合併（重なりは 1 つに。順は保つ）。"""
    return functools.reduce(operator.or_, dict.fromkeys(types))


def _annotation(schema: dict):
    """JSON Schema → Python の型注釈（MCP の SDK が道具の入力の形を作るのに使う）。型の書かれていないものは Any。"""
    if "anyOf" in schema:                               # どれかの形（整数か "semihosting" 等）
        inner = [s for s in schema["anyOf"] if s.get("type") != "null"]
        if not inner:
            return type(None)
        base = _union([_annotation(s) for s in inner])
        return base | None if len(inner) < len(schema["anyOf"]) else base
    if "enum" in schema:                                # 値の型（[8, 16, 32] は整数）
        return _union([type(v) for v in schema["enum"]])
    kind = schema.get("type")
    if isinstance(kind, list):                          # {"type": ["integer", "null"]} の形
        inner = [k for k in kind if k != "null"]
        base = _union([_annotation({**schema, "type": k}) for k in inner]) if inner else type(None)
        return base | None if "null" in kind and inner else base
    if kind == "array":
        return list[_annotation(schema.get("items", {}))]
    if kind == "object":
        return dict[str, typing.Any]
    return _PY_TYPES.get(kind, typing.Any)


NOTICE_TOPICS = ("elf.changed",)                      # AI に知らせる通知（次の道具の結果の notices に添える）
NOTICE_LIMIT = 20


class Notices:
    """デーモンの通知のうち AI に知らせるもの。MCP には押し出しが無いので、次に返す道具の結果（辞書）に
    notices = [{"topic", "message", ...}] として添える（添えたら消す。溜めるのは最新 NOTICE_LIMIT 件。古いものを
    捨てたら notices_dropped = 捨てた数・notices_limit も添える）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._pending: list[dict] = []
        self._dropped = 0

    def add(self, topic: str, params: dict) -> None:
        with self._lock:
            self._pending.append({"topic": topic, **params})
            over = len(self._pending) - NOTICE_LIMIT
            if over > 0:
                del self._pending[:over]
                self._dropped += over

    def attach(self, result):
        if not isinstance(result, dict):
            return result                               # 辞書でない結果の形は変えない（次の辞書の結果に添える）
        with self._lock:
            pending, self._pending = self._pending, []
            dropped, self._dropped = self._dropped, 0
        if not pending:
            return result
        out = {**result, "notices": pending}
        if dropped:
            out.update(notices_dropped=dropped, notices_limit=NOTICE_LIMIT)
        return out


def _accepts_null(schema: dict) -> bool:
    kind = schema.get("type")
    return (kind == "null" or (isinstance(kind, list) and "null" in kind)
            or any(_accepts_null(s) for s in schema.get("anyOf", [])))


def tool_function(client, method: Method, notices: Notices | None = None):
    """仕様のメソッドを、型付きの引数を持つ関数（MCP の道具）にする。呼ぶとデーモンへ転送する。
    "x-mcp": false の引数は出さない（AI に許さない設定。例: セミホスティングのファイルの root）。
    notices を渡すと、溜まった通知を結果に添える。"""
    props = {name: schema for name, schema in method.params.get("properties", {}).items()
             if schema.get("x-mcp", True)}
    required = set(method.params.get("required", []))
    parameters = [
        _inspect.Parameter(name, _inspect.Parameter.KEYWORD_ONLY, annotation=_annotation(schema),
                           default=_inspect.Parameter.empty if name in required else schema.get("default"))
        for name, schema in props.items()]

    # 省略した省略可能な引数は、MCP の SDK が署名の既定値 None で渡してくる。既定値が仕様に無く null も許さない引数は、
    # null のまま転送するとデーモンが断るので、送らない（実装の既定値に任せる）。
    omit_when_none = {name for name, schema in props.items()
                      if name not in required and "default" not in schema and not _accepts_null(schema)}

    def tool(**kwargs):
        kwargs = {k: v for k, v in kwargs.items() if v is not None or k not in omit_when_none}
        result = client.call(method.name, **kwargs)
        return result if notices is None else notices.attach(result)

    tool.__signature__ = _inspect.Signature(parameters)
    tool.__annotations__ = {p.name: p.annotation for p in parameters}
    tool.__name__ = method.name.replace(".", "_")
    tool.__doc__ = method.summary
    return tool


def build_client_server(client, spec: Spec, notices: Notices | None = None) -> MCPServer:
    server = MCPServer("fprobe", instructions=INSTRUCTIONS)
    for method in spec.methods.values():
        if method.name.startswith("session.") or method.binary:      # バイナリの応答は画面用（AI は la.view 等）
            continue
        server.add_tool(tool_function(client, method, notices), name=method.name.replace(".", "_"),
                        description=method.summary)
    return server


def ensure_daemon(args) -> str:
    """デーモンにつなぐ URL。動いていなければ子プロセスで起動し、待ち受けるまで待つ。起動の出力は ~/.fprobe/daemon.log。"""
    url = f"ws://127.0.0.1:{args.listen}/api"
    if _listening(args.listen):
        return url
    command = [_sys.executable, "-m", "fprobe.api.daemon", "--listen", str(args.listen)]
    for flag, value in (("--serial", args.serial), ("--port", args.port), ("--eth", args.eth)):
        if value:
            command += [flag, value]
    if args.port:
        command += ["--baud", str(args.baud)]
    if getattr(args, "bitstreams", None):
        command += ["--bitstreams", args.bitstreams]
    fprobe_home().mkdir(parents=True, exist_ok=True)
    log = open(fprobe_home() / "daemon.log", "a", encoding="utf-8")
    _subprocess.Popen(command, stdout=log, stderr=_subprocess.STDOUT,
                      creationflags=getattr(_subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    deadline = time.monotonic() + 20
    while not _listening(args.listen):
        if time.monotonic() > deadline:
            raise RuntimeError(f"fprobe-daemon が起動しない（{log.name} を見る）")
        time.sleep(0.2)
    return url


class ReconnectingClient:
    """デーモンへの接続（切れたら次の呼び出しでつなぎ直す）。切れた呼び出しはやり直さない（二重に実行しない）。"""

    def __init__(self, connect):
        self._connect = connect
        self._client = None
        self._lock = threading.Lock()

    def call(self, method: str, **params):
        with self._lock:
            if self._client is None:
                self._client = self._connect()
            client = self._client
        try:
            return client.call(method, **params)
        except ConnectionError as e:
            with self._lock:
                if self._client is client:
                    self._client = None
            client.close()
            raise ConnectionError(f"fprobe-daemon との接続が切れた（{e}）。次の呼び出しでつなぎ直す") from e

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                self._client.close()
                self._client = None


def requested_device(args) -> dict:
    return device_of(args.serial, args.port, args.eth)


def check_device(requested: dict, running: dict) -> None:
    """既に動いているデーモンが、頼まれた機器を握っているか（違えば黙って別の機器を操作しないよう誤りにする）。"""
    if requested != running:
        raise RuntimeError(f"動いている fprobe-daemon は別の機器を握っている（{running}、頼まれたのは {requested}）。"
                           "そのデーモンを止めるか、別のポート（--listen）で起動する")


def _listening(port: int) -> bool:
    with _socket.socket() as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) == 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--serial", help="fprobe の USB シリアル番号（複数台あるとき）")
    parser.add_argument("--name", help="個体名（fprobe name set で付けた名前）。--serial / --port / --eth の代わり")
    parser.add_argument("--port", help="SerialLink（UART のホストリンク）の COM ポート。指定すると USB ではなくこれで開く")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD, help="SerialLink のボーレート")
    parser.add_argument("--eth", help="EthLink（UDP のホストリンク）のボードの IP アドレス。指定すると USB ではなくこれで開く")
    parser.add_argument("--listen", type=int, default=DEFAULT_PORT, help="fprobe-daemon のポート")
    parser.add_argument("--bitstreams", help="起動するデーモンに渡す: Primer の構成のビットストリームの置き場所"
                                             "（<dir>/<design>/top.fs）")
    parser.add_argument("--token", help="LAN に開いた（--lan の）fprobe-daemon のトークン")
    args = parser.parse_args()
    try:
        probe_names.apply_to_args(args)                  # 名前 → --serial / --port / --eth（デーモンにもそれを渡す）
    except probe_names.ProbeNameError as e:
        parser.error(str(e))

    notices = Notices()

    def connect() -> DaemonClient:
        client = DaemonClient(ensure_daemon(args), "mcp", "ai", token=args.token)
        try:
            check_device(requested_device(args), client.hello["device"])
            client.subscribe(list(NOTICE_TOPICS), notices.add)
        except BaseException:
            client.close()
            raise
        return client

    client = ReconnectingClient(connect)
    client.call("session.status")                        # 起動時に一度つないで、機器の取り違えを早く知らせる
    try:
        build_client_server(client, load_spec(), notices).run("stdio")
    finally:
        client.close()


if __name__ == "__main__":
    main()
