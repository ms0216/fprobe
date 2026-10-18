"""fprobe.decode.onewire: 1-Wire の発生器 → 解読の往復（リセット・ROM コマンド・ROM・検索・オーバードライブ）。"""

import pytest

from fprobe.decode import decode
from fprobe.decode.onewire import decode_onewire, onewire_crc8
from fprobe.decode.synth import OneWireBus, Wave, onewire_rom

CH = 7
ROM = onewire_rom(0x28, 0x0000_0567_89AB)


def bus(rate=2e6):
    w = Wave(channels=8, sample_rate_hz=rate, idle={CH: 1})
    return w, OneWireBus(w, CH, t=20e-6)


def finish(w, b):
    return w.build(end_s=b.t + 100e-6)


def test_crc8_known_value():
    assert onewire_crc8(b"\x02\x1c\xb8\x01\x00\x00\x00") == 0xA2      # Maxim AN27 の例
    assert onewire_crc8(ROM.to_bytes(8, "little")) == 0                 # CRC を含めると 0


def test_read_rom_then_function_bytes():
    w, b = bus()
    b.reset()
    b.rom_command(0x33)
    for byte in ROM.to_bytes(8, "little"):
        b.read_byte(byte)
    b.write_byte(0x44)                                                   # CONVERT T
    b.reset(presence=False)
    d = decode_onewire(finish(w, b), CH)
    resets = d.of("reset")
    assert [r.fields["presence"] for r in resets] == [True, False] and len(d.of("presence")) == 1
    (cmd,) = d.of("rom_command")
    assert (cmd.value, cmd.text) == (0x33, "READ ROM")
    (rom,) = d.of("rom")
    assert rom.value == ROM and rom.fields["family"] == 0x28 and rom.fields["crc_ok"] and rom.error is None
    assert [a.value for a in d.of("data")] == [0x44]
    assert d.errors() == []


def test_match_rom_with_a_bad_crc_and_skip_rom():
    w, b = bus()
    b.reset()
    b.rom_command(0x55)
    b.write_byte(0x28)
    for byte in (1, 2, 3, 4, 5, 6, 0x99):
        b.write_byte(byte)
    b.reset()
    b.rom_command(0xCC)
    b.write_byte(0xBE)
    b.read_byte(0x50)
    b.read_byte(0x05)
    d = decode_onewire(finish(w, b), CH)
    rom = d.of("rom")[0]
    assert rom.error == "crc" and not rom.fields["crc_ok"]
    assert [a.text for a in d.of("rom_command")] == ["MATCH ROM", "SKIP ROM"]
    assert [a.value for a in d.of("data")] == [0xBE, 0x50, 0x05]


def test_search_rom_with_two_devices():
    other = onewire_rom(0x28, 0x1111_2222_3333)
    w, b = bus()
    b.reset()
    b.rom_command(0xF0)
    b.search(ROM, others=[other])
    d = decode_onewire(finish(w, b), CH)
    (rom,) = d.of("rom")
    assert rom.value == ROM and rom.fields["search"] and rom.error is None


def test_overdrive_switch_and_back():
    w, b = bus(rate=10e6)
    b.reset()
    b.rom_command(0x3C)                                                 # 以降はオーバードライブ
    b.write_byte(0x4E)
    b.reset()                                                           # OD のリセット
    b.rom_command(0xCC)
    b.read_byte(0xA5)
    b.reset(overdrive=False)                                            # 標準のリセットで戻る
    b.rom_command(0xCC)
    d = decode_onewire(finish(w, b), CH)
    assert [r.fields["overdrive"] for r in d.of("reset")] == [False, True, False]
    assert all(r.fields["presence"] for r in d.of("reset"))
    assert [a.value for a in d.of("data")] == [0x4E, 0xA5]
    assert [a.value for a in d.of("rom_command")] == [0x3C, 0xCC, 0xCC]
    assert d.errors() == []


def test_timing_error_and_truncation_and_by_name():
    w, b = bus()
    b.reset()
    w.set(CH, 0, b.t)
    w.set(CH, 1, b.t + 250e-6)                                          # ビットにもリセットにもならない長さ
    b.t += 300e-6
    b.write_bit(1)
    w.set(CH, 0, b.t)
    d = decode("onewire", w.build(end_s=b.t + 10e-6), channel=CH)
    assert [a.error for a in d.errors()] == ["timing", "truncated"]
    with pytest.raises(TypeError):
        decode_onewire(finish(w, b))                                    # チャンネルが要る
