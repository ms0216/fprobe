"""プラグイン heuristics 共通: 結果の札（heuristic = 経験則の名前・assumption = 仮定）。"""

from typing import Any


def _tag(name: str, assumption: str, **out: Any) -> dict:
    return {"heuristic": name, "assumption": assumption, **out}
