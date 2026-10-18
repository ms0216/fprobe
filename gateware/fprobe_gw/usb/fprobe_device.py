"""fprobe の USB 複合デバイス構成。

  IF0   CMSIS-DAP v2（ベンダー, WinUSB）: EP2 OUT / EP2 IN（DAP）, EP3 IN（SWO）
  IF1   fprobe 拡張（ベンダー, WinUSB）: EP1 OUT / EP1 IN（CONTROL）, EP6 IN（ストリーム）
  IF2-3 CDC-ACM（IAD）: EP5 IN（通知, インタラプト）, EP4 OUT / EP4 IN（データ）

CMSIS-DAP のツールはインターフェース文字列（ツールによっては製品文字列）の "CMSIS-DAP" で探す。
"""

from dataclasses import dataclass

from . import descriptors as d

VID, PID = 0x1209, 0x0001          # pid.codes の試験用。公開時に正式 PID を取得する
BCD_DEVICE = 0x0002                # MS OS 記述子を変えたら上げる（Windows がキャッシュするため）
MSOS20_VENDOR_CODE = 0x20
FPROBE_INTERFACE_GUID = "{8E7F3C2A-5B61-4D9E-9C1A-3F6B2E4D7A10}"
CMSIS_DAP_V2_GUID = "{CDB3B5AD-293B-4663-AA36-1AAE46463776}"      # CMSIS-DAP v2 の標準
DEVICE_CLASS_IAD = (0xEF, 0x02, 0x01)

STRINGS = ("fprobe project", "fprobe CMSIS-DAP", "0001", "fprobe control", "fprobe UART")
STR_MANUFACTURER, STR_PRODUCT, STR_SERIAL, STR_CONTROL, STR_UART = 1, 2, 3, 4, 5
STR_DAP = STR_PRODUCT              # CMSIS-DAP の IF 文字列は製品文字列と同じ

# エンドポイント番号・インターフェース番号（usb_descriptors_pkg に生成してゲートウェアと共有する）
EP_CONTROL = 1      # OUT/IN
EP_DAP = 2          # OUT/IN
EP_SWO = 3          # IN
EP_STREAM = 6       # IN
EP_UART_DATA = 4    # OUT/IN
EP_UART_NOTIFY = 5  # IN
DAP_INTERFACE = 0
CONTROL_INTERFACE = 1
CDC_COMM_INTERFACE = 2
CDC_DATA_INTERFACE = 3


@dataclass(frozen=True)
class FprobeUsbDevice:
    def gateware_constants(self) -> dict[str, int]:
        """VHDL パッケージに出す名前付き定数。"""
        return {"EP_CONTROL": EP_CONTROL, "EP_DAP": EP_DAP, "EP_SWO": EP_SWO, "EP_STREAM": EP_STREAM,
                "EP_UART_DATA": EP_UART_DATA, "EP_UART_NOTIFY": EP_UART_NOTIFY,
                "CDC_COMM_INTERFACE": CDC_COMM_INTERFACE}

    def device_descriptor(self) -> bytes:
        return d.device(vid=VID, pid=PID, bcd_device=BCD_DEVICE, device_class=DEVICE_CLASS_IAD,
                        i_manufacturer=STR_MANUFACTURER, i_product=STR_PRODUCT,
                        i_serial=STR_SERIAL)

    def device_qualifier(self) -> bytes:
        return d.device_qualifier(DEVICE_CLASS_IAD)

    def _body(self, *, high_speed: bool) -> list[bytes]:
        bulk = 512 if high_speed else 64
        notify_interval = 8 if high_speed else 16      # どちらも 16 ms（HS は 2^(n-1) マイクロフレーム）
        return [
            # CMSIS-DAP v2: DAP の OUT・IN、SWO の IN の順（仕様で決まっている）
            d.interface(DAP_INTERFACE, 0, 3, (0xFF, 0x00, 0x00), STR_DAP),
            d.endpoint(EP_DAP, d.BULK, bulk),
            d.endpoint(0x80 | EP_DAP, d.BULK, bulk),
            d.endpoint(0x80 | EP_SWO, d.BULK, bulk),
            d.interface(CONTROL_INTERFACE, 0, 3, (0xFF, 0x00, 0x00), STR_CONTROL),
            d.endpoint(EP_CONTROL, d.BULK, bulk),
            d.endpoint(0x80 | EP_CONTROL, d.BULK, bulk),
            d.endpoint(0x80 | EP_STREAM, d.BULK, bulk),
            d.interface_association(CDC_COMM_INTERFACE, 2, (0x02, 0x02, 0x01), STR_UART),
            d.interface(CDC_COMM_INTERFACE, 0, 1, (0x02, 0x02, 0x01), STR_UART),
            d.cdc_header(),
            d.cdc_call_management(data_interface=CDC_DATA_INTERFACE),
            d.cdc_acm(),
            d.cdc_union(control_interface=CDC_COMM_INTERFACE, data_interface=CDC_DATA_INTERFACE),
            d.endpoint(0x80 | EP_UART_NOTIFY, d.INTERRUPT, 16, notify_interval),
            d.interface(CDC_DATA_INTERFACE, 0, 2, (0x0A, 0x00, 0x00)),
            d.endpoint(EP_UART_DATA, d.BULK, bulk),
            d.endpoint(0x80 | EP_UART_DATA, d.BULK, bulk),
        ]

    def configuration(self, *, high_speed: bool) -> bytes:
        return d.configuration(self._body(high_speed=high_speed), max_power_ma=500)

    def other_speed_configuration(self, *, high_speed: bool) -> bytes:
        """現在と逆の速度での構成（種別 7）。"""
        return d.configuration(self._body(high_speed=not high_speed), max_power_ma=500,
                               type_=d.OTHER_SPEED_CONFIGURATION)

    def strings(self) -> list[bytes]:
        return [d.string_languages(), *(d.string(s) for s in STRINGS)]

    def msos20_set(self) -> bytes:
        return d.msos20_descriptor_set([d.MsOsFunction(DAP_INTERFACE, CMSIS_DAP_V2_GUID),
                                        d.MsOsFunction(CONTROL_INTERFACE, FPROBE_INTERFACE_GUID)])

    def bos(self) -> bytes:
        return d.bos([d.msos20_platform_capability(set_length=len(self.msos20_set()),
                                                   vendor_code=MSOS20_VENDOR_CODE)])


FPROBE_DEVICE = FprobeUsbDevice()
