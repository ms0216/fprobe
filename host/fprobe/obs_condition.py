"""FPGA の観測の値（fpga_obs_read の items）で、信号の条件が成り立った回数を数える。

条件の書き方は fprobe.correlate と同じ（「restart changed and state=BUSY」。fpga. の前置きは外す）。数える範囲は
渡した値の並びそのもので、暗黙の窓・暗黙の件数を持たない（範囲は使う側が fpga_obs_count の last で明示する）。
返すのは事実だけ: 回数・数えた値の数・最初と最後の値の時刻・その間の秒・切れ目（gap）の数・失った変化の数。
値が無いのを「変化なし」とみなさない（values = 0・時刻は null のまま返す）。
「変化」は同じ組（group）の直前の値と比べる（別の組の値と比べない。切れ目をまたいでも直前の値と比べる）。
rose / fell は 0 と 0 以外（"0" は 0）。
"""

from typing import Any

from .correlate import parse_condition

__all__ = ["count_condition"]


def _on(x: Any) -> bool:
    return x is not None and x != 0 and x != "0"


def _holds(op: str, want: str | None, cur: Any, prev: Any) -> bool:
    if op == "changed":
        return cur != prev
    if op == "rose":
        return prev is not None and not _on(prev) and _on(cur)
    if op == "fell":
        return cur is not None and _on(prev) and not _on(cur)
    return cur is not None and str(cur) == want


def count_condition(items: list[dict], condition: str) -> dict:
    """items（fpga_obs_read の items。時刻順）で condition が成り立った回数と、数えた範囲の事実。数えない値の数も
    返す: untimed = 時刻の決まらない値、unnamed = 信号の定義の無い値（fields が無い）。
    条件が読めなければ fprobe.correlate.ConditionError。"""
    cond = parse_condition(condition)
    terms = [(t.name.removeprefix("fpga."), t.op, t.value) for t in cond.terms]
    values = [it for it in items if it.get("kind") == "value" and it.get("fields") is not None
              and it.get("time_us") is not None]
    count = 0
    previous: dict[int, dict] = {}
    for it in values:
        group, cur = it.get("group", 0), it["fields"]
        prev = previous.get(group)
        if prev is not None and all(_holds(op, want, cur.get(name), prev.get(name)) for name, op, want in terms):
            count += 1
        previous[group] = cur
    t0 = values[0]["time_us"] if values else None
    t1 = values[-1]["time_us"] if values else None
    return {"condition": cond.text, "count": count, "values": len(values), "t0_us": t0, "t1_us": t1,
            "span_s": None if t0 is None else (t1 - t0) / 1e6,
            "gaps": sum(1 for it in items if it.get("kind") == "gap"),
            "lost": sum(int(it.get("lost") or 0) for it in items if it.get("kind") == "gap"),
            "untimed": sum(1 for it in items if it.get("kind") == "value" and it.get("time_us") is None),
            "unnamed": sum(1 for it in items if it.get("kind") == "value" and it.get("time_us") is not None
                           and it.get("fields") is None)}
