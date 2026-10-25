"""fprobe-daemon: fprobe を 1 つ握り、JSON-RPC 2.0 over WebSocket で全機能を出す。--gdb で同じセッションの上に
GDB サーバ（fprobe.gdb）も開く（IDE とロジアナ・記録・AI を同時に使う）。
仕様: docs/superpowers/specs/2026-10-17-host-daemon-design.md、docs/superpowers/specs/2026-10-18-u04-gdb-server-design.md

    fprobe-daemon [--serial 0001 | --name NAME | --port COM3 | --eth 192.168.77.2] [--lan [--token T]]
                  [--gdb 3333 [--gdb-target stm32f401xe] [--gdb-role human]]
                  [--stream rtt0=19021:telnet --stream swo=3443 …] [--stream-log rtt0=~/rtt0.log:time …]

--stream は生の流れ（rtt0〜 / swo / uart / semihosting）を TCP で公開する（fprobe.probe_streams。127.0.0.1 だけ）。
"""

import argparse
import asyncio
import contextlib
import contextvars
import functools
import dataclasses
import enum
import itertools
import json
import math
import re
import secrets
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from starlette.applications import Starlette
from starlette.responses import FileResponse, HTMLResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from . import (API_VERSION, DEFAULT_PORT, E_DEVICE, E_INTERNAL, E_INVALID_PARAMS, E_INVALID_REQUEST,
               E_METHOD_NOT_FOUND, E_NO_DATA, E_NO_HELLO, E_NOT_ALLOWED, E_NOT_CONNECTED, E_PARSE, E_TIMEOUT, E_VERSION,
               NoData, NotConnected, device_of)
from .activity import ActivityLog, summarize
from .arbiter import Arbiter, Client, HoldExpired, NotAllowed
from .binary import BinaryResult, encode_frame
from .spec import InvalidParams, Spec, load_spec
from ..gdb.server import GdbServer
from ..home import fprobe_home
from ..hooks import HookRefused, event_of, expand
from ..probe import Probe
from ..gdb.session import Options as GdbOptions

__all__ = ["DEFAULT_PORT", "GdbOptions", "StreamHost", "create_app", "device_of", "main", "parse_stream",
           "parse_stream_log"]

NOTIFY_LIMIT = 1000                                          # 利用者ごとの送り途中の通知の上限
UI_DIR = Path(__file__).resolve().parents[2] / "ui" / "dist"     # 画面のビルドの出力（host/ui）
WATCHER = Client(0, "daemon", "script")                    # 見張りの読み（記録しない）。見張りが見つけた止まりのフックも
_SOURCE = contextvars.ContextVar("fprobe_source", default=None)      # 要求の source（どの窓から。操作の記録に残す）
_IN_HOOK = contextvars.ContextVar("fprobe_in_hook", default=False)   # フックの動作の中（その操作はフックを起こさない）
# コアを動かし得る操作。終わるたびに見張りの状態の run を 1 増やす（同じ場所で止まり直しても画面が読み直せる）
# 記録を開く・始める・止める操作: 終わったら通知 "recording" を送る（画面の時間軸・記録のビューが読み直す。だれが
# 変えても届く。操作の記録の文字列に頼ると、読み直しが記録に残る場合に止まらなくなる）。値は通知の action
RECORDING_METHODS = {"recording.open": "open", "recording.start": "start", "recording.stop": "stop"}
RUN_METHODS = {"target.connect", "target.halt", "target.resume", "target.step", "target.reset", "target.flash",
               "target.program_ram", "target.debug_mode.set", "debug.run_until_halt", "debug.step", "debug.call"}
BUILD_HELP = ("<!doctype html><meta charset=utf-8><title>fprobe</title>"
              "<p>画面がまだビルドされていません。<pre>cd host/ui\nnpm install\nnpm run build</pre>"
              "<p>API（ws://…/api）はこのままでも使えます。")


_PLAIN = frozenset({str, int, bool, type(None)})             # そのまま返せる型（派生型は列挙などの分岐を通す）


class _NonFinite(Exception):
    """JSON に無い字句 NaN・Infinity・-Infinity を受けた。number の検証を NaN がすり抜け、待ち時間が NaN になると作業スレッドが戻らない。"""


def _refuse_constant(token: str):
    raise _NonFinite(token)


_SURROGATE = re.compile("[\ud800-\udfff]")


def _has_lone_surrogate(value: Any) -> bool:
    """引数に孤立サロゲートの文字列があるか（UTF-8 に符号化できず、実装の encode が UnicodeEncodeError で漏れる）。"""
    if isinstance(value, str):
        return _SURROGATE.search(value) is not None
    if isinstance(value, dict):
        return any(_has_lone_surrogate(k) or _has_lone_surrogate(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_has_lone_surrogate(v) for v in value)
    return False


def jsonable(value: Any) -> Any:
    """結果を JSON にできる形に: 集合は並べたリスト、バイト列は 16 進、データクラスは辞書、列挙は名前、Path は文字列。
    （ライブラリの結果には frozenset 等が含まれる。MCP の SDK は自動で変換するがデーモンでは明示する）"""
    kind = type(value)
    if kind in _PLAIN:                                   # 大きな結果の大半: 型の判定を 1 回で済ませる
        return value
    if kind is dict:
        return {(k if type(k) is str else str(k)): jsonable(v) for k, v in value.items()}
    if kind is list or kind is tuple:
        return [jsonable(v) for v in value]
    if kind is float:
        return value if math.isfinite(value) else None   # NaN・無限大は JSON に無い（ブラウザが読めなくなる）
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(jsonable(v) for v in value)
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return jsonable(dataclasses.asdict(value))
    if isinstance(value, enum.Enum):
        return value.name
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (bool, int, str)):
        return value
    raise TypeError(f"{kind.__name__} は JSON にできない")


def _who(client: Client) -> dict:
    """利用者の名乗り {id, name, role}（実装への caller と通知 recording に渡す）。"""
    return {"id": client.id, "name": client.name, "role": client.role}


class LinkRecovered(Exception):
    """機器の USB が切れたので開き直した（B28）。ターゲットの接続は切れている。文言は説明と元の誤り。"""


class _Rpc(Exception):
    """JSON-RPC の誤り。data は構造の欄（機器の誤りなら exception = 例外の型の名前・detail = 例外の文そのまま。
    画面は文の形を推して頭を外さず、detail を出す）。"""
    def __init__(self, code: int, message: str, data: dict | None = None):
        super().__init__(message)
        self.code = code
        self.data = data


class _Connection:
    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.client: Client | None = None
        self.topics: set[str] = set()
        self.sending = 0                                   # 送り途中の通知の数（NOTIFY_LIMIT まで）
        self.dropped = 0                                   # 読まないので捨てた通知の数
        self.tasks: set[asyncio.Task] = set()


class Daemon:
    def __init__(self, backend: Any, token: str | None, activity_dir: Path, spec: Spec, device: dict,
                 watch_s: float = 1.0):
        self.backend, self.token, self.spec, self.device, self.watch_s = backend, token, spec, device, watch_s
        self.target_state: dict | None = None                # 見張りが最後に見たターゲットの状態
        self.link_lost: dict | None = None                   # USB が切れて開き直した（B28）。ターゲットにつなぎ直すまで状態に出す
        self.boot = secrets.token_hex(4)                       # この起動の識別（記録の番号の振り直しを利用者が見分ける）
        self._wake: asyncio.Event | None = None
        self.runs = 0                                          # RUN_METHODS が終わった回数
        self.connections: set[_Connection] = set()
        self.guests: dict[int, Client] = {}                    # WebSocket でない利用者（GDB の接続）
        self.gdb_port: int | None = None                       # GDB サーバの待ち受けのポート
        self.loop: asyncio.AbstractEventLoop | None = None
        self._ids = itertools.count(1)
        self.arbiter = Arbiter(on_control=lambda state: self.publish("control", state))
        # 機器に触らない要求（offline: 記録の読み・画面の状態）の作業スレッド。大きな記録のタイルの間も機器の操作・状態の
        # 読みが止まらず、重い機器の操作の間も記録を読める。読みどうしは 1 本で順に（Probe の記録の読み手を同時に使わない）
        self.reader = ThreadPoolExecutor(1, thread_name_prefix="fprobe-reader")
        self.hooks = getattr(backend, "hook_table", None)      # フックの登録簿（U44。機能の実装が持つ）
        self._observe_lock: asyncio.Lock | None = None
        self._inline = 0                                       # 応答の前に状態を見直す操作の数（止まりはその操作のもの）
        self._stop_pending = False                             # その間に見張りが見つけた止まり
        self._hook_tasks: set[asyncio.Task] = set()
        self.activity = ActivityLog(activity_dir, on_entry=lambda entry: self.publish("activity", entry))
        attach = getattr(backend, "attach_publisher", None)     # 機能の実装が操作の途中で出す通知（Flash の進捗等）
        if attach is not None:
            attach(self.publish)
        attach = getattr(backend, "attach_stream_host", None)   # TCP の流れの口からの書き込みを調停・記録に通す
        if attach is not None:
            attach(StreamHost(self))

    # 作業スレッドからも呼ばれる: イベントループへ渡して配る
    def publish(self, topic: str, data: dict) -> None:
        if self.loop is None:
            return
        message = {"jsonrpc": "2.0", "method": topic, "params": data}
        for conn in list(self.connections):
            if topic in conn.topics:
                self.loop.call_soon_threadsafe(self._notify, conn, message)

    def _notify(self, conn: _Connection, message: dict) -> None:
        """イベントループの中で: 読まない利用者（固まった画面）への送り待ちは NOTIFY_LIMIT までにし、超えた分は捨てて数える
        （記録は session.activity.list で読み直せる）。"""
        if conn.sending >= NOTIFY_LIMIT:
            conn.dropped += 1
            return
        conn.sending += 1
        task = self.loop.create_task(self._send(conn, message))
        conn.tasks.add(task)
        task.add_done_callback(conn.tasks.discard)

    @staticmethod
    async def _send(conn: _Connection, message: dict) -> None:
        try:
            await conn.ws.send_json(message)
        except Exception:
            pass                                           # 切れた利用者: 受信の側が片付ける
        finally:
            conn.sending -= 1

    async def handle(self, conn: _Connection, request: dict) -> dict | None:
        rid, method, params = request.get("id"), request.get("method"), request.get("params")
        token = None
        try:
            if not isinstance(method, str):              # 配列・辞書を辞書引きすると内部の TypeError（unhashable）が漏れる
                raise _Rpc(E_INVALID_REQUEST, "method が無い" if method is None else "method は文字列")
            if params is None:                           # null と省略は同じ（JSON-RPC 2.0）。[]・0・""・false は検証で断る
                params = {}
            if "source" in request:                      # どの窓から（使われ方の記録。呼び手の申告をそのまま残す）
                source = request["source"]
                if not isinstance(source, str) or not 1 <= len(source) <= 64:
                    raise _Rpc(E_INVALID_REQUEST, "source は 1〜64 文字の文字列")
                token = _SOURCE.set(source)
            result = await self._dispatch(conn, method, params, request.get("quiet") is True)
            return None if rid is None else {"jsonrpc": "2.0", "id": rid, "result": result}
        except _Rpc as e:
            error = {"code": e.code, "message": str(e)}
            if e.data is not None:
                error["data"] = e.data
            return {"jsonrpc": "2.0", "id": rid, "error": error}
        finally:
            if token is not None:
                _SOURCE.reset(token)

    def _validate(self, method: str, params: dict) -> None:
        """メソッドが仕様にあり、引数が仕様に合うか（合わなければ _Rpc）。"""
        if method not in self.spec.methods:
            raise _Rpc(E_METHOD_NOT_FOUND, f"{method} は無い")
        try:
            self.spec.validate_params(method, params)
        except InvalidParams as e:
            raise _Rpc(E_INVALID_PARAMS, str(e)) from None
        if _has_lone_surrogate(params):
            raise _Rpc(E_INVALID_PARAMS, f"{method}: 引数に孤立サロゲート（UTF-8 にできない文字。JSON の ud800 等）がある")

    async def _dispatch(self, conn: _Connection, method: str, params: dict, quiet: bool = False) -> Any:
        self._validate(method, params)
        if method == "session.hello":
            return self._hello(conn, params)
        if conn.client is None:
            raise _Rpc(E_NO_HELLO, "最初に session.hello を呼ぶ")
        if method == "session.subscribe":
            conn.topics = set(params["topics"])
            if "target.state" in conn.topics:            # 見張りは変化しか送らないので、今の状態をこの利用者へ
                self._poke()
                if self.target_state is not None:
                    message = {"jsonrpc": "2.0", "method": "target.state", "params": self.target_state}
                    asyncio.get_running_loop().create_task(conn.ws.send_json(message))
            return {"topics": sorted(conn.topics)}
        return await self._invoke(conn.client, method, params, quiet)

    async def invoke(self, client: Client, method: str, params: dict) -> Any:
        """WebSocket でない利用者（GDB の monitor）が API のメソッドを呼ぶ: 検証・調停・記録は WebSocket と同じ。
        誤りは _Rpc（文言に理由）。接続に結びつく session.hello / session.subscribe は呼べない。"""
        if method in ("session.hello", "session.subscribe"):
            raise _Rpc(E_NOT_ALLOWED, f"{method} はこの利用者には無い")
        if method not in self.spec.methods:
            raise _Rpc(E_METHOD_NOT_FOUND, f"{method} は無い")
        if self.spec.methods[method].binary:
            raise _Rpc(E_NOT_ALLOWED, f"{method} の応答はバイナリ（WebSocket の利用者だけ）")
        self._validate(method, params)
        return await self._invoke(client, method, params)

    async def _invoke(self, client: Client, method: str, params: dict, quiet: bool = False) -> Any:
        if method == "session.status":
            return {"api_version": API_VERSION, "boot": self.boot, "device": self.device, "control": self.arbiter.state(),
                    "clients": self.clients(), "target": self.target_state, "gdb": {"port": self.gdb_port}}
        if method == "session.activity.list":
            return {"entries": self.activity.read(params.get("before"), params.get("limit", 100))}
        if method in ("session.control.pause", "session.control.resume"):
            try:
                act = self.arbiter.pause if method.endswith("pause") else self.arbiter.resume
                return act(client)
            except NotAllowed as e:
                raise _Rpc(E_NOT_ALLOWED, str(e)) from None
        return await self._call(client, self.spec.methods[method], params, quiet)

    def _hello(self, conn: _Connection, params: dict) -> dict:
        if conn.client is not None:
            raise _Rpc(E_NOT_ALLOWED, "session.hello は 1 回だけ（名乗り直すと一時停止・保留の持ち主が分からなくなる）")
        if self.token is not None and not secrets.compare_digest(str(params.get("token") or ""), self.token):
            raise _Rpc(E_NOT_ALLOWED, "トークンが違う（--lan のデーモンにはトークンが要る）")
        if params["api_version"] != API_VERSION:
            raise _Rpc(E_VERSION, f"API の版 {params['api_version']} はこのデーモン（{API_VERSION}）と合わない")
        conn.client = Client(next(self._ids), params["name"], params["role"], automated=params.get("automated"))
        self.announce_clients()
        return {"api_version": API_VERSION, "boot": self.boot, "client_id": conn.client.id, "device": self.device,
                "control": self.arbiter.state()}

    async def run(self, client: Client, kind: str, fn: Callable[[], Any], *, record: tuple[str, dict] | None = None,
                  moves: bool = False, operation: str | None = None) -> Any:
        """機器の操作 fn を利用者 client として調停の作業スレッドで実行し、結果を返す（誤りはそのまま投げる）。
        record = (名前, 引数) なら操作の記録に残す。moves（コアを動かし得る）なら終わった後に見張りの run を進め、
        そうでない control も状態を見直させる。operation（省略時は record の名前）が出来事（fprobe.hooks.EVENTS）なら
        その前後のフックを実行し、moves で止まりのフックがあれば応答の前に状態を見直す（U44）。"""
        operation = operation or (record[0] if record is not None else None)
        hooked = self.hooks is not None and not _IN_HOOK.get()
        event = event_of(operation) if hooked else None
        if event is not None:
            await self._run_hooks(event, "before", client, operation)        # required の失敗は HookRefused
        inline = hooked and moves and self._wants_stops()
        if inline:
            self._inline += 1
        try:
            future = self.arbiter.submit(client, kind, fn)
            # 記録は操作の完了時に作業スレッドで書く（利用者が途中で切れても、実行された操作は記録に残る）
            if record is not None:
                future.add_done_callback(self._recorder(client, record[0], record[1], time.monotonic()))
            if moves:
                future.add_done_callback(lambda _: self._ran())
            elif kind == "control":
                future.add_done_callback(lambda _: self._poke())     # 操作の後はすぐ状態を見直す
            result = await asyncio.wrap_future(future)
            if event is not None and event != "halt":                 # halt の after は止まりを見つけたとき
                await self._run_hooks(event, "after", client, operation)
            if inline:
                await self._observe(client, inline=True)
        finally:
            if inline:
                self._inline -= 1
        return result

    def _guarded(self, fn: Callable[[], Any]) -> Callable[[], Any]:
        """fn の誤りが機器の USB の切断なら、その場（作業スレッド）で開き直し、説明を付けた LinkRecovered にする（B28）。
        開き直すのは機器（backend.recover_link）の仕事で、切断かどうかもそちらが決める。"""
        recover = getattr(self.backend, "recover_link", None)
        if recover is None:
            return fn

        def guarded():
            try:
                return fn()
            except (NotConnected, NoData, HoldExpired):
                raise
            except Exception as e:
                done = recover(e)
                if done is None:
                    raise
                self.link_lost = {"at": time.time(), "changed": done.get("changed", {}), "version": done.get("version")}
                raise LinkRecovered(f"{done['message']}（{type(e).__name__}: {e}）") from e
        return guarded

    async def _call(self, client: Client, method, params: dict, quiet: bool = False) -> Any:
        fn = getattr(self.backend, method.impl)
        if method.caller:                                # 呼び出し元を実装へ（引数の仕様・記録には出さない）
            fn = functools.partial(fn, caller=_who(client))
        # quiet（画面の自動の読み）は状態を変えない読みにだけ効く。操作は必ず記録する
        record = method.record and not (quiet and (method.kind == "read" or method.observes))
        try:
            if method.offline:
                result = await self._read_offline(client, method.name, lambda: fn(**params), record, params)
            else:
                result = await self.run(client, method.kind, self._guarded(lambda: fn(**params)),
                                        record=(method.name, params) if record else None,
                                        moves=method.name in RUN_METHODS, operation=method.name)
        except asyncio.CancelledError:
            raise                                        # 利用者が切れた: 応答は要らない
        except BaseException as e:                       # 機器の処理の BaseException（SystemExit 等）にも必ず応答する
            code = (E_TIMEOUT if isinstance(e, HoldExpired) else E_NOT_CONNECTED if isinstance(e, NotConnected)
                    else E_NO_DATA if isinstance(e, NoData) else E_DEVICE)
            raise _Rpc(code, f"{type(e).__name__}: {e}", {"exception": type(e).__name__, "detail": str(e)}) from None
        if method.name in RECORDING_METHODS:
            self._announce_recording(client, method.name, result)
        try:
            if method.binary:                            # バイナリの応答: JSON の部分だけ JSON にし、区画はそのまま
                if not isinstance(result, BinaryResult):
                    raise TypeError(f"{method.name} は BinaryResult を返すはず（{type(result).__name__}）")
                return BinaryResult(jsonable(result.result), result.sections)
            return jsonable(result)
        except TypeError as e:
            raise _Rpc(E_INTERNAL, f"結果を返せない: {e}") from None

    async def _read_offline(self, client: Client, name: str, fn: Callable[[], Any], record: bool, params: dict) -> Any:
        """機器に触らない要求を読みのスレッドで実行する（調停・フックの外。操作の記録には残す）。"""
        future = self.reader.submit(fn)
        if record:
            future.add_done_callback(self._recorder(client, name, params, time.monotonic()))
        return await asyncio.wrap_future(future)

    def _announce_recording(self, client: Client, method: str, result: Any) -> None:
        """記録を開いた・始めた・止めたことを知らせる（通知 recording）。"""
        name = None
        if isinstance(result, dict):
            name = result.get("name") or result.get("recording")
        self.publish("recording", {"action": RECORDING_METHODS[method], "name": name,
                                   "client": _who(client)})

    def clients(self) -> list[dict]:
        """利用者の一覧。WebSocket の利用者には、読まないので捨てた通知の数 notifications_dropped と、使った送り待ちの
        上限 notify_limit（捨てたことを黙らない）。"""
        return [{**dataclasses.asdict(c.client), "notifications_dropped": c.dropped, "notify_limit": NOTIFY_LIMIT}
                for c in self.connections if c.client] + [dataclasses.asdict(c) for c in self.guests.values()]

    def new_client(self, name: str, role: str, via: str) -> Client:
        return Client(next(self._ids), name, role, via)

    def join(self, client: Client) -> None:
        """WebSocket でない利用者（GDB の接続）が加わる: 利用者の一覧に出す。"""
        self.guests[client.id] = client
        self.announce_clients()

    def leave(self, client: Client) -> None:
        """去る: 一時停止で保留していた要求を取り消し、止めていたなら再開する。"""
        self.guests.pop(client.id, None)
        self.arbiter.leave(client)
        self.forget(client)
        self.announce_clients()

    def forget(self, client: Client) -> None:
        """去った利用者の画面の名残（ui.state のマウスのカーソル）を消す（機能の実装が持っていれば）。"""
        forget = getattr(self.backend, "ui_state_client_left", None)
        if forget is not None:
            forget(client.id)

    def announce_clients(self) -> None:
        self.publish("clients", {"clients": self.clients()})

    def _note(self, stream: str, kind: str, body: dict) -> None:
        """機能の実装が共通の時間軸の記録（F3）を持っていれば、操作とターゲットの状態の変化を渡す（記録中だけ書かれる）。"""
        note = getattr(self.backend, "record_host_event", None)
        if note is not None:
            note(stream, kind, body)

    def _ran(self) -> None:
        self.runs += 1
        self._poke()

    def _poke(self) -> None:
        if self.loop is not None and self._wake is not None:
            self.loop.call_soon_threadsafe(self._wake.set)

    def _wants_stops(self) -> bool:
        return self.hooks is not None and self.hooks.wants("halt", "after")

    async def watch(self) -> None:
        """ターゲットの状態の見張り: control の操作が終わるたびと watch_s ごとに、target.state の購読者か止まりの
        フックがあれば、作業スレッドで状態を読み、変わったときだけ送る。読みは read なので一時停止中も動き、記録しない。"""
        self._wake = asyncio.Event()
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self.watch_s)
            self._wake.clear()
            if not any("target.state" in c.topics for c in self.connections) and not self._wants_stops():
                continue
            await self._observe()

    async def _observe(self, client: Client | None = None, inline: bool = False) -> None:
        """状態を読み、変わったら送って記録へ渡す。止まったのを見つけたら halt の after フック: inline（コアを動かし得る
        操作の後）なら client として応答の前に、見張りなら別のタスクで。操作の途中に見張りが見つけた止まりは、
        その操作（inline）に任せる（同じ止まりで 2 回走らせない・きっかけの利用者を正しく）。"""
        if self._observe_lock is None:
            self._observe_lock = asyncio.Lock()
        async with self._observe_lock:
            try:
                state = jsonable(await asyncio.wrap_future(self.arbiter.submit(WATCHER, "read", self._guarded(self.backend.target_status))))
            except asyncio.CancelledError:
                raise
            except BaseException as e:                   # 見張りは止めない
                if not isinstance(e, (NotConnected, LinkRecovered)):
                    return        # ponytail: 機器の誤りは見張りでは送らない（操作した利用者に誤りとして返る）
                state = {"state": "DISCONNECTED"}
            if state["state"] != "DISCONNECTED":
                self.link_lost = None
            elif self.link_lost is not None:
                state = {**state, "link_lost": self.link_lost}     # 画面が理由を出す（訳せるよう値で）
            state = {**state, "run": self.runs}
            previous, stopped = self.target_state, False
            if state != previous:
                self.target_state = state
                self.publish("target.state", state)
                self._note("target", "state", state)
                stopped = (previous is not None and state.get("state") == "HALTED"
                           and (previous.get("state") != "HALTED" or previous.get("run") != state["run"]))
            if not self._wants_stops():
                return
            if inline:
                stopped, self._stop_pending = stopped or self._stop_pending, False
            elif stopped and self._inline:
                self._stop_pending, stopped = True, False
        if not stopped:
            return
        if inline:
            await self._run_hooks("halt", "after", client, None, state)
        else:
            task = asyncio.get_running_loop().create_task(self._hooks_quietly("halt", "after", WATCHER, None, state))
            self._hook_tasks.add(task)
            task.add_done_callback(self._hook_tasks.discard)

    async def _hooks_quietly(self, *args) -> None:
        with contextlib.suppress(Exception):             # 見張りのフックの失敗は hooks.log に残る（見張りは止めない）
            await self._run_hooks(*args)

    async def _run_hooks(self, event: str, phase: str, client: Client, operation: str | None,
                         state: dict | None = None) -> None:
        """出来事のフックを登録順に実行する。動作はきっかけの利用者と同じ role の利用者 "hook <id>" として呼ぶ（検証・
        調停・操作の記録は普通の呼び出しと同じ）。結果は hooks.log・通知 hook・記録の stream hook。required の
        before フックの動作が失敗したら HookRefused。"""
        for hook in self.hooks.matching(event, phase):
            started = time.monotonic()
            where = state or self.target_state or {}
            pc = where.get("pc")
            context = {"event": event, "phase": phase, "method": operation, "client": client.name,
                       "pc": f"0x{pc:08X}" if isinstance(pc, int) else None, "where": where.get("where")}
            actor = Client(next(self._ids), f"hook {hook.id}", client.role, "hook")
            token = _IN_HOOK.set(True)
            try:
                results = [await self._hook_action(hook.id, actor, action, context) for action in hook.actions]
            finally:
                _IN_HOOK.reset(token)
            entry = self.hooks.record({"hook": hook.id, "name": hook.name, "event": event, "phase": phase,
                                       "method": operation, "client": client.name, "results": results,
                                       "seconds": round(time.monotonic() - started, 4)})
            self.publish("hook", entry)
            self._note("hook", "run", entry)
            failed = [r for r in results if not r["ok"]]
            if hook.required and failed:
                raise HookRefused(f"フック {hook.id}（{hook.name or event}）が失敗したので {operation} をしなかった: "
                                  f"{failed[0]['error']}")

    async def _hook_action(self, hook_id: int, actor: Client, action: dict, context: dict) -> dict:
        if "mark" in action:
            text = expand(action["mark"], context)
            self._note("annotation", "mark", {"text": text, "hook": hook_id})
            return {"action": "mark", "target": action["mark"], "ok": True, "value": text}
        if "evaluate" in action:
            kind, target, method, params = "evaluate", action["evaluate"], "debug.evaluate", {"expression": action["evaluate"]}
        else:
            kind, target, method, params = "call", action["call"], action["call"], action.get("params", {})
        try:
            return {"action": kind, "target": target, "ok": True, "value": await self.invoke(actor, method, params)}
        except asyncio.CancelledError:
            raise
        except _Rpc as e:
            return {"action": kind, "target": target, "ok": False, "error": str(e)}
        except Exception as e:
            return {"action": kind, "target": target, "ok": False, "error": f"{type(e).__name__}: {e}"}

    def _recorder(self, client: Client, method: str, params: dict, started: float) -> Callable[[Future], None]:
        entry = {"client": client.name, "role": client.role, "via": client.via, "method": method, "params": summarize(params)}
        if client.automated is not None:                 # 申告の無いものは書かない（推定しない）
            entry["automated"] = client.automated
        if (source := _SOURCE.get()) is not None:
            entry["source"] = source

        def record(future: Future) -> None:
            if future.cancelled():                       # 保留のまま利用者が去った: 実行されていない
                return
            seconds = round(time.monotonic() - started, 4)
            held = round(max(0.0, getattr(future, "started_at", started) - started), 4)   # 一時停止で保留された時間
            error = future.exception()
            if error is None:
                try:
                    outcome = {"result": summarize(jsonable(future.result()))}
                except TypeError as e:
                    outcome = {"error": str(e)}
            else:
                outcome = {"error": str(error)}
            recorded = self.activity.record({**entry, **outcome, "seconds": seconds, "held_s": held})
            self._note("host", "call", recorded)
        return record

class StreamHost:
    """TCP の流れの口（fprobe.probe_streams）からの書き込みを、デーモンの利用者（via = tcp、role = script）として
    調停（人の一時停止で待つ）と操作の記録に通す。口のスレッドから呼ばれる。"""

    def __init__(self, daemon: Daemon):
        self.daemon = daemon

    def join(self, name: str) -> Client:
        client = self.daemon.new_client(name, "script", "tcp")
        self._on_loop(self.daemon.join, client)
        return client

    def call(self, client: Client, method: str, params: dict) -> Any:
        loop = self.daemon.loop
        if loop is None:
            raise RuntimeError("デーモンが動いていない")
        return asyncio.run_coroutine_threadsafe(self.daemon.invoke(client, method, params), loop).result()

    def leave(self, client: Client) -> None:
        self._on_loop(self.daemon.leave, client)

    def _on_loop(self, fn: Callable, arg: Any) -> None:
        loop = self.daemon.loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(fn, arg)
        else:
            fn(arg)


def parse_stream(text: str) -> dict:
    """--stream の値 "名前=ポート[:raw|telnet]" → stream_serve の引数。"""
    name, sep, rest = text.partition("=")
    port, _, protocol = rest.partition(":")
    if not sep or not name or not port.isdigit():
        raise ValueError(f"--stream は 名前=ポート[:raw|telnet]（{text!r}）")
    return {"stream": name, "port": int(port), "protocol": protocol or "raw"}


def parse_stream_log(text: str) -> dict:
    """--stream-log の値 "名前=ファイル[:time]" → stream_log_start の引数。:time なら行の頭に時刻を付ける（文字の流れ
    用。流れが文字かバイナリかは名前から決めないので、使う側が明示する）。"""
    name, sep, path = text.partition("=")
    timestamps = path.endswith(":time")
    if timestamps:
        path = path[:-len(":time")]
    if not sep or not name or not path:
        raise ValueError(f"--stream-log は 名前=ファイル[:time]（{text!r}）")
    return {"stream": name, "path": path, "timestamps": timestamps}


LOCAL_NAMES = {"127.0.0.1", "localhost", "[::1]"}


def _host_name(host: str) -> str:
    """Host ヘッダからポートを除いた名前（"[::1]:7820" → "[::1]"）。"""
    return host[:host.index("]") + 1] if host.startswith("[") else host.split(":")[0]


def create_app(backend: Any, *, token: str | None, activity_dir: Path, spec: Spec | None = None,
               device: dict | None = None, ui_dir: Path = UI_DIR, watch_s: float = 1.0, gdb_port: int | None = None,
               gdb_options: GdbOptions | None = None, gdb_role: str = "human", streams: list[dict] = (),
               stream_logs: list[dict] = ()) -> Starlette:
    """gdb_port を渡すと GDB サーバも開く（0 なら空いているポート。実際のポートは daemon.gdb_port）。GDB の接続の
    利用者の role は gdb_role（既定 human: IDE を操作するのは人。人の一時停止中も GDB の操作は通る）。
    streams（stream_serve の引数の並び）・stream_logs（stream_log_start の引数の並び）は起動時に始める。"""
    daemon = Daemon(backend, token, activity_dir, spec or load_spec(), device or device_of(None, None, None), watch_s)

    async def api(ws: WebSocket) -> None:
        # ブラウザは WebSocket に同一生成元の制限をかけないので、訪れた Web ページがプローブを操作できてしまう。
        # Origin を送る接続（＝ブラウザ）は、デーモン自身が配る画面（Origin がこのデーモンの Host と同じ）だけ通す。
        # Python・MCP の利用者は Origin を送らない。DNS rebinding（攻撃者の名前を 127.0.0.1 に向け直すと Origin と Host が
        # そろう）を防ぐため、トークンの無いデーモンは Host の名前もこの PC の名前に限る（--lan はトークンが守る）
        origin, host = ws.headers.get("origin"), ws.headers.get("host", "")
        if origin is not None and (origin not in (f"http://{host}", f"https://{host}")
                                   or (daemon.token is None and _host_name(host) not in LOCAL_NAMES)):
            await ws.close(code=1008)
            return
        await ws.accept()
        if daemon.loop is None:                          # 起動の手順（lifespan）を通らない起動でも通知を配る
            daemon.loop = asyncio.get_running_loop()
        conn = _Connection(ws)
        daemon.connections.add(conn)
        tasks: set[asyncio.Task] = set()

        async def serve(request: dict) -> None:
            try:
                reply = await daemon.handle(conn, request)
            except Exception as e:                       # 想定外でも必ず応答する（利用者を待たせない）
                reply = {"jsonrpc": "2.0", "id": request.get("id"),
                         "error": {"code": E_INTERNAL, "message": f"{type(e).__name__}: {e}",
                                   "data": {"exception": type(e).__name__, "detail": str(e)}}}
            if reply is None or "id" not in request:    # 通知（id なし）には、誤りでも応答しない（JSON-RPC 2.0）
                return
            result = reply.get("result")
            if isinstance(result, BinaryResult):        # バイナリの応答: JSON-RPC の応答 + 区画を 1 つのフレームで
                await ws.send_bytes(encode_frame({**reply, "result": result.result}, result.sections))
            else:
                await ws.send_json(reply)

        try:
            while True:
                text = await ws.receive_text()
                try:
                    request = json.loads(text, parse_constant=_refuse_constant)
                except _NonFinite as e:                    # 要求の id が要るので、字句だけ無害な値にして読み直し、id 付きで断る
                    try:
                        rid = json.loads(text, parse_constant=lambda _t: None).get("id")
                    except (ValueError, AttributeError):
                        rid = None
                    await ws.send_json({"jsonrpc": "2.0", "id": rid,
                                        "error": {"code": E_INVALID_PARAMS,
                                                  "message": f"JSON に無い字句 {e.args[0]} は使えない（数は有限の値）"}})
                    continue
                except ValueError:
                    await ws.send_json({"jsonrpc": "2.0", "id": None,
                                        "error": {"code": E_PARSE, "message": "JSON として読めない"}})
                    continue
                if not isinstance(request, dict):           # 一括（配列）の要求は扱わない
                    await ws.send_json({"jsonrpc": "2.0", "id": None,
                                        "error": {"code": E_INVALID_REQUEST, "message": "要求は 1 つのオブジェクト"}})
                    continue
                task = asyncio.create_task(serve(request))      # 長い操作の間も他の要求（read）を受ける
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        except WebSocketDisconnect:
            pass
        finally:
            daemon.connections.discard(conn)
            if conn.client is not None:
                daemon.arbiter.leave(conn.client)
                daemon.forget(conn.client)
                daemon.announce_clients()
            for task in tasks:
                task.cancel()

    async def index(request) -> FileResponse | HTMLResponse:
        page = ui_dir / "index.html"
        # 作り直すと assets の名前が変わるので、ページは毎回確かめさせる（古いページが消えた資産を指して白くならない）
        return FileResponse(page, headers={"Cache-Control": "no-cache"}) if page.exists() else HTMLResponse(BUILD_HELP)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        daemon.loop = asyncio.get_running_loop()
        watcher = asyncio.create_task(daemon.watch())
        gdb = None
        if gdb_port is not None:
            gdb = GdbServer(daemon, backend, gdb_options, gdb_role)
            daemon.gdb_port = await gdb.start(gdb_port)
        for options in streams:
            await asyncio.to_thread(backend.stream_serve, **options)
        for options in stream_logs:
            await asyncio.to_thread(backend.stream_log_start, **options)
        yield
        if gdb is not None:
            await gdb.close()
        watcher.cancel()

    app = Starlette(routes=[WebSocketRoute("/api", api), Route("/", index),
                            Mount("/assets", StaticFiles(directory=ui_dir / "assets", check_dir=False))],
                    lifespan=lifespan)
    app.state.daemon = daemon
    return app


def lan_token(given: str | None, home: Path, renew: bool = False) -> str:
    """--lan の合言葉。指定があればそれ（保存は変えない）。無ければ home/lan_token に保存したものを使い回し、無いか
    renew なら作って保存する（デーモンを起動し直しても画面の URL が変わらない）。"""
    if given:
        return given
    path = home / "lan_token"
    if not renew and path.is_file():
        saved = path.read_text(encoding="ascii").strip()
        if saved:
            return saved
    token = secrets.token_urlsafe(16)
    home.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="ascii")
    return token


def main() -> None:
    import uvicorn
    from ..serial_link import DEFAULT_BAUD
    parser = argparse.ArgumentParser(description="fprobe-daemon: fprobe を握って API を出す")
    parser.add_argument("--serial", help="fprobe の USB シリアル番号")
    parser.add_argument("--name", help="個体名（fprobe name set で付けた名前）。--serial / --port / --eth の代わり")
    parser.add_argument("--port", help="SerialLink の COM ポート")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    parser.add_argument("--eth", help="EthLink のボードの IP アドレス")
    parser.add_argument("--listen", type=int, default=DEFAULT_PORT, help="待ち受けるポート")
    parser.add_argument("--bitstreams", help="Primer の構成のビットストリームの置き場所（<dir>/<design>/top.fs。"
                                             "既定は FPROBE_BITSTREAMS か gateware/build）")
    parser.add_argument("--lan", action="store_true", help="LAN にも開く（トークンが要る）")
    parser.add_argument("--token", help="--lan のトークン（省略時は生成して表示）")
    parser.add_argument("--gdb", type=int, metavar="PORT", help="GDB サーバを開くポート（例 3333。127.0.0.1 だけ）")
    parser.add_argument("--gdb-target", default="cortex_m",
                        help="GDB がつながったときにターゲットが未接続なら接続する pyOCD のターゲット名")
    parser.add_argument("--gdb-frequency", type=int, default=4_000_000, help="そのときの SWD / JTAG の周波数")
    parser.add_argument("--gdb-role", choices=["human", "ai", "script"], default="human",
                        help="GDB の利用者の役割（human は人の一時停止でも保留されない）")
    parser.add_argument("--stream", action="append", default=[], type=parse_stream, metavar="NAME=PORT[:telnet]",
                        help="生の流れ（rtt0〜 / swo / uart / semihosting）を 127.0.0.1:PORT で公開する（繰り返せる。"
                             "例 rtt0=19021:telnet。RTT・SWO の受信は API で始める）")
    parser.add_argument("--stream-log", action="append", default=[], type=parse_stream_log, metavar="NAME=PATH[:time]",
                        help="流れをファイルに書く（繰り返せる。:time なら行の頭に時刻。例 rtt0=rtt0.log:time）")
    parser.add_argument("--new-token", action="store_true", help="--lan の保存した合言葉を作り直す（URL が変わる）")
    args = parser.parse_args()
    from .. import probe_names
    try:
        probe_names.apply_to_args(args)
    except probe_names.ProbeNameError as e:
        parser.error(str(e))
    token = lan_token(args.token, fprobe_home(), renew=args.new_token) if args.lan else None
    if token:
        print(f"トークン: {token}")
    print(f"画面: http://127.0.0.1:{args.listen}/" + (f"#token={token}" if token else ""), flush=True)
    if args.gdb is not None:
        print(f"GDB: target remote 127.0.0.1:{args.gdb}", flush=True)
    for s in args.stream:
        print(f"流れ {s['stream']}: 127.0.0.1:{s['port']}（{s['protocol']}）", flush=True)
    backend = Probe(args.serial, args.port, args.baud, args.eth, bitstreams=args.bitstreams)
    backend.ui_state_persist(fprobe_home() / "ui_state.json")   # 信号の定義・システム解析の表等を再起動を越えて
    app = create_app(backend, token=token, activity_dir=fprobe_home() / "activity",
                     device=device_of(args.serial, args.port, args.eth), gdb_port=args.gdb,
                     gdb_options=GdbOptions(args.gdb_target, args.gdb_frequency), gdb_role=args.gdb_role,
                     streams=args.stream, stream_logs=args.stream_log)
    try:
        uvicorn.run(app, host="0.0.0.0" if args.lan else "127.0.0.1", port=args.listen, log_level="warning")
    finally:
        app.state.daemon.arbiter.close()
        backend.close()


if __name__ == "__main__":
    main()
