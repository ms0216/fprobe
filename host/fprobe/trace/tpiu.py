"""TPIU のフォーマッタの枠を外す（CoreSight Architecture Specification のフォーマッタ・TPIU の章）。

フォーマッタは複数のトレース源（ETM・ITM…）を 16 バイトのフレームに多重化する:
- 偶数のバイト（0, 2, …, 14）: ビット 0 = 1 なら ID の変更（新しい ID = ビット 7:1）。最後のバイト（15）の対応する
  補助ビットが 1 なら、続く奇数のバイトはまだ前の ID、0 なら新しい ID のもの。ビット 0 = 0 ならデータで、
  データのビット 0 は補助ビット。
- 奇数のバイト: 常にデータ（そのときの ID）。
- ID 0 は空き（捨てる）、0x7F は予約。
- FSYNC = 0x7FFF_FFFF（バイト列で FF FF FF 7F）はフレームの境目に、HSYNC = 0x7FFF（FF 7F）は継続モードで端子が
  空いているとき 16 ビットの境目に入る（どちらも偶数のバイトが ID 0x7F になり、正しいフレームには現れない）。

    out = deformat(data)                 # FSYNC を探してフレームを揃える
    out.streams[0x10]                    # ID 0x10 のバイト列
"""

from dataclasses import dataclass, field

__all__ = ["ALIGNS", "Deformatted", "Deformatter", "deformat"]

FSYNC = b"\xff\xff\xff\x7f"
HSYNC = b"\xff\x7f"
ALIGNS = ("fsync", "start", "auto")


@dataclass
class Deformatted:
    streams: dict[int, bytes]                        # ID → バイト列（ID 0 は含まない）
    chunks: list[tuple[int, bytes]]                  # 届いた順の (ID, 続いたバイト列)
    frames: int                                      # 解いたフレームの数
    aligned: str | None                              # "fsync" / "start" / None（揃わなかった）
    skipped: int                                     # 最初の FSYNC の前に捨てたバイト数
    leftover: int                                    # 最後の切れたフレームのバイト数（解かずに残した）
    sync_offsets: list[int] = field(default_factory=list)   # FSYNC を見つけた位置（入力の先頭からのバイト）

    def as_dict(self) -> dict:
        return {"frames": self.frames, "aligned": self.aligned, "skipped": self.skipped, "leftover": self.leftover,
                "streams": {f"0x{i:02X}": len(b) for i, b in sorted(self.streams.items())}}


class Deformatter:
    """少しずつ渡せる（feed）。align: "fsync" = 最初の FSYNC からフレームを揃える（無ければ何も出さない）、
    "start" = 先頭がフレームの始まり、"auto" = FSYNC が見つかればそこから、無ければ先頭から（result の時点で決める）。"""

    def __init__(self, align: str = "fsync"):
        if align not in ALIGNS:
            raise ValueError(f"align は {', '.join(ALIGNS)} のどれか: {align!r}")
        self.align = align
        self._raw = bytearray()                       # 揃うまでの入力（auto で先頭から読み直すため）
        self._pos = 0                                 # 入力の先頭からの、次に見るバイトの位置
        self._buf = bytearray()                       # 揃った後の未処理の入力
        self._frame = bytearray()
        self._synced = align == "start"
        self._aligned: str | None = "start" if self._synced else None
        self._skipped = 0
        self._frames = 0
        self._current = 0                             # いまの ID（最初は空き）
        self._chunks: list[tuple[int, bytearray]] = []
        self._sync_offsets: list[int] = []

    # --- 入力 ---------------------------------------------------------------------------

    def feed(self, data: bytes) -> None:
        if not self._synced:
            searched = max(0, len(self._raw) - (len(FSYNC) - 1))      # 前に探した所（境目をまたぐ FSYNC の分は戻る）
            self._raw += data
            at = self._raw.find(FSYNC, searched)
            if at < 0:
                return
            self._synced, self._aligned, self._skipped = True, "fsync", at
            self._sync_offsets.append(at)
            self._pos = at + len(FSYNC)
            data = bytes(self._raw[self._pos:])
            self._raw.clear()
        self._buf += data
        self._consume()

    def _consume(self) -> None:
        buf, i = self._buf, 0
        while True:
            if not self._frame:                                     # フレームの境目: FSYNC を飛ばす
                if len(buf) - i < 4 and buf[i:] == FSYNC[:len(buf) - i]:
                    break                                           # FSYNC かもしれない: 続きを待つ
                if buf[i:i + 4] == FSYNC:
                    self._sync_offsets.append(self._pos + i)
                    i += 4
                    continue
            if len(buf) - i < 2:
                break
            pair = buf[i:i + 2]
            i += 2
            if pair == HSYNC:
                continue
            self._frame += pair
            if len(self._frame) == 16:
                self._unpack(bytes(self._frame))
                self._frame.clear()
        del buf[:i]
        self._pos += i

    # --- フレーム -------------------------------------------------------------------------

    def _emit(self, ident: int, byte: int) -> None:
        if ident == 0:
            return
        if self._chunks and self._chunks[-1][0] == ident:
            self._chunks[-1][1].append(byte)
        else:
            self._chunks.append((ident, bytearray([byte])))

    def _unpack(self, frame: bytes) -> None:
        self._frames += 1
        aux = frame[15]
        for i in range(0, 15, 2):
            byte = frame[i]
            flag = aux >> (i // 2) & 1
            if byte & 1:                                            # ID の変更
                new = byte >> 1
                if i == 14:
                    self._current = new
                elif flag:                                          # 次の 1 バイトは前の ID
                    self._emit(self._current, frame[i + 1])
                    self._current = new
                else:
                    self._current = new
                    self._emit(self._current, frame[i + 1])
            else:
                self._emit(self._current, byte | flag)
                if i != 14:
                    self._emit(self._current, frame[i + 1])

    # --- 結果 ---------------------------------------------------------------------------

    def result(self) -> Deformatted:
        if not self._synced and self.align == "auto" and self._raw:
            raw = bytes(self._raw)
            restart = Deformatter("start")
            restart.feed(raw)
            return restart.result()
        skipped = self._skipped if self._synced else len(self._raw)
        streams: dict[int, bytearray] = {}
        for ident, data in self._chunks:
            streams.setdefault(ident, bytearray()).extend(data)
        leftover = len(self._frame) + len(self._buf)
        return Deformatted({i: bytes(b) for i, b in streams.items()}, [(i, bytes(b)) for i, b in self._chunks],
                           self._frames, self._aligned, skipped, leftover, list(self._sync_offsets))


def deformat(data: bytes, align: str = "fsync") -> Deformatted:
    d = Deformatter(align)
    d.feed(bytes(data))
    return d.result()
