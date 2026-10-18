"""パラレルバスの解読。仕様: docs/superpowers/specs/2026-10-18-u20-decoders-design.md

data のチャンネルを並べた値（data[0] がビット 0）を、clock があればその辺（直前の値）で、無ければ値が変わるたびに読む。
enable を与えると、その線が enable_level の間だけ読む。クロックの無いバスでは settle サンプルより短く続いた値
（線ごとの変化のずれの途中の値）を捨てる。
"""

from ..la import Transitions
from .core import Annotation, Decoding, project

__all__ = ["decode_parallel"]

EDGES = ("rising", "falling", "both")


def decode_parallel(tr: Transitions, *, data: list[int], clock: int | None = None, edge: str = "rising",
                    enable: int | None = None, enable_level: int = 0, settle: int = 0) -> Decoding:
    """パラレルバスを解読する。"""
    if not data:
        raise ValueError("data のチャンネルが要る")
    if len(set(data)) != len(data) or clock in data or (enable is not None and enable in data):
        raise ValueError("チャンネルが重なっている")
    if edge not in EDGES:
        raise ValueError(f"辺は {EDGES} のどれか（{edge!r}）")
    if settle < 0:
        raise ValueError(f"settle は 0 以上（{settle}）")
    lines = list(data) + [ch for ch in (clock, enable) if ch is not None]
    starts, values = project(tr, lines)
    n = len(data)
    mask = (1 << n) - 1
    b_clock = 1 << n if clock is not None else 0
    b_enable = 1 << (n + (clock is not None)) if enable is not None else 0
    params = {"data": list(data), "clock": clock, "edge": edge, "enable": enable, "enable_level": enable_level,
              "settle": settle}
    digits = (n + 3) // 4
    out: list[Annotation] = []

    def active(v: int) -> bool:
        return enable is None or bool(v & b_enable) == bool(enable_level)

    def word(start: int, end: int, value: int) -> None:
        out.append(Annotation(start, max(end, start + 1), "word", f"0x{value:0{digits}X}", value=value))

    if clock is not None:
        pending: tuple[int, int] | None = None          # (辺の位置, 値) — 次の辺で閉じる
        prev = values[0] if len(values) else 0
        for i in range(1, len(values)):
            now, at = values[i], starts[i]
            if (prev ^ now) & b_clock:
                rising = bool(now & b_clock)
                if edge == "both" or rising == (edge == "rising"):
                    if pending is not None:
                        word(pending[0], at, pending[1])
                        pending = None
                    if active(prev):
                        pending = (at, prev & mask)
            prev = now
        if pending is not None:
            word(pending[0], tr.length, pending[1])
    else:
        runs: list[tuple[int, int, int]] = []           # (始まり, 終わり, 値)（無効の間は値 None で区切る）
        for i in range(len(values)):
            v, at = values[i], starts[i]
            end = starts[i + 1] if i + 1 < len(values) else tr.length
            value = v & mask if active(v) else None
            if end - at < settle and runs and i + 1 < len(values):
                continue                                # 落ち着かない途中の値: 前の値の区間に含める
            if runs and runs[-1][2] == value:
                runs[-1] = (runs[-1][0], end, value)
            else:
                if runs:
                    runs[-1] = (runs[-1][0], at, runs[-1][2])
                runs.append((at, end, value))
        for k, (s, e, value) in enumerate(runs):
            if value is not None and k > 0:             # 捕捉の始まりの値は変化ではない
                word(s, e, value)
    return Decoding("parallel", tuple(out), tr.sample_period_s, tr.trigger, tr.length, params)
