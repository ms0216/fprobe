"""ロジックアナライザの捕捉データ: 値の表現（Capture・Transitions・Waveform）と、深いバッファの拍の復号。

fprobe.la が再公開する（使う側は fprobe.la から読んでよい）。デバイスにも VCD にも依らない純粋なデータの層。
"""

from array import array
from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass

__all__ = ["BEAT_BYTES", "Capture", "Transitions", "Waveform", "decode_beats", "decode_transitions", "raw_facts", "skew_unknown",
           "stream_transitions", "trigger_sample"]

try:                                                          # 入っていれば波形の変化点の表を一括の計算で作る
    import numpy as _np
except ImportError:                                           # 無くても同じ結果（遅いだけ）
    _np = None


def _array_of(code: str, values) -> array:
    """numpy の配列を array に（写しは 1 回。tobytes の中間のバイト列を作らない）。"""
    out = array(code)
    out.frombytes(memoryview(_np.ascontiguousarray(values)).cast("B"))
    return out


def _runs(samples: Sequence[int], first: int = 0) -> tuple["array[int]", "array[int]"]:
    """値が変わるところだけ: (値, 開始サンプル番号 + first)。先頭は必ず含む。"""
    values, starts = array("H"), array("Q")
    if _np is not None and len(samples):
        if isinstance(samples, array):
            a = _np.frombuffer(samples, dtype=_np.uint16)
        else:                                       # numpy の配列は型のまま比べる（1 ビットの展開の uint8 を写して広げない）
            a = samples if isinstance(samples, _np.ndarray) else _np.asarray(samples, dtype=_np.uint16)
        at = _np.concatenate(([0], _np.flatnonzero(a[1:] != a[:-1]) + 1)).astype(_np.int64, copy=False)
        values = _array_of("H", a[at].astype(_np.uint16, copy=False))
        at += first                                               # 位置は負にならない: 写さずに符号なしとして見る
        return values, _array_of("Q", at.view(_np.uint64))
    previous = None
    for i, v in enumerate(samples):
        if v != previous:
            values.append(v)
            starts.append(i + first)
            previous = v
    return values, starts


def skew_unknown(channel_skew: Sequence[float | None], channels: int) -> list[int]:
    """channel_skew のうち、ずれが分からない（None）ch の番号の昇順（channels 本まで）。"""
    return [ch for ch in range(min(channels, len(channel_skew))) if channel_skew[ch] is None]


@dataclass(frozen=True)
class Capture:
    samples: Sequence[int]       # 時間順（深いバッファでは array）
    trigger: int                 # トリガのサンプルの位置
    trigger_time: int            # トリガ時のタイムベース（カウント）
    sample_period_s: float
    channels: int
    # 取り込みのクロックが sys の整数倍の構成（ケイパビリティの la_sample_hz > sys_clock_hz）だけ: ピンが変わった取り込みの周期の番号
    # （タイムベースの刻み × 比 の単位。trigger_time はこれを比で割って切り捨てたもの）
    trigger_cap_cycles: int | None = None
    # sample_period_s の細かさで取った ch のビットマップ（ケイパビリティ LA_FINE。None = 全部の ch）。ほかの ch は
    # coarse_period_s（sys の周期）ごとに取った値が並ぶ（その間の変化の時刻は分からない）
    fine_channels: int | None = None
    coarse_period_s: float | None = None
    # ch ごとの時刻のずれ（タイムベースの刻み。fprobe.timing.la_channel_skew）: ch の変化の時刻 = サンプルの時刻 + これ。
    # None = 分からない（ずらさない）。() = 申告なし（どの ch もずらさない。ファイル・合成の波形）
    channel_skew: tuple[float | None, ...] = ()

    def channel(self, index: int) -> list[int]:
        return [s >> index & 1 for s in self.samples]

    def skew_unknown(self) -> list[int]:
        """ch ごとのずれが分からない ch（Transitions.skew_unknown と同じ）。"""
        return skew_unknown(self.channel_skew, self.channels)

    def channel_period_s(self, index: int) -> float:
        """ch の時刻の分解能（秒）: その ch の値を取った間隔。"""
        if self.fine_channels is None or self.fine_channels >> index & 1 or self.coarse_period_s is None:
            return self.sample_period_s
        return self.coarse_period_s


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
    channel_skew: tuple[float | None, ...] = ()   # Capture.channel_skew と同じ

    @classmethod
    def from_capture(cls, cap: "Capture") -> "Transitions":
        values, starts = _runs(cap.samples)
        return cls(values, starts, len(cap.samples), cap.trigger, cap.trigger_time, cap.sample_period_s,
                   cap.channels, cap.channel_skew)

    def skew_samples(self, ticks_per_sample: float | None) -> list[float]:
        """ch ごとのずれをサンプルの単位で（波形を置くとき。分からない ch・刻みの長さが無いときは 0 = ずらさずに置く。
        ずらさずに置いた分からない ch は skew_unknown で返す。置き場所の決まりで、推定ではない）。"""
        if not ticks_per_sample:
            return [0.0] * self.channels
        return [((self.channel_skew[ch] or 0.0) / ticks_per_sample if ch < len(self.channel_skew) else 0.0)
                for ch in range(self.channels)]

    def skew_unknown(self) -> list[int]:
        """ch ごとのずれが分からない ch（channel_skew の None）。() = 申告なしの捕捉は [] = 誰もずらさない決まり。"""
        return skew_unknown(self.channel_skew, self.channels)

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

    def __init__(self, tr: Transitions, ticks_per_sample: float | None = None):
        # 変化点の表だけを持つ（元の並び tr は持たない）。サンプル番号が 32 ビットに収まれば 4 バイトの整数で
        # （記録のかたまりは 1 秒程度。大きな記録の時間軸でデーモンのメモリが 1 GB を超えた）
        # ticks_per_sample があれば、ch ごとの時刻のずれ（tr.channel_skew）で ch を置き直す: 引数・戻り値のサンプルの
        # 位置は時間軸の位置（ずらした ch の変化点は小数になる）
        self.length = tr.length
        self._shift = tr.skew_samples(ticks_per_sample)
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

    def level_at(self, ch: int, s: float) -> int:
        """チャンネル ch のサンプル s の値（s 自身の変化を含む）。"""
        return self._initial[ch] ^ (bisect_right(self._edges[ch], s - self._shift[ch]) & 1)

    def edges_between(self, ch: int, s0: float, s1: float) -> array:
        """チャンネル ch の s0 < t < s1 の変化点（サンプル番号の昇順。共通タイムラインのタイル用）。ずらした ch は小数の位置。"""
        edges, shift = self._edges[ch], self._shift[ch]
        picked = edges[bisect_right(edges, s0 - shift):bisect_left(edges, s1 - shift)]
        return picked if not shift else array("d", (e + shift for e in picked))

    def count_between(self, ch: int, s0: float, s1: float) -> int:
        """edges_between の数（取り出さずに数える）。"""
        edges, shift = self._edges[ch], self._shift[ch]
        return max(0, bisect_left(edges, s1 - shift) - bisect_right(edges, s0 - shift))

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
        for initial, edges, shift in zip(self._initial, self._edges, self._shift):
            chars = []
            for s, e in bounds:
                before = bisect_right(edges, s - shift)                 # s までの変化の数（s 自身の変化を含む）
                if before < bisect_left(edges, e - shift):
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


def decode_transitions(data: bytes, rle: bool, first: int = 0) -> tuple["array[int]", "array[int]", int]:
    """拍の列を変化点（値, 開始サンプル + first）と全サンプル数にする（RLE は展開しない）。array で返す。"""
    if not rle and len(data) % BEAT_BYTES:
        raise ValueError("拍（16 バイト）の倍数でない")
    values, starts = stream_transitions(data, rle, 0, first, None)
    if _np is None:
        if not rle:
            return values, starts, len(data) // 2
        records = array("I")
        records.frombytes(data)
        return values, starts, sum(r >> 16 for r in records) + len(records)
    length = (int((_np.frombuffer(data, dtype="<u4") >> 16).sum(dtype=_np.uint64)) + len(data) // 4 if rle
              else len(data) // 2)
    return _array_of("H", values), _array_of("Q", starts), length


def stream_transitions(data: bytes, rle: bool, pack: int = 0, first: int = 0, previous: int | None = None):
    """流れの拍の列の変化点: (値, 通し番号の開始)。previous（直前のサンプルの値。None = 無い）と同じ先頭は含めない
    （つなぐと流れ全体の変化点になる）。RAW の詰めた語（pack 1〜4 = 8・4・2・1 ビットのサンプルが 1 語に 2〜16。
    時間の古いサンプルが下位）はサンプルに展開せずに数える。

    numpy があれば numpy の配列（値 uint16・開始 uint64。写さずに次の段へ渡す口）、無ければ array（"H"・"Q"）。"""
    if len(data) % (4 if rle else BEAT_BYTES):
        raise ValueError("RLE のレコード（4 バイト）・RAW の拍（16 バイト）の倍数でない")
    if _np is None:
        return _stream_transitions_py(data, rle, pack, first, previous)
    if not data:
        return _np.empty(0, _np.uint16), _np.empty(0, _np.uint64)
    if rle:
        return _rle_transitions_np(data, first, previous)
    if pack <= 1:                                             # 16・8 ビットのサンプル: 隣どうしを比べる
        return _whole_transitions_np(_np.frombuffer(data, dtype="<u2" if pack == 0 else _np.uint8), first, previous)
    return _packed_transitions_np(data, 16 >> pack, first, previous)


def _stream_transitions_py(data: bytes, rle: bool, pack: int, first: int, previous: int | None):
    """stream_transitions の numpy の無い道（変化点ごとのループ）。"""
    values, starts = array("H"), array("Q")
    if rle:
        records = array("I")
        records.frombytes(data)
        position = first
        for record in records:
            v = record & 0xFFFF
            if v != previous:
                values.append(v)
                starts.append(position)
                previous = v
            position += (record >> 16) + 1
        return values, starts
    words = array("H")
    words.frombytes(data)
    width, count = 16 >> pack, 1 << pack
    mask = (1 << width) - 1
    position = first
    for word in words:
        for j in range(count):
            v = word >> (width * j) & mask
            if v != previous:
                values.append(v)
                starts.append(position)
                previous = v
            position += 1
    return values, starts


def _first_differs(head, previous: int | None) -> bool:
    return previous is None or int(head) != previous


def _rle_transitions_np(data: bytes, first: int, previous: int | None):
    records = _np.frombuffer(data, dtype="<u4")
    value = records.astype(_np.uint16)                        # 下位 16 ビット = 値（astype は切り捨て）
    changed = _np.empty(len(value), dtype=bool)
    changed[0] = _first_differs(value[0], previous)
    _np.not_equal(value[1:], value[:-1], out=changed[1:])
    at = _np.flatnonzero(changed)
    # 各レコードの開始 = first + その前のレコードの繰り返しの和（prefix[k]）。uint64 どうしの cumsum が速い
    run = (records >> 16).astype(_np.uint64)
    run += _np.uint64(1)
    run[0] += _np.uint64(first)
    prefix = _np.empty(len(records) + 1, dtype=_np.uint64)
    prefix[0] = first
    _np.cumsum(run, out=prefix[1:])
    if len(at) == len(records):                              # 全部が変化（方形波など）: 拾わずにそのまま
        return value, prefix[:-1]
    return value[at], prefix[at]


def _whole_transitions_np(samples, first: int, previous: int | None):
    """1 サンプルが 1 つの整数（16・8 ビット）の列の変化点。"""
    changed = _np.empty(len(samples), dtype=bool)
    changed[0] = _first_differs(samples[0], previous)
    _np.not_equal(samples[1:], samples[:-1], out=changed[1:])
    at = _np.flatnonzero(changed)
    return samples[at].astype(_np.uint16, copy=False), _offset(at, first)


def _offset(positions, first: int):
    """非負の位置（int64。新しい配列）を、そのまま uint64 として first を足す（写さない）。"""
    starts = positions.view(_np.uint64)
    if first:
        starts += _np.uint64(first)
    return starts


_FIELD_LOW = {4: 0x1111111111111111, 2: 0x5555555555555555, 1: 0xFFFFFFFFFFFFFFFF}


def _packed_transitions_np(data: bytes, width: int, first: int, previous: int | None):
    """詰めた語（1 サンプル width ビット）の変化点を、64 ビットの語のまま一括で: 各サンプルの欄と 1 つ前の欄の XOR を
    欄の最下位ビットに畳み、立ったビットの位置 = 変化したサンプルの番号 × width。"""
    words = _np.frombuffer(data, dtype="<u8")
    mask = (1 << width) - 1
    up = _np.uint64(64 - width)
    before = _np.empty_like(words)                            # 各欄の 1 つ前のサンプルの欄（先頭の欄は前の語の最後）
    _np.left_shift(words, _np.uint64(width), out=before)
    before[1:] |= words[:-1] >> up
    head = int(words[0]) & mask
    before[0] |= (head ^ 1) if previous is None else (previous & mask)   # 前の値が無い: 先頭を必ず変化に
    diff = _np.bitwise_xor(words, before, out=before)
    shift = 1
    while shift < width:                                      # 欄の中のどこかが違う → 欄の最下位ビット
        diff |= diff >> _np.uint64(shift)
        shift <<= 1
    diff &= _np.uint64(_FIELD_LOW[width])
    bits = _set_bits(diff)                             # 立ったビットの通しの位置（昇順）
    octets = words.view(_np.uint8)                            # 欄はバイトをまたがない（width は 4・2・1）
    values = ((octets[bits >> 3] >> (bits & 7).astype(_np.uint8)) & _np.uint8(mask)).astype(_np.uint16)
    bits >>= width.bit_length() - 1                           # ビットの位置 → サンプルの番号（width は 2 の累乗）
    return values, _offset(bits, first)


def _popcount(x):
    if hasattr(_np, "bitwise_count"):                         # numpy 2
        return _np.bitwise_count(x)
    x = x - ((x >> _np.uint64(1)) & _np.uint64(0x5555555555555555))
    x = (x & _np.uint64(0x3333333333333333)) + ((x >> _np.uint64(2)) & _np.uint64(0x3333333333333333))
    x = (x + (x >> _np.uint64(4))) & _np.uint64(0x0F0F0F0F0F0F0F0F)
    return ((x * _np.uint64(0x0101010101010101)) >> _np.uint64(56)).astype(_np.uint8)


def _set_bits(words):
    """64 ビットの語の列の、立ったビットの通しの位置（語の番号 × 64 + ビット。昇順、int64）。拾い方は立ったビットの
    多さで選ぶ（速さの選び方で、結果は同じ）。"""
    nonzero = _np.flatnonzero(words)
    picked = words[nonzero]
    counts = _popcount(picked).astype(_np.int64)
    total = int(counts.sum())
    if total >= len(words):                                   # 平均で 1 語に 1 つ以上: 全部を広げて拾う
        return _np.flatnonzero(_np.unpackbits(words.view(_np.uint8), bitorder="little"))
    if total > 2 * len(nonzero):                              # 立った語に多い: その語だけ広げて拾う（語あたり 64 バイト）
        flat = _np.flatnonzero(_np.unpackbits(picked.view(_np.uint8), bitorder="little"))
        return nonzero[flat >> 6] * 64 + (flat & 63)
    # まばら: 最下位のビットを 1 つずつ外す（回数 = 1 語の最大のビット数。各回は残っている語だけ）
    out = _np.empty(total, dtype=_np.int64)
    slot = _np.cumsum(counts) - counts                        # その語の最初のビットの置き場所
    base = nonzero * 64
    while len(picked):
        low = picked & (~picked + _np.uint64(1))
        out[slot] = base + _popcount(low - _np.uint64(1))
        picked ^= low
        left = picked != 0
        picked, slot, base = picked[left], slot[left] + 1, base[left]
    return out


def trigger_sample(data: bytes, rle: bool, beat: int, slot: int) -> int:
    """拍 beat の slot 番目（トリガ）が、data の先頭から何サンプル目か。"""
    if not rle:
        return beat * 8 + slot
    records = array("I")
    records.frombytes(data[:beat * BEAT_BYTES + slot * 4])
    return sum((r >> 16) + 1 for r in records)


def _extent(data, rle: bool) -> tuple[int, int]:
    """拍の列のサンプル数と最後の値（展開しない）。RAW は長さと最後の 2 バイトだけ、RLE は繰り返しの和を numpy で 1 回。"""
    n = len(data)
    if not rle:
        return n >> 1, data[n - 2] | data[n - 1] << 8
    if _np is not None:
        words = _np.frombuffer(data, dtype="<u2")
        return int(words[1::2].sum(dtype=_np.int64)) + (n >> 2), int(words[-2])
    words = array("H")
    words.frombytes(data)
    return sum(words[1::2]) + len(words) // 2, words[-2]          # レコード = 値（下位）, 繰り返し − 1（上位）


def _samples_of(data, rle: bool, pack: int) -> tuple[int, int]:
    """拍の列のサンプル数と最後のサンプルの値（詰めた語は、最後の語の最後 = 上位のサンプル）。"""
    samples, last = _extent(data, rle)
    if pack:
        width = 16 >> pack
        samples <<= pack
        last = (last >> (width * ((1 << pack) - 1))) & ((1 << width) - 1)
    return samples, last


def raw_facts(pieces, rle: bool, pack: int) -> tuple[int, int, int]:
    """拍の列（bytes・memoryview の並び。サンプルの順につながる）の事実: (最初のサンプルの値, 最後のサンプルの値, 最初の値と
    違う値が出たビット)。変化点を作らずに求める（記録の縮図の first / last / toggles。変化点の値の列から求めたものと同じ）。
    あるビットが立つ = そのビットが最初の値と違うサンプルがある。かたまりごとに (最初, 最後, そのかたまりの最初の値との違い) を
    求めて、かたまりの最初の値と全体の最初の値の違いと合わせる（ビットごとに: かたまりの中で違う、または、かたまりの最初が違う）。"""
    first = last = toggles = None
    parts = []
    for piece in pieces:
        if len(piece):
            parts.append(_piece_facts(piece, rle, pack))
    if not parts:
        raise ValueError("拍が無い")
    first = parts[0][0]
    toggles = 0
    for f, _l, t in parts:
        toggles |= t | (f ^ first)
    return first, parts[-1][1], toggles


def _piece_facts(data, rle: bool, pack: int) -> tuple[int, int, int]:
    """1 つのかたまりの (最初の値, 最後の値, そのかたまりの最初の値と違うビット)。"""
    if len(data) % (4 if rle else BEAT_BYTES):
        raise ValueError("RLE のレコード（4 バイト）・RAW の拍（16 バイト）の倍数でない")
    width, count = (16, 1) if (rle or pack == 0) else (16 >> pack, 1 << pack)
    mask = (1 << width) - 1
    if _np is not None:
        words = _np.frombuffer(data, dtype="<u2")
        if rle:
            words = words[0::2]                                   # レコード = 値（下位 16 ビット）, 繰り返し − 1
        head, tail = int(words[0]), int(words[-1])
        first = head & mask
        replicated = _np.uint16(first * (0xFFFF // mask))
        x = int(_np.bitwise_or.reduce(words ^ replicated))
    else:
        words = array("H")
        words.frombytes(bytes(data))
        if rle:
            words = words[0::2]
        head, tail = words[0], words[-1]
        first = head & mask
        replicated = first * (0xFFFF // mask)
        x = 0
        for w in set(words):
            x |= w ^ replicated
    toggles = 0
    for j in range(count):
        toggles |= (x >> (width * j)) & mask
    return first, (tail >> (width * (count - 1))) & mask, toggles
