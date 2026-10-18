"""USB 2.0 ディスクリプタのバイト列ビルダ（リトルエンディアン）。

根拠: docs/reference/usb2-hs-device-on-ulpi.md §6（標準）、§7（MS OS 2.0）、§8（CDC-ACM）。
"""

import struct
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

# ディスクリプタ種別
DEVICE, CONFIGURATION, STRING, INTERFACE, ENDPOINT = 1, 2, 3, 4, 5
DEVICE_QUALIFIER, OTHER_SPEED_CONFIGURATION = 6, 7
INTERFACE_ASSOCIATION, BOS, DEVICE_CAPABILITY = 0x0B, 0x0F, 0x10
CS_INTERFACE = 0x24

# エンドポイント種別（bmAttributes）
CONTROL, ISOCHRONOUS, BULK, INTERRUPT = 0, 1, 2, 3

USB_2_1 = 0x0210              # BOS（MS OS 2.0）を取りに来させるには 2.01 以上
MPS_EP0 = 64

MSOS20_PLATFORM_UUID = uuid.UUID("D8DD60DF-4589-4CC7-9CD2-659D9E648A9F")
WINDOWS_8_1 = 0x06030000


def _with_length(type_: int, body: bytes) -> bytes:
    return bytes([2 + len(body), type_]) + body


def device(*, vid: int, pid: int, bcd_device: int, device_class: tuple[int, int, int],
           i_manufacturer: int, i_product: int, i_serial: int, bcd_usb: int = USB_2_1,
           num_configurations: int = 1) -> bytes:
    return _with_length(DEVICE, struct.pack(
        "<HBBBBHHHBBBB", bcd_usb, *device_class, MPS_EP0, vid, pid, bcd_device,
        i_manufacturer, i_product, i_serial, num_configurations))


def device_qualifier(device_class: tuple[int, int, int], *, bcd_usb: int = USB_2_1,
                     num_configurations: int = 1) -> bytes:
    return _with_length(DEVICE_QUALIFIER, struct.pack(
        "<HBBBBBB", bcd_usb, *device_class, MPS_EP0, num_configurations, 0))


def configuration(body: Sequence[bytes], *, max_power_ma: int, value: int = 1,
                  i_configuration: int = 0, attributes: int = 0x80,
                  type_: int = CONFIGURATION) -> bytes:
    """body はインターフェース以下のディスクリプタ列。全長とインターフェース数は自動計算する。"""
    content = b"".join(body)
    interfaces = {desc[2] for desc in split(content) if desc[1] == INTERFACE}
    total = 9 + len(content)
    return struct.pack("<BBHBBBBB", 9, type_, total, len(interfaces), value, i_configuration,
                       attributes, max_power_ma // 2) + content


def interface(number: int, alternate: int, num_endpoints: int, cls: tuple[int, int, int],
              i_interface: int = 0) -> bytes:
    return _with_length(INTERFACE, bytes([number, alternate, num_endpoints, *cls, i_interface]))


def endpoint(address: int, kind: int, max_packet: int, interval: int = 0) -> bytes:
    return _with_length(ENDPOINT, struct.pack("<BBHB", address, kind, max_packet, interval))


def interface_association(first: int, count: int, cls: tuple[int, int, int],
                          i_function: int = 0) -> bytes:
    return _with_length(INTERFACE_ASSOCIATION, bytes([first, count, *cls, i_function]))


# --- CDC 機能ディスクリプタ ---------------------------------------------------

def cdc_header(bcd_cdc: int = 0x0110) -> bytes:
    return _with_length(CS_INTERFACE, struct.pack("<BH", 0x00, bcd_cdc))


def cdc_call_management(data_interface: int, capabilities: int = 0) -> bytes:
    return _with_length(CS_INTERFACE, bytes([0x01, capabilities, data_interface]))


def cdc_acm(capabilities: int = 0x02) -> bytes:
    """0x02: Set/Get_Line_Coding, Set_Control_Line_State, Serial_State に対応。"""
    return _with_length(CS_INTERFACE, bytes([0x02, capabilities]))


def cdc_union(control_interface: int, data_interface: int) -> bytes:
    return _with_length(CS_INTERFACE, bytes([0x06, control_interface, data_interface]))


# --- 文字列 ----------------------------------------------------------------------

def string_languages(*language_ids: int) -> bytes:
    ids = language_ids or (0x0409,)
    return _with_length(STRING, b"".join(struct.pack("<H", i) for i in ids))


def string(text: str) -> bytes:
    return _with_length(STRING, text.encode("utf-16-le"))


# --- BOS と MS OS 2.0 --------------------------------------------------------------

def bos(capabilities: Sequence[bytes]) -> bytes:
    content = b"".join(capabilities)
    return struct.pack("<BBHB", 5, BOS, 5 + len(content), len(capabilities)) + content


def msos20_platform_capability(*, set_length: int, vendor_code: int) -> bytes:
    return struct.pack("<BBBB", 28, DEVICE_CAPABILITY, 0x05, 0x00) + \
        MSOS20_PLATFORM_UUID.bytes_le + struct.pack("<IHBB", WINDOWS_8_1, set_length, vendor_code, 0)


@dataclass(frozen=True)
class MsOsFunction:
    """WinUSB を割り当てるインターフェース（複合デバイスの機能）。"""
    first_interface: int
    guid: str                  # DeviceInterfaceGUIDs に登録する "{...}"


def _msos20_registry_guid(guid: str) -> bytes:
    name = "DeviceInterfaceGUIDs\0".encode("utf-16-le")
    data = f"{guid}\0\0".encode("utf-16-le")
    body = struct.pack("<HH", 0x0004, 0x0007) + struct.pack("<H", len(name)) + name + \
        struct.pack("<H", len(data)) + data
    return struct.pack("<H", 2 + len(body)) + body


def msos20_descriptor_set(functions: Sequence[MsOsFunction]) -> bytes:
    subsets = []
    for f in functions:
        compatible_id = struct.pack("<HH", 20, 0x0003) + b"WINUSB\0\0" + bytes(8)
        content = compatible_id + _msos20_registry_guid(f.guid)
        subsets.append(struct.pack("<HHBBH", 8, 0x0002, f.first_interface, 0, 8 + len(content))
                       + content)
    functions_raw = b"".join(subsets)
    config = struct.pack("<HHBBH", 8, 0x0001, 0, 0, 8 + len(functions_raw)) + functions_raw
    return struct.pack("<HHIH", 10, 0x0000, WINDOWS_8_1, 10 + len(config)) + config


# --- 解析 ----------------------------------------------------------------------

def split(raw: bytes) -> Iterator[bytes]:
    """標準形式（先頭が bLength）のディスクリプタ列を 1 つずつに分ける。"""
    i = 0
    while i < len(raw):
        length = raw[i]
        if length < 2 or i + length > len(raw):
            raise ValueError(f"不正なディスクリプタ長 {length}（位置 {i}）")
        yield raw[i:i + length]
        i += length
