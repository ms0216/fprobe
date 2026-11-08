"""ロジアナの連続ストリームの位置決め: サンプルの位置の数え（StreamCursor）・欠けの区間（StreamGap）・
欠けの記録と拍の突き合わせ（GapPlacer）・詰めた語のサンプルへの展開。

デバイスにも時間にも触れない純粋な符号の層（LaStream が使う）。fprobe.la_stream・fprobe.la が再公開する。
仕様: docs/superpowers/specs/2026-11-01-la-deep-fast-design.md §12
"""

from array import array
from collections import deque
from dataclasses import dataclass

from .la_segments import StreamFormatError, StreamHoldExceeded
from .la_tokens import token_extent
from .la_wave import BEAT_BYTES, _array_of, _np, _samples_of

class StreamCursor:
    """流れのサンプルの位置（先頭 = 0。欠けの区間も数える）と直前の値。LaStream のサンプルの数えはここ 1 か所
    （欠けの区間を記録する流れでは GapPlacer がこれを進める）。"""

    __slots__ = ("rle", "pack", "rle_format", "position", "previous", "gap_samples", "bytes", "pad_bytes")

    def __init__(self, rle: bool, pack: int, rle_format: str = "records"):
        self.rle, self.pack, self.rle_format = rle, pack, rle_format
        self.bytes = 0                        # 受け取ったロジアナの拍のバイト数（見出し・イベントを含まない）
        self.pad_bytes = 0                    # そのうち NOP で埋めた分（トークン形式）
        # トークン形式では position = 直前の出来事の時刻（次のチャンクの first）。サンプルの位置ではなく、出来事の時刻で進む
        self.position = 0                     # 次のサンプルの位置
        self.previous: int | None = None      # 直前のサンプルの値（欠けの後は None）
        self.gap_samples = 0                  # 欠けの区間のサンプルの数の和

    @property
    def samples(self) -> int:
        """値のあるサンプルの数。トークン形式は、最後の出来事の時刻 + 1（出来事が来てから。それ以降は届くまで分からない）。"""
        started = 1 if self.rle_format == "tokens" and self.previous is not None else 0
        return self.position - self.gap_samples + started

    def take(self, data) -> tuple[int, int | None, int]:
        """拍の列 data を進める。(最初の位置, 直前の値, サンプル数)。"""
        first, previous = self.position, self.previous
        self.bytes += len(data)
        if self.rle_format == "tokens":
            count, self.previous, pad = token_extent(data, first, previous)
            self.pad_bytes += pad
        else:
            count, self.previous = _samples_of(data, self.rle, self.pack)
        self.position += count
        return first, previous, count

    def skip(self, samples: int) -> int:
        """値の無い区間（欠け）を進める。最初の位置を返す。"""
        first = self.position
        self.position += samples
        self.gap_samples += samples
        self.previous = None
        return first


@dataclass(frozen=True)
class StreamGap:
    """垂れ流しの欠けの区間: 回路が拍を捨てた区間（CSR la_deep.control.gaps の流れ。docs/superpowers/specs/2026-11-01-la-deep-fast-design.md §12）。
    流れのサンプルの位置 start〜start + samples − 1 は値が無い（埋めない・補わない）。次に値のあるサンプルは start + samples。
    位置の時刻は値のあるサンプルと同じ式（1 周期に複数のサンプルの構成は LaStream.sample_time、ほかは起点 + 位置 × サンプルの間隔）。"""
    start: int                   # 流れのサンプルの位置（先頭 = 0。前の欠けも数える）
    samples: int                 # 値の無いサンプルの数（回路が数えた事実）
    beat: int                    # 欠けの前に届いたロジアナの拍の数（流れの開始から）
    drop_time48: int             # 回路が拍を捨て始めた周期のタイムベースの下位 48 ビット（サンプルの時刻ではない。回路の記録の写し）

    def as_dict(self) -> dict:
        return {"start": self.start, "samples": self.samples, "beat": self.beat}


class GapPlacer:
    """欠けの記録（種類 19 のイベント）と届いたロジアナの拍から、欠けの区間の位置を決める（ホスト側。docs/superpowers/specs/2026-11-01-la-deep-fast-design.md §12）。

    回路は欠けの記録を、その後の拍より先にイベントのリングへ書くが、流れの上ではイベントとロジアナの拍が別のかたまりで届き、
    どちらが先に届くかは決まらない。そこで「拍を受け取った後に読んだ CSR の gaps（記録の件数 N）」を使う: 受け取った拍 R 個に
    関わる記録（拍の位置 < R）は、その拍が書かれる前に出ているので N 件の中にある。届いた記録が N 件そろうまで、関わりうる拍を
    出さずに持つ（推定しない）。届いていない記録が 1 件だけなら、その位置は gap_last_beat（gaps より先に読む）なので、その前の拍は出せる。
    2 件以上なら、届いた最後の記録の位置より前だけ出せる（届いていない記録の位置はそれ以上）。
    finish で残った拍（届かなかった記録の後ろで位置が決まらない拍）は出さず、unplaced_beats に数える（黙って位置をずらさない）。

    持つ量の上限 hold_bytes（使う側が明示する。既定は持たない）: 位置の決まらない拍がそれを超えたら StreamHoldExceeded で止める。
    超えた拍を「位置の決まらない区間」として返して続ける形は取らない: 届いていない記録のサンプルの数が分からないので、その後ろの
    すべての拍の位置が決まらない（区間は流れの終わりまで伸び、返せるものが無い）。止めれば、それまでに返した分は正しく、
    何が欠けたか（届いていない記録の件数・最初の位置の決まらない拍）を事実として返せる。

    拍は届いたバッファを写さずに持つ（deque）。出すときは、1 つの塊に収まれば memoryview、またがれば 1 回つないで渡す。"""

    def __init__(self, rle: bool, pack: int, *, hold_bytes: int, cursor: StreamCursor | None = None):
        if hold_bytes <= 0:
            raise ValueError(f"hold_bytes（位置の決まらない拍を持つ上限）が不正: {hold_bytes}")
        self.cursor = cursor if cursor is not None else StreamCursor(rle, pack)
        self.rle, self.pack = rle, pack
        self.hold_bytes = hold_bytes
        self._held: deque = deque()       # まだ出していない拍（_released 拍目から。届いた塊のまま）
        self._held_bytes = 0
        self._received = 0                # 受け取った拍の数
        self._released = 0                # 位置を決めて出した拍の数
        self._pending: deque[tuple[int, int, int]] = deque()   # 届いたがまだ置いていない記録（拍の位置, サンプルの数, 時刻）古い順
        self._records = 0                 # 届いた記録の件数（分けた件も 1 件ずつ）
        self._last_beat = 0               # 最後に届いた記録の拍の位置
        self.gaps: list[StreamGap] = []
        self.unplaced_beats = 0
        self.peak_held_bytes = 0          # 持った量の最大（事実）

    @property
    def position(self) -> int:
        """次に出すサンプルの位置（cursor）。"""
        return self.cursor.position

    @property
    def previous(self) -> int | None:
        return self.cursor.previous

    @property
    def received_beats(self) -> int:
        return self._received

    @property
    def held_bytes(self) -> int:
        return self._held_bytes

    def _unwrap(self, beat32: int) -> int:
        """32 ビットで折り返す拍の位置を、出した拍の数より後の値に伸ばす（置いていない記録の位置は、それ以上）。"""
        return self._released + ((beat32 - self._released) % (1 << 32))

    def add_beats(self, data) -> None:
        n = len(data)
        if n % BEAT_BYTES:
            raise StreamFormatError("ロジアナの拍の途中で切れた")
        if n:
            self._held.append(data)
            self._held_bytes += n
            self._received += n // BEAT_BYTES
            if self._held_bytes > self.peak_held_bytes:
                self.peak_held_bytes = self._held_bytes

    def add_record(self, beat32: int, samples: int, time48: int) -> None:
        beat = self._unwrap(beat32)
        if self._records and beat < self._last_beat:
            raise StreamFormatError(f"欠けの記録の位置が戻った: {beat}（前は {self._last_beat}）")
        self._pending.append((beat, samples, time48))
        self._records += 1
        self._last_beat = beat

    def release(self, received_at_read: int, reported: int, last_beat32: int) -> list[tuple]:
        """received_at_read = CSR を読む前に受け取っていた拍の数、reported・last_beat32 = その後に読んだ la_deep.gaps・gap_last_beat
        （gap_last_beat → gaps の順に読んだ値）。位置の決まった分を、古い順の ("data", 拍のバイト列, 最初の位置, 直前の値) と
        ("gap", StreamGap) の並びで返す。出した後も持つ量が hold_bytes を超えていれば StreamHoldExceeded。"""
        out = self._release(received_at_read, reported, last_beat32)
        if self._held_bytes > self.hold_bytes:
            raise StreamHoldExceeded(self._held_bytes, self.hold_bytes, max(0, reported - self._records), self._released)
        return out

    def _release(self, received_at_read: int, reported: int, last_beat32: int) -> list[tuple]:
        missing = reported - self._records
        if missing <= 0:
            limit = received_at_read
        elif missing == 1:
            limit = min(received_at_read, self._unwrap(last_beat32))
        else:
            limit = min(received_at_read, self._last_beat if self._records else self._released)
        return self._emit(max(self._released, limit))

    def finish(self, reported: int, last_beat32: int) -> list[tuple]:
        """流れが終わった（出し切った）後: 最後の CSR の読みで出せる分を返し、残りを unplaced_beats に数える。"""
        out = self._release(self._received, reported, last_beat32)
        self.unplaced_beats = self._received - self._released
        self._held.clear()
        self._held_bytes = 0
        return out

    def _take(self, n: int):
        """持っている拍の先頭 n バイト（1 つの塊に収まれば写さない）。"""
        held = self._held
        first = held[0]
        if len(first) == n:
            held.popleft()
            out = first
        elif len(first) > n:
            view = first if isinstance(first, memoryview) else memoryview(first)
            out = view[:n]
            held[0] = view[n:]
        else:
            parts, left = [], n
            while left:
                piece = held[0]
                if len(piece) <= left:
                    parts.append(held.popleft())
                    left -= len(piece)
                else:
                    view = piece if isinstance(piece, memoryview) else memoryview(piece)
                    parts.append(view[:left])
                    held[0] = view[left:]
                    left = 0
            out = b"".join(parts)
        self._held_bytes -= n
        return out

    def _emit(self, limit: int) -> list[tuple]:
        out: list[tuple] = []
        cursor, pending = self.cursor, self._pending
        while True:
            nxt = pending[0][0] if pending else None
            stop = limit if nxt is None or nxt > limit else nxt
            if stop > self._released:
                data = self._take((stop - self._released) * BEAT_BYTES)
                first, previous, count = cursor.take(data)
                out.append(("data", data, first, previous, count))
                self._released = stop
            if nxt is None or nxt > limit:
                return out
            beat, samples, time48 = pending.popleft()
            gap = StreamGap(cursor.skip(samples), samples, beat, time48)
            self.gaps.append(gap)
            out.append(("gap", gap))



def _expand_words(words: "array[int]", pack: int) -> "array[int]":
    """詰めた 16 ビットの語を、サンプルに展開する（1 語 = 1 << pack サンプル。時間の古いサンプルが下位）。pack = 0 はそのまま。"""
    if not pack:
        return words
    if _np is not None:
        return _array_of("H", _expand_np(_np.frombuffer(words, dtype=_np.uint16), pack).astype(_np.uint16, copy=False))
    width, count = 16 >> pack, 1 << pack
    mask = (1 << width) - 1
    out = array("H")
    for word in words:
        out.extend(array("H", ((word >> (width * j)) & mask for j in range(count))))
    return out


def _expand_np(words, pack: int):
    """_expand_words の numpy 版（numpy の配列を返す。1 ビットの語は unpackbits で uint8、ほかは uint16）。"""
    if pack == 4:                                       # 1 語 = 1 ビット × 16: リトルエンディアンの語の下位ビットが古い = バイトの下位から
        return _np.unpackbits(words.view(_np.uint8), bitorder="little")
    width, count = 16 >> pack, 1 << pack
    shifts = (_np.arange(count, dtype=_np.uint16) * width)[None, :]
    return ((words[:, None] >> shifts) & ((1 << width) - 1)).astype(_np.uint16).ravel()
