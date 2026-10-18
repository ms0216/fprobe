"""fprobe デバイス（CSR の読み書き・ケイパビリティ・タイムベース）。"""

import time
from collections.abc import Sequence

from . import generated as g
from .caps import Capabilities, parse, total_length
from .control import ControlChannel, Transport

__all__ = ["LINK_UP_S", "OLDEST_PROTOCOL_VERSION", "Device", "DeviceMismatch"]

# 受け入れる CONTROL プロトコルの版の下限。版は上位互換で上げる（2 は 1 に EXCHANGE を足しただけ）
OLDEST_PROTOCOL_VERSION = 1
LINK_UP_S = 15.0           # Ethernet のリンク確立（ボードの書き込み直後の自動ネゴシエーション）を待つ時間


class DeviceMismatch(RuntimeError):
    """接続先が fprobe でない、またはプロトコル版が合わない。"""


def _bytes(words: Sequence[int]) -> bytes:
    return b"".join(w.to_bytes(4, "little") for w in words)


class Device:
    def __init__(self, transport: Transport, *, wait_s: float = 0.0):
        """wait_s: 識別の読み出しが応答しなければ、この時間まで繰り返す（Ethernet のリンク確立待ち等）。"""
        self._transport = transport
        self.control = ControlChannel(transport)
        deadline = time.monotonic() + wait_s
        while True:
            try:
                magic, version = self.read(g.SYSTEM_MAGIC, 2)
                break
            except TimeoutError:
                if time.monotonic() > deadline:
                    raise
                transport.flush()                     # 遅れて届く応答を捨ててから繰り返す
        if magic != g.SYSTEM_MAGIC_VALUE:
            raise DeviceMismatch(f"magic 0x{magic:08X} は fprobe ではない")
        if not OLDEST_PROTOCOL_VERSION <= version <= g.PROTOCOL_VERSION:
            raise DeviceMismatch(f"プロトコル版 {version}（このライブラリは {OLDEST_PROTOCOL_VERSION}〜"
                                 f"{g.PROTOCOL_VERSION}）")
        self.protocol_version = version           # 新しい版の要求（EXCHANGE は 2 から）を使うかはこれで決める

    @classmethod
    def open(cls, serial: str | None = None) -> "Device":
        from .transport import UsbTransport
        return cls(UsbTransport.open(serial))

    @classmethod
    def open_serial(cls, port: str, baud: int | None = None) -> "Device":
        """SerialLink（UART のホストリンク）の COM ポート port で開く。baud の既定は serial_link.DEFAULT_BAUD。"""
        from .serial_link import DEFAULT_BAUD
        from .transport import LinkTransport
        transport = LinkTransport.open(port, baud or DEFAULT_BAUD)
        try:
            return cls(transport)
        except BaseException:
            transport.close()
            raise

    @classmethod
    def open_eth(cls, host: str, port: int | None = None) -> "Device":
        """EthLink（UDP のホストリンク）でボードの IP アドレス host に開く。"""
        from .transport import LinkTransport
        transport = LinkTransport.open_eth(host, port)
        try:
            return cls(transport, wait_s=LINK_UP_S)
        except BaseException:
            transport.close()
            raise

    def close(self) -> None:
        close = getattr(self._transport, "close", None)
        if close is not None:
            close()

    def __enter__(self) -> "Device":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- CSR -------------------------------------------------------------------------

    def read(self, address: int, count: int = 1) -> list[int]:
        return self.control.read(address, count)

    def write(self, address: int, words: Sequence[int]) -> None:
        self.control.write(address, words)

    def exchange(self, address: int, words: Sequence[int]) -> list[int]:
        """同じ番地に語を順に書き、書くごとに読む（CONTROL の EXCHANGE。プロトコル 2 から）。"""
        return self.control.exchange(address, words)

    def read32(self, address: int) -> int:
        return self.read(address, 1)[0]

    def write32(self, address: int, value: int) -> None:
        self.write(address, [value])

    # --- 機能 ------------------------------------------------------------------------

    def capabilities(self) -> Capabilities:
        total = total_length(_bytes(self.read(g.CAPS_BASE, 2)))
        if not 8 <= total <= g.CAPS_SIZE or total % 4:
            raise ValueError(f"ケイパビリティの長さが不正: {total}")
        self.last_capabilities = parse(_bytes(self.read(g.CAPS_BASE, total // 4)))   # USB が切れた後の比較用（B28）
        return self.last_capabilities

    def read_stream(self, size: int, timeout_ms: int = 2000) -> bytes:
        """拡張ストリーム（ロジアナの深いバッファの読み出し等）から size バイト。"""
        return self._transport.read_stream(size, timeout_ms)

    def read_stream_some(self, size: int, timeout_ms: int = 2000) -> bytes:
        """拡張ストリームの 1 転送（連続ストリーミング用。size 以下か最大 1 パケット。transport の説明を参照）。"""
        return self._transport.read_stream_some(size, timeout_ms)

    def time(self) -> int:
        """タイムベースの値（カウント。秒にするには capabilities().timebase_hz で割る）。"""
        lo, hi = self.read(g.TIMEBASE_TIME_LO, 2)      # time_lo を読んだ時点の上位が time_hi に保持される
        return hi << 32 | lo
