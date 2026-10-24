"""操作の調停: 機器（fprobe・pyOCD）はスレッド安全でないので、すべての操作を 1 本の作業スレッドで順に実行する。
一時停止中（human が pause）は、human 以外の control の要求を保留し、resume で受けた順に流す。"""

import queue
import threading
import time
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, Callable

__all__ = ["Arbiter", "Client", "HoldExpired", "NotAllowed"]


class NotAllowed(Exception):
    """この利用者には許されない操作（一時停止・再開は human だけ）。"""


class HoldExpired(TimeoutError):
    """一時停止中に保留が hold_s を超えた要求。呼び出し側はもう諦めているかもしれないので、再開後に遅れて実行しない。"""


@dataclass(frozen=True)
class Client:
    id: int
    name: str
    role: str            # "human" / "ai" / "script"
    via: str = "api"     # つながり方: api（WebSocket の JSON-RPC）/ gdb（GDB サーバ）
    automated: bool | None = None   # hello で申告した自動操作か（画面は navigator.webdriver）。申告が無ければ None


class Arbiter:
    def __init__(self, on_control: Callable[[dict], None], hold_s: float = 300.0):
        self._on_control = on_control
        self._hold_s = hold_s
        self._lock = threading.Lock()
        self._ready: queue.Queue = queue.Queue()
        self._held: deque[tuple[Client, Callable[[], Any], Future, float]] = deque()      # 最後は保留した時刻
        self._paused_by: Client | None = None
        self._thread = threading.Thread(target=self._work, name="fprobe-arbiter", daemon=True)
        self._thread.start()

    def submit(self, client: Client, kind: str, fn: Callable[[], Any]) -> Future:
        future: Future = Future()
        with self._lock:
            if self._paused_by is not None and kind == "control" and client.role != "human":
                self._held.append((client, fn, future, time.monotonic()))
                held = True
            else:
                held = False
        if held:
            self._notify()
        else:
            self._ready.put((fn, future))
        return future

    def pause(self, client: Client) -> dict:
        if client.role != "human":
            raise NotAllowed("一時停止できるのは human だけ")
        with self._lock:
            self._paused_by = client
        self._notify()
        return self.state()

    def resume(self, client: Client) -> dict:
        if client.role != "human":
            raise NotAllowed("再開できるのは human だけ")
        self._release()
        return self.state()

    def leave(self, client: Client) -> None:
        with self._lock:
            kept = deque()
            for item in self._held:
                if item[0].id == client.id:
                    item[2].cancel()
                else:
                    kept.append(item)
            self._held = kept
            pauser_left = self._paused_by is not None and self._paused_by.id == client.id
        if pauser_left:
            self._release()                    # 止めた人がいなくなったら AI を永久に待たせない
        else:
            self._notify()

    def state(self) -> dict:
        with self._lock:
            return {"paused": self._paused_by is not None,
                    "paused_by": self._paused_by.name if self._paused_by else None, "held": len(self._held)}

    def close(self) -> None:
        self._ready.put(None)
        self._thread.join(timeout=5)

    def _release(self) -> None:
        with self._lock:
            self._paused_by = None
            held, self._held = self._held, deque()
        now = time.monotonic()
        for _, fn, future, since in held:
            # ponytail: 期限は再開のときにだけ見る（期限切れの要求は再開まで応答を待つ）。すぐ断る必要が出たらタイマーにする
            if now - since > self._hold_s:
                future.set_exception(HoldExpired(f"一時停止で {now - since:.0f} 秒保留され、取り消した（再開後にやり直す）"))
            else:
                self._ready.put((fn, future))
        self._notify()

    def _notify(self) -> None:
        self._on_control(self.state())

    def _work(self) -> None:
        while True:
            item = self._ready.get()
            if item is None:
                return
            fn, future = item
            if not future.set_running_or_notify_cancel():
                continue
            future.started_at = time.monotonic()        # 実行を始めた時刻（記録の「保留された時間」）
            try:
                future.set_result(fn())
            except BaseException as e:          # 機器の誤りも含めて呼び出し側へ。作業スレッドは止めない
                future.set_exception(e)
