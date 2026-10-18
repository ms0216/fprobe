"""フレームのホストリンク（SerialLink・EthLink）に共通の、論理チャンネルの振り分け。

フレーム = ch(1) | payload(0..512)。チャンネルの意味は FPGA 側の frame_mux と同じ（全体設計 §3.2）:

- パケットのチャンネル:
  - CONTROL（ch 0）: PC → FPGA は 1 フレーム = 1 要求（≤ 512 バイト）。FPGA → PC の応答は USB の Bulk IN と同じく
    512 バイトのフレームを続け、512 未満のフレーム（ちょうど 512 の倍数で終わるときは空のフレーム）で終わる。
  - DAP（ch 1）: 両方向とも 1 フレーム = 1 パケット（≤ 512。USB と同じく終端の空フレームは付けない）。
- バイト列のチャンネル（SWO = 2・UART0 = 3・STREAM = 4）は届いた順にバッファへつなぐ。
- LINE_CODING（ch 5、PC → FPGA のみ）: payload は u32 LE のボーレート。ターゲット UART0 のボーレートを設定する
  （USB の CDC SET_LINE_CODING の代わり。応答なし）。

設計: 受信は背景スレッド 1 本（pyOCD の PyUSBv2 の rx スレッドと同じ形）がリンクを読み続け、フレームを
チャンネルごとのキュー / バッファへ振り分ける。CONTROL（Device）と DAP（pyOCD）と UART0 を別々の利用者が
同時に使えるようにするため。送信はロックで 1 フレームずつ書く。応答の欠けは再送しない（読み出しに副作用のある
CSR があるため）。応答待ちの timeout で TimeoutError にし、上位（ControlChannel の flush / pyOCD）に任せる。
フレームをどう運ぶか（COBS + CRC の UART、UDP）は派生クラスが _write_frame / _rx_loop で受け持つ。
"""

import threading
import time
from collections import deque

__all__ = ["CH_CONTROL", "CH_DAP", "CH_LINE_CODING", "CH_STREAM", "CH_SWO", "CH_UART0", "MAX_PAYLOAD", "FrameLink"]

CH_CONTROL, CH_DAP, CH_SWO, CH_UART0, CH_STREAM, CH_LINE_CODING = range(6)
_PACKET_CHANNELS = (CH_CONTROL, CH_DAP)
_BYTE_CHANNELS = (CH_SWO, CH_UART0, CH_STREAM)

MAX_PAYLOAD = 512
UART_CHUNK = 32                                       # UART0 の送信の単位（バイト）
UART_AHEAD = 64                                       # ターゲットのボーレート換算で先行して送ってよい量


class FrameLink:
    """フレームの送受信とチャンネルの振り分け（スレッド安全）。派生クラスは _write_frame・_rx_loop・
    _close_transport を実装し、__init__ の最後に _start() を呼ぶ。"""

    name = "frame link"
    dap_packet_count = 1          # DAP のパケットを先送りしてよい数（FPGA 側の受信の余裕で決まる）

    def __init__(self):
        self._tx_lock = threading.Lock()
        self._cond = threading.Condition()
        self._packets = {ch: deque() for ch in _PACKET_CHANNELS}
        self._partial = bytearray()                       # CONTROL の応答の組み立て途中
        self._bytes = {ch: bytearray() for ch in _BYTE_CHANNELS}
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

    # --- 開閉 ------------------------------------------------------------------------

    def _start(self) -> None:
        self._thread = threading.Thread(target=self._rx_task, name=f"fprobe-{self.name}-rx", daemon=True)
        self._thread.start()

    def close(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        self._close_transport()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- 送信 ------------------------------------------------------------------------

    def send(self, ch: int, payload: bytes) -> None:
        """1 フレームを送る（payload ≤ 512 バイト）。"""
        payload = bytes(payload)
        if not 0 <= ch <= 0xFF or len(payload) > MAX_PAYLOAD:
            raise ValueError(f"フレームが不正: ch {ch}, {len(payload)} バイト")
        with self._tx_lock:
            self._write_frame(ch, payload)

    # --- 受信 ------------------------------------------------------------------------

    def receive_packet(self, ch: int, timeout_s: float = 2.0) -> bytes:
        """CONTROL / DAP のパケットを 1 つ（timeout_s で届かなければ TimeoutError）。"""
        queue = self._packets[ch]
        with self._cond:
            if not self._cond.wait_for(lambda: queue or self._error is not None, timeout_s):
                raise TimeoutError(f"ch {ch} の応答が届かない")
            self._check()
            return queue.popleft()

    def read_bytes(self, ch: int, size: int, timeout_s: float = 0.0) -> bytes:
        """バイト列のチャンネルから最大 size バイト。timeout_s まで 1 バイト以上を待つ（届かなければ空）。"""
        buffer = self._bytes[ch]
        with self._cond:
            self._cond.wait_for(lambda: buffer or self._error is not None, timeout_s)
            self._check()
            data = bytes(buffer[:size])
            del buffer[:size]
            return data

    def discard(self, ch: int) -> None:
        """ch の受け取り済みを捨てる。"""
        with self._cond:
            if ch in self._packets:
                self._packets[ch].clear()
                if ch == CH_CONTROL:
                    self._partial.clear()
            else:
                self._bytes[ch].clear()

    def _check(self) -> None:
        if self._error is not None:
            raise ConnectionError(f"{self.name} の受信が止まった") from self._error

    def _rx_task(self) -> None:
        try:
            self._rx_loop()
        except BaseException as e:                         # 切断等。待っている利用者へ伝える
            with self._cond:
                self._error = e
                self._cond.notify_all()

    def _count_bad(self) -> None:
        with self._cond:
            self.bad_frames += 1

    def _dispatch(self, ch: int, payload: bytes) -> None:
        with self._cond:
            if ch == CH_CONTROL:
                self._partial += payload
                if len(payload) < MAX_PAYLOAD:            # 短いフレーム（空を含む）で応答が終わる
                    self._packets[ch].append(bytes(self._partial))
                    self._partial.clear()
            elif ch == CH_DAP:
                self._packets[ch].append(payload)
            elif ch in self._bytes:
                self._bytes[ch] += payload
            else:
                self.bad_frames += 1
                return
            self.frames += 1
            self._cond.notify_all()

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
