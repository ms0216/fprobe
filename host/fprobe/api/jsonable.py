"""デーモンの JSON の入出力: 結果を JSON にできる形へ（jsonable）、要求の検査（NaN・孤立サロゲート）。"""

import dataclasses
import enum
import math
import re
from pathlib import Path
from typing import Any

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
