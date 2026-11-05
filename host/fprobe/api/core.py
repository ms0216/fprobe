"""デーモンの核: Daemon（要求の検査・許可・調停・実行・操作の記録・通知・ターゲット状態の見張り・フック）と接続・誤りの型。
通信路（WebSocket・HTTP）は webapp.py、起動の引数は daemon.py。"""

import asyncio
import dataclasses
import functools
import itertools
import secrets
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from . import API_VERSION, E_DEVICE, E_INTERNAL, E_INVALID_REQUEST, E_METHOD_NOT_FOUND, E_NO_DATA, E_NO_HELLO, E_NOT_ALLOWED, E_NOT_CONNECTED, E_TIMEOUT, NoData, NotConnected
from ..hooks import event_of
from .activity import ActivityLog, summarize
from .arbiter import Arbiter, Client, HoldExpired, NotAllowed
from .binary import BinaryResult
from .core_hooks import HooksMixin
from .core_session import SessionMixin
from .core_types import (INTERRUPT_METHODS, NOTIFY_LIMIT, RECORDING_METHODS, RUN_METHODS, UNQUEUED_READS,  # noqa: F401（再公開を含む）
                         _IN_HOOK, _SOURCE, LinkRecovered, _Connection, _Rpc, _who, _with_hints)
from .core_watch import WatchMixin
from .jsonable import jsonable
from .permissions import Permissions
from .spec import Spec


class Daemon(SessionMixin, WatchMixin, HooksMixin):
    def __init__(self, backend: Any, token: str | None, activity_dir: Path, spec: Spec, device: dict,
                 watch_s: float = 1.0, permissions: Permissions | None = None):
        self.backend, self.token, self.spec, self.device, self.watch_s = backend, token, spec, device, watch_s
        self.permissions = permissions                         # 許可の表（None = パスを確かめない。試験・ライブラリの使い方）
        self.target_state: dict | None = None                # 見張りが最後に見たターゲットの状態
        self.link_lost: dict | None = None                   # USB が切れて開き直した（B28）。ターゲットにつなぎ直すまで状態に出す
        self.boot = secrets.token_hex(4)                       # この起動の識別（記録の番号の振り直しを利用者が見分ける）
        self._wake: asyncio.Event | None = None
        self.runs = 0                                          # RUN_METHODS が終わった回数
        self._moved = False                                    # RUN_METHODS が終わった後、まだ状態を読み直していない
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
        if method == "session.permissions":
            if self.permissions is None:
                return {"checked": False, "file": None, "loaded": False, "created": False, "exec": [], "write": []}
            return self.permissions.as_dict()
        if method == "session.methods":
            return self._methods(params)
        if method == "session.hints":
            return self._hints(params)
        params = self._permit(method, params)
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
            if moves:                    # 応答の前に状態を読み直す: 直後の session.status がこの操作の結果を返す
                await self._observe(client, inline=inline)
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
        interrupt = getattr(self.backend, "request_halt_now", None)
        if method.name in INTERRUPT_METHODS and interrupt is not None                 and not (self.arbiter.state()["paused"] and client.role != "human"):
            interrupt()
        if method.caller:                                # 呼び出し元を実装へ（引数の仕様・記録には出さない）
            fn = functools.partial(fn, caller=_who(client))
        # quiet（画面の自動の読み）は状態を変えない読みにだけ効く。操作は必ず記録する
        record = method.record and not (quiet and (method.kind == "read" or method.observes))
        unqueued = getattr(self.backend, "read_unqueued", None) if method.name in UNQUEUED_READS else None
        if unqueued is not None:
            try:
                result = await asyncio.to_thread(unqueued, method.name, params)
            except asyncio.CancelledError:
                raise
            except BaseException as e:
                code = E_NOT_CONNECTED if isinstance(e, NotConnected) else E_DEVICE
                raise _Rpc(code, f"{type(e).__name__}: {e}", _with_hints(method.name, {"exception": type(e).__name__, "detail": str(e)}, {"code": "device", "exception": type(e).__name__})) from None
            if result is not None:
                return jsonable(result)
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
            raise _Rpc(code, f"{type(e).__name__}: {e}", _with_hints(method.name, {"exception": type(e).__name__, "detail": str(e)}, {"code": "device", "exception": type(e).__name__})) from None
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

    def _note(self, stream: str, kind: str, body: dict, time_source: str = "host",
              started_host_s: float | None = None) -> None:
        """機能の実装が共通の時間軸の記録（F3）を持っていれば、操作とターゲットの状態の変化を渡す（記録中だけ書かれる）。
        時刻は PC の時計（time_source: "host" = PC の出来事、"host_received" = PC が読んだターゲットの状態）。
        started_host_s = 操作を受けた PC の単調時計の秒（記録が始まりも写す）。"""
        note = getattr(self.backend, "record_host_event", None)
        if note is not None:
            extra = {} if started_host_s is None else {"started_host_s": started_host_s}
            note(stream, kind, body, time_source=time_source, **extra)

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
            self._note("host", "call", recorded, started_host_s=started)
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
