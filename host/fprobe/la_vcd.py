"""ロジックアナライザの VCD 出力（PulseView 等で表示）。fprobe.la が再公開する。"""

from collections.abc import Sequence
from operator import itemgetter
from pathlib import Path

from .caps import legacy_la_layout
from .la_wave import Capture, Transitions

__all__ = ["DEFAULT_NAMES", "VcdWriter", "vcd_blocks", "write_vcd"]

# 古い並び（ケイパビリティ LA_LAYOUT の前）の内部の ch の名前。今の並びは channel_names（申告から）
DEFAULT_NAMES = {c.channel: c.name for c in legacy_la_layout(10) if c.kind == "internal"}


def write_vcd(capture: "Capture | Transitions", path: str | Path, names: dict[int, str] | None = None) -> None:
    """VCD（PulseView 等で表示）。時刻 0 = 最初のサンプル、単位は ns。トリガ位置にマーカー用の信号を置く。"""
    if isinstance(capture, Transitions):
        _write_vcd_transitions(capture, path, names)
        return
    tr = Transitions.from_capture(capture)
    ids, trigger_id, lines = _vcd_header(capture.channels, names, trigger=True)
    # トリガの信号: 最初・トリガ・その次のサンプルで、そのサンプルがトリガなら 1
    marks = [(i, f"{int(i == capture.trigger)}{trigger_id}")
             for i in sorted({0, capture.trigger, capture.trigger + 1}) if 0 <= i < len(capture.samples)]
    lines += _vcd_lines(tr.values, tr.starts, ids, capture.sample_period_s, None, marks)[0]
    Path(path).write_text("\n".join(lines) + "\n", encoding="ascii")


def _vcd_header(channels: int, names: dict[int, str] | None, trigger: bool) -> tuple[dict[int, str], str, list[str]]:
    """チャンネルの識別子・トリガの識別子・ヘッダの行。"""
    names = {**DEFAULT_NAMES, **(names or {})}
    ids = {ch: chr(33 + ch) for ch in range(channels)}
    trigger_id = chr(33 + channels)
    lines = ["$timescale 1ns $end", "$scope module fprobe $end"]
    lines += [f"$var wire 1 {ids[ch]} {names.get(ch, f'ch{ch}')} $end" for ch in range(channels)]
    if trigger:
        lines.append(f"$var wire 1 {trigger_id} trigger $end")
    lines += ["$upscope $end", "$enddefinitions $end"]
    return ids, trigger_id, lines


def _vcd_change_text(ids: tuple[tuple[int, str], ...], value: int, previous: int | None) -> str:
    """値が previous から value に変わったチャンネルの VCD の行（previous が None なら全チャンネル）。"""
    if previous is None:
        return "\n".join(f"{value >> ch & 1}{i}" for ch, i in ids)
    changed, out = value ^ previous, []
    for ch, i in ids:
        if changed >> ch & 1:
            out.append(f"{value >> ch & 1}{i}")
    return "\n".join(out)


_VCD_CACHE_MAX = 1 << 16


def vcd_blocks(values: Sequence[int], starts: Sequence[int], ids: dict[int, str],
               previous: int | None) -> tuple[list[tuple[int, str]], int | None]:
    """変化点（値, 開始位置）のうち、ids のチャンネルが変わるものの (開始位置, VCD の行) と、最後の値（次の呼び出しの
    previous）。previous が None なら最初の値で全チャンネルを書く。"""
    items = tuple(ids.items())
    cache: dict[int, str] = {}                                    # (前の値, 値) → 行。同じ組は何度も出る（クロックなど）
    blocks = []
    for v, t in zip(values, starts):
        if previous is None:
            text = _vcd_change_text(items, v, None)
        else:
            key = previous << 16 | v
            text = cache.get(key)
            if text is None:
                if len(cache) >= _VCD_CACHE_MAX:
                    cache.clear()
                text = cache[key] = _vcd_change_text(items, v, previous)
        previous = v
        if text:
            blocks.append((t, text))
    return blocks, previous


def _vcd_lines(values: Sequence[int], starts: Sequence[int], ids: dict[int, str], period_s: float,
               previous: int | None, marks: Sequence[tuple[int, str]] = ()) -> tuple[list[str], int | None]:
    """変化点（値, 開始サンプル番号）の VCD の行と、最後の値。marks = (サンプル番号, 行) の昇順は、同じ時刻の変化の後に入れる。"""
    blocks, previous = vcd_blocks(values, starts, ids, previous)
    if marks:
        blocks = sorted(blocks + list(marks), key=itemgetter(0))      # 安定: 同じ時刻は変化が先
    lines, current = [], None
    for t, text in blocks:
        if t != current:
            lines.append(f"#{round(t * period_s * 1e9)}")
            current = t
        lines.append(text)
    return lines, previous


def _write_vcd_transitions(tr: "Transitions", path: str | Path, names: dict[int, str] | None) -> None:
    ids, trigger_id, lines = _vcd_header(tr.channels, names, trigger=True)
    marks = [(0, f"0{trigger_id}"), (tr.trigger, f"1{trigger_id}"), (tr.trigger + 1, f"0{trigger_id}")]
    lines += _vcd_lines(tr.values, tr.starts, ids, tr.sample_period_s, None, sorted(marks, key=itemgetter(0)))[0]
    Path(path).write_text("\n".join(lines) + "\n", encoding="ascii")


class VcdWriter:
    """VCD を変化点の順に少しずつ書く（ストリーミング用。全体をメモリに持たない）。トリガの信号は無い。"""

    def __init__(self, path: str | Path, channels: int, sample_period_s: float, names: dict[int, str] | None = None):
        self._ids, _, header = _vcd_header(channels, names, trigger=False)
        self._period = sample_period_s
        self._previous: int | None = None
        self._file = open(path, "w", encoding="ascii", newline="\n")
        self._file.write("\n".join(header) + "\n")

    def write(self, values: Sequence[int], starts: Sequence[int]) -> None:
        """変化点（値, 通し番号の開始）を書き足す。starts は前回の続きから昇順。"""
        lines, self._previous = _vcd_lines(values, starts, self._ids, self._period, self._previous)
        if lines:
            self._file.write("\n".join(lines) + "\n")

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> "VcdWriter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
