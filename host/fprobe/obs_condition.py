"""FPGA の観測の信号の条件: FPGA に書く一致への翻訳（compile_match）と、観測の値で成り立った回数を数える評価（count_condition）。
値の読み方・比べ方は両方とも obs_value（同じ意味）。

以下は count_condition: fpga_obs_read の items で、信号の条件が成り立った回数を数える。

条件の書き方は fprobe.correlate と同じ（「restart changed and state=BUSY」。fpga. の前置きは外す）。数える範囲は
渡した値の並びそのもので、暗黙の窓・暗黙の件数を持たない（範囲は使う側が fpga_obs_count の last で明示する）。
返すのは事実だけ: 回数・数えた値の数・最初と最後の値の時刻・その間の秒・切れ目（gap）の数・標本型の見えない変化（hidden）の数・失った変化の数。
値が無いのを「変化なし」とみなさない（values = 0・時刻は null のまま返す）。
「変化」は同じ組（group）の直前の値と比べる（別の組の値と比べない。切れ目をまたいでも直前の値と比べる）。
rose / fell は 0 と 0 以外（"0" は 0）。
"""

from typing import Any

from .correlate import ConditionError, parse_condition
from .fpga_signals import FpgaSignals
from .obs_value import is_on, number, same

__all__ = ["compile_match", "count_condition"]


def _holds(op: str, want: str | None, cur: Any, prev: Any) -> bool:
    if op == "changed":
        return cur != prev
    if op == "rose":
        return prev is not None and not is_on(prev) and is_on(cur)
    if op == "fell":
        return cur is not None and is_on(prev) and not is_on(cur)
    return same(cur, want)


def _numeric(fields: dict, sigs: dict) -> dict:
    """列挙の名前を信号の定義の表（名前 -> 値）で数に直す（表に無い名前はそのまま）。"""
    out = dict(fields)
    for name, cur in fields.items():
        sig = sigs.get(name)
        if sig is not None and isinstance(cur, str):
            for v, label in sig.enum.items():
                if label == cur:
                    out[name] = v
                    break
    return out


def _want(want: str | None, sig) -> str | None:
    """'=' の値が列挙の名前なら、信号の定義の表で数に（compile_match と同じ）。"""
    if want is not None and sig is not None:
        for v, label in sig.enum.items():
            if label == want:
                return str(v)
    return want


def count_condition(items: list[dict], condition: str, signals: FpgaSignals | None = None) -> dict:
    """signals（信号の定義）を渡すと、列挙の名前を定義の表で数に直して比べる（rose / fell は値 0 への / からの遷移、
    state=1 と state=BUSY は同じ値に一致。compile_match・correlate と同じ意味）。渡さなければ名前は文字のまま
    （rose / fell は名前を 0 以外とみなす。表が無いので数に直せない）。items（fpga_obs_read の items。時刻順）で condition が成り立った回数と、数えた範囲の事実。数えない値の数も
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
        sigs = {} if signals is None else {x.name: x for x in signals.signals(group)}
        if sigs:
            cur = _numeric(cur, sigs)
        prev = previous.get(group)
        if prev is not None and all(_holds(op, _want(want, sigs.get(name)), cur.get(name), prev.get(name))
                                    for name, op, want in terms):
            count += 1
        previous[group] = cur
    times = [it["time_us"] for it in values]                 # 鎖が違えば並びが時刻順でないこともある: 最小・最大を取る
    t0 = min(times) if times else None
    t1 = max(times) if times else None
    return {"condition": cond.text, "count": count, "values": len(values), "t0_us": t0, "t1_us": t1,
            "span_s": None if t0 is None else (t1 - t0) / 1e6,
            "gaps": sum(1 for it in items if it.get("kind") == "gap"),
            "hidden": sum(1 for it in items if it.get("kind") == "hidden"),    # 標本型の採取の間の見えない変化
            "lost": sum(int(it.get("lost") or 0) for it in items if it.get("kind") in ("gap", "hidden")),
            "untimed": sum(1 for it in items if it.get("kind") == "value" and it.get("time_us") is None),
            "unnamed": sum(1 for it in items if it.get("kind") == "value" and it.get("time_us") is not None
                           and it.get("fields") is None)}


def compile_match(condition: str, signals: FpgaSignals, group: int = 0) -> tuple[int, int, int]:
    """システム解析の条件（例 "restart changed and state=BUSY"）を、観測のエンジンの値の一致（CSR obs.match・
    match_change）の (mask, value, change) に。瞬間の項（changed / rose / fell）は 1 つまで（ハードは変化のマスクの
    どれかを見る）、無ければ「一致に入った瞬間」。rose / fell は 1 ビットの信号だけ。信号は組 group の定義から。
    docs/superpowers/specs/2026-10-21-system-analysis-design.md §4.1"""
    try:
        cond = parse_condition(condition, need_edge=False)
    except ConditionError as e:
        raise ValueError(str(e)) from None
    by_name = {s.name: s for s in signals.signals(group)}
    if not cond.terms:
        raise ValueError("条件が空（マスク 0 は「常に一致」になる。使う側が条件を明示する）")
    edges = [t for t in cond.terms if t.op != "="]
    if len(edges) > 1:
        raise ValueError("FPGA で比べる条件の瞬間の項（changed / rose / fell）は 1 つまで")
    mask = value = change = 0
    for t in cond.terms:
        name = t.name[5:] if t.name.startswith("fpga.") else t.name
        sig = by_name.get(name)
        if sig is None:
            raise ValueError(f"組 {group} に信号 {name!r} が無い（FPGA の信号だけで書く）")
        bits = sig.mask << sig.lsb
        if t.op == "=":
            names = {v: k for k, v in sig.enum.items()}
            text = t.value or ""
            v = names[text] if text in names else number(text)
            if v is None:
                raise ValueError(f"{name} の値 {text!r} が数でも列挙の名前でもない")
            if not 0 <= v <= sig.mask:
                raise ValueError(f"{name} の値 {v} は {sig.width} ビットに入らない")
            if mask & bits and (value & bits) != v << sig.lsb:
                raise ValueError(f"{name} に二つの値を求めている")
            mask |= bits
            value |= v << sig.lsb
        else:
            change |= bits
            if t.op in ("rose", "fell"):
                if sig.width != 1:
                    raise ValueError(f"rose / fell は 1 ビットの信号だけ（{name} は {sig.width} ビット）")
                want = bits if t.op == "rose" else 0
                if mask & bits and (value & bits) != want:
                    raise ValueError(f"{name} に二つの値を求めている（{t.op} は変化の後が {1 if want else 0}）")
                mask |= bits
                value = (value & ~bits) | want
    return mask, value, change
