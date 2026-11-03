"""fprobe デバイス（CSR の読み書き・ケイパビリティ・タイムベース）。"""

import time
from collections.abc import Sequence
from typing import Protocol

from . import generated as g
from .caps import Capabilities, parse, total_length
from .control import ControlChannel, Transport
from .frame_link import StreamBuffer

__all__ = ["OLDEST_PROTOCOL_VERSION", "Device", "DeviceMismatch", "DeviceTransport", "select_stream_path"]

# 受け入れる CONTROL プロトコルの版の下限。版は上位互換で上げる（2 は 1 に EXCHANGE を足しただけ）
OLDEST_PROTOCOL_VERSION = 1


class DeviceTransport(Transport, Protocol):
    """Device の下の口（transport.UsbTransport・LinkTransport）: CONTROL（Transport）に、拡張ストリームの読みと道の口を足したもの。
    Device はこの口だけを使い、下のリンクの中を覗かない。"""

    @property
    def stream_tcp_open(self) -> bool:
        """拡張ストリームを PS 直読みの TCP でも受けている。"""

    def read_stream(self, size: int, timeout_ms: int = 2000) -> bytes: ...

    def read_stream_some(self, size: int, timeout_ms: int = 2000) -> bytes: ...

    def select_stream_path(self, ps_read: bool) -> None:
        """これから頼む読み方の道（PS 直読み / 窓経由）を受け付ける道にする（道が 1 つなら何もしない）。"""

    def stream_buffer(self, reset_peak: bool = False) -> StreamBuffer | None:
        """PC の受け取り済みで未読の量と欠け（PC に溜めない道では None）。"""

    def relay_status(self) -> dict | None:
        """中継の状態（中継が無ければ None）。"""

    def close(self) -> None: ...


class DeviceMismatch(RuntimeError):
    """接続先が fprobe でない、またはプロトコル版が合わない。"""


def select_stream_path(dev, ps_read: bool) -> None:
    """拡張ストリームを頼む命令の前に、その読み方の道（PS 直読み = TCP、窓経由 = フレーム）を受け取りのリングが受け付ける道に
    する（後から選ぶと、先に届いた分を選んでいない道として捨てる）。read_stream を使う読み手は必ずこれを通す。道が 1 つの
    リング・偽の機器（select_stream_path の無い機器）では何もしない。"""
    select = getattr(dev, "select_stream_path", None)
    if select is not None:
        select(ps_read)


def _bytes(words: Sequence[int]) -> bytes:
    return b"".join(w.to_bytes(4, "little") for w in words)


class Device:
    def __init__(self, transport: DeviceTransport, *, identify_wait_s: float = 0.0):
        """identify_wait_s: 識別の READ（SYSTEM_MAGIC。読みに副作用が無い）が時間切れなら、開き始めからこの秒数まで送り直す
        （Ethernet のリンク確立待ち等）。使う側が明示する（0 = 送り直さない。Ethernet で開く口は必ず渡させる）。
        送り直した回数は identify_resends（事実）。"""
        if identify_wait_s < 0:
            raise ValueError(f"identify_wait_s は 0 以上: {identify_wait_s}")
        self._transport = transport
        self.control = ControlChannel(transport)
        self.identify_wait_s = identify_wait_s
        self.identify_resends = 0                     # 識別の READ を時間切れで送り直した回数
        deadline = time.monotonic() + identify_wait_s
        while True:
            try:
                magic, version = self.read(g.SYSTEM_MAGIC, 2)
                break
            except TimeoutError as e:
                if time.monotonic() > deadline:
                    e.add_note(f"識別の READ を {self.identify_resends} 回送り直した（待つ時間 {identify_wait_s:g} 秒）")
                    raise
                transport.flush()                     # 遅れて届く応答を捨ててから送り直す
                self.identify_resends += 1
        if magic != g.SYSTEM_MAGIC_VALUE:
            raise DeviceMismatch(f"magic 0x{magic:08X} は fprobe ではない")
        if not OLDEST_PROTOCOL_VERSION <= version <= g.PROTOCOL_VERSION:
            raise DeviceMismatch(f"プロトコル版 {version}（このライブラリは {OLDEST_PROTOCOL_VERSION}〜"
                                 f"{g.PROTOCOL_VERSION}）")
        self.protocol_version = version           # 新しい版の要求（EXCHANGE は 2 から）を使うかはこれで決める
        # 最後に capabilities() で読んだケイパビリティ（まだ読んでいなければ None）。回路が替わる（構成の切り替え・USB の
        # 開き直し）のは Device を開き直すときだけなので、同じ Device の間は古くならない（新しい Device は None から）
        self.last_capabilities: Capabilities | None = None

    @classmethod
    def open(cls, serial: str | None = None) -> "Device":
        from .transport import UsbTransport
        return cls(UsbTransport.open(serial))

    def close(self) -> None:
        self._transport.close()

    def __enter__(self) -> "Device":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- CSR -------------------------------------------------------------------------

    def read(self, address: int, count: int = 1, *, timeout_retries: int = 0) -> list[int]:
        """count ワードを読む。timeout_retries は ControlChannel.read（時間切れの READ を送り直してよい回数。使う側が明示する）。"""
        return self.control.read(address, count, timeout_retries=timeout_retries)

    def write(self, address: int, words: Sequence[int]) -> None:
        self.control.write(address, words)

    def exchange(self, address: int, words: Sequence[int]) -> list[int]:
        """同じ番地に語を順に書き、書くごとに読む（CONTROL の EXCHANGE。プロトコル 2 から）。"""
        return self.control.exchange(address, words)

    def control_counters(self) -> dict[str, int]:
        """CONTROL の開いてからの数え（ControlChannel.counters: read_retries・stale_responses。事実）。"""
        return self.control.counters()

    def read32(self, address: int) -> int:
        return self.read(address, 1)[0]

    def write32(self, address: int, value: int) -> None:
        self.write(address, [value])

    # --- 機能 ------------------------------------------------------------------------

    def capabilities(self) -> Capabilities:
        """ケイパビリティを機器から読む（呼ぶたびに読み直す。UDP のリンクでは何往復もする）。読んだものは
        last_capabilities に残す（USB が切れた後の比較・known_capabilities）。"""
        total = total_length(_bytes(self.read(g.CAPS_BASE, 2)))
        if not 8 <= total <= g.CAPS_SIZE or total % 4:
            raise ValueError(f"ケイパビリティの長さが不正: {total}")
        self.last_capabilities = parse(_bytes(self.read(g.CAPS_BASE, total // 4)))
        return self.last_capabilities

    def known_capabilities(self) -> Capabilities:
        """最後に読んだケイパビリティ（まだ読んでいなければ読む）。同じ Device の間は回路が替わらない（last_capabilities）
        ので、毎秒の読み・流れの始まりはこちらで読み直しの往復を省く。"""
        return self.last_capabilities if self.last_capabilities is not None else self.capabilities()

    def read_stream(self, size: int, timeout_ms: int = 2000) -> bytes:
        """拡張ストリーム（ロジアナの深いバッファの読み出し等）から size バイト。"""
        return self._transport.read_stream(size, timeout_ms)

    @property
    def stream_tcp_open(self) -> bool:
        """拡張ストリーム（ch 4）を TCP で受けている（Ethernet のリンクで open_stream_tcp 済み）。USB・シリアルでは False。"""
        return self._transport.stream_tcp_open

    def select_stream_path(self, ps_read: bool) -> None:
        """これから頼む拡張ストリームの読み方の道を、リングに受け付ける道として選ぶ: ps_read = True は PS 直読み（TCP で
        つながっていれば TCP）、False は窓経由（このリンクのフレーム）。選んでいない道から届いたものは捨てて数える
        （frame_link.FrameLink.select_source）。TCP を開いていないリンク・USB では道は 1 つなので何もしない。
        TCP が切れた後も道は選ぶ: 窓経由は選べ（UDP で届くものを受ける）、PS 直読みは TCP の道のまま読みが「止まった」で失敗する。"""
        self._transport.select_stream_path(ps_read)

    def stream_buffer(self, reset_peak: bool = False) -> StreamBuffer | None:
        """拡張ストリーム（ch 4）の、PC の受け取り済みで未読の量と、その道の欠けの数（frame_link.StreamBuffer）。フレームのリンク
        （SerialLink・EthLink）だけが持つ。USB では None（受け取りは EP の読みそのもので、PC 側に溜めない。Bulk は欠けない）。"""
        return self._transport.stream_buffer(reset_peak=reset_peak)

    def relay_status(self) -> dict | None:
        """PYNQ の中継の状態（eth_link.EthLink.relay_status: ストリームをゼロコピーの道と複写の道のどちらで送っているか、
        完了通知の copied・ENOBUFS 等の事実）。中継の無いリンク・返事の無い中継では None。"""
        return self._transport.relay_status()

    def read_stream_some(self, size: int, timeout_ms: int = 2000) -> bytes:
        """拡張ストリームの 1 転送（連続ストリーミング用。size 以下か最大 1 パケット。transport の説明を参照）。"""
        return self._transport.read_stream_some(size, timeout_ms)

    def time(self) -> int:
        """タイムベースの値（カウント。秒にするには capabilities().timebase_hz で割る）。"""
        lo, hi = self.read(g.TIMEBASE_TIME_LO, 2)      # time_lo を読んだ時点の上位が time_hi に保持される
        return hi << 32 | lo
