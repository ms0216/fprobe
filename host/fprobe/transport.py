"""CONTROL チャンネルのトランスポート。USB（pyusb + libusb-package。fprobe 拡張インターフェースの CONTROL
エンドポイント）と フレームのリンク（SerialLink・EthLink。ch 0 = CONTROL、ch 4 = STREAM）。"""

import usb.core
import usb.util
from libusb_package import get_libusb1_backend

from . import generated as g
from .frame_link import CH_CONTROL, CH_STREAM, MAX_PAYLOAD, FrameLink
from .serial_link import DEFAULT_BAUD, SerialLink

__all__ = ["DeviceNotFound", "LinkTransport", "UsbTransport", "find_device"]

TIMEOUT_MS = 2000


class DeviceNotFound(RuntimeError):
    pass


def find_device(serial: str | None = None) -> usb.core.Device:
    """接続中の fprobe を 1 台選ぶ（複数あればシリアル番号の指定が必要）。"""
    found = list(usb.core.find(find_all=True, idVendor=g.USB_VID, idProduct=g.USB_PID,
                               backend=get_libusb1_backend()))
    if serial is not None:
        found = [d for d in found if usb.util.get_string(d, d.iSerialNumber) == serial]
    if not found:
        where = "" if serial is None else f", シリアル {serial}"
        raise DeviceNotFound(f"fprobe（{g.USB_VID:04X}:{g.USB_PID:04X}{where}）が見つからない")
    if len(found) > 1:
        raise DeviceNotFound(f"fprobe が {len(found)} 台ある。シリアル番号で指定すること")
    return found[0]


class UsbTransport:
    """fprobe 拡張インターフェース（CONTROL）。device を他の利用者（pyOCD 等）と共有するときは、
    device の後始末（dispose）は持ち主が行う。"""

    def __init__(self, device: usb.core.Device):
        self._device = device
        self._owns_device = False
        config = device.get_active_configuration()
        interface = next((i for i in config
                          if i.iInterface and usb.util.get_string(device, i.iInterface)
                          == g.USB_CONTROL_INTERFACE), None)
        if interface is None:
            raise DeviceNotFound(f"インターフェース {g.USB_CONTROL_INTERFACE!r} が無い")
        self._interface = interface.bInterfaceNumber
        usb.util.claim_interface(device, self._interface)
        out = usb.util.find_descriptor(interface, bEndpointAddress=g.USB_EP_CONTROL)
        self.max_packet = out.wMaxPacketSize

    @classmethod
    def open(cls, serial: str | None = None) -> "UsbTransport":
        transport = cls(find_device(serial))
        transport._owns_device = True
        return transport

    def send(self, message: bytes) -> None:
        self._device.write(g.USB_EP_CONTROL, message, timeout=TIMEOUT_MS)

    def receive(self, max_length: int) -> bytes:
        # 応答は短パケット（MPS 丁度なら ZLP）で終わるので、MPS の倍数に切り上げた長さで読む
        size = -(-max_length // self.max_packet) * self.max_packet
        return bytes(self._device.read(0x80 | g.USB_EP_CONTROL, size, timeout=TIMEOUT_MS))

    def read_stream(self, size: int, timeout_ms: int = 2000) -> bytes:
        """拡張ストリーム EP から size バイトを読む（足りなければ timeout_ms で TimeoutError）。"""
        out = bytearray()
        while len(out) < size:
            chunk = min(size - len(out), 1 << 20)
            chunk = -(-chunk // self.max_packet) * self.max_packet
            try:
                out += self._device.read(0x80 | g.USB_EP_STREAM, chunk, timeout=timeout_ms)
            except usb.core.USBTimeoutError as e:
                raise TimeoutError(f"ストリームが {len(out)}/{size} バイトで止まった") from e
        if len(out) != size:
            raise ValueError(f"ストリームが {len(out)} バイト（{size} バイトを要求）")
        return bytes(out)

    def read_stream_some(self, size: int, timeout_ms: int = 2000) -> bytes:
        """拡張ストリーム EP の 1 転送（連続ストリーミング用）。size を MPS の倍数に切り下げた長さ（最小 MPS）まで読み、
        短パケットで終わればそこまでを返す。届いているはずの量だけを求めること（足りなければ TimeoutError）。"""
        size = max(self.max_packet, size // self.max_packet * self.max_packet)
        try:
            return bytes(self._device.read(0x80 | g.USB_EP_STREAM, size, timeout=timeout_ms))
        except usb.core.USBTimeoutError as e:
            raise TimeoutError("ストリームが届かない") from e

    def flush(self) -> None:
        while True:
            try:
                self._device.read(0x80 | g.USB_EP_CONTROL, 64 * 1024, timeout=50)
            except usb.core.USBTimeoutError:
                return

    def close(self) -> None:
        usb.util.release_interface(self._device, self._interface)
        if self._owns_device:
            usb.util.dispose_resources(self._device)


class LinkTransport:
    """フレームのリンク（SerialLink・EthLink）上の CONTROL（ch 0）と拡張ストリーム（ch 4）。UsbTransport と同じ I/F。
    link を他の利用者（pyOCD の DAP 等）と共有するときは、link の後始末は持ち主が行う。"""

    max_packet = MAX_PAYLOAD

    def __init__(self, link: FrameLink, *, owns_link: bool = False):
        self.link = link
        self._owns_link = owns_link

    @classmethod
    def open(cls, port: str, baud: int = DEFAULT_BAUD) -> "LinkTransport":
        """SerialLink の COM ポートで開く。"""
        return cls(SerialLink.open(port, baud), owns_link=True)

    @classmethod
    def open_eth(cls, host: str, port: int | None = None) -> "LinkTransport":
        """EthLink（UDP）で開く。"""
        from .eth_link import DEFAULT_PORT, EthLink
        return cls(EthLink.open(host, port or DEFAULT_PORT), owns_link=True)

    def send(self, message: bytes) -> None:
        self.link.send(CH_CONTROL, message)

    def receive(self, max_length: int) -> bytes:
        return self.link.receive_packet(CH_CONTROL, TIMEOUT_MS / 1000)

    def read_stream(self, size: int, timeout_ms: int = 2000) -> bytes:
        """拡張ストリームから size バイト（timeout_ms の間 1 バイトも届かなければ TimeoutError）。"""
        out = bytearray()
        while len(out) < size:
            chunk = self.link.read_bytes(CH_STREAM, size - len(out), timeout_ms / 1000)
            if not chunk:
                raise TimeoutError(f"ストリームが {len(out)}/{size} バイトで止まった")
            out += chunk
        return bytes(out)

    def read_stream_some(self, size: int, timeout_ms: int = 2000) -> bytes:
        """拡張ストリームの届いている分（最大 size バイト。1 バイトも届かなければ TimeoutError）。"""
        data = self.link.read_bytes(CH_STREAM, size, timeout_ms / 1000)
        if not data:
            raise TimeoutError("ストリームが届かない")
        return data

    def flush(self) -> None:
        """遅れて届く応答も含めて、50 ms 何も来なくなるまで CONTROL を読み捨てる。"""
        while True:
            try:
                self.link.receive_packet(CH_CONTROL, 0.05)
            except TimeoutError:
                break
        self.link.discard(CH_CONTROL)

    def close(self) -> None:
        if self._owns_link:
            self.link.close()
