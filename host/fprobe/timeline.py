"""共通タイムラインのタイル（G01）: 画面が毎フレームの間引きに使う材料を、範囲ごとにバイナリで返す。

仕様: docs/superpowers/specs/2026-10-18-timeline-tiles-design.md
（背景: docs/superpowers/specs/2026-10-18-ui-framework-evaluation.md §4。毎フレームの間引きは画面の Web Worker）

タイル = 見えている範囲（と前後の余白）の
- **生データ**（raw）: 波形はかたまり（piece）ごとに、範囲の頭の値とチャンネルごとの変化点（サンプル番号）。
  出来事は stream ごとに t・t_end・種類。画面は `la.Waveform.view` / `Recording.waveform_view` / `Recording.density`
  と同じ定義で、どの視点にも正確に間引ける。
- **要約**（summary）: 生データが上限（max_edges / max_events）を超える範囲では、幅 bin の区間ごとの
  波形の状態（0 / 1 / 変化 / 無し）と出来事の件数。区間の境は絶対時刻の bin の倍数（長期の記録では縮図の段の幅に
  そろえ、事前計算の索引から出す）。

時刻の単位: 捕捉（source = capture）はサンプル番号、記録（source = recording）はタイムベース。tick_s が 1 単位の秒。
区画の時刻は f64（2^53 まで整数で正確。100 MHz で 1000 日）。
"""

from __future__ import annotations

from array import array
from collections.abc import Sequence

from .api.binary import BinaryResult
from .la import Waveform
from .recording import Recording
from .timeline_rows import carry_in, classify, summarize, summary_sql

__all__ = ["DEFAULT_MAX_EDGES", "DEFAULT_MAX_EVENTS", "MAX_BINS", "STATES", "TILE_FORMAT", "capture_tile",
           "recording_tile"]

TILE_FORMAT = 2                        # 2: 出来事の段に段の規則の区画（row・label・value・depth・tone・address）
DEFAULT_MAX_EDGES = 2_000_000          # 生の変化点（全チャンネルの和）の上限。超えたら要約
DEFAULT_MAX_EVENTS = 100_000           # 段（stream）ごとの生の出来事の上限
MAX_BINS = 16_384                      # 要約の区間の数の上限（超えたら bin を広げる）
MAX_RAW_CHUNKS = 256                   # 生で返す波形のかたまりの数の上限
STATES = {"0": 0, "1": 1, "x": 2, "-": 3}            # 要約の状態の符号（u8）
_TRANSLATE = bytes.maketrans(b"01x-", bytes([0, 1, 2, 3]))


def _ceil_div(a: int, b: int) -> int:
    return -(-a // b)


def _power_of_two_at_most(n: int) -> int:
    return 1 << max(0, int(n).bit_length() - 1)


def _bins(t0: int, t1: int, width: int, widen: int) -> tuple[int, int, int]:
    """[t0, t1) を覆う、width の倍数にそろえた区間: (始め, 幅, 数)。数が MAX_BINS を超えたら幅を widen 倍に。"""
    while True:
        first, last = t0 // width, _ceil_div(t1, width)
        if last - first <= MAX_BINS:
            return first * width, width, max(1, last - first)
        width *= widen


def _header(source: str, t0: int, t1: int, tick_s: float, extent: Sequence[int] | None) -> dict:
    return {"format": TILE_FORMAT, "source": source, "t0": t0, "t1": t1, "tick_s": tick_s,
            "extent": list(extent) if extent is not None else None, "wave": {"mode": "none", "channels": []},
            "lanes": []}


def _states(rows: list[str], sections: dict, name: str) -> None:
    sections[name] = "".join(rows).encode("ascii").translate(_TRANSLATE)


def _raw_piece(wave: Waveform, channels: Sequence[int], s0: int, s1: int, index: int, sections: dict) -> list[int]:
    """かたまりの [s0, s1) の範囲: チャンネルごとの s0 の値を返し、s0 < t < s1 の変化点を区画に入れる。"""
    levels = []
    for ch in channels:
        levels.append(wave.level_at(ch, s0))
        sections[f"wave.{index}.{ch}"] = array("d", wave.edges_between(ch, s0, s1))
    return levels


def capture_tile(wave: Waveform, info: dict, t0: int, t1: int | None = None, channels: Sequence[int] | None = None,
                 bin: int | None = None, max_edges: int = DEFAULT_MAX_EDGES) -> BinaryResult:
    """最後の捕捉（info は la.view の capture）のタイル。時刻はサンプル番号、範囲は捕捉の中に切り詰める。"""
    length = info["length"]
    t0, t1 = max(0, int(t0)), min(length, length if t1 is None else int(t1))
    if t1 <= t0:
        raise ValueError(f"タイルの範囲が空（t0={t0}, t1={t1}）")
    channels = list(range(wave.channels)) if channels is None else _checked(channels, wave.channels)
    out = _header("capture", t0, t1, info["sample_period_ns"] * 1e-9, [0, length])
    out["capture"] = info
    sections: dict = {}
    edges = sum(wave.count_between(ch, t0, t1) for ch in channels)
    if edges <= max_edges:
        levels = _raw_piece(wave, channels, t0, t1, 0, sections)
        out["wave"] = {"mode": "raw", "channels": channels, "edges": edges,
                       "pieces": [{"t0": 0, "t1": length, "tps": 1, "length": length, "s0": t0, "s1": t1,
                                   "channels": channels, "levels": levels}]}
    else:
        start, width, count = _bins(t0, t1, _power_of_two_at_most(bin or max(1, (t1 - t0) // 4096)), 2)
        if start + width * count <= length:
            rows = wave.view(start, start + width * count, count)
        else:                                       # 最後の区間は捕捉の終わりで切れる（短い）
            head = wave.view(start, start + width * (count - 1), count - 1) if count > 1 else [""] * wave.channels
            tail = wave.view(start + width * (count - 1), length, 1)
            rows = [a + b for a, b in zip(head, tail)]
        _states([rows[ch] for ch in channels], sections, "wave.states")
        out["wave"] = {"mode": "summary", "channels": channels, "edges": edges, "start": start, "bin": width,
                       "bins": count}
    return BinaryResult(out, sections)


def _checked(channels: Sequence[int], available: int) -> list[int]:
    channels = [int(c) for c in channels]
    bad = [c for c in channels if not 0 <= c < available]
    if bad:
        raise ValueError(f"チャンネル {bad} は無い（0〜{available - 1}）")
    return channels


def recording_tile(rec: Recording, t0: int, t1: int, channels: Sequence[int] | None = None,
                   streams: Sequence[str] | None = None, bin: int | None = None,
                   max_edges: int = DEFAULT_MAX_EDGES, max_events: int = DEFAULT_MAX_EVENTS) -> BinaryResult:
    """記録のタイル（時刻はタイムベース）。波形と、streams（省略時はすべて）の出来事の段。"""
    t0, t1 = int(t0), int(t1)
    if t1 <= t0:
        raise ValueError(f"タイルの範囲が空（t0={t0}, t1={t1}）")
    out = _header("recording", t0, t1, 1.0 / rec.meta["timebase_hz"], rec.span())
    sections: dict = {}
    grid = getattr(rec, "grid", None)
    want = max(1, bin or (t1 - t0) // 4096)
    if grid is not None and want >= grid.w0:                       # 長期の記録: 縮図の段の幅にそろえる
        start, width, count = _bins(t0, t1, grid.width(grid.level_for(want)), 1 << 4)
    else:
        start, width, count = _bins(t0, t1, _power_of_two_at_most(want), 2)
    available = rec.channel_count()
    channels = list(range(available)) if channels is None else _checked(channels, available)
    if channels:
        out["wave"] = _recording_wave(rec, t0, t1, channels, (start, width, count), max_edges, sections)
        titles = rec.meta.get("channel_names")                        # 記録の情報にチャンネルの名前があれば
        if isinstance(titles, list):
            out["wave"]["names"] = [str(titles[ch]) if ch < len(titles) else f"ch{ch}" for ch in channels]
    names = rec.distinct("stream") if streams is None else list(streams)
    coverage = rec.coverage(t0, t1)
    if not coverage["complete"]:
        out["coverage"] = coverage
    for i, stream in enumerate(names):
        n = rec.density(t0, t1, 1, [stream])[0]
        if n <= max_events:
            out["lanes"].append(_raw_lane(rec, stream, t0, t1, n, i, sections))
        else:
            out["lanes"].append(_summary_lane(rec, stream, n, (start, width, count), i, sections))
    return BinaryResult(out, sections)


def name_rows(result: dict, stream: str, namer) -> None:
    """stream の段のうち、番地で分けた段（id が "…:0x…"）の名前を namer(番地) で付け直す（None なら番地のまま）。
    記録には番地しか無い段（DAP サンプラの語）を、観測している式・ELF の変数の名前で見せる。"""
    for lane in result.get("lanes", []):
        if lane.get("stream") != stream:
            continue
        for row in lane.get("rows", []):
            _, sep, key = row["id"].partition(":")
            if sep and key.startswith("0x"):
                name = namer(int(key, 16))
                if name:
                    row["title"] = name


def _raw_lane(rec: Recording, stream: str, t0: int, t1: int, n: int, i: int, sections: dict) -> dict:
    """生の段: 出来事（t0 より前に始まって t0 以降に終わるものも。状態・値の段は t0 の前の最後の出来事も）と、段の規則で
    分けた段・ラベル・値・深さ・色の意味・飛び先の番地（fprobe.timeline_rows）。"""
    items = list(rec.iter_events(t0, t1, [stream], overlapping=True))
    carried = carry_in(rec, stream, t0, t1)
    if carried:
        items = sorted(carried + items, key=lambda e: e.t)
    kinds = sorted({e.kind for e in items})
    code = {k: j for j, k in enumerate(kinds)}
    c = classify(stream, items)
    sections[f"lane.{i}.t"] = array("d", (e.t for e in items))
    sections[f"lane.{i}.t_end"] = array("d", (e.t if e.t_end is None else e.t_end for e in items))
    sections[f"lane.{i}.kind"] = array("H", (code[e.kind] for e in items))
    sections[f"lane.{i}.row"] = array("H", c.row)
    sections[f"lane.{i}.label"] = array("I", c.label)
    sections[f"lane.{i}.value"] = array("d", c.value)
    sections[f"lane.{i}.depth"] = bytes(c.depth)
    sections[f"lane.{i}.tone"] = bytes(c.tone)
    sections[f"lane.{i}.address"] = array("d", c.address)
    return {"stream": stream, "mode": "raw", "count": n, "items": len(items), "kinds": kinds,
            "rows": [r.to_dict() for r in c.table.rows], "labels": c.labels}


def _summary_lane(rec: Recording, stream: str, n: int, bins: tuple[int, int, int], i: int, sections: dict) -> dict:
    """要約の段: stream の区間ごとの件数と、段 × 区間の件数（values の段は最小・最大）。"""
    start, width, count = bins
    counts = rec.event_bins(start, width, count, [stream]).get(stream, array("I", [0]) * count)
    sections[f"lane.{i}.counts"] = counts
    s = summarize(stream, rec.query_events(start, start + width * count, summary_sql(stream, start, width, count)),
                  count)
    sections[f"lane.{i}.row_counts"] = array("I", s.counts)
    if s.low is not None:
        sections[f"lane.{i}.row_min"] = array("d", s.low)
        sections[f"lane.{i}.row_max"] = array("d", s.high)
    return {"stream": stream, "mode": "summary", "count": n, "start": start, "bin": width, "bins": count,
            "rows": [r.to_dict() for r in s.table.rows]}


def _recording_wave(rec: Recording, t0: int, t1: int, channels: list[int], bins: tuple[int, int, int],
                    max_edges: int, sections: dict) -> dict:
    chunks = rec.wave_chunks(t0, t1)
    if len(chunks) <= MAX_RAW_CHUNKS and all(kept for _, kept in chunks):
        pieces, plan, edges = [], [], 0
        for c, _ in chunks:
            tps = c.ticks_per_sample
            s0 = max(0, int((t0 - c.t0) / tps))
            s1 = min(c.length, max(s0 + 1, int(-(-(t1 - c.t0) // tps))))
            wave = rec.waveform(c)
            plan.append((c, wave, s0, s1))
            edges += sum(wave.count_between(ch, s0, s1) for ch in channels if ch < c.channels)
            if edges > max_edges:
                break
        if edges <= max_edges:
            for i, (c, wave, s0, s1) in enumerate(plan):
                present = [ch for ch in channels if ch < c.channels]
                levels = _raw_piece(wave, present, s0, s1, i, sections)
                pieces.append({"t0": c.t0, "t1": c.t1, "tps": c.ticks_per_sample, "length": c.length, "s0": s0, "s1": s1,
                               "channels": present, "levels": levels})
            return {"mode": "raw", "channels": channels, "edges": edges, "pieces": pieces}
    start, width, count = bins
    rows = rec.waveform_view(start, start + width * count, count)
    _states([rows[ch] for ch in channels], sections, "wave.states")
    return {"mode": "summary", "channels": channels, "start": start, "bin": width, "bins": count}
