"""デバッグの出来事のフック（U44）: 停止・リセット・書き込み・接続・再開の前後に、式の評価・記録の印・任意の API の
呼び出しを登録する。

ここは登録簿（HookTable: 検証・一致・実行の記録・保存と読み込み）だけ。実行はデーモン（fprobe.api.daemon）が、
AI・画面・GDB のどの操作でも同じに行う（だから全員が同じフックを見る）。
仕様: docs/superpowers/specs/2026-10-18-u25-u44-rules-hooks-design.md の 3 節。
"""

from __future__ import annotations  # HookTable.list が組み込みの list を隠すため、注釈は評価しない

import re
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any

__all__ = ["ACTIONS", "EVENTS", "Hook", "HookError", "HookRefused", "HookTable", "PHASES", "event_of", "expand"]

PHASES = ("before", "after")
ACTIONS = ("evaluate", "mark", "call")
# 出来事 → それを起こす操作（API のメソッド名と、GDB の操作の記録名）
EVENTS: dict[str, frozenset[str]] = {
    "connect": frozenset({"target.connect", "gdb.attach"}),
    "disconnect": frozenset({"target.disconnect"}),
    "reset": frozenset({"target.reset", "gdb.reset"}),
    "flash": frozenset({"target.flash", "target.program_ram", "target.flash.erase", "gdb.flash"}),
    "resume": frozenset({"target.resume", "debug.run_until_halt", "gdb.continue"}),
    # halt の before は止める操作の前。after は操作ではなく「止まったことを見つけた後」（デーモンの見張り）
    "halt": frozenset({"target.halt", "gdb.interrupt"}),
}
_BY_OPERATION = {op: event for event, ops in EVENTS.items() for op in ops}
TEMPLATE_NAMES = ("event", "phase", "method", "client", "pc", "where")
_TEMPLATE = re.compile(r"\{(\w+)\}")
LOG_SIZE = 500
FORBIDDEN_PREFIXES = ("hooks.", "session.")       # フックからフック・接続の状態は変えない


class HookError(ValueError):
    """フックの書き方の誤り。"""


class HookRefused(RuntimeError):
    """required の before フックが失敗したので、操作をしなかった。"""


def event_of(operation: str | None) -> str | None:
    """操作の名前（API のメソッド名・GDB の操作の記録名）→ 出来事。フックの対象でなければ None。"""
    return _BY_OPERATION.get(operation) if operation else None


def expand(text: str, context: dict) -> str:
    """文の {event} {phase} {method} {client} {pc} {where} を置き換える（知らない名前・値の無い名前はそのまま）。"""
    def one(m: re.Match) -> str:
        value = context.get(m[1]) if m[1] in TEMPLATE_NAMES else None
        return m[0] if value is None else str(value)
    return _TEMPLATE.sub(one, text)


@dataclass(frozen=True)
class Hook:
    id: int
    event: str
    phase: str
    actions: tuple[dict, ...]
    name: str = ""
    required: bool = False
    enabled: bool = True

    def to_dict(self) -> dict:
        d = asdict(self)
        d["actions"] = list(self.actions)
        return d


def _check_action(action: Any, methods: set[str] | None, where: str) -> dict:
    if not isinstance(action, dict):
        raise HookError(f"{where} は辞書（evaluate / mark / call）")
    kinds = [k for k in ACTIONS if k in action]
    if len(kinds) != 1:
        raise HookError(f"{where}: evaluate / mark / call のどれか 1 つ")
    kind = kinds[0]
    allowed = {kind, "params"} if kind == "call" else {kind}
    if set(action) - allowed:
        raise HookError(f"{where}: 未知の欄 {sorted(set(action) - allowed)}")
    if not isinstance(action[kind], str) or not action[kind]:
        raise HookError(f"{where}.{kind} は文字列")
    if kind == "call":
        method = action["call"]
        if method.startswith(FORBIDDEN_PREFIXES):
            raise HookError(f"{where}: フックから {method} は呼べない（フック・接続の状態は変えない）")
        if methods is not None and method not in methods:
            raise HookError(f"{where}: メソッド {method} は無い")
        if not isinstance(action.get("params", {}), dict):
            raise HookError(f"{where}.params は辞書（メソッドの引数）")
    return dict(action)


class HookTable:
    """フックの登録簿と実行の記録（スレッド安全: 作業スレッドの道具とデーモンのイベントループの両方から触る）。"""

    def __init__(self, methods: set[str] | None = None, log_size: int = LOG_SIZE):
        self._methods = methods                   # call のメソッドの実在を確かめる（API の仕様のメソッド名）
        self._lock = threading.Lock()
        self._hooks: list[Hook] = []
        self._next = 1
        self._log: deque[dict] = deque(maxlen=log_size)
        self._seq = 0

    def _make(self, event: str, phase: str, actions: list, name: str, required: bool, enabled: bool) -> Hook:
        if event not in EVENTS:
            raise HookError(f"event は {sorted(EVENTS)} のどれか（{event!r}）")
        if phase not in PHASES:
            raise HookError(f"phase は before か after（{phase!r}）")
        if not isinstance(actions, list) or not actions:
            raise HookError("動作（actions）が 1 つ以上要る")
        if required and phase != "before":
            raise HookError("required（失敗したら操作をしない）は before のフックだけ")
        checked = tuple(_check_action(a, self._methods, f"actions[{n}]") for n, a in enumerate(actions))
        hook = Hook(self._next, event, phase, checked, str(name or ""), bool(required), bool(enabled))
        self._next += 1
        return hook

    def add(self, event: str, phase: str, actions: list, name: str = "", required: bool = False,
            enabled: bool = True) -> Hook:
        with self._lock:
            hook = self._make(event, phase, actions, name, required, enabled)
            self._hooks.append(hook)
            return hook

    def remove(self, hook_id: int | None) -> int:
        """hook_id のフックを外す（None ならすべて）。外した数。"""
        with self._lock:
            if hook_id is None:
                n, self._hooks = len(self._hooks), []
                return n
            kept = [h for h in self._hooks if h.id != hook_id]
            if len(kept) == len(self._hooks):
                raise KeyError(f"フック {hook_id} は無い")
            self._hooks = kept
            return 1

    def list(self) -> list[dict]:
        with self._lock:
            return [h.to_dict() for h in self._hooks]

    def matching(self, event: str, phase: str) -> list[Hook]:
        with self._lock:
            return [h for h in self._hooks if h.enabled and h.event == event and h.phase == phase]

    def wants(self, event: str, phase: str) -> bool:
        return bool(self.matching(event, phase))

    # --- 実行の記録 -------------------------------------------------------------------------

    def record(self, entry: dict) -> dict:
        with self._lock:
            self._seq += 1
            entry = {"seq": self._seq, "time": time.time(), **entry}
            self._log.append(entry)
            return entry

    def read_log(self, last: int = 50) -> list[dict]:
        with self._lock:
            return list(self._log)[-last:] if last > 0 else []

    # --- 保存と読み込み ---------------------------------------------------------------------

    def dump(self) -> dict:
        """宣言（JSON にできる形）。id は読み込むときに振り直す。"""
        with self._lock:
            return {"hooks": [{k: v for k, v in h.to_dict().items() if k != "id"} for h in self._hooks]}

    def load(self, data: dict, replace: bool = True) -> int:
        """dump の形を読む。replace なら今のフックを置き換える。全部を確かめてから入れる（途中で失敗しても変えない）。"""
        items = data.get("hooks") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise HookError("フックの宣言は {hooks: [{event, phase, actions, name, required, enabled}]}")
        with self._lock:
            made = []
            for n, item in enumerate(items):
                if not isinstance(item, dict) or set(item) - {"event", "phase", "actions", "name", "required", "enabled"}:
                    raise HookError(f"hooks[{n}] は {{event, phase, actions, name, required, enabled}}")
                try:
                    made.append(self._make(item.get("event"), item.get("phase"), item.get("actions"),
                                           item.get("name", ""), item.get("required", False), item.get("enabled", True)))
                except HookError as e:
                    raise HookError(f"hooks[{n}]: {e}") from None
            self._hooks = made if replace else self._hooks + made
            return len(made)
