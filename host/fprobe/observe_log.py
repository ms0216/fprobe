"""受け取ったバイトの貯め場（ByteLog）と、UTF-8 の切れ目の判定。純粋なデータ構造（機器にも API にも依らない）。

fprobe.probe_observe が再公開する（端末・SWO 受信・ObserveService が使う）。
"""

import bisect

LOG_LIMIT = 1 << 20                    # 1 つの貯め場に残すバイト数（超えたら古いものから捨てる）
MARKS_LIMIT = 100_000                  # 1 つの貯め場に残す受け取りの時刻の数


class ByteLog:
    """受け取ったバイトの貯め場。位置は受け取りの最初からの通し番号。既定の読み手の位置（cursor。読むと進む）と、
    明示の位置（since。画面が自分で持つ。cursor は動かさない）の 2 通りで読める。受け取りごとの時刻（marks）も持つ
    （端末の行の時刻・時間軸へ飛ぶ。G04）。"""

    def __init__(self, limit: int = LOG_LIMIT):
        self.limit = limit
        self._data = bytearray()
        self.start = 0                 # _data[0] の通し番号
        self.cursor = 0
        # 受け取りごとの (位置, 壁時計の秒, タイムベースの時刻 | None)。位置の昇順。読めなくなった分は捨てる（読める
        # 最初のバイトを含む受け取りの印は 1 つ残す）
        self.marks: list[tuple[int, float, int | None]] = []

    @property
    def end(self) -> int:
        return self.start + len(self._data)

    def append(self, data: bytes, wall: float | None = None, t: int | None = None) -> None:
        """data を足す。wall（受けた壁時計の秒）を渡すと、その受け取りの時刻の印も残す（t は記録中のタイムベース）。"""
        if wall is not None and data:
            self.marks.append((self.end, wall, t))
        self._data += data
        excess = len(self._data) - self.limit
        if excess > 0:
            del self._data[:excess]
            self.start += excess
            first = bisect.bisect_right(self.marks, self.start, key=lambda m: m[0]) - 1
            if first > 0:
                del self.marks[:first]
        if len(self.marks) > MARKS_LIMIT:
            del self.marks[:len(self.marks) - MARKS_LIMIT]

    def times(self, start: int, length: int) -> list[tuple[int, float, int | None]]:
        """位置 [start, start + length) のバイトの受け取りの時刻: start を含む受け取りの印（位置は start に詰める）と、
        その後に始まる受け取りの印。"""
        i = bisect.bisect_right(self.marks, start, key=lambda m: m[0])     # 後に始まる最初の印
        out = [(start, *self.marks[i - 1][1:])] if i > 0 else []
        for pos, wall, t in self.marks[i:]:
            if pos >= start + length:
                break
            out.append((pos, wall, t))
        return out

    def read(self, since: int | None = None, max_bytes: int = 65536) -> tuple[bytes, int, int]:
        """(データ, 読み始めた位置, 捨てられて読めなかったバイト数)。"""
        pos = self.cursor if since is None else since
        lost = max(0, self.start - pos)
        pos = min(max(pos, self.start), self.end)
        data = bytes(self._data[pos - self.start:pos - self.start + max_bytes])
        if since is None:
            self.cursor = pos + len(data)
        return data, pos, lost

    def unread(self, count: int) -> None:
        """既定の読み手の位置を count 戻す（途中で切れた UTF-8 の文字を次の読みに回す）。"""
        self.cursor -= count


def _incomplete_utf8_tail(data: bytes) -> int:
    """末尾の、まだ続きが来ていない UTF-8 の文字のバイト数（0〜3）。"""
    for back in range(1, min(4, len(data)) + 1):
        b = data[-back]
        if b & 0xC0 != 0x80:                                 # 先頭のバイト（または ASCII）
            need = 2 if b & 0xE0 == 0xC0 else 3 if b & 0xF0 == 0xE0 else 4 if b & 0xF8 == 0xF0 else 1
            return back if need > back else 0
    return 0
