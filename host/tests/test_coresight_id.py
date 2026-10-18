"""fprobe.coresight_id: 識別子の解読・ROM テーブルの走査・部品の判別（機器は使わない）。"""

import pytest
from coresight_fakes import ARM, ST, add_component, add_rom_table, stm32f401_words

from fprobe import coresight_id as ci


def reader(words):
    def read(address):
        if address not in words and not 0xE0000000 <= address < 0xE0100000:
            raise RuntimeError(f"FAULT at 0x{address:08X}")
        return words.get(address, 0)
    return read


def test_dpidr_idcode_targetid_and_ap_idr_are_decoded():
    dp = ci.decode_dpidr(0x2BA01477)                           # STM32F4 の SW-DP
    assert dp["designer"] == "Arm" and dp["version"] == 1 and dp["partno"] == 0xBA and dp["revision"] == 2
    assert dp["adi"] == "ADIv5" and dp["valid"]
    assert ci.decode_dpidr(0x00003477)["adi"] == "ADIv6"
    jtag = ci.decode_idcode(0x4BA00477)                        # JTAG-DP（Cortex-A7 等）
    assert jtag["designer"] == "Arm" and jtag["partno"] == 0xBA00 and jtag["version"] == 4
    assert ci.decode_idcode(0x06433041)["designer"] == "STMicroelectronics"
    tid = ci.decode_targetid(0x01002927)                       # RP2040 の core 0
    assert tid["designer"] == "Raspberry Pi" and tid["partno"] == 0x1002
    ap = ci.decode_ap_idr(0x24770011)
    assert ap["kind"] == "mem-ap" and ap["bus"] == "AHB3" and ap["designer"] == "Arm"
    assert ci.decode_ap_idr(0)["present"] is False
    assert ci.decode_cpuid(0x410FC241)["core"] == "Cortex-M4"
    assert ci.decode_cpuid(0x410CC601)["core"] == "Cortex-M0+"


def test_the_rom_table_is_walked_to_the_components():
    words = stm32f401_words()
    root = ci.walk_rom_table(reader(words), 0xE00FF003)        # BASE の下位ビットは捨てる
    assert root.is_rom_table and root.designer == ST
    names = [c.name for c in root.children]
    assert names == ["SCS (Cortex-M4)", "DWT", "FPB", "ITM", "TPIU (Cortex-M4)", "ETM (Cortex-M4)"]
    assert any(c.is_scs for c in root.flatten())
    d = root.as_dict()
    assert d["designer"] == "STMicroelectronics" and len(d["children"]) == 6


def test_nested_class9_tables_and_unreadable_components():
    words: dict[int, int] = {}
    add_rom_table(words, 0x1000, [0x2000, 0x3000], cls=9)
    add_rom_table(words, 0x2000, [0x4000])
    add_component(words, 0x4000, 9, ARM, 0x906)
    words[0x3000] = 0                                          # 3000 は CIDR を読めない（例外）

    def read(address):
        if 0x3000 <= address < 0x4000:
            raise RuntimeError("FAULT")
        return words.get(address, 0)
    root = ci.walk_rom_table(read, 0x1000)
    assert [c.address for c in root.children] == [0x2000, 0x3000]
    assert root.children[0].children[0].name == "CTI"
    assert root.children[1].error == "FAULT" and root.children[1].name == "unreadable"


def test_a_self_referencing_table_does_not_loop():
    words: dict[int, int] = {}
    add_rom_table(words, 0x1000, [0x1000, 0x2000])
    add_component(words, 0x2000, 9, ARM, 0x906)
    root = ci.walk_rom_table(lambda a: words.get(a, 0), 0x1000)
    assert [c.address for c in root.children] == [0x2000]


def test_stm32_is_identified_from_dbgmcu():
    part = ci.identify_part(reader(stm32f401_words()), {ST})
    assert part.name == "STM32F401xD/E" and part.id == 0x433 and part.option_family == "f4"
    assert part.suggested_target == "stm32f401xe"
    assert part.as_dict()["candidates"] == ["stm32f401xe"]


def test_other_vendors_and_unknown_parts():
    nrf = ci.identify_part(reader({0x10000100: 0x52840}))
    assert nrf.name == "nRF52840" and nrf.suggested_target == "nrf52840"
    rp = ci.identify_part(reader({0x40000000: 0x10002927}), {ci.RASPBERRY_PI})
    assert rp.name == "RP2040" and rp.suggested_target == "rp2040"
    assert ci.identify_part(reader({0x40000000: 0x00004927})).name == "RP2350"
    f429 = ci.identify_part(reader({0xE0042000: 0x20006419}))
    assert f429.suggested_target is None and len(f429.candidates) == 4         # 複数: 利用者が選ぶ
    assert ci.identify_part(reader({})) is None


@pytest.mark.parametrize("designer", [(4, 0x3B), (0, 0x20), (2, 0x44), (9, 0x13), (5, 0x55)])
def test_designer_names(designer):
    assert ci.designer_name(designer)
