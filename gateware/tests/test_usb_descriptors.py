"""USB ディスクリプタ生成のテスト（値の根拠: docs/reference/usb2-hs-device-on-ulpi.md §6-8）。"""

import struct

import pytest

from fprobe_gw.usb import descriptors as d
from fprobe_gw.usb.fprobe_device import FPROBE_DEVICE
from fprobe_gw.usb.vhdl_rom import render_package


def test_device_descriptor_layout():
    raw = d.device(vid=0x1209, pid=0x0001, bcd_device=0x0100, device_class=(0xEF, 0x02, 0x01),
                   i_manufacturer=1, i_product=2, i_serial=3)
    assert raw == bytes.fromhex("12 01 1002 EF 02 01 40 0912 0100 0001 01 02 03 01".replace(" ", ""))


def test_endpoint_and_interface():
    assert d.endpoint(0x81, d.BULK, 512) == bytes.fromhex("0705 81 02 0002 00".replace(" ", ""))
    assert d.interface(0, 0, 2, (0xFF, 0, 0), 4) == bytes.fromhex("0904 00 00 02 FF 00 00 04".replace(" ", ""))


def test_configuration_computes_total_length_and_interface_count():
    body = [d.interface(0, 0, 0, (0xFF, 0, 0)), d.interface(1, 0, 0, (0xFF, 0, 0))]
    raw = d.configuration(body, max_power_ma=100)
    assert raw[:2] == bytes([9, 2])
    assert struct.unpack_from("<H", raw, 2)[0] == len(raw) == 9 + 18
    assert raw[4] == 2                      # bNumInterfaces
    assert raw[7] == 0x80 and raw[8] == 50  # バス給電, 100 mA


def test_strings():
    assert d.string_languages() == bytes.fromhex("04030904")
    assert d.string("ab") == bytes([6, 3, ord("a"), 0, ord("b"), 0])


def test_device_qualifier():
    assert d.device_qualifier((0xEF, 0x02, 0x01)) == bytes.fromhex("0A06 1002 EF 02 01 40 01 00".replace(" ", ""))


def test_msos20_platform_capability():
    cap = d.msos20_platform_capability(set_length=0x00B2, vendor_code=0x20)
    assert len(cap) == 28
    assert cap[:4] == bytes.fromhex("1C100500")
    assert cap[4:20] == bytes.fromhex("DF60DDD889 45C74C9CD2659D9E648A9F".replace(" ", ""))
    assert cap[20:24] == bytes.fromhex("00000306")
    assert cap[24:] == bytes.fromhex("B2002000")


def test_msos20_set_lengths_are_consistent():
    raw = d.msos20_descriptor_set([d.MsOsFunction(first_interface=0, guid="{CDB3B5AD-293B-4663-AA36-1AAE46463776}")])
    assert struct.unpack_from("<H", raw, 8)[0] == len(raw)              # 全長
    config_len = struct.unpack_from("<H", raw, 10 + 6)[0]
    assert config_len == len(raw) - 10                                  # 構成サブセット長
    function_len = struct.unpack_from("<H", raw, 18 + 6)[0]
    assert function_len == 8 + 20 + 132                                 # 機能サブセット + 互換 ID + 属性
    assert b"WINUSB\x00\x00" in raw
    assert "DeviceInterfaceGUIDs".encode("utf-16-le") in raw


class TestFprobeDevice:
    def test_hs_and_fs_configurations_differ_only_in_bulk_size(self):
        hs = FPROBE_DEVICE.configuration(high_speed=True)
        fs = FPROBE_DEVICE.configuration(high_speed=False)
        assert len(hs) == len(fs)
        assert b"\x07\x05\x01\x02\x00\x02" in hs and b"\x07\x05\x01\x02\x40\x00" in fs

    def test_other_speed_configuration_uses_type_7(self):
        other = FPROBE_DEVICE.other_speed_configuration(high_speed=True)
        assert other[1] == 7
        assert other[2:] == FPROBE_DEVICE.configuration(high_speed=False)[2:]

    def test_bos_announces_msos20_set_with_correct_length(self):
        bos = FPROBE_DEVICE.bos()
        assert struct.unpack_from("<H", bos, 2)[0] == len(bos)
        assert struct.unpack_from("<H", bos, 5 + 24)[0] == len(FPROBE_DEVICE.msos20_set())

    def test_device_descriptor_requests_bos_capable_usb_2_1(self):
        assert FPROBE_DEVICE.device_descriptor()[2:4] == b"\x10\x02"

    def test_interface_numbers_are_contiguous(self):
        raw = FPROBE_DEVICE.configuration(high_speed=True)
        numbers = [desc[2] for desc in d.split(raw) if desc[1] == d.INTERFACE and desc[3] == 0]
        assert numbers == list(range(raw[4]))


def test_interface_endpoint_map_is_derived_from_configuration():
    from fprobe_gw.usb.vhdl_rom import interface_endpoint_masks
    masks = interface_endpoint_masks(FPROBE_DEVICE.configuration(high_speed=True))
    # IF0: EP2（DAP）と EP3（SWO）、IF1: EP1（OUT/IN）と EP6、IF2: EP5（通知）、IF3: EP4（データ）
    assert masks == [(1 << 2) | (1 << 3), (1 << 1) | (1 << 6), 1 << 5, 1 << 4]


def test_endpoint_direction_masks_include_ep0():
    from fprobe_gw.usb.vhdl_rom import endpoint_masks
    out_mask, in_mask = endpoint_masks(FPROBE_DEVICE.configuration(high_speed=True))
    assert out_mask == (1 << 0) | (1 << 1) | (1 << 2) | (1 << 4)
    assert in_mask == (1 << 0) | (1 << 1) | (1 << 2) | (1 << 3) | (1 << 4) | (1 << 5) | (1 << 6)


def test_package_exports_named_constants():
    text = render_package(FPROBE_DEVICE)
    for line in ("constant EP_CONTROL : natural := 1;", "constant EP_STREAM : natural := 6;",
                 "constant EP_UART_DATA : natural := 4;", "constant EP_UART_NOTIFY : natural := 5;",
                 "constant EP_DAP : natural := 2;", "constant EP_SWO : natural := 3;",
                 "constant CDC_COMM_INTERFACE : natural := 2;",
                 'constant OUT_ENDPOINTS : std_logic_vector(15 downto 0) := x"0017";',
                 'constant IN_ENDPOINTS : std_logic_vector(15 downto 0) := x"007F";'):
        assert line in text, line
