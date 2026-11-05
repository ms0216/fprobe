"""フレームのホストリンクの部品: チャンネル番号と定数・DAP の柵・受信側の数え（StreamBuffer・DapPacketStats）・
バイト列のチャンネルのリング（ByteRing・_ByteChannel）。

リンクにもスレッドにも触れない純粋なデータ構造（fprobe.frame_link の FrameLink が使う。fprobe.frame_link が再公開する）。
チャンネルの意味と振り分けの設計は fprobe.frame_link の先頭にある。
"""

from dataclasses import dataclass

CH_CONTROL, CH_DAP, CH_SWO, CH_UART0, CH_STREAM, CH_LINE_CODING = range(6)
_PACKET_CHANNELS = (CH_CONTROL, CH_DAP)
BYTE_CHANNELS = (CH_SWO, CH_UART0, CH_STREAM)

MAX_PAYLOAD = 512
UART_CHUNK = 32                                       # UART0 の送信の単位（バイト）
UART_AHEAD = 64                                       # ターゲットのボーレート換算で先行して送ってよい量

# DAP の柵: DAP_Info(0xFF = パケットの大きさ)。応答は 00 | 02 | u16。pyOCD は DAP_Info を開くときにしか送らない
_DAP_FENCE = bytes([0x00, 0xFF])


def _is_fence_reply(packet: bytes) -> bool:
    return len(packet) >= 4 and packet[0] == 0x00 and packet[1] == 0x02


class DapDesync(ConnectionError):
    """DAP の応答が時間切れになった後（遅れて届く応答と次の要求の応答を区別できない）。discard（pyOCD の開き直し）まで使えない。"""


@dataclass(frozen=True)
class StreamBuffer:
    """バイト列のチャンネルの受け取り済みで、まだ読まれていない量（PC のメモリに溜まっている量）と、その道の欠けの数。
    すべて測った事実（数は開いてから。LaStream は流れの始めとの差を返す）。"""
    capacity_bytes: int          # 溜める上限（開くときに使う側が渡した値）
    used_bytes: int              # 今の量
    peak_bytes: int              # 測り始め（開いた時・reset_peak）からの最大
    full_count: int              # 流量制御のある道: 上限に達して受け取りを止めた回数
    full_s: float                # 同: 止めていた時間の合計（秒。止めている最中の分を含む）
    backpressure: bool = False   # True = 上限で受け取りを止める道（TCP）。False = 上限を超えたフレームを捨てる道（UDP・UART）
    dropped_frames: int = 0      # 流量制御の無い道: 上限を超えて捨てたフレームの数（このチャンネルのもの）
    dropped_bytes: int = 0       # 同: 捨てたバイト数
    lost_frames: int = 0         # このチャンネルを運ぶ道で落ちた・壊れたフレームの数（どのチャンネルのものかは分からない。
                                 # UDP は seq の飛びから数えた下限、UART は CRC・形の崩れ。TCP の道は 0）

    def as_dict(self) -> dict:
        return {"capacity_bytes": self.capacity_bytes, "used_bytes": self.used_bytes, "peak_bytes": self.peak_bytes,
                "full_count": self.full_count, "full_s": self.full_s, "backpressure": self.backpressure,
                "dropped_frames": self.dropped_frames, "dropped_bytes": self.dropped_bytes, "lost_frames": self.lost_frames}


@dataclass(frozen=True)
class DapPacketStats:
    """DAP（ch 1）の応答の対応づけの事実（開いてから）。"""
    timeouts: int                # 応答を待って時間切れになった回数（そのたびに同期を外す）
    fences: int                  # 同期し直した（柵の応答が届いた）回数
    stale: int                   # 柵の前に届いて捨てた応答（時間切れの後に遅れて届いた応答）
    unsolicited: int             # 同期している間に、送った要求より多く届いて捨てた応答
    synchronized: bool           # 今、応答と要求が 1 対 1 に対応している

    def as_dict(self) -> dict:
        return {"timeouts": self.timeouts, "fences": self.fences, "stale": self.stale, "unsolicited": self.unsolicited,
                "synchronized": self.synchronized}


class ByteRing:
    """決まった大きさのバイトのリング（写しは書きと読みの 1 回ずつ）。スレッド安全ではない（FrameLink のロックの中で使う）。

    書き手は 2 通り: write（写して入れる）か、reserve → 書き手が直接書く（recv_into）→ commit。reserve の間に読み手が読んで
    空になっても、書きかけの場所（末尾）は動かさない。"""

    __slots__ = ("capacity", "_buf", "_view", "_head", "_used", "_reserved")

    def __init__(self, capacity: int):
        if capacity <= 0:
            raise ValueError(f"リングの大きさが不正: {capacity}")
        self.capacity = capacity
        self._buf = bytearray(capacity)
        self._view = memoryview(self._buf)
        self._head = 0                 # 次に読む位置
        self._used = 0
        self._reserved = 0             # reserve して、まだ commit していない量

    @property
    def used(self) -> int:
        return self._used

    @property
    def room(self) -> int:
        return self.capacity - self._used

    def _tail(self) -> int:
        tail = self._head + self._used
        return tail - self.capacity if tail >= self.capacity else tail

    def write(self, data) -> None:
        """data（len(data) ≤ room）を末尾に写す。"""
        n = len(data)
        if n > self.capacity - self._used:
            raise ValueError("リングに入らない")
        tail = self._tail()
        first = min(n, self.capacity - tail)
        self._view[tail:tail + first] = data[:first] if first < n else data
        if first < n:
            self._view[:n - first] = data[first:]
        self._used += n

    def reserve(self, limit: int) -> memoryview:
        """末尾の続いた空き（最大 limit バイト）。書き手はそこに書いて commit する。空きが無ければ長さ 0。"""
        if self._used == 0 and not self._reserved:
            self._head = 0                                  # 空なら先頭から（続いた空きを最大にする）
        tail = self._tail()
        if self._used == self.capacity:
            end = tail
        elif tail >= self._head:
            end = self.capacity                             # 空きは [tail, 終わり) と [0, head)。続いているのは前者
        else:
            end = self._head
        n = max(0, min(limit, end - tail))
        self._reserved = n
        return self._view[tail:tail + n]

    def commit(self, n: int) -> None:
        if not 0 <= n <= self._reserved:
            raise ValueError(f"commit {n} が reserve {self._reserved} を超える")
        self._used += n
        self._reserved = 0

    def read(self, size: int) -> bytes:
        """先頭から最大 size バイトを取り出す（1 回の写し）。"""
        n = min(size, self._used)
        if n <= 0:
            return b""
        head = self._head
        first = min(n, self.capacity - head)
        if first == n:
            out = bytes(self._view[head:head + n])
        else:
            out = b"".join((self._view[head:], self._view[:n - first]))
        head += n
        self._head = head - self.capacity if head >= self.capacity else head
        self._used -= n
        if self._used == 0 and not self._reserved:
            self._head = 0
        return out

    def clear(self) -> None:
        """溜まった分を捨てる（書きかけの場所は動かさない: 先頭を末尾へ）。"""
        self._head = self._tail()
        self._used = 0
        if not self._reserved:
            self._head = 0


class _ByteChannel:
    """1 つのバイト列のチャンネル: リングと、その事実の数え。"""

    __slots__ = ("ring", "peak", "full_count", "full_s", "full_since", "dropped_frames", "dropped_bytes", "backpressure",
                 "source")

    def __init__(self, capacity: int):
        self.ring = ByteRing(capacity)
        self.peak = 0
        self.full_count = 0
        self.full_s = 0.0
        self.full_since: float | None = None      # 今止めている（止め始めた時刻）
        self.dropped_frames = 0
        self.dropped_bytes = 0
        self.backpressure = False                  # 上限で受け取りを止める道で受けている（EthLink の TCP）
        # 今どちらの道から受けるか: "frames"（このリンクのフレーム）/ "stream"（流量制御のある道 = TCP）。読み手が頼んだ読み方で
        # 選ぶ（select_source）。選んでいない道から届いたものは並びのどこにも入れず、捨てて数える（2 つの道を混ぜない）
        self.source = "frames"
