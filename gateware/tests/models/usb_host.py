"""UlpiPhy 上で動く USB ホストの振る舞いモデル（テスト専用）。

トランザクション単位の操作（トークン・データ・ハンドシェイクの送出と、デバイス応答の受け取り）と、
その上の制御転送・バルク転送を提供する。応答時間（ホストのパケット終了からデバイスの送信開始まで、
PHY クロック周期）も測る。
"""

from dataclasses import dataclass

from cocotb.triggers import FallingEdge

from . import usb_packets as up
from .ulpi_phy import UlpiPhy

# HS のデバイス応答は 192 ビット時間 = 24 周期以内に始まらなければならない
HS_RESPONSE_LIMIT = 24


@dataclass
class Response:
    packet: object | None          # up.Data / up.Handshake / None（無応答）
    latency: int | None            # 周期


class UsbHost:
    def __init__(self, phy: UlpiPhy, clock, *, address: int = 0):
        self.phy = phy
        self._clock = clock
        self.address = address
        self.latencies: list[int] = []

    async def _cycles(self, n: int):
        for _ in range(n):
            await FallingEdge(self._clock)

    async def send(self, packet: bytes, **kwargs):
        """パケットを PHY に渡し、link への受け渡しが終わるまで待つ。"""
        target = self.phy.receptions_done + 1
        self.phy.receive(packet, **kwargs)
        while self.phy.receptions_done < target:
            await FallingEdge(self._clock)

    async def response(self, *, timeout: int = 200) -> Response:
        """直前に送ったパケットに対するデバイスの応答を待つ。

        timeout は応答が始まるまでの周期数。始まった送信は（長いデータパケットでも）終わるまで待つ。
        """
        start = len(self.phy.transmissions)
        waited = 0
        while waited < timeout or self.phy.transmitting:
            if len(self.phy.transmissions) > start:
                t = self.phy.transmissions[start]
                latency = t.start_cycle - self.phy.last_reception_end
                self.latencies.append(latency)
                return Response(up.parse(t.packet), latency)
            if not self.phy.transmitting:
                waited += 1
            await FallingEdge(self._clock)
        return Response(None, None)

    # --- トランザクション -----------------------------------------------------

    async def setup(self, request: bytes, *, ep: int = 0) -> Response:
        await self.send(up.token(up.SETUP, self.address, ep))
        await self.send(up.data_packet(up.DATA0, request))
        return await self.response()

    async def out(self, ep: int, pid: int, payload: bytes, *, corrupt: bool = False) -> Response:
        await self.send(up.token(up.OUT, self.address, ep))
        packet = bytearray(up.data_packet(pid, payload))
        if corrupt:
            packet[-1] ^= 0x01
        await self.send(bytes(packet))
        return await self.response()

    async def ping(self, ep: int) -> Response:
        await self.send(up.token(up.PING, self.address, ep))
        return await self.response()

    async def in_(self, ep: int, *, ack: bool = True) -> Response:
        await self.send(up.token(up.IN, self.address, ep))
        r = await self.response()
        if ack and isinstance(r.packet, up.Data):
            await self._cycles(2)
            await self.send(up.handshake(up.ACK))
        return r


class TransferError(AssertionError):
    """制御転送・バルク転送がプロトコル上失敗した（STALL・無応答・想定外の応答）。"""


class Stalled(TransferError):
    """デバイスが STALL を返した。"""


def setup_packet(request_type: int, request: int, value: int = 0, index: int = 0,
                 length: int = 0) -> bytes:
    return bytes([request_type, request]) + value.to_bytes(2, "little") + \
        index.to_bytes(2, "little") + length.to_bytes(2, "little")


async def _retry_nak(transaction, *, attempts: int = 200):
    """NAK の間は同じトランザクションを繰り返す。"""
    for _ in range(attempts):
        r = await transaction()
        if r.packet != up.Handshake(up.NAK):
            return r
    raise TransferError("NAK が続いた")


async def control_in(host: UsbHost, request: bytes, *, max_packet: int = 64) -> bytes:
    """IN データ段のある制御転送。受け取ったデータを返す。"""
    length = int.from_bytes(request[6:8], "little")
    r = await host.setup(request)
    if r.packet != up.Handshake(up.ACK):
        raise TransferError(f"SETUP に {r.packet}")
    data = bytearray()
    toggle = up.DATA1
    while True:
        r = await _retry_nak(lambda: host.in_(0))
        if r.packet == up.Handshake(up.STALL):
            raise Stalled("データ段で STALL")
        if not isinstance(r.packet, up.Data) or r.packet.pid != toggle:
            raise TransferError(f"データ段に {r.packet}")
        data += r.packet.payload
        toggle = up.DATA0 if toggle == up.DATA1 else up.DATA1
        if len(r.packet.payload) < max_packet or len(data) >= length:
            break
    status = await _retry_nak(lambda: host.out(0, up.DATA1, b""))
    if status.packet != up.Handshake(up.ACK):
        raise TransferError(f"ステータス段に {status.packet}")
    return bytes(data)


async def control_out(host: UsbHost, request: bytes, data: bytes = b"", *,
                      max_packet: int = 64) -> None:
    """OUT データ段（無しも可）の制御転送。"""
    r = await host.setup(request)
    if r.packet != up.Handshake(up.ACK):
        raise TransferError(f"SETUP に {r.packet}")
    toggle = up.DATA1
    for i in range(0, len(data), max_packet):
        chunk = data[i:i + max_packet]
        r = await _retry_nak(lambda: host.out(0, toggle, chunk))
        if r.packet == up.Handshake(up.STALL):
            raise Stalled("データ段で STALL")
        # NYET（HS）は受理済みで次はまだ受けられない意味。次の OUT は準備できるまで NAK される
        if r.packet not in (up.Handshake(up.ACK), up.Handshake(up.NYET)):
            raise TransferError(f"データ段に {r.packet}")
        toggle = up.DATA0 if toggle == up.DATA1 else up.DATA1
    r = await _retry_nak(lambda: host.in_(0))
    if r.packet == up.Handshake(up.STALL):
        raise Stalled("ステータス段で STALL")
    if r.packet != up.Data(up.DATA1, b""):
        raise TransferError(f"ステータス段に {r.packet}")


# --- バスリセット・チャープ ------------------------------------------------------

async def reset_and_negotiate(host: UsbHost, *, line_stable: int, high_speed: bool = True,
                              timeout: int = 100_000) -> None:
    """バスリセットを行い、デバイスのチャープに K-J を 3 組返して HS にする。

    high_speed=False のときは K-J を返さず、デバイスが FS に戻ることを待つ。
    """
    from .ulpi_phy import LINE_J, LINE_K, LINE_SE0

    phy = host.phy
    before = sum(1 for t in phy.transmissions if t.pid is None)
    phy.set_line_state(LINE_SE0)
    for _ in range(timeout):
        if sum(1 for t in phy.transmissions if t.pid is None) > before:
            break
        await FallingEdge(host._clock)
    else:
        raise TransferError("デバイスのチャープが来ない")
    if high_speed:
        for _ in range(3):
            phy.set_line_state(LINE_K)
            await host._cycles(line_stable + 20)
            phy.set_line_state(LINE_J)
            await host._cycles(line_stable + 20)
        phy.set_line_state(0b00)                    # HS アイドル（squelch）
        expected = 0x40
    else:
        expected = 0x45
    for _ in range(timeout):
        if phy.func_ctrl == expected:
            if not high_speed:
                phy.set_line_state(LINE_J)
            return
        await FallingEdge(host._clock)
    raise TransferError(f"Function Control が {expected:#x} にならない（{phy.func_ctrl:#x}）")


# --- バルク転送 ----------------------------------------------------------------

class BulkPipe:
    """1 つのバルクエンドポイント（1 方向）のトグルを管理して転送する。

    corrupt_prob: OUT データの CRC を壊す確率（デバイスは無応答 → 同じトグルで再送）
    drop_ack_prob: IN データに ACK を返さない確率（デバイスは同じデータを再送 → 受け流す）
    """

    def __init__(self, host: UsbHost, ep: int, *, max_packet: int, rng=None,
                 corrupt_prob: float = 0.0, drop_ack_prob: float = 0.0):
        self.host, self.ep, self.max_packet = host, ep, max_packet
        self.toggle = up.DATA0
        self.rng = rng
        self.corrupt_prob = corrupt_prob
        self.drop_ack_prob = drop_ack_prob

    def _flip(self):
        self.toggle = up.DATA1 if self.toggle == up.DATA0 else up.DATA0

    def _chance(self, p):
        return self.rng is not None and self.rng.random() < p

    async def write(self, data: bytes, *, attempts: int = 500) -> None:
        chunks = [data[i:i + self.max_packet] for i in range(0, len(data), self.max_packet)]
        if len(data) % self.max_packet == 0:
            chunks.append(b"")                          # MPS 境界ちょうどで終わる転送は ZLP で締める
        for chunk in chunks:
            for _ in range(attempts):
                r = await self.host.out(self.ep, self.toggle, chunk,
                                        corrupt=self._chance(self.corrupt_prob))
                if r.packet in (up.Handshake(up.ACK), up.Handshake(up.NYET)):
                    self._flip()
                    if r.packet == up.Handshake(up.NYET):
                        await self._ping_until_ready(attempts)
                    break
                if r.packet == up.Handshake(up.STALL):
                    raise Stalled(f"EP{self.ep} OUT が STALL")
                if r.packet == up.Handshake(up.NAK):
                    await self._ping_until_ready(attempts)
                # 無応答（CRC 異常を注入した場合）は同じトグルで再送する
            else:
                raise TransferError(f"EP{self.ep} OUT が進まない")

    async def _ping_until_ready(self, attempts):
        for _ in range(attempts):
            r = await self.host.ping(self.ep)
            if r.packet == up.Handshake(up.ACK):
                return
            await self.host._cycles(20)
        raise TransferError(f"EP{self.ep} の PING が ACK にならない")

    async def read(self, length: int, *, attempts: int = 2000) -> bytes:
        """短パケット（ZLP を含む）か length バイトに達するまで読む。"""
        data = bytearray()
        for _ in range(attempts):
            drop = self._chance(self.drop_ack_prob)
            r = await self.host.in_(self.ep, ack=not drop)
            if r.packet == up.Handshake(up.NAK):
                await self.host._cycles(10)
                continue
            if r.packet == up.Handshake(up.STALL):
                raise Stalled(f"EP{self.ep} IN が STALL")
            if not isinstance(r.packet, up.Data) or drop:
                continue                                # 無応答、または ACK を返していない（再送される）
            if r.packet.pid != self.toggle:
                continue                                # 再送された重複（ACK は返してある）
            self._flip()
            data += r.packet.payload
            if len(r.packet.payload) < self.max_packet or len(data) >= length:
                return bytes(data)
        raise TransferError(f"EP{self.ep} IN が終わらない（{len(data)} バイト）")


# --- 列挙 ----------------------------------------------------------------------

def _get_descriptor(dtype: int, index: int = 0, length: int = 255, lang: int = 0) -> bytes:
    return setup_packet(0x80, 6, (dtype << 8) | index, lang, length)


async def enumerate_like_windows(host: UsbHost, *, address: int) -> dict[str, bytes]:
    """Windows 10/11 と同じ順序で列挙し、取得したディスクリプタを返す。"""
    result = {"device64": await control_in(host, _get_descriptor(1, length=64))}
    await control_out(host, setup_packet(0x00, 5, address))
    host.address = address
    await host._cycles(50)
    result["device"] = await control_in(host, _get_descriptor(1, length=18))
    result["config9"] = await control_in(host, _get_descriptor(2, length=9))
    total = int.from_bytes(result["config9"][2:4], "little")
    result["config"] = await control_in(host, _get_descriptor(2, length=total))
    result["string0"] = await control_in(host, _get_descriptor(3, 0))
    result["product"] = await control_in(host, _get_descriptor(3, result["device"][15], lang=0x0409))
    result["serial"] = await control_in(host, _get_descriptor(3, result["device"][16], lang=0x0409))
    result["bos5"] = await control_in(host, _get_descriptor(15, length=5))
    bos_total = int.from_bytes(result["bos5"][2:4], "little")
    result["bos"] = await control_in(host, _get_descriptor(15, length=bos_total))
    set_length = int.from_bytes(result["bos"][5 + 24:5 + 26], "little")
    vendor_code = result["bos"][5 + 26]
    result["msos20"] = await control_in(host, setup_packet(0xC0, vendor_code, 0, 7, set_length))
    await control_out(host, setup_packet(0x00, 9, 1))
    return result
