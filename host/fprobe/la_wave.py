"""ロジックアナライザの捕捉データ: 値の表現（Capture・Transitions・Waveform）と、深いバッファの拍の復号。

fprobe.la が再公開する（使う側は fprobe.la から読んでよい）。デバイスにも VCD にも依らない純粋なデータの層。
"""

from array import array
from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass

__all__ = ["BEAT_BYTES", "Capture", "Transitions", "Waveform", "decode_beats", "decode_transitions", "trigger_sample"]

try:                                                          # 入っていれば波形の変化点の表を一括の計算で作る
    import numpy as _np
except ImportError:                                           # 無くても同じ結果（遅いだけ）
    _np = None


def _array_of(code: str, values) -> array:
    """numpy の配列を array に（バイト列の写しだけ）。"""
    out = array(code)
    out.frombytes(values.tobytes())
    return out


def _runs(samples: Sequence[int]) -> tuple["array[int]", "array[int]"]:
    """値が変わるところだけ: (値, 開始サンプル番号)。先頭は必ず含む。"""
    values, starts = array("H"), array("Q")
    if _np is not None and len(samples):
        a = _np.frombuffer(samples, dtype=_np.uint16) if isinstance(samples, array) else _np.asarray(samples, dtype=_np.uint16)
        at = _np.concatenate(([0], _np.flatnonzero(a[1:] != a[:-1]) + 1))
        return _array_of("H", a[at]), _array_of("Q", at.astype(_np.uint64))
    previous = None
    for i, v in enumerate(samples):
        if v != previous:
            values.append(v)
            starts.append(i)
            previous = v
    return values, starts


@dataclass(frozen=True)
class Capture:
    samples: Sequence[int]       # 時間順（深いバッファでは array）
    trigger: int                 # トリガのサンプルの位置
    trigger_time: int            # トリガ時のタイムベース（カウント）
    sample_period_s: float
    channels: int

    def channel(self, index: int) -> list[int]:
        return [s >> index & 1 for s in self.samples]


@dataclass(frozen=True)
class Transitions:
    """変化点の表現（長い記録を毎サンプルに展開しない）。starts[i] から次の starts まで値 values[i] が続く。"""
    values: Sequence[int]
    starts: Sequence[int]        # サンプル番号（昇順、starts[0] = 0）
    length: int                  # 全サンプル数
    trigger: int                 # トリガのサンプル番号
    trigger_time: int
    sample_period_s: float
    channels: int

    @classmethod
    def from_capture(cls, cap: "Capture") -> "Transitions":
        values, starts = _runs(cap.samples)
        return cls(values, starts, len(cap.samples), cap.trigger, cap.trigger_time, cap.sample_period_s,
                   cap.channels)

    def edges(self, channel: int) -> list[int]:
        """チャンネルが変化したサンプル番号。"""
        out, previous = [], None
        for v, t in zip(self.values, self.starts):
            bit = v >> channel & 1
            if previous is not None and bit != previous:
                out.append(t)
            previous = bit
        return out

    def level_at(self, channel: int, index: int) -> int:
        return self.values[bisect_right(self.starts, index) - 1] >> channel & 1


class Waveform:
    """画面に返す縮めた波形: 見ている区間を width 画素に分け、画素ごと・チャンネルごとに '0'（ずっと 0）/ '1'（ずっと 1）/
    'x'（区間内で変化）。チャンネルごとの変化点の表を一度だけ作り、表示のたびには二分探索で引く（全サンプルを送らない）。"""

    def __init__(self, tr: Transitions):
        # 変化点の表だけを持つ（元の並び tr は持たない）。サンプル番号が 32 ビットに収まれば 4 バイトの整数で
        # （記録のかたまりは 1 秒程度。大きな記録の時間軸でデーモンのメモリが 1 GB を超えた）
        self.length = tr.length
        first = tr.values[0] if len(tr.values) else 0
        self._initial = [first >> ch & 1 for ch in range(tr.channels)]
        code = "I" if tr.length < 1 << 32 else "Q"
        self._edges = [array(code) for _ in range(tr.channels)]         # チャンネルが変わったサンプル番号
        if _np is not None and len(tr.values) > 1:
            # numpy があれば一括の計算で（SWD のクロック等で数千万の変化点がある記録も数秒で開く）
            values, starts = _np.asarray(tr.values), _np.asarray(tr.starts, dtype=_np.uint64)
            dtype = _np.uint32 if code == "I" else _np.uint64
            changed = values[1:] ^ values[:-1]
            for ch, edges in enumerate(self._edges):
                edges.frombytes(starts[_np.flatnonzero((changed >> ch) & 1) + 1].astype(dtype).tobytes())
            return
        # ponytail: 変化点ごとの Python のループ（100 万変化点で 1 秒ほど、捕捉ごとに 1 回）。numpy が無いとき
        previous = first
        for v, t in zip(tr.values, tr.starts):
            changed = v ^ previous
            while changed:
                bit = changed & -changed
                self._edges[bit.bit_length() - 1].append(t)
                changed ^= bit
            previous = v

    @property
    def channels(self) -> int:
        return len(self._edges)

    @property
    def nbytes(self) -> int:
        """変化点の表の大きさ（記録の読み手が覚えておく量の上限に使う）。"""
        return sum(len(e) * e.itemsize for e in self._edges)

    def level_at(self, ch: int, s: int) -> int:
        """チャンネル ch のサンプル s の値（s 自身の変化を含む）。"""
        return self._initial[ch] ^ (bisect_right(self._edges[ch], s) & 1)

    def edges_between(self, ch: int, s0: int, s1: int) -> array:
        """チャンネル ch の s0 < t < s1 の変化点（サンプル番号の昇順。共通タイムラインのタイル用）。"""
        edges = self._edges[ch]
        return edges[bisect_right(edges, s0):bisect_left(edges, s1)]

    def count_between(self, ch: int, s0: int, s1: int) -> int:
        """edges_between の数（取り出さずに数える）。"""
        edges = self._edges[ch]
        return max(0, bisect_left(edges, s1) - bisect_right(edges, s0))

    def view(self, start: int, end: int, width: int) -> list[str]:
        """サンプル [start, end) を width 画素に縮めた、チャンネルごとの文字列。区間は捕捉の中に切り詰める。"""
        start, end = max(0, int(start)), min(self.length, int(end))
        if end <= start or width < 1:
            raise ValueError(f"表示の区間が空（start={start}, end={end}, width={width}）")
        span = end - start
        bounds = []
        for i in range(width):                                          # 整数で割る（浮動小数の誤差で区間が延びない）
            s = start + i * span // width
            bounds.append((s, max(s + 1, start - (-(i + 1) * span // width))))
        out = []
        for initial, edges in zip(self._initial, self._edges):
            chars = []
            for s, e in bounds:
                before = bisect_right(edges, s)                         # s までの変化の数（s 自身の変化を含む）
                if before < bisect_left(edges, e):
                    chars.append("x")
                else:
                    chars.append("1" if initial ^ (before & 1) else "0")
            out.append("".join(chars))
        return out


# --- 深いバッファ（外部メモリ）の拍の復号（gateware/rtl/la/la_encoder.vhd の符号化） ---------------------

BEAT_BYTES = 16


def decode_beats(data: bytes, rle: bool) -> "array[int]":
    """拍の列（16 バイト単位）をサンプル列に戻す。RAW: 1 拍 = 8 サンプル。RLE: 1 拍 = 4 レコード（値, 繰り返し）。"""
    if len(data) % BEAT_BYTES:
        raise ValueError("拍（16 バイト）の倍数でない")
    if not rle:
        samples = array("H")
        samples.frombytes(data)                     # リトルエンディアンの 16 ビット（このホストもリトル）
        return samples
    if _np is not None:
        records = _np.frombuffer(data, dtype="<u4")
        return _array_of("H", _np.repeat((records & 0xFFFF).astype(_np.uint16), (records >> 16).astype(_np.intp) + 1))
    records = array("I")
    records.frombytes(data)
    samples = array("H")
    for record in records:
        samples.extend(array("H", [record & 0xFFFF]) * ((record >> 16) + 1))
    return samples


def decode_transitions(data: bytes, rle: bool) -> tuple["array[int]", "array[int]", int]:
    """拍の列を変化点（値, 開始サンプル）と全サンプル数にする（RLE は展開しない）。"""
    if not rle:
        samples = decode_beats(data, False)
        return (*_runs(samples), len(samples))
    if _np is not None and len(data):
        records = _np.frombuffer(data, dtype="<u4")
        value, run = (records & 0xFFFF).astype(_np.uint16), (records >> 16).astype(_np.uint64) + 1
        position = _np.cumsum(run) - run                       # 各レコードの開始サンプル
        keep = _np.concatenate(([True], value[1:] != value[:-1]))
        return _array_of("H", value[keep]), _array_of("Q", position[keep]), int(run.sum())
    values, starts, previous = array("H"), array("Q"), None
    records = array("I")
    records.frombytes(data)
    position = 0
    for record in records:
        v = record & 0xFFFF
        if v != previous:
            values.append(v)
            starts.append(position)
            previous = v
        position += (record >> 16) + 1
    return values, starts, position


def trigger_sample(data: bytes, rle: bool, beat: int, slot: int) -> int:
    """拍 beat の slot 番目（トリガ）が、data の先頭から何サンプル目か。"""
    if not rle:
        return beat * 8 + slot
    records = array("I")
    records.frombytes(data[:beat * BEAT_BYTES + slot * 4])
    return sum((r >> 16) + 1 for r in records)
