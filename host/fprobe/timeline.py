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
from pathlib import Path

from .api.binary import BinaryResult
from .la import Waveform
from .recording import Recording
from . import timeline_fpga
from .timeline_rows import carry_in, classify, summarize, summary_sql

__all__ = ["DEFAULT_MAX_EDGES", "DEFAULT_MAX_EVENTS", "MAX_BINS", "STATES", "TILE_FORMAT", "capture_tile",
           "recording_tile"]

TILE_FORMAT = 2                        # 2: 出来事の段に段の規則の区画（row・label・value・depth・tone・address）
DEFAULT_MAX_EDGES = 2_000_000          # 生の変化点（全チャンネルの和）の上限。超えたら要約
DEFAULT_MAX_EVENTS = 100_000           # 段（stream）ごとの生の出来事の上限
MAX_BINS = 16_384                      # 要約の区間の数の上限（超えたら bin を広げる）
MAX_RAW_CHUNKS = 256                   # 生で返す波形のかたまりの数の上限
NO_DATA_ROW = "-"                      # 要約の 1 区間の「波形が無い」（recording.NO_DATA と同じ）
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
    sources = _wave_sources(rec)
    try:
        available = sum(n for _, _, n in sources)
        channels = list(range(available)) if channels is None else _checked(channels, available)
        if channels:
            out["wave"] = _recording_wave(sources, t0, t1, channels, (start, width, count), max_edges, sections)
            names = _channel_names(sources, channels)
            if names is not None:
                out["wave"]["names"] = names
    finally:
        for _, r, _ in sources[1:]:
            r.close()
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
    分けた段・ラベル・値・深さ・色の意味・飛び先の番地（fprobe.timeline_rows）。t0 の前を探した始め carry_from と、
    記録の始めまで探したか carry_complete（False なら carry_from より前に変わったきりの段の始めの値は探していない）。"""
    if stream == timeline_fpga.STREAM:                    # 組の値を記録の信号の定義で信号ごとに（後から付けた名前も効く）
        items, carried = timeline_fpga.raw_items(rec, t0, t1)
    else:
        items = list(rec.iter_events(t0, t1, [stream], overlapping=True))
        carried = carry_in(rec, stream, t0, t1)
        if carried.events:
            items = sorted(carried.events + items, key=lambda e: e.t)
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
    out = {"stream": stream, "mode": "raw", "count": n, "items": len(items), "kinds": kinds,
           "rows": [r.to_dict() for r in c.table.rows], "labels": c.labels, **carried.facts()}
    return _with_signals_error(rec, stream, out)


def _with_signals_error(rec: Recording, stream: str, lane: dict) -> dict:
    """FPGA の段: 記録の信号の定義が壊れていれば signals_error（組の値の段 1 本で見せている理由）。"""
    if stream == timeline_fpga.STREAM and (error := timeline_fpga.signals_error(rec)) is not None:
        lane["signals_error"] = error
    return lane


def _summary_lane(rec: Recording, stream: str, n: int, bins: tuple[int, int, int], i: int, sections: dict) -> dict:
    """要約の段: stream の区間ごとの件数と、段 × 区間の件数（values の段は最小・最大）。"""
    start, width, count = bins
    counts = rec.event_bins(start, width, count, [stream]).get(stream, array("I", [0]) * count)
    sections[f"lane.{i}.counts"] = counts
    end = start + width * count
    if stream == timeline_fpga.STREAM:                    # 信号は出来事を 1 回だけ読んでまとめて数える（summary_rows）
        rows = timeline_fpga.summary_rows(rec, start, width, count) +             rec.query_events(start, end, timeline_fpga.marker_sql(start, width, count))
    else:
        rows = rec.query_events(start, end, summary_sql(stream, start, width, count))
    s = summarize(stream, rows, count)
    sections[f"lane.{i}.row_counts"] = array("I", s.counts)
    if s.low is not None:
        sections[f"lane.{i}.row_min"] = array("d", s.low)
        sections[f"lane.{i}.row_max"] = array("d", s.high)
    return _with_signals_error(rec, stream, {"stream": stream, "mode": "summary", "count": n, "start": start,
                                             "bin": width, "bins": count, "rows": [r.to_dict() for r in s.table.rows]})


def _wave_sources(rec: Recording) -> list[tuple[str, Recording, int]]:
    """波形の源: 記録そのものと、合成した記録（python -m fprobe merge）の下のボードごとの記録 boards/<名前>（R25）。
    (名前、記録、チャンネルの数) の並び。ボードのチャンネルは、親のチャンネルの後ろに続く番号で並べる。"""
    out = [("", rec, rec.channel_count())]
    base = getattr(rec, "directory", None)
    for child in (rec.meta.get("merged") or {}).get("children", []):
        path = child.get("waveforms")
        if base is None or not path or not (Path(base) / path / "meta.json").is_file():
            continue
        sub = Recording.open(Path(base) / path)
        out.append((str(child.get("name", path)), sub, sub.channel_count()))
    return out


def _locate(sources: list[tuple[str, Recording, int]], channel: int) -> tuple[int, int]:
    """番号 channel の源の番号と、その源の中のチャンネル"""
    for k, (_, _, n) in enumerate(sources):
        if channel < n:
            return k, channel
        channel -= n
    raise ValueError(f"チャンネル {channel} は無い")


def _channel_names(sources: list[tuple[str, Recording, int]], channels: list[int]) -> list[str] | None:
    """チャンネルの名前: 記録の情報の channel_names、ボードのものは「ボード名.名前」。名前もボードも無ければ None。"""
    titles = sources[0][1].meta.get("channel_names")
    if len(sources) == 1 and not isinstance(titles, list):
        return None
    out = []
    for ch in channels:
        k, local = _locate(sources, ch)
        name, r, _ = sources[k]
        own = r.meta.get("channel_names")
        title = str(own[local]) if isinstance(own, list) and local < len(own) else f"ch{local}"
        out.append(f"{name}.{title}" if k else title)
    return out


def _recording_wave(sources: list[tuple[str, Recording, int]], t0: int, t1: int, channels: list[int],
                    bins: tuple[int, int, int], max_edges: int, sections: dict) -> dict:
    """波形（生の変化点か、そろえた区間の要約）。channels は源をまたいだ番号（親の後ろにボードのチャンネル）。"""
    by_source: dict[int, list[tuple[int, int]]] = {}
    for ch in channels:
        k, local = _locate(sources, ch)
        by_source.setdefault(k, []).append((ch, local))
    plan, edges, raw = [], 0, True
    for k, wanted in by_source.items():
        rec = sources[k][1]
        chunks = rec.wave_chunks(t0, t1)
        if len(chunks) > MAX_RAW_CHUNKS or not all(kept for _, kept in chunks):
            raw = False
            break
        for c, _ in chunks:
            tps = c.ticks_per_sample
            s0 = max(0, int((t0 - c.t0) / tps))
            s1 = min(c.length, max(s0 + 1, int(-(-(t1 - c.t0) // tps))))
            wave = rec.waveform(c)
            present = [(ch, local) for ch, local in wanted if local < c.channels]
            plan.append((c, wave, s0, s1, present))
            edges += sum(wave.count_between(local, s0, s1) for _, local in present)
            if edges > max_edges:
                raw = False
                break
        if not raw:
            break
    if raw:
        pieces = []
        for i, (c, wave, s0, s1, present) in enumerate(plan):
            levels = []
            for ch, local in present:                     # 区画の名前は源をまたいだ番号（wave.<かたまり>.<番号>）
                levels.append(wave.level_at(local, s0))
                sections[f"wave.{i}.{ch}"] = array("d", wave.edges_between(local, s0, s1))
            pieces.append({"t0": c.t0, "t1": c.t1, "tps": c.ticks_per_sample, "length": c.length, "s0": s0, "s1": s1,
                           "channels": [ch for ch, _ in present], "levels": levels})
        return {"mode": "raw", "channels": channels, "edges": edges, "pieces": pieces}
    start, width, count = bins
    views = {k: sources[k][1].waveform_view(start, start + width * count, count) for k in by_source}
    rows = []
    for ch in channels:
        k, local = _locate(sources, ch)
        view = views[k]
        rows.append(view[local] if local < len(view) else NO_DATA_ROW * count)
    _states(rows, sections, "wave.states")
    return {"mode": "summary", "channels": channels, "start": start, "bin": width, "bins": count}
