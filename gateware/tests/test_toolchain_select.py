import pytest

from fprobe_gw.toolchain.ftdi import FtdiDeviceInfo
from fprobe_gw.toolchain.programmer import JtagIdentity, UnsafeTargetError, select_jtag_location

PRIMER = JtagIdentity(board="Tang Primer 20K Dock", usb_id=0x04036010, usb_serial="FACTORYAIOT_PRO",
                      ftdi_description="USB Debugger A", gowin_cable="USB Debugger A",
                      gowin_device="GW2A-18C", idcode=0x0000081B)
PRIMER_CHAIN = (1, 2)       # 実機: FACTORYAIOT_PRO は USB(1)#USB(2)


def dev(index, location, serial, description):
    return FtdiDeviceInfo(index=index, flags=0, device_type=6, usb_id=0x04036010,
                          location=location, serial=serial, description=description)


# 実機で観測した列挙（PYNQ-Z2 の A チャンネルは他プロセス使用中で空。
# 同じ説明の Tang Nano 9K がポート 1→3 に接続されている）
OBSERVED = [
    dev(0, 0, "", ""),
    dev(1, 546, "1234-tulB", "TUL B"),
    dev(2, 8497, "A", "USB Debugger A"),
    dev(3, 8498, "B", "USB Debugger B"),
    dev(4, 8481, "A", "USB Debugger A"),
    dev(5, 8482, "B", "USB Debugger B"),
]


def test_port_chain_is_decoded_from_location():
    assert OBSERVED[4].port_chain == (1, 2)
    assert OBSERVED[2].port_chain == (1, 3)
    assert OBSERVED[1].port_chain == (2,)


def test_selects_the_channel_on_the_primer_port_among_same_named_boards():
    assert select_jtag_location(OBSERVED, PRIMER, PRIMER_CHAIN) == 8481


def test_refuses_when_no_channel_is_on_the_primer_port():
    with pytest.raises(UnsafeTargetError, match="見つからない"):
        select_jtag_location(OBSERVED, PRIMER, (1, 4))


def test_never_selects_pynq_even_when_it_is_the_only_device():
    with pytest.raises(UnsafeTargetError, match="見つからない"):
        select_jtag_location([dev(0, 545, "1234-tulA", "TUL A"),
                              dev(1, 546, "1234-tulB", "TUL B")], PRIMER, (2,))


def test_refuses_when_two_channels_match():
    two = OBSERVED + [dev(6, 8481, "A", "USB Debugger A")]
    with pytest.raises(UnsafeTargetError, match="2 台"):
        select_jtag_location(two, PRIMER, PRIMER_CHAIN)


def test_refuses_matching_device_without_location():
    with pytest.raises(UnsafeTargetError, match="見つからない"):
        select_jtag_location([dev(0, 0, "A", "USB Debugger A")], PRIMER, PRIMER_CHAIN)


def test_busy_devices_are_mentioned_but_never_selected():
    with pytest.raises(UnsafeTargetError, match="使用中"):
        select_jtag_location([dev(0, 0, "", "")], PRIMER, PRIMER_CHAIN)


def test_busy_device_is_detected():
    assert OBSERVED[0].busy
    assert not OBSERVED[2].busy
