"""出来事のフック（U44）の実行: 登録順に、きっかけの利用者と同じ role の利用者 "hook <id>" として動作（mark / evaluate / call）を
呼び、結果を hooks.log・通知 hook・記録へ渡す。Daemon に混ぜて使う（self.hooks・self.invoke・self._note 等は Daemon が持つ）。"""

import asyncio
import contextlib
import time

from ..hooks import HookRefused, expand
from .arbiter import Client
from .core_types import _IN_HOOK, _Rpc


class HooksMixin:
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
