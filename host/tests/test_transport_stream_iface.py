"""Device は拡張ストリームの道（TCP・道の選択・PC の溜まり・中継の状態）を Transport の口で問う（C2）。

前は Device が `getattr(transport, "link")` で LinkTransport の中のリンクを覗き、さらに `getattr(link, ...)` で
EthLink だけが持つ口を探していた（Link の抽象の漏れ。口の名前を打ち違えても黙って None・False になる）。今は
Transport が 4 つの口を必ず持ち（UsbTransport は道が 1 つ・PC に溜めない・中継なし）、LinkTransport はリンクに
任せ、FrameLink が既定（TCP なし・中継なし）を持つ。
"""

from fprobe.device import Device
from fprobe.frame_link import CH_STREAM, StreamBuffer
from fprobe.serial_link import SerialLink
from fprobe.session import link_buffer_bytes
from fprobe.transport import LinkTransport, UsbTransport

from test_serial_link import FpgaModel


class _Recorder:
    """Transport の口を呼ばれたとおりに記録する偽物（Device が中を覗かずに任せるかを見る）。"""

    max_packet = 512

    def __init__(self, *, tcp: bool):
        self.stream_tcp_open, self.calls = tcp, []

    def select_stream_path(self, ps_read):
        self.calls.append(("select", ps_read))

    def stream_buffer(self, reset_peak=False):
        self.calls.append(("buffer", reset_peak))
        return "buffer"

    def relay_status(self):
        self.calls.append(("relay",))
        return {"zc_state": 1}


def _device(transport) -> Device:
    dev = Device.__new__(Device)          # 識別の読み出し（CONTROL）を通さずに、ストリームの口だけを試す
    dev._transport = transport
    return dev


def test_device_asks_the_transport_and_does_not_look_inside():
    t = _Recorder(tcp=True)
    dev = _device(t)
    assert dev.stream_tcp_open is True
    dev.select_stream_path(True)
    assert dev.stream_buffer(reset_peak=True) == "buffer"
    assert dev.relay_status() == {"zc_state": 1}
    assert t.calls == [("select", True), ("buffer", True), ("relay",)]


def test_usb_has_one_path_no_pc_buffer_and_no_relay():
    usb = UsbTransport.__new__(UsbTransport)      # 機器なし（口の答えは機器によらない）
    assert usb.stream_tcp_open is False
    assert usb.select_stream_path(True) is None
    assert usb.stream_buffer(reset_peak=True) is None
    assert usb.relay_status() is None


def _serial_link():
    return SerialLink(FpgaModel(), buffer_bytes=link_buffer_bytes())


def test_a_frame_link_without_tcp_has_one_path_and_no_relay():
    link = _serial_link()
    try:
        t = LinkTransport(link)
        assert link.stream_tcp_open is False
        assert link.relay_status() is None
        assert t.stream_tcp_open is False
        t.select_stream_path(True)                                # 道が 1 つなら何もしない（例外にしない）
        assert link._bytes[CH_STREAM].source == "frames"
        assert t.relay_status() is None
    finally:
        link.close()


def test_link_transport_hands_the_pc_buffer_of_the_stream_channel():
    link = _serial_link()
    try:
        buf = LinkTransport(link).stream_buffer(reset_peak=True)
        assert isinstance(buf, StreamBuffer)
        assert buf.capacity_bytes == link_buffer_bytes()[CH_STREAM]
    finally:
        link.close()


class _TcpSerialLink(SerialLink):
    """TCP の道を開いたリンク（EthLink.open_stream_tcp と同じく、流量制御のある道を開いて stream_tcp_open・stream_tcp_opened が真）。"""

    stream_tcp_open = True
    stream_tcp_opened = True


class _TcpDroppedSerialLink(_TcpSerialLink):
    """TCP の道を開いた後に切れたリンク（EthLink: stream_tcp_open は偽、stream_tcp_opened は真のまま）。"""

    stream_tcp_open = False


def test_link_transport_selects_the_path_on_the_link_when_tcp_is_open():
    link = _TcpSerialLink(FpgaModel(), buffer_bytes=link_buffer_bytes())
    try:
        link._set_backpressure(CH_STREAM, 1024)
        t = LinkTransport(link)
        t.select_stream_path(False)
        assert link._bytes[CH_STREAM].source == "frames"
        t.select_stream_path(True)
        assert link._bytes[CH_STREAM].source == "stream"
    finally:
        link.close()


def test_link_transport_still_selects_the_path_after_the_tcp_dropped():
    # TCP が切れた後も道は選ぶ（窓経由を選べるように。2026-11-03 のレビュー §A の直しを、§B の Transport の口に移したもの）
    link = _TcpDroppedSerialLink(FpgaModel(), buffer_bytes=link_buffer_bytes())
    try:
        link._set_backpressure(CH_STREAM, 1024)
        t = LinkTransport(link)
        assert t.stream_tcp_open is False
        t.select_stream_path(True)
        assert link._bytes[CH_STREAM].source == "stream"
        t.select_stream_path(False)
        assert link._bytes[CH_STREAM].source == "frames"
    finally:
        link.close()
