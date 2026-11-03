"""1 つの USB ハンドルで fprobe 拡張（CSR・ロジアナ・UART 状態）と pyOCD（CMSIS-DAP）を同時に使う。

Windows の libusb は機器を開くと全インターフェースを排他的に握るので、fprobe.Device と pyOCD を
別々に開くと（別プロセスでも）どちらかが失敗する。Session は機器を 1 度だけ開き、拡張インターフェースを
Device に、CMSIS-DAP v2 インターフェースを pyOCD に渡す。

フレームのリンク（SerialLink = Session.open_serial、EthLink = Session.open_eth）でも同じで、1 つのリンクの CONTROL（ch 0）を Device に、
DAP（ch 1）と SWO（ch 2）を pyOCD に渡す。

    from fprobe.session import Session
    with Session.open() as s, s.debug(target_override="stm32f401xe") as dbg:
        la = LogicAnalyzer(s.device); la.start(...)
        dbg.target.read32(0xE000ED00)
"""

from collections.abc import Callable, Sequence
from types import MethodType

import usb.core
import usb.util
from pyocd.core import exceptions
from pyocd.core.session import Session as PyocdSession
from pyocd.coresight.ap import CSW_SIZE32, MEM_AP, MEM_AP_CSW, MEM_AP_DRW, MEM_AP_TAR
from pyocd.probe.cmsis_dap_probe import CMSISDAPProbe
from pyocd.probe.pydapaccess.dap_access_api import DAPAccessIntf
from pyocd.probe.pydapaccess.cmsis_dap_core import DAPSWOControl, DAPSWOMode, DAPSWOTransport
from pyocd.probe.pydapaccess.dap_access_cmsis_dap import DAPAccessCMSISDAP, SWOStatus
from pyocd.probe.pydapaccess.interface.interface import Interface
from pyocd.utility.concurrency import locked
from pyocd.probe.pydapaccess.interface.pyusb_v2_backend import PyUSBv2, _match_cmsis_dap_v2_interface

from . import generated as g
from .device import Device
from .pyocd_targets import register
from .frame_link import CH_DAP, CH_STREAM, CH_SWO, CH_UART0, MAX_PAYLOAD, FrameLink
from .serial_link import DEFAULT_BAUD, SerialLink
from .transport import LinkTransport, UsbTransport, find_device

__all__ = ["SWO_MODES", "LinkDapInterface", "Session", "link_buffer_bytes", "swo_start"]


def link_buffer_bytes() -> dict[int, int]:
    """フレームのリンクのバイト列のチャンネルごとに、PC に溜める上限（FrameLink の buffer_bytes）。値と根拠:
    - STREAM（ch 4）: la.STREAM_BUFFER_BYTES（消費側 LaStream の 1 回の読みの 2 倍 = 二重のバッファ。根拠はそこ）。
    - SWO（ch 2）・UART0（ch 3）: 4 MB。消費側（pyOCD の SWO の読み手・uart_read）が読まない間に溜める量の上限で、それを超えた
      フレームは捨てて数える（StreamBuffer.dropped_frames・dropped_bytes。黙って捨てない）。どちらの線も 2 Mbaud（約 200 KB/s）
      以下なので、約 20 秒読まれなくても欠けない量。値はメモリの上限を決めるだけで、欠けたかどうかは数えた事実で返る。"""
    from .la import STREAM_BUFFER_BYTES
    return {CH_SWO: 4 << 20, CH_UART0: 4 << 20, CH_STREAM: STREAM_BUFFER_BYTES}


class _SharedPyUSBv2(PyUSBv2):
    """pyOCD の CMSIS-DAP v2 バックエンドを、既に開いている usb.core.Device で動かす。

    ponytail: pyOCD 0.45 の PyUSBv2.open / close の手順を写している（探索と dispose だけを除く）。
    pyOCD を上げて壊れたら HIL（test_la_hw）で分かる。
    """

    def __init__(self, dev: usb.core.Device):
        super().__init__(dev)
        self._shared = dev

    def open(self):
        assert self.closed
        interface = usb.util.find_descriptor(self._shared.get_active_configuration(),
                                             custom_match=_match_cmsis_dap_v2_interface)
        if interface is None:
            raise DAPAccessIntf.DeviceError(f"{self.serial_number} に CMSIS-DAP v2 インターフェースが無い")
        endpoints = interface.endpoints()
        usb.util.claim_interface(self._shared, interface.bInterfaceNumber)
        self.ep_out, self.ep_in = endpoints[0], endpoints[1]
        self.ep_swo = endpoints[2] if len(endpoints) > 2 else None
        self.dev = self._shared
        self.intf_number = interface.bInterfaceNumber
        self.closed = False
        self.start_rx()

    def close(self):
        assert not self.closed
        if self.is_swo_running:
            self.stop_swo()
        self.closed = True
        self.rx_stop_event.set()
        self.read_sem.release()
        self.thread.join()
        self.rx_stop_event.clear()
        usb.util.release_interface(self.dev, self.intf_number)     # 機器は閉じない（持ち主は Session）
        self.ep_out = self.ep_in = self.ep_swo = self.dev = self.intf_number = self.thread = None
        self._read_thread_did_exit = False
        self._read_thread_exception = None


class LinkDapInterface(Interface):
    """pyOCD の CMSIS-DAP v2 インターフェースを、フレームのリンクの DAP（ch 1）と SWO（ch 2）で実装する。

    先送りするパケットの数は DAP_Info の値とリンクの上限（link.dap_packet_count。FPGA 側の受信の余裕で決まる）の
    小さい方、packet_size は 512 以下に抑える。SWO は DAP_SWO_Transport = 2（専用の EP）として ch 2 で受ける。"""

    def __init__(self, link: FrameLink, name: str = "serial"):
        super().__init__()
        self._link = link
        self.vid, self.pid = g.USB_VID, g.USB_PID
        self.vendor_name, self.product_name = "fprobe", f"fprobe {link.name} link"
        self.serial_number = name
        self.packet_size = MAX_PAYLOAD
        self.closed = True
        self._swo_running = False

    @property
    def has_swo_ep(self):
        return True

    @property
    def is_bulk(self):
        return True

    def open(self):
        # 前の利用者が時間切れで残した応答を読まない（必要なら柵で同期し直す。frame_link の説明）
        self._link.discard(CH_DAP, fence_timeout_s=self.DEFAULT_USB_TIMEOUT_S)
        self.closed = False

    def close(self):
        self._swo_running = False
        self.closed = True

    def write(self, data):
        self._link.send(CH_DAP, bytes(data))

    def read(self):
        try:
            return self._link.receive_packet(CH_DAP, self.DEFAULT_USB_TIMEOUT_S)
        except (TimeoutError, ConnectionError) as e:
            raise DAPAccessIntf.DeviceError(f"{self.serial_number} の DAP 応答が届かない") from e

    def set_packet_count(self, count):
        self.packet_count = min(count, self._link.dap_packet_count)

    def set_packet_size(self, size):
        self.packet_size = min(size, MAX_PAYLOAD)

    def start_swo(self):
        self._link.discard(CH_SWO)
        self._swo_running = True

    def stop_swo(self):
        self._swo_running = False

    def read_swo(self):
        data = bytearray()
        while self._swo_running and (chunk := self._link.read_bytes(CH_SWO, 1 << 16)):
            data += chunk
        return data


@locked
def _pipelined_read_memory_block32(self: MEM_AP, addr: int, size: int) -> Sequence[int]:
    """MEM_AP._read_memory_block32 の置き換え: 自動増加の境界（ページ）ごとの読みを全部先に積んでから結果を集める。

    pyOCD は 1 ページ読むたびに応答を待つので、往復（約 0.2 ms）がページごとに乗る（実測 1.12 MB/s）。
    先に全ページの TAR 設定と読みを積めば、プローブのパケット数（4）だけ先送りされる（実測 1.5〜1.65 MB/s）。
    転送の誤り（フォールト等）は、積んだどこで起きたか分からないので、元の方法（1 ページずつ）で読み直して
    元と同じ例外（フォールトの番地付き）を返す。"""
    assert (addr & 0x3) == 0
    try:
        results = []
        a, left = addr & self._address_mask, size
        while left > 0:
            n = min(left, (self.auto_increment_page_size - (a & (self.auto_increment_page_size - 1))) // 4)
            self.write_reg(self._reg_offset + MEM_AP_CSW, self._csw | CSW_SIZE32)
            self.write_reg(self._reg_offset + MEM_AP_TAR, a)
            results.append(self.dp.read_ap_multiple(self.address.address + self._reg_offset + MEM_AP_DRW, n,
                                                    now=False))
            left -= n
            a += n * 4
        return [w for result in results for w in result()]
    except exceptions.TransferError as error:
        self._handle_error(error, self.dp.next_access_number)
    return self._read_memory_block32(addr, size)                   # 誤りの場所を特定して報告する（元の方法）


def _attach_pipelined_reads(session: PyocdSession) -> None:
    """開いた pyOCD のセッションの MEM-AP のブロック読み出しを先送り版にする（その AP にだけ。pyOCD のクラスは変えない）。

    AP は開くとき（探索）に作られ、生成時に _read_memory_block32 を read_memory_block32 に束ねる。プローブが加速の口を
    出した AP（read_memory_block32 が _accelerated_*）はそのまま。
    ponytail: pyOCD 0.45 の MEM_AP の内部（_read_memory_block32 の束ね・_reg_offset・_csw）に頼る。pyOCD を上げて
    内部が変わったら test_dap_perf_hw の読み比べで分かる。上流に入れば消す。"""
    for ap in session.target.aps.values():
        if isinstance(ap, MEM_AP) and ap.read_memory_block32 == ap._read_memory_block32:
            ap.read_memory_block32 = MethodType(_pipelined_read_memory_block32, ap)


class _PipelinedPyocdSession(PyocdSession):
    """開いた後に、そのセッションの AP にだけ先送りの読み出しを付ける pyOCD のセッション（with でも open でも）。"""

    def open(self, init_board: bool = True) -> None:
        super().open(init_board)
        if init_board:
            _attach_pipelined_reads(self)


SWO_MODES = {"uart": DAPSWOMode.UART, "manchester": DAPSWOMode.MANCHESTER}


def swo_start(probe: CMSISDAPProbe, baud: int, mode: str = "uart") -> float:
    """SWO の受信を方式 mode（"uart" = NRZ / "manchester"）で始め、プローブが答えた実際の速さを返す。

    pyOCD の swo_start は方式が UART に決まっているので、同じ手順（止める → Transport → Mode → Baudrate → Control）を
    方式付きで送る。設定の失敗は黙って捨てずに例外にする（pyOCD の swo_start は捨てる）。
    ponytail: pyOCD 0.45 の DAPAccessCMSISDAP.swo_configure / swo_control を写している（_protocol・_interface・
    _swo_status）。pyOCD を上げて壊れたら HIL の SWO の試験で分かる。"""
    if mode not in SWO_MODES:
        raise ValueError(f"SWO の方式は {'/'.join(SWO_MODES)}: {mode!r}")
    link = probe._link
    link.flush()
    protocol = link._protocol
    protocol.swo_mode(DAPSWOMode.OFF)                     # 受信中は設定を変えられない（Mode で止める）
    protocol.swo_transport(DAPSWOTransport.NONE)
    link._swo_status = SWOStatus.DISABLED
    if protocol.swo_transport(DAPSWOTransport.DAP_SWO_EP) != 0:
        raise RuntimeError("プローブが SWO の転送（専用のエンドポイント）を断った")
    if protocol.swo_mode(SWO_MODES[mode]) != 0:
        raise RuntimeError(f"プローブが SWO の方式 {mode} を断った（この構成に無い）")
    actual = protocol.swo_baudrate(int(baud))
    if actual == 0:
        raise RuntimeError(f"プローブが SWO の速さ {baud} を断った")
    link._swo_status = SWOStatus.CONFIGURED
    protocol.swo_control(DAPSWOControl.START)
    link._interface.start_swo()
    link._swo_status = SWOStatus.RUNNING
    return float(actual)


class _ChainedDapAccess(DAPAccessCMSISDAP):
    """JTAG の接続時に、pyOCD の既定（IR 4 の 1 台）ではなく jtag_chain でチェーンを構成する。"""

    jtag_chain: list[int] | None = None

    def configure_jtag(self, devices_irlen=None):
        super().configure_jtag(devices_irlen if devices_irlen is not None else self.jtag_chain)


class Session:
    """device（fprobe.Device）と pyOCD の DAP の接続口を同じリンクで持つ。open / open_serial で作る。"""

    def __init__(self, device: Device, dap_interface: Callable[[], Interface], release: Callable[[], None],
                 link: FrameLink | None = None, address: dict | None = None):
        self.device = device
        self.link = link                    # フレームのリンクで開いたとき（UART0 は link.uart_*）。USB なら None
        # 開いた機器のつなぎ方 {"link": "usb" / "serial" / "eth", "address": USB シリアル番号 / COM ポート / IP}
        # （個体名 probe_names の鍵。シリアル番号を省いて開いたときも実際の機器のもの）
        self.address = address
        self._dap_interface = dap_interface
        self._release = release

    @classmethod
    def open(cls, serial: str | None = None) -> "Session":
        dev = find_device(serial)
        try:
            device = Device(UsbTransport(dev))
            address = {"link": "usb", "address": serial or usb.util.get_string(dev, dev.iSerialNumber)}
        except BaseException:
            usb.util.dispose_resources(dev)
            raise
        return cls(device, lambda: _SharedPyUSBv2(dev), lambda: usb.util.dispose_resources(dev), address=address)

    @classmethod
    def open_serial(cls, port: str, baud: int = DEFAULT_BAUD) -> "Session":
        """SerialLink（UART のホストリンク）の COM ポート port で開く。PC に溜める上限は link_buffer_bytes（根拠はそこ）。"""
        link = SerialLink.open(port, baud, buffer_bytes=link_buffer_bytes())
        try:
            device = Device(LinkTransport(link))
        except BaseException:
            link.close()
            raise
        return cls(device, lambda: LinkDapInterface(link, port), link.close, link,
                   address={"link": "serial", "address": port})

    @classmethod
    def open_eth(cls, host: str, port: int | None = None, *, link_up_wait_s: float, stream_tcp: bool = True,
                 stream_buffer_bytes: int | None = None) -> "Session":
        """EthLink（UDP のホストリンク）でボードの IP アドレス host に開く。PYNQ の中継には、byte チャンネルのフレームを
        1 つの UDP にまとめて送るよう頼む（eth_link.BUNDLE_BYTES。ほかのボードは無視する）。PS が DDR を直接読めるボード
        （ケイパビリティ la_stream_ps）では、ストリームを TCP でも受ける（stream_tcp = False で UDP のまま）。
        stream_buffer_bytes: TCP のストリームを PC に溜める上限（EthLink.open_stream_tcp の buffer_bytes）。None = la.STREAM_BUFFER_BYTES
        （消費側の 1 回の読みの 2 倍。根拠はそこ）。
        link_up_wait_s: リンクの確立を待つ秒数（識別の READ をこの間だけ送り直す。Device の identify_wait_s。使う側が明示する。
        送り直した回数は device.identify_resends）。"""
        from .eth_link import BUNDLE_BYTES, DEFAULT_PORT, EthLink
        from .la import STREAM_BUFFER_BYTES
        link = EthLink.open(host, port or DEFAULT_PORT, bundle_bytes=BUNDLE_BYTES, buffer_bytes=link_buffer_bytes())
        try:
            device = Device(LinkTransport(link), identify_wait_s=link_up_wait_s)
            _maybe_open_stream_tcp(link, device.capabilities(), stream_tcp,
                                   STREAM_BUFFER_BYTES if stream_buffer_bytes is None else stream_buffer_bytes)
        except BaseException:
            link.close()
            raise
        return cls(device, lambda: LinkDapInterface(link, host), link.close, link,
                   address={"link": "eth", "address": host})

    def debug(self, *, jtag_chain: list[int] | None = None, **options) -> PyocdSession:
        """pyOCD のセッション（with で開く）。options は pyOCD のセッションオプション
        （target_override、frequency、connect_mode、dap_protocol 等）。

        jtag_chain: JTAG チェーンの IR 長（装置 0 = TDO 側から。例: STM32 は [4, 5]）。指定すると JTAG で接続する
        （pyOCD 自体にはチェーンの指定が無く、IR 4 の 1 台を仮定するため）。"""
        register()
        link = _ChainedDapAccess(None, interface=self._dap_interface())
        if jtag_chain is not None:
            link.jtag_chain = list(jtag_chain)
            options.setdefault("dap_protocol", "jtag")
        return _PipelinedPyocdSession(CMSISDAPProbe(link), **options)

    def dap_access(self) -> DAPAccessCMSISDAP:
        """pyOCD のターゲットを作らずに CMSIS-DAP の層（DAP の接続口）を開いて返す。低レベルの道具・接続の診断が使う
        （fprobe.raw_dap・fprobe.jtag_chain）。閉じる（close）のは呼び手。debug() のセッションと同時には開かない
        （同じ接続口を取り合う）。"""
        link = DAPAccessCMSISDAP(None, interface=self._dap_interface())
        link.open()
        return link

    def close(self) -> None:
        """機器の口とリンクを閉じる（機器の口を閉じるのに失敗しても、リンクは必ず閉じる）。"""
        try:
            self.device.close()
        finally:
            self._release()

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _maybe_open_stream_tcp(link, caps, enabled: bool, buffer_bytes: int) -> None:
    """PS が DDR を直接読めるボード（la_stream_ps）では、ストリームを TCP でも受ける（PC に溜める上限 buffer_bytes）。ほかのボード・
    呼び手が断ったときは何もしない。"""
    if enabled and caps.la_stream_ps:
        link.open_stream_tcp(buffer_bytes=buffer_bytes)
