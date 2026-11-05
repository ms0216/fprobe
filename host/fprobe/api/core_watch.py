"""ターゲットの状態の見張り: control の操作が終わるたびと watch_s ごとに状態を読み、変わったときだけ target.state として送り、
止まりを見つけたら halt の after フックを起こす。Daemon に混ぜて使う（self.arbiter・self.publish・self.target_state 等は Daemon が持つ）。"""

import asyncio
import contextlib

from . import NotConnected
from .arbiter import Client
from .core_types import WATCHER, LinkRecovered
from .jsonable import jsonable


class WatchMixin:
    def _ran(self) -> None:
        self.runs += 1
        self._moved = True                               # 購読が無くても読み直す（session.status の target を古くしない）
        self._poke()

    def _poke(self) -> None:
        if self.loop is not None and self._wake is not None:
            self.loop.call_soon_threadsafe(self._wake.set)

    def _wants_stops(self) -> bool:
        return self.hooks is not None and self.hooks.wants("halt", "after")

    async def watch(self) -> None:
        """ターゲットの状態の見張り: control の操作が終わるたびと watch_s ごとに、target.state の購読者か止まりの
        フックがあれば（コアを動かし得る操作の後は購読が無くても: session.status の target を古くしない）、作業スレッドで
        状態を読み、変わったときだけ送る。読みは read なので一時停止中も動き、記録しない。"""
        self._wake = asyncio.Event()
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self.watch_s)
            self._wake.clear()
            moved, self._moved = self._moved, False
            if not moved and not any("target.state" in c.topics for c in self.connections) and not self._wants_stops():
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
                # 作業スレッドを通さずに読める状態（Linux のプロセスの全コア停止デバッグ: 長い待ちの間も配る）があればそれ
                unqueued = getattr(self.backend, "target_status_unqueued", None)
                state = await asyncio.to_thread(unqueued) if unqueued is not None else None
                if state is None:
                    state = await asyncio.wrap_future(self.arbiter.submit(WATCHER, "read",
                                                                          self._guarded(self.backend.target_status)))
                state = jsonable(state)
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
                self._note("target", "state", state, time_source="host_received")
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
