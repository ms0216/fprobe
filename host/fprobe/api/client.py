"""デーモンの利用者（同期）。MCP・CLI・スクリプトが使う。

    with DaemonClient("ws://127.0.0.1:7820/api", "my-script", "script") as c:
        c.call("target.halt")
"""

import itertools
import json
import logging
import threading
from contextlib import ExitStack
from concurrent.futures import Future
from typing import Any, Callable

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from . import API_VERSION
from .binary import decode_frame

__all__ = ["ApiError", "DaemonClient"]


class ApiError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


class DaemonClient:
    def __init__(self, url: str, name: str, role: str, token: str | None = None, api_version: int = API_VERSION,
                 _hello: bool = True, automated: bool | None = None, source: str | None = None):
        """automated: hello で自動操作かを申告する（省略で申告しない）。source: 以後の要求に付ける「どこから」
        （操作の記録の source。省略で付けない）。"""
        self.source = source
        self._stack = ExitStack()                       # websockets 17 は connect を with で使う形が正
        self._ws = self._stack.enter_context(connect(url, max_size=None, ping_interval=None))
        self._lost: BaseException | None = None          # 受信が止まった理由（以後の要求はすぐ失敗させる）
        self._ids = itertools.count(1)
        self._pending: dict[int, Future] = {}
        self._lock = threading.Lock()
        self._listeners: list[tuple[set[str], Callable[[str, dict], None]]] = []
        self._reader = threading.Thread(target=self._read, name="fprobe-api-client", daemon=True)
        self._reader.start()
        if _hello:
            try:
                extra = {} if automated is None else {"automated": automated}
                self.hello = self.call("session.hello", name=name, role=role, api_version=api_version, token=token, **extra)
            except BaseException:
                self.close()
                raise

    @classmethod
    def raw(cls, url: str) -> "DaemonClient":
        """hello せずにつなぐ（試験用）。request で生の応答を見る。"""
        return cls(url, "", "", _hello=False)

    def request(self, method: str, params: dict) -> dict:
        return self.request_with(method, params, source=self.source)

    def request_with(self, method: str, params: dict, *, source: Any = None) -> dict:
        """生の応答を返す。source を付ける（None なら付けない）。"""
        rid = next(self._ids)
        future: Future = Future()
        with self._lock:
            if self._lost is not None:
                raise ConnectionError("デーモンとの接続が切れている") from self._lost
            self._pending[rid] = future
        try:
            message = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
            if source is not None:
                message["source"] = source
            self._ws.send(json.dumps(message, allow_nan=False))
        except ConnectionClosed as e:
            with self._lock:
                self._pending.pop(rid, None)
            raise ConnectionError("デーモンとの接続が切れている") from e
        except ValueError:                                   # NaN・±Infinity は JSON に無い（送らない）。登録した待ちを残さない
            with self._lock:
                self._pending.pop(rid, None)
            raise
        return future.result()

    def call(self, method: str, /, **params: Any) -> Any:          # /: 引数の名前 method（rtos.timeline.start 等）とぶつからない
        reply = self.request(method, params)
        if "error" in reply:
            raise ApiError(reply["error"]["code"], reply["error"]["message"])
        return reply["result"]

    def subscribe(self, topics: list[str], callback: Callable[[str, dict], None]) -> None:
        self._listeners.append((set(topics), callback))
        self.call("session.subscribe", topics=sorted({t for ts, _ in self._listeners for t in ts}))

    def close(self) -> None:
        self._stack.close()
        self._reader.join(timeout=2)

    def __enter__(self) -> "DaemonClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _read(self) -> None:
        try:
            for frame in self._ws:
                if isinstance(frame, bytes):            # バイナリの応答: 区画は result["data"] に array で
                    message, data = decode_frame(frame)
                    message["result"]["data"] = data
                else:
                    message = json.loads(frame)
                if "id" in message and message["id"] is None:
                    continue                            # 要求に結び付かない誤り（壊れたフレームへの応答）: 受信は続ける
                if "id" in message and message["id"] is not None:
                    with self._lock:
                        future = self._pending.pop(message["id"], None)
                    if future is not None:
                        future.set_result(message)
                else:
                    for topics, callback in self._listeners:
                        if message["method"] in topics:
                            try:
                                callback(message["method"], message["params"])
                            except Exception:          # 購読側の不具合で受信を止めない（止めると以後の呼び出しが全部失敗する）
                                logging.getLogger(__name__).exception("購読の関数が例外を投げた: %s", message["method"])
        except BaseException as e:
            self._lost = e
        finally:
            with self._lock:
                if self._lost is None:
                    self._lost = ConnectionError("デーモンとの接続が閉じた")
                pending, self._pending = self._pending, {}
            for future in pending.values():
                future.set_exception(ConnectionError("デーモンとの接続が切れた"))
