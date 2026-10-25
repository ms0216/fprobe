"""Probe の道具: デバッグの出来事のフック（U44）の登録・一覧・実行の記録・保存と読み込み。

Probe（fprobe.probe。共通の部品の型は fprobe.probe_host.ProbeHost）に混ぜて使う mixin。登録簿（hook_table）を持つだけで、実行はデーモン
（fprobe.api.daemon）が行う（AI・画面・GDB のどの操作でも同じフックが走る）。機器には触らない。
仕様: docs/superpowers/specs/2026-10-18-u25-u44-rules-hooks-design.md の 3 節。
"""

import json
from pathlib import Path

from .hooks import EVENTS, HookError, HookTable

__all__ = ["HookTools"]


def _api_methods() -> set[str]:
    from .api.spec import load_spec                     # API の仕様のメソッド名（call の実在の検査）
    return set(load_spec().methods)


class HookTools:
    """フックの登録簿（Probe の mixin）。"""

    def _init_hooks(self) -> None:
        self.hook_table = HookTable(methods=_api_methods())

    def hooks_add(self, event: str, phase: str, actions: list[dict], name: str = "", required: bool = False,
                  enabled: bool = True) -> dict:
        """フックを登録する。event: connect / disconnect / reset / flash / resume / halt、phase: before / after
        （halt の after は誰の操作でも・ブレークポイントでも「止まったのを見つけた後」）。actions: {"evaluate": C の式} /
        {"mark": 記録の印の文（{event} {phase} {method} {client} {pc} {where} を置き換え）} /
        {"call": API のメソッド, "params": {...}}。required の before フックが失敗したら操作をしない。
        デーモン経由の操作（AI・画面・GDB）で走る。"""
        try:
            hook = self.hook_table.add(event, phase, actions, name, required, enabled)
        except HookError as e:
            raise ValueError(str(e)) from None
        return {**hook.to_dict(), "operations": sorted(EVENTS[event])}

    def hooks_remove(self, id: int | None = None, all: bool = False) -> dict:
        """フックを外す（id、または all=true ですべて）。"""
        if (id is None) == (not all):
            raise ValueError("id か all=true のどちらか一方")
        return {"removed": self.hook_table.remove(None if all else id)}

    def hooks_list(self) -> dict:
        """登録したフックの一覧（AI と人が同じものを見る）。"""
        return {"hooks": self.hook_table.list()}

    def hooks_log(self, last: int = 50) -> dict:
        """フックの実行の記録（新しいものが後。動作ごとの成否・値・誤り、きっかけの操作と利用者）。"""
        return {"entries": self.hook_table.read_log(last)}

    def hooks_save(self, path: str) -> dict:
        """フックの宣言を JSON のファイルに書く（hooks_load で読み直せる）。"""
        data = self.hook_table.dump()
        Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"path": path, "hooks": len(data["hooks"])}

    def hooks_load(self, path: str, replace: bool = True) -> dict:
        """JSON のファイルからフックを読む（replace なら今のフックを置き換え、false なら足す）。"""
        try:
            loaded = self.hook_table.load(json.loads(Path(path).read_text(encoding="utf-8")), replace)
        except HookError as e:
            raise ValueError(str(e)) from None
        return {"loaded": loaded, "hooks": self.hook_table.list()}
