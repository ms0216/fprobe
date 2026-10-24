"""共通の時間軸の FPGA の観測の段（docs/superpowers/specs/2026-10-20-fpga-observation-ui-design.md §3）。

記録には組の生の値（stream fpga: value の a = 組・b = 16 ビットの値、gap、group）だけがある。段は見るときに、記録の
情報の信号の定義（meta の fpga.signals。fprobe.fpga_signals）で信号ごとに作る。だから後から付けた名前も過去の記録に
効く。生の段・要約の段・範囲の前の値の持ち込みの 3 つの経路が同じ段（同じ規則の名前と段の値）を作る。

- 1 ビット・列挙の信号: 状態の帯（規則 state）。文字は書式どおり（列挙は名前）。
- 幅のある数の信号: 値の階段（規則 number）。値は信号のビット（`(b >> lsb) & mask`）。
- 定義の無い組: 組の値の状態の帯 1 本（16 進）。段の値は組の番号。
- gap（溢れの切れ目）・group（観測する組の切り替え）: 印。
"""

from typing import Any

from .fpga_signals import FpgaSignals, Signal

try:                                                          # 入っていれば要約をまとめて数える（大きな記録で 10 倍速い）
    import numpy as _np
except ImportError:                                           # 無くても同じ結果（遅いだけ）
    _np = None
from .recording import Event
from .timeline_rows import CarryIn, lookback_ranges

__all__ = ["STREAM", "carry_in", "expand", "raw_items", "signals_error", "signals_of", "summary_sql"]

STREAM = "fpga"


def signals_of(rec: Any) -> FpgaSignals:
    """記録の情報の信号の定義（無い・壊れていれば空 = すべての組を組の値の段 1 本で。壊れていたことは signals_error）。"""
    try:
        return FpgaSignals.from_dict(rec.meta.get("fpga.signals"))
    except ValueError:
        return FpgaSignals({})


def signals_error(rec: Any) -> str | None:
    """記録の情報の信号の定義が壊れていればその誤り（段に出す。黙って空にしない）。無い・正しければ None。"""
    try:
        FpgaSignals.from_dict(rec.meta.get("fpga.signals"))
    except ValueError as e:
        return str(e)
    return None


def _lanes(signals: FpgaSignals, group: int) -> list[tuple[str, str, str, Signal | None]]:
    """組の段: (仮の出来事の種類, 段の値, 段の名前, 信号)。"""
    sigs = signals.signals(group)
    if not sigs:
        return [("signal_state", str(group), str(group), None)]
    return [("signal_state" if s.width == 1 or s.format == "enum" else "signal_number", f"{group}.{s.name}", s.name, s)
            for s in sigs]


UNKNOWN = "—"


def expand(events: list[Event], signals: FpgaSignals) -> list[Event]:
    """組の値の出来事を、信号ごとの仮の出来事（kind signal_state / signal_number、b = 信号の値、body の row・title・
    text）に。変わっていない信号の出来事は作らない（帯が同じ文字で切れない）。値以外の出来事はそのまま。
    組の切り替え（group）ではほかの組の段を、溢れの切れ目（gap）ではすべての段を「—」（body の unknown）で閉じる
    （観測していない値を、観測しているように続けない）。"""
    out: list[Event] = []
    last: dict[str, int] = {}
    rows: dict[str, tuple[str, int, str]] = {}            # 段 → (仮の出来事の種類, 組, 名前)

    def close(e: Event, keep: int | None) -> None:
        for row, (kind, g, title) in rows.items():
            if g != keep and row in last:
                del last[row]
                out.append(Event(e.t, e.stream, kind, g, 0, {"row": row, "title": title, "text": UNKNOWN,
                                                            "unknown": True}))

    for e in events:
        if e.kind == "group":
            close(e, e.a)
        elif e.kind == "gap" or e.kind.endswith("_untimed"):     # 欠け・時刻の決まらない記録: 値は不明
            close(e, None)
        if e.kind != "value":
            out.append(e)
            continue
        for kind, row, title, s in _lanes(signals, e.a):
            v = s.extract(e.b) if s is not None else e.b
            rows[row] = (kind, e.a, title)
            if last.get(row) == v:
                continue
            last[row] = v
            text = signals.text(s, v) if s is not None else f"0x{v:X}"
            out.append(Event(e.t, e.stream, kind, e.a, v, {"row": row, "title": title, "text": text}, e.t_end,
                             e.uncertainty))
    return out


def carry_in(rec: Any, t0: int, t1: int) -> CarryIn:
    """t0 の時点で観測していた組の、t0 の前の最後の値（範囲の始めでの状態を描くため）。組の切り替えの記録があれば、
    最後の切り替えから t0 まで（その組はそこからしか観測していない: 探すべき範囲をすべて探す）。切り替えの記録が無い
    古い記録は組ごとに、範囲の幅の 16 倍・256 倍・4096 倍と前を探し、何か見つかった所で止める（それでも無ければ記録の
    始めまで。fprobe.timeline_rows.carry_in と同じ探し方）。探した始めは searched_from（黙って打ち切らない）。"""
    span = rec.span()
    if span is None or span[0] >= t0:
        return CarryIn([], None, True)
    switches = rec.query_events(span[0], t0, [(
        "SELECT a, t FROM events WHERE stream = ? AND kind = 'group' AND t < ? ORDER BY t DESC LIMIT 1", [STREAM, t0])])
    if switches:
        g, since = max(switches, key=lambda x: x[1])
        rows = rec.query_events(since, t0, [(
            "SELECT t, stream, kind, a, b, t_end, unc FROM events WHERE stream = ? AND kind = 'value' AND a = ? "
            "AND t >= ? AND t < ? ORDER BY t DESC LIMIT 1", [STREAM, g, since, t0])])
        return CarryIn([Event(t, s, kind, a, b, None, t_end, unc) for t, s, kind, a, b, t_end, unc in rows[:1]],
                       since, True)
    for lo in lookback_ranges(span, t0, t1):
        sql = ("SELECT t, stream, kind, a, b, body, t_end, unc, MAX(t) FROM events "
               "WHERE stream = ? AND kind = 'value' AND t >= ? AND t < ? GROUP BY a")
        latest: dict[int, Event] = {}
        for t, s, kind, a, b, _body, t_end, unc, _ in rec.query_events(lo, t0, [(sql, [STREAM, lo, t0])]):
            if a not in latest or latest[a].t < t:
                latest[a] = Event(t, s, kind, a, b, None, t_end, unc)
        if latest or lo == span[0]:
            return CarryIn(sorted(latest.values(), key=lambda e: e.t), lo, lo == span[0])
    raise AssertionError("lookback_ranges の最後は記録の始め")


def raw_items(rec: Any, t0: int, t1: int) -> tuple[list[Event], CarryIn]:
    """生の段の出来事（持ち込みを含め、信号ごとに展開した後）と、持ち込みの探し方の事実。"""
    carried = carry_in(rec, t0, t1)
    items = carried.events + list(rec.iter_events(t0, t1, [STREAM], overlapping=True))
    return expand(sorted(items, key=lambda e: e.t), signals_of(rec)), carried


def summary_rows(rec: Any, start: int, width: int, count: int, fast: bool = True) -> list[tuple]:
    """要約の信号の段の行（summary_sql の信号の問い合わせと同じ: 規則の名前, 区間, 段の値, 名前, 件数, 最小, 最大）を、
    出来事を 1 回だけ読んで全信号まとめて数える（信号ごとの窓関数の SQL は大きな記録で信号 1 つ数秒かかった）。
    信号が変わった値の出来事だけを数え、溢れの切れ目・組の切り替えの後は数え直す。"""
    end = start + width * count
    signals = signals_of(rec)
    if fast and _np is not None:
        return _summary_rows_np(rec.query_events(start, end, [(
            "SELECT t, kind = 'value', a, b FROM events WHERE stream = ? AND t >= ? AND t < ? AND "
            "kind IN ('value', 'gap', 'group') ORDER BY t, rowid", [STREAM, start, end])]), signals, start, width)
    events = rec.query_events(start, end, [(
        "SELECT t, kind, a, b FROM events WHERE stream = ? AND t >= ? AND t < ? AND kind IN ('value', 'gap', 'group') "
        "ORDER BY t, rowid", [STREAM, start, end])])
    lanes: dict[int, list] = {}
    fields: dict[int, list[tuple[tuple[int, int], int, int]]] = {}   # 組 → [((組, 段), lsb, mask)]（毎回求めない）
    last: dict[tuple[int, int], int] = {}                     # (組, 段) → 前の値（切れ目・切り替えで忘れる）
    cells: dict[tuple[int, int], dict[int, list]] = {}       # (組, 段) → 区間 → [件数, 最小, 最大]
    for t, kind, a, b in events:
        if kind != "value":
            last.clear()
            continue
        if a not in lanes:
            lanes[a] = _lanes(signals, a)
            fields[a] = [((a, k), sig.lsb, sig.mask) if sig is not None else ((a, k), 0, -1)
                         for k, (_kind, _row, _title, sig) in enumerate(lanes[a])]
        for key, lsb, mask in fields[a]:
            v = b >> lsb & mask
            if key in last and last[key] == v:
                continue
            last[key] = v
            cell = cells.setdefault(key, {}).get((t - start) // width)
            if cell is None:
                cells[key][(t - start) // width] = [1, v, v]
            else:
                cell[0] += 1
                cell[1], cell[2] = min(cell[1], v), max(cell[2], v)
    out = []
    for g in sorted(lanes):
        for k, (lane_kind, row, title, sig) in enumerate(lanes[g]):
            number = lane_kind == "signal_number"
            for bin_, (n, lo, hi) in sorted(cells.get((g, k), {}).items()):
                out.append(("number" if number else "state", bin_, row, title, n, lo if number else None,
                            hi if number else None))
    return out


def _summary_rows_np(events: list, signals: FpgaSignals, start: int, width: int) -> list[tuple]:
    """summary_rows と同じ結果を numpy でまとめて（events は (t, 値の出来事なら 1, a, b)）: 切れ目・組の切り替えで
    区切った区画（epoch）の中で、信号の値が前の値の出来事から変わった所だけを数え、区間ごとに件数・最小・最大。"""
    if not events:
        return []
    cols = _np.array(events, dtype=_np.int64)
    t, is_value, a, b = cols[:, 0], cols[:, 1].astype(bool), cols[:, 2], cols[:, 3]
    epoch = _np.cumsum(~is_value)                             # 切れ目・組の切り替えのたびに数え直す
    out = []
    for g in sorted(int(x) for x in _np.unique(a[is_value])):
        sel = is_value & (a == g)
        tg, bg, eg = t[sel], b[sel], epoch[sel]
        new_epoch = _np.ones(len(tg), dtype=bool)
        new_epoch[1:] = eg[1:] != eg[:-1]
        bins_all = (tg - start) // width
        for k, (lane_kind, row, title, sig) in enumerate(_lanes(signals, g)):
            v = (bg >> sig.lsb) & sig.mask if sig is not None else bg
            changed = new_epoch.copy()
            changed[1:] |= v[1:] != v[:-1]
            bins, vals = bins_all[changed], v[changed]
            if not len(bins):
                continue
            first = _np.flatnonzero(_np.r_[True, bins[1:] != bins[:-1]])     # 区間は時刻順なので並んでいる
            counts = _np.diff(_np.r_[first, len(bins)])
            lo, hi = _np.minimum.reduceat(vals, first), _np.maximum.reduceat(vals, first)
            number = lane_kind == "signal_number"
            for i, n, x, y in zip(bins[first].tolist(), counts.tolist(), lo.tolist(), hi.tolist()):
                out.append(("number" if number else "state", i, row, title, n, x if number else None,
                            y if number else None))
    return out


def summary_sql(rec: Any, start: int, width: int, count: int) -> list[tuple[str, list]]:
    """要約の問い合わせ（fprobe.timeline_rows.summarize の形: 規則の名前, 区間, 段の値, 名前, 件数, 最小, 最大）。
    信号ごとに、組の値の出来事から信号のビットを SQL の式で取り出す。"""
    end = start + width * count
    signals = signals_of(rec)
    groups = {g for g, in rec.query_events(start, end, [(
        "SELECT DISTINCT a FROM events WHERE stream = ? AND kind = 'value' AND t >= ? AND t < ?",
        [STREAM, start, end])])}
    out = []
    for g in sorted(groups):
        for kind, row, title, s in _lanes(signals, g):
            rule = "state" if kind == "signal_state" else "number"
            value = f"((b >> {s.lsb}) & {s.mask})" if s is not None and rule == "number" else "NULL"
            bits = f"((b >> {s.lsb}) & {s.mask})" if s is not None else "b"
            # 信号が変わった値の出来事だけを数える（生の段の expand と同じ: 溢れの切れ目・組の切り替えの後は数え直す）
            out.append((f"SELECT ?, (t - ?) / ?, ?, ?, COUNT(*), MIN({value}), MAX({value}) FROM ("
                        f"SELECT t, b, kind, v, LAG(v) OVER (PARTITION BY epoch ORDER BY t, id) AS p FROM ("
                        f"SELECT t, b, kind, rowid AS id, CASE WHEN kind = 'value' THEN {bits} END AS v, SUM(CASE WHEN kind = 'value' THEN 0 ELSE 1 END) "
                        f"OVER (ORDER BY t, rowid) AS epoch FROM events WHERE stream = ? AND t >= ? AND t < ? AND "
                        f"(kind IN ('gap', 'group') OR (kind = 'value' AND a = ?)))) "
                        f"WHERE kind = 'value' AND (p IS NULL OR p != v) GROUP BY 2",
                        [rule, start, width, row, title, STREAM, start, end, g]))
    return out + marker_sql(start, width, count)


def marker_sql(start: int, width: int, count: int) -> list[tuple[str, list]]:
    """要約の印の段（溢れの切れ目・標本型の見えない変化・組の切り替え）の区間ごとの件数の問い合わせ。"""
    end = start + width * count
    return [("SELECT ?, (t - ?) / ?, NULL, NULL, COUNT(*), NULL, NULL FROM events "
             "WHERE stream = ? AND kind = ? AND t >= ? AND t < ? GROUP BY 2",
             [kind, start, width, STREAM, kind, start, end]) for kind in ("gap", "hidden", "group")]
