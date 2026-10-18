"""Probe の道具: ブレークポイントの動作（条件・無視する回数・ログ）。IAR の Log breakpoint・条件付き、GDB の
condition / ignore / dprintf の相当。

Probe（fprobe.mcp_server）に混ぜて使う mixin。止まったときの処理はセミホスティングと同じ場所（ObserveTools の
_run_until_halt と背景の見張り）で行い、続けるときは Debugger.resume（ブレークポイントを 1 命令踏み越える）で再開する。

- condition: C の式（止まった場所のローカル変数も使える）。偽なら止まらずに続ける。
- ignore_count: 最初の n 回は止まらない。
- log: 式の一覧（[] なら式なしで message だけ）。当たるたびに値を読んでログ（breakpoint_log）と記録（stream "debug"、kind "log"）に残し、止まらない。
  記録中はログの項に記録のタイムベースの時刻 t を付ける。
- 道具が置くブレークポイントの処理（_break_hooks: 番地 → (道具の名前, 処理)。例 probe_heap の malloc / free の記録）: 処理が
  True なら止まらずに続け、False なら止まり、None ならふつうの動作（上の 3 つ）に任せる。
ponytail: 当たるたびにコアを止めて読むので 1 回に数 ms かかる。速い頻度の観測は watch（サンプラ）か RTT・テストポイントで。
"""

import collections
import time
from dataclasses import dataclass, field

__all__ = ["BreakpointActions"]

LOG_LIMIT = 10_000


@dataclass
class _Action:
    condition: str | None = None
    ignore_count: int = 0
    log: list[str] | None = None
    message: str | None = None
    hits: int = 0
    errors: list[str] = field(default_factory=list)

    def describe(self) -> dict:
        return {"condition": self.condition, "ignore_count": self.ignore_count, "log": self.log,
                "message": self.message, "hits": self.hits, "last_error": self.errors[-1] if self.errors else None}


def _truth(value: object) -> bool:
    if isinstance(value, dict):
        value = value.get("value", value.get("string", True))     # 列挙・文字の配列
    return bool(value)


class BreakpointActions:
    def _init_breakpoints(self) -> None:
        self._actions: dict[int, _Action] = {}
        self._break_log: collections.deque = collections.deque(maxlen=LOG_LIMIT)
        self._break_seq = 0
        self._break_hooks: dict = {}

    def _set_action(self, address: int, condition, ignore_count, log, message) -> None:
        if condition or ignore_count or log is not None:
            self._actions[address] = _Action(condition, ignore_count, None if log is None else list(log), message)
        else:
            self._actions.pop(address, None)

    def _drop_actions(self, address: int | None = None) -> None:
        if address is None:
            self._actions.clear()
        else:
            self._actions.pop(address, None)

    def _break_action(self) -> bool:
        """止まった場所のブレークポイントの動作（_lock の中で）。True なら止まらずに続ける（呼び手が再開する）。"""
        pc = self.target.read_core_register("pc")
        hook = self._break_hooks.get(pc)
        if hook is not None:
            handled = hook[1](pc)
            if handled is not None:
                return handled
        action = self._actions.get(pc)
        if action is None:
            return False
        action.hits += 1
        if action.hits <= action.ignore_count:
            return True
        inspector = self.dbg.inspector()
        if action.condition:
            try:
                if not _truth(self._read_expression(inspector, action.condition)):
                    return True
            except Exception as e:                                # 条件が評価できない: 止めて知らせる
                action.errors.append(f"{action.condition}: {e}")
                return False
        if action.log is None:
            return False
        values = {}
        for expression in action.log:
            try:
                values[expression] = self._read_expression(inspector, expression)
            except Exception as e:
                values[expression] = {"error": str(e)}
        now = time.monotonic()
        self._break_seq += 1
        entry = {"seq": self._break_seq, "host_s": now, "address": pc, **self._place_of(pc), "hit": action.hits,
                 "message": action.message, "values": values}
        t = self._record_host("debug", "log", now, a=pc, body={k: entry[k] for k in ("where", "hit", "message", "values")})
        if t is not None:
            entry["t"] = t                                        # 記録の時刻（画面が時間軸のその時刻へ飛ぶ）
        self._break_log.append(entry)
        return True

    @staticmethod
    def _read_expression(inspector, expression: str) -> object:
        if inspector is None:
            raise RuntimeError("ELF に DWARF が無い（式は読めない）")
        return inspector.read(expression)["value"]

    def breakpoint_log(self, since: int = 0, limit: int = 200) -> dict:
        """ログ・ブレークポイントの記録（seq が since より後のものを古い順に limit 件）。next を次の since に渡す。"""
        items = [e for e in self._break_log if e["seq"] > since][:limit]
        return {"entries": items, "next": items[-1]["seq"] if items else since}
