"""見出し付きのストリーム（R1: ロジアナとイベントを 1 本に流す構成）のかたまりの分割。

仕様: docs/superpowers/specs/2026-10-19-r1-unified-stream-design.md §3
純粋な符号の層（デバイスにも時間にも依らない）。fprobe.la が再公開する。
"""

import struct
from dataclasses import dataclass

from . import generated as g
from .la_wave import BEAT_BYTES


class StreamFormatError(RuntimeError):
    """ストリームの見出しが壊れている・通し番号が飛んだ（同期を失った。それまでの分は正しい）。"""


class StreamLinkLoss(StreamFormatError):
    """流れを運ぶリンクで欠けた（または欠けた可能性がある）: 欠けた後のサンプルの位置が決まらないので、そこで止める
    （黙って位置をずらさない。それまでに返した分は正しい）。lost = 欠けの事実（数えたもの）の辞書。"""

    def __init__(self, message: str, lost: dict):
        super().__init__(message)
        self.lost = lost


class StreamHoldExceeded(RuntimeError):
    """欠けの区間を記録する流れ（gaps）で、位置の決まらない拍（届いていない欠けの記録の後ろ）が、使う側が明示した上限
    （gap_hold_bytes）を超えた。推定で置かずに止める（それまでに返した分の位置は正しい）。属性は数えた事実。"""

    def __init__(self, held_bytes: int, hold_bytes: int, missing_records: int, first_unplaced_beat: int):
        self.held_bytes, self.hold_bytes = held_bytes, hold_bytes
        self.missing_records, self.first_unplaced_beat = missing_records, first_unplaced_beat
        super().__init__(f"欠けの記録 {missing_records} 件が届かず、位置の決まらない拍が {held_bytes} バイト（上限 gap_hold_bytes = "
                         f"{hold_bytes}）。拍 {first_unplaced_beat} から後ろの位置が決まらないので止めた")


@dataclass(frozen=True)
class StreamSegment:
    """見出し付きのストリームの 1 かたまり。"""
    kind: int                    # g.STREAM_SEG_LA（ロジアナの拍）/ g.STREAM_SEG_EVENTS（イベント 1 件 = 1 拍）
    seq: int                     # ロジアナ: 最初の拍の、ストリームの先頭からの番号（32 ビットで折り返す）。
                                 # イベント: 最初のイベントのリングの位置（events.written で数えた番号 mod capacity）
    data: bytes                  # 中身（拍の整数倍。bytes か、受け取ったバッファを写さずに指す memoryview）


_SEG_HEAD = struct.Struct("<HHIQ")               # 見出しの拍: 種類(16) | 拍の数(16) | 番号(32) | 印(64)


class SegmentSplitter:
    """見出し付きのストリームをかたまりに分ける。届く量はかたまり・拍の境に揃っていなくてよい（続きは次の feed で）。

    かたまりの中身は、届いたバッファ（bytes・memoryview）を写さずに指す memoryview（届いた側のバッファは変えないこと）。
    写すのは、届いた量の境で切れたかたまり（次の feed で続きをつなぐ）だけ。"""

    def __init__(self):
        self._carry = bytearray()        # 切れたかたまりの頭から（見出しを含む）

    @property
    def pending(self) -> int:
        """まだかたまりにならないバイト数。"""
        return len(self._carry)

    @staticmethod
    def _head(buf, pos: int) -> int:
        """pos の見出しを確かめ、かたまり全体のバイト数を返す。"""
        kind, count, _, magic = _SEG_HEAD.unpack_from(buf, pos)
        if (magic != g.STREAM_SEG_MAGIC or kind not in (g.STREAM_SEG_LA, g.STREAM_SEG_EVENTS)
                or not 1 <= count <= g.STREAM_SEG_MAX_BEATS):
            head = int.from_bytes(bytes(buf[pos:pos + BEAT_BYTES]), "little")
            raise StreamFormatError(f"ストリームの見出しが壊れている: {head:032x}")
        return BEAT_BYTES * (1 + count)

    @staticmethod
    def _segment(buf, pos: int, end: int) -> StreamSegment:
        kind, _, seq, _ = _SEG_HEAD.unpack_from(buf, pos)
        return StreamSegment(kind, seq, buf[pos + BEAT_BYTES:end])

    def feed(self, data) -> list[StreamSegment]:
        view = memoryview(data).cast("B") if not isinstance(data, memoryview) or data.format != "B" else data
        out: list[StreamSegment] = []
        pos, size = 0, len(view)
        carry = self._carry
        if carry:                                       # 前の feed で切れたかたまりを、要る分だけつないで仕上げる
            if len(carry) < BEAT_BYTES:
                take = min(BEAT_BYTES - len(carry), size)
                carry += view[:take]
                pos = take
                if len(carry) < BEAT_BYTES:
                    return out
            need = self._head(carry, 0) - len(carry)
            take = min(need, size - pos)
            carry += view[pos:pos + take]
            pos += take
            if take < need:
                return out
            whole = bytes(carry)
            carry.clear()
            out.append(self._segment(memoryview(whole), 0, len(whole)))
        head = _SEG_HEAD.unpack_from
        magic, la, ev, top = g.STREAM_SEG_MAGIC, g.STREAM_SEG_LA, g.STREAM_SEG_EVENTS, g.STREAM_SEG_MAX_BEATS
        while size - pos >= BEAT_BYTES:
            kind, count, seq, mark = head(view, pos)
            if mark != magic or (kind != la and kind != ev) or not 1 <= count <= top:
                self._head(view, pos)                   # 誤りの文を作る
            end = pos + BEAT_BYTES * (1 + count)
            if end > size:
                break
            out.append(StreamSegment(kind, seq, view[pos + BEAT_BYTES:end]))
            pos = end
        if pos < size:
            carry += view[pos:]
        return out


def split_segments(data: bytes) -> list[StreamSegment]:
    """見出し付きのストリーム全体をかたまりに分ける（途中で切れていれば誤り）。中身は bytes。"""
    splitter = SegmentSplitter()
    out = [StreamSegment(seg.kind, seg.seq, bytes(seg.data)) for seg in splitter.feed(data)]
    if splitter.pending:
        raise StreamFormatError(f"かたまりの途中で終わっている（{splitter.pending} バイト）")
    return out
