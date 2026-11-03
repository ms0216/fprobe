"""フレームのホストリンク（SerialLink・EthLink）に共通の、論理チャンネルの振り分け。

フレーム = ch(1) | payload(0..512)。チャンネルの意味は FPGA 側の frame_mux と同じ（全体設計 §3.2）:

- パケットのチャンネル:
  - CONTROL（ch 0）: PC → FPGA は 1 フレーム = 1 要求（≤ 512 バイト）。FPGA → PC の応答は USB の Bulk IN と同じく
    512 バイトのフレームを続け、512 未満のフレーム（ちょうど 512 の倍数で終わるときは空のフレーム）で終わる。
  - DAP（ch 1）: 両方向とも 1 フレーム = 1 パケット（≤ 512。USB と同じく終端の空フレームは付けない）。
- バイト列のチャンネル（SWO = 2・UART0 = 3・STREAM = 4）は届いた順に、チャンネルごとの決まった大きさのリング（ByteRing）へつなぐ。
- LINE_CODING（ch 5、PC → FPGA のみ）: payload は u32 LE のボーレート。ターゲット UART0 のボーレートを設定する
  （USB の CDC SET_LINE_CODING の代わり。応答なし）。

設計: 受信は背景スレッド 1 本（pyOCD の PyUSBv2 の rx スレッドと同じ形）がリンクを読み続け、フレームを
チャンネルごとのキュー / リングへ振り分ける。CONTROL（Device）と DAP（pyOCD）と UART0 を別々の利用者が
同時に使えるようにするため。送信はロックで 1 フレームずつ書く。フレームをどう運ぶか（COBS + CRC の UART、UDP）は
派生クラスが _write_frame / _rx_loop で受け持つ。

PC のメモリに溜める量（バイト列のチャンネル）には必ず上限がある（使う側が開くときに buffer_bytes で明示する。既定は持たない）。
上限に達したとき:
- 流量制御のある道（EthLink の TCP で受けるストリーム）: 受信のスレッドは読み手が減らすまで受け取りを止める（_wait_room）。
  止めた分は相手の流量制御で送り手が止まり、送り手の後ろ（PYNQ の DDR のリング）が溢れれば機器が溢れを記録する
  （設計: docs/superpowers/specs/2026-11-01-bandwidth-observation-design.md 1.7.7）。
- 流量制御の無い道（UDP・UART のフレーム）: 入らないフレームを丸ごと捨て、捨てたフレームとバイトを数える（黙って捨てない）。
どちらも、溜まり具合と捨てた数・落ちた数は buffer(ch)（StreamBuffer）で事実として返す。

応答の欠けは再送しない（CONTROL の WRITE・EXCHANGE は回路が要求を覚えていないので、送り直すと 2 回実行しうる。
docs の A11 の設計を参照）。応答待ちの timeout で TimeoutError にし、上位（ControlChannel / pyOCD）に任せる。
時間切れの後に遅れて届く応答を次の要求の応答に読まない:
- CONTROL: 要求の tag（8 ビット）を ControlChannel が照らし、違う tag の応答は読み捨てる（数える）。
- DAP: CMSIS-DAP の応答に要求の番号は無い。時間切れになったら DAP を「同期していない」にし、開き直す（discard）まで送受信を
  断る（DapDesync）。discard は柵（fence）を立てて同期し直す: DAP_Info(パケットの大きさ) を送り、その応答が届くまでの応答を
  すべて読み捨てる（回路は要求を順に処理するので、柵の応答より前の応答は柵より前の要求のもの）。同期している間は、送った要求の
  数より多い応答（頼んでいない応答）は捨てて数える（応答のキューは送った要求の数までしか伸びない）。
  限界（回路を変えずには消せない）: PC → FPGA の要求そのものが UDP で落ちると、その後の応答が 1 つずつ前の要求に対応してしまう
  （要求の欠けは PC から見えない）。要求に番号を付けて応答に写す回路の設計は A11 の報告にある。
"""

import threading
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass

__all__ = ["CH_CONTROL", "CH_DAP", "CH_LINE_CODING", "CH_STREAM", "CH_SWO", "CH_UART0", "BYTE_CHANNELS", "MAX_PAYLOAD",
           "ByteRing", "DapDesync", "DapPacketStats", "FrameLink", "StreamBuffer"]

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


class FrameLink:
    """フレームの送受信とチャンネルの振り分け（スレッド安全）。派生クラスは _write_frame・_rx_loop・
    _close_transport を実装し、__init__ の最後に _start() を呼ぶ。

    buffer_bytes: バイト列のチャンネル（SWO・UART0・STREAM）ごとに、PC に溜める上限（バイト）。全部を明示する（既定は持たない）。"""

    name = "frame link"
    dap_packet_count = 1          # DAP のパケットを先送りしてよい数（FPGA 側の受信の余裕で決まる）

    def __init__(self, *, buffer_bytes: Mapping[int, int]):
        missing = [ch for ch in BYTE_CHANNELS if ch not in buffer_bytes]
        if missing or set(buffer_bytes) - set(BYTE_CHANNELS):
            raise ValueError(f"buffer_bytes はバイト列のチャンネル {BYTE_CHANNELS} の全部を明示する（渡された鍵 {sorted(buffer_bytes)}）")
        self._tx_lock = threading.Lock()
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        # チャンネルごとの条件変数（ロックは _cond と共通）と、その待ち人の数。到着したフレームは、そのチャンネルを待つ
        # 利用者だけを起こす（1 つの条件変数で全員を起こすと、毎秒 1 万以上のストリームのフレームが、CSR の応答を待つスレッドを
        # 毎回起こして GIL を奪い合い、受信が詰まる）
        self._chan_cond = {ch: threading.Condition(self._lock) for ch in (*_PACKET_CHANNELS, *BYTE_CHANNELS)}
        self._waiters = dict.fromkeys(self._chan_cond, 0)
        self._stream_error: BaseException | None = None   # バイト列のチャンネル（ch 4）だけを運ぶ別の道（TCP）の失敗
        self._packets = {ch: deque() for ch in _PACKET_CHANNELS}
        self._partial = bytearray()                       # CONTROL の応答の組み立て途中
        self._bytes = {ch: _ByteChannel(buffer_bytes[ch]) for ch in BYTE_CHANNELS}
        self._writer = {ch: threading.Lock() for ch in BYTE_CHANNELS}   # リングの書き手は同時に 1 つ（reserve〜commit を含む）
        self._room = threading.Condition(self._lock)      # 上限で止まった受信のスレッドを起こす
        self._discard = bytearray(1 << 16)                # 受けていない道から届いたバイトを受け取って捨てる場所（_receive_into）
        # DAP の応答と要求の対応づけ（モジュールの説明）
        self._dap_synced = True
        self._dap_outstanding = 0                         # 同期している間: 送って、まだ応答を受け取っていない要求の数
        self._dap_fencing = False                         # 柵の応答を待っている
        self._dap_timeouts = self._dap_fences = self._dap_stale = self._dap_unsolicited = 0
        self.frames = 0                                   # 受け取った正しいフレームの数
        self.bad_frames = 0                               # 形の崩れ・未知の ch で捨てた数
        self._error: BaseException | None = None
        self._stop = threading.Event()
        self.uart_baud: int | None = None              # 最後に LINE_CODING で設定したボーレート
        self._uart_free_at = 0.0
        self._thread: threading.Thread | None = None

    # --- 派生クラスが実装する ------------------------------------------------------------

    def _write_frame(self, ch: int, payload: bytes) -> None:
        raise NotImplementedError

    def _rx_loop(self) -> None:
        """self._stop まで受信し、フレームごとに _dispatch（壊れたものは _count_bad）を呼ぶ。"""
        raise NotImplementedError

    def _close_transport(self) -> None:
        raise NotImplementedError

    def _lost_frames(self, ch: int) -> int:
        """ch を運ぶ道で落ちた・壊れたフレームの数（どのチャンネルのものかは分からない）。派生クラスが道に合わせて返す。"""
        return self.bad_frames

    # --- 開閉 ------------------------------------------------------------------------

    def _start(self) -> None:
        self._thread = threading.Thread(target=self._rx_task, name=f"fprobe-{self.name}-rx", daemon=True)
        self._thread.start()

    def close(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        with self._cond:
            if self._error is None:                        # 待っている人・この後に読む人へ、時間切れでなく閉じた事実を返す
                self._error = ConnectionError(f"{self.name} は閉じた")
            for cond in self._chan_cond.values():
                cond.notify_all()
            self._cond.notify_all()
            self._room.notify_all()
        if self._thread is not None:
            self._thread.join()
        self._close_transport()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- 送信 ------------------------------------------------------------------------

    def send(self, ch: int, payload: bytes) -> None:
        """1 フレームを送る（payload ≤ 512 バイト）。DAP は、同期していなければ DapDesync（モジュールの説明）。"""
        payload = bytes(payload)
        if not 0 <= ch <= 0xFF or len(payload) > MAX_PAYLOAD:
            raise ValueError(f"フレームが不正: ch {ch}, {len(payload)} バイト")
        if ch == CH_DAP:
            with self._cond:
                self._check_dap_synced()
                self._dap_outstanding += 1
        with self._tx_lock:
            self._write_frame(ch, payload)

    def _check_dap_synced(self) -> None:
        if not self._dap_synced:
            raise DapDesync(f"{self.name} の DAP の応答が時間切れになった後は、開き直す（discard）まで使えない"
                            "（遅れて届く応答を次の要求の応答に読まないため）")

    def _dap_fence(self, timeout_s: float) -> None:
        """柵を立てて DAP の応答と要求を同期し直す（_cond の外で呼ぶ）。柵の応答が届くまでの応答は捨てる。
        timeout_s のうちに届かなければ TimeoutError（同期していないまま）。"""
        with self._cond:
            self._packets[CH_DAP].clear()
            self._dap_outstanding = 0
            self._dap_synced = False
            self._dap_fencing = True                       # 柵の応答を待つ間は、頼んでいない応答も受ける（捨てるかはここで決める）
        try:
            with self._tx_lock:
                self._write_frame(CH_DAP, _DAP_FENCE)
            deadline = time.monotonic() + timeout_s
            queue = self._packets[CH_DAP]
            with self._cond:
                while True:
                    if not self._wait(CH_DAP, lambda: queue or self._error is not None,
                                      max(0.0, deadline - time.monotonic())):
                        raise TimeoutError(f"{self.name} の DAP の柵の応答が {timeout_s} 秒届かない（同期し直せない）")
                    self._check()
                    packet = queue.popleft()
                    if _is_fence_reply(packet):
                        self._dap_synced = True
                        self._dap_fences += 1
                        return
                    self._dap_stale += 1
        finally:
            with self._cond:
                self._dap_fencing = False

    # --- 受信 ------------------------------------------------------------------------

    def receive_packet(self, ch: int, timeout_s: float = 2.0) -> bytes:
        """CONTROL / DAP のパケットを 1 つ（timeout_s で届かなければ TimeoutError）。"""
        queue = self._packets[ch]
        with self._cond:
            if ch == CH_DAP:
                self._check_dap_synced()
            if not self._wait(ch, lambda: queue or self._error is not None, timeout_s):
                if ch == CH_DAP:                          # 遅れて届く応答を次の要求の応答に読まない: 同期を外す
                    self._dap_synced = False
                    self._dap_timeouts += 1
                raise TimeoutError(f"ch {ch} の応答が届かない")
            self._check()
            if ch == CH_DAP:
                self._dap_outstanding -= 1
            return queue.popleft()

    def dap_stats(self) -> DapPacketStats:
        with self._cond:
            return DapPacketStats(self._dap_timeouts, self._dap_fences, self._dap_stale, self._dap_unsolicited,
                                  self._dap_synced)

    def read_bytes(self, ch: int, size: int, timeout_s: float = 0.0) -> bytes:
        """バイト列のチャンネルから最大 size バイト。timeout_s まで 1 バイト以上を待つ（届かなければ空）。"""
        channel = self._bytes[ch]
        ring = channel.ring
        with self._cond:
            if not ring.used:
                self._wait(ch, lambda: ring.used or self._error is not None
                           or (ch == CH_STREAM and self._stream_error is not None), timeout_s)
            if not ring.used:
                self._check()
                if ch == CH_STREAM and self._stream_error is not None:      # 届いた分を読み切ってから伝える
                    raise ConnectionError(f"{self.name} の TCP のストリームが止まった") from self._stream_error
                return b""
            data = ring.read(size)
            if channel.full_since is not None:
                self._room.notify_all()
            return data

    def discard(self, ch: int, fence_timeout_s: float | None = None) -> None:
        """ch の受け取り済みを捨てる。DAP は fence_timeout_s を明示し、時間切れの後・応答待ちの要求があるときは柵で同期し直す
        （捨てた後に遅れて届く応答を読まないため。柵の応答が fence_timeout_s のうちに届かなければ TimeoutError）。"""
        if ch == CH_DAP:
            if fence_timeout_s is None:
                raise ValueError("DAP の discard は fence_timeout_s（柵の応答を待つ秒）を明示する")
            with self._cond:
                self._packets[ch].clear()
                need_fence = not self._dap_synced or self._dap_outstanding > 0
            if need_fence:
                self._dap_fence(fence_timeout_s)
            return
        with self._cond:
            if ch == CH_CONTROL:
                self._packets[ch].clear()
                self._partial.clear()
            else:
                self._bytes[ch].ring.clear()
                self._room.notify_all()

    def buffer(self, ch: int, reset_peak: bool = False) -> StreamBuffer:
        """バイト列のチャンネル ch の受け取り済みの量と欠けの数（StreamBuffer）。reset_peak = True なら、返した後で最大を今の量から
        測り直す（1 回の流れの最大を測る LaStream が始めに呼ぶ）。"""
        channel = self._bytes[ch]
        with self._cond:
            used = channel.ring.used
            full_s = channel.full_s
            if channel.full_since is not None:
                full_s += time.monotonic() - channel.full_since
            out = StreamBuffer(channel.ring.capacity, used, max(channel.peak, used), channel.full_count, full_s,
                               channel.source == "stream", channel.dropped_frames, channel.dropped_bytes,
                               # 落ちの数えは今受けている道のもの: 流量制御のある道（TCP）は欠けない。TCP を開いた後でも
                               # 窓経由（"frames"）を選んでいれば UDP の seq の飛びを返す（レビュー 2026-11-03）
                               0 if channel.source == "stream" else self._lost_frames(ch))
            if reset_peak:
                channel.peak = used
            return out

    def stream_buffer(self, reset_peak: bool = False) -> StreamBuffer:
        """ストリーム（ch 4）の buffer。"""
        return self.buffer(CH_STREAM, reset_peak)

    @property
    def stream_tcp_open(self) -> bool:
        """ストリーム（ch 4）を流量制御のある別の道（TCP）でも受けているか。既定のリンクはフレームの道だけ（EthLink が上書き）。"""
        return False

    @property
    def stream_tcp_opened(self) -> bool:
        """ストリーム（ch 4）の別の道（TCP）を開いた（切れた後も True）。ch 4 の受け口の道（select_source）を選べるのはこの間。
        既定のリンクはフレームの道だけ（EthLink が上書き）。"""
        return False

    def relay_status(self) -> dict | None:
        """中継の状態。既定のリンクには中継が無い（None。PYNQ の中継につながる EthLink が上書き）。"""
        return None

    def _set_backpressure(self, ch: int, capacity: int) -> None:
        """ch を流量制御のある道で受ける（派生クラスが、その道を開くときに呼ぶ）。溜める上限を capacity にする（溜まっている分は捨てる）。"""
        if capacity <= 0:
            raise ValueError(f"溜める上限が不正: {capacity}")
        with self._writer[ch], self._cond:
            channel = self._bytes[ch]
            if channel.ring.capacity != capacity:
                channel.ring = ByteRing(capacity)
            else:
                channel.ring.clear()
            channel.backpressure = True
            channel.source = "stream"

    def select_source(self, ch: int, source: str) -> None:
        """ch のバイト列をどちらの道から受けるか（"frames" = このリンクのフレーム、"stream" = 流量制御のある道）。読み手が、
        機器に頼んだ読み方（PYNQ の深いバッファ: PS 直読みは TCP、窓経由は UDP のフレーム）に合わせて、頼む前に選ぶ。
        選んでいない道から届いたものは捨てて数える（StreamBuffer.dropped_frames・dropped_bytes）。"""
        if source not in ("frames", "stream"):
            raise ValueError(f"道の名前が不正: {source}")
        channel = self._bytes[ch]
        if source == "stream" and not channel.backpressure:
            raise RuntimeError("流量制御のある道が開いていない（open_stream_tcp の前）")
        with self._writer[ch], self._cond:
            channel.source = source

    def _receive_into(self, ch: int, recv, limit: int) -> int:
        """流量制御のある道の受け取り: リングの末尾の空き（最大 limit）へ recv(memoryview) で直接書く（写しはここだけ）。
        上限まで溜まっていれば、読み手が減らすまで待つ（その間は受け取らない = 相手の流量制御で送り手が止まる）。
        受け取ったバイト数（0 = 相手が閉じた。負 = recv が何も受け取らずに戻った）を返す。止める（close）なら -1。
        recv の例外はそのまま伝える。"""
        channel = self._bytes[ch]
        with self._writer[ch]:
            if channel.source != "stream":                  # 今は受けていない道: 受け取って捨て、数える（並びに混ぜない）
                size = recv(memoryview(self._discard)[:limit])
                if size > 0:
                    with self._cond:
                        channel.dropped_frames += 1
                        channel.dropped_bytes += size
                return size
            with self._cond:
                ring = channel.ring
                if not ring.room:
                    channel.full_count += 1
                    channel.full_since = time.monotonic()
                    try:
                        while not ring.room and not self._stop.is_set():
                            self._room.wait(0.05)          # close を見落とさないように時々起きる
                    finally:
                        channel.full_s += time.monotonic() - channel.full_since
                        channel.full_since = None
                    if self._stop.is_set():
                        return -1
                view = ring.reserve(limit)
            try:
                size = recv(view)
            except BaseException:
                with self._cond:
                    ring.commit(0)
                raise
            with self._cond:
                ring.commit(max(size, 0))                   # 負 = 受け取りが無かった（recv の時間切れ等）
                if size > 0:
                    self._delivered(ch, channel)
            return size

    def _wait(self, ch: int, predicate, timeout_s: float) -> bool:
        """_cond のロックを持って呼ぶ。ch のフレームか受信の失敗を待つ。待つ間は _waiters に数える
        （_dispatch が、そのチャンネルを待つ人がいなければ通知しないため）。"""
        self._waiters[ch] += 1
        try:
            return self._chan_cond[ch].wait_for(predicate, timeout_s)
        finally:
            self._waiters[ch] -= 1

    def _check(self) -> None:
        if self._error is not None:
            raise ConnectionError(f"{self.name} の受信が止まった") from self._error

    def _rx_task(self) -> None:
        try:
            self._rx_loop()
        except BaseException as e:                         # 切断等。待っている利用者へ伝える
            with self._cond:
                self._error = e
                for cond in self._chan_cond.values():
                    cond.notify_all()
                self._cond.notify_all()

    def _count_bad(self) -> None:
        with self._cond:
            self.bad_frames += 1

    def _delivered(self, ch: int, channel: _ByteChannel) -> None:
        """_cond の中で: バイト列が入った後の数えと通知。"""
        used = channel.ring.used
        if used > channel.peak:
            channel.peak = used
        self.frames += 1
        if self._waiters[ch]:
            self._chan_cond[ch].notify_all()

    def _dispatch(self, ch: int, payload) -> None:
        """1 フレームを振り分ける。payload は bytes か memoryview（受け取りのバッファの一部。ここで写す）。"""
        with self._cond:
            channel = self._bytes.get(ch)
            if channel is not None:
                ring = channel.ring
                # 今は流量制御のある道（TCP）から受けているチャンネルに、この道（UDP 等）で届いたフレームは、TCP のバイトの
                # 並びのどこに入るか分からない。上限を超えたフレームも、流量制御の無い道では止められない。どちらも丸ごと捨てて数える
                if channel.source != "frames" or len(payload) > ring.room:
                    channel.dropped_frames += 1
                    channel.dropped_bytes += len(payload)
                    return
                ring.write(payload)
                self._delivered(ch, channel)
                return
            if ch == CH_CONTROL:
                self._partial += payload
                if len(payload) < MAX_PAYLOAD:            # 短いフレーム（空を含む）で応答が終わる
                    self._packets[ch].append(bytes(self._partial))
                    self._partial.clear()
            elif ch == CH_DAP:
                queue = self._packets[ch]
                if not self._dap_synced and not self._dap_fencing:
                    self._dap_stale += 1                      # 時間切れの後に遅れて届いた応答: 捨てて数える
                    return
                if not self._dap_fencing and len(queue) >= self._dap_outstanding:
                    self._dap_unsolicited += 1                # 頼んでいない応答（送った要求より多い）: 捨てて数える
                    return
                queue.append(bytes(payload))
            else:
                self.bad_frames += 1
                return
            self.frames += 1
            if self._waiters[ch]:                          # このチャンネルを待つ人がいなければ通知しない
                self._chan_cond[ch].notify_all()

    # --- UART0 -----------------------------------------------------------------------

    def set_uart_baud(self, baud: int) -> None:
        """ターゲット UART0 のボーレートを設定する（LINE_CODING フレーム）。"""
        self.send(CH_LINE_CODING, baud.to_bytes(4, "little"))
        self.uart_baud = baud

    def uart_write(self, data: bytes) -> int:
        """ターゲットの UART0 へ送る。ボーレートを設定済みなら、その速さを超えないように間をあける
        （FPGA 側は受け取り可能量を知らせないため。先行は UART_AHEAD バイトまで）。"""
        for i in range(0, len(data), UART_CHUNK):
            chunk = data[i:i + UART_CHUNK]
            if self.uart_baud:
                byte_s = 10 / self.uart_baud
                now = time.monotonic()
                wait = self._uart_free_at - now - UART_AHEAD * byte_s
                if wait > 0:
                    time.sleep(wait)
                    now = time.monotonic()
                self._uart_free_at = max(now, self._uart_free_at) + len(chunk) * byte_s
            self.send(CH_UART0, chunk)
        return len(data)

    def uart_read(self, size: int = 4096, timeout_s: float = 0.0) -> bytes:
        return self.read_bytes(CH_UART0, size, timeout_s)
