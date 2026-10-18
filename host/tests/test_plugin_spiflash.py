"""プラグイン fprobe.plugins.spiflash: 本体の汎用の API（spi.transfer・gpio.*）だけで SPI NOR Flash を読み書きする。
偽の機器の端子に偽の SPI Flash（spi_fakes.SpiNorFlash）をつなぐ。"""

import random
from types import SimpleNamespace

import pytest

from fprobe.mcp_server import Probe
from fprobe.plugins import ProbeCaller
from fprobe.plugins.spiflash import EraseType, SpiFlash, SpiFlashError
from spi_fakes import GpioDevice, SpiNorFlash


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("fprobe.plugins.spiflash.time.sleep", lambda s: None)


def connect(chip: SpiNorFlash, lag=1, **kwargs):
    device = GpioDevice([chip], lag=lag)
    probe = Probe("unused")
    probe._session = SimpleNamespace(device=device)
    return SpiFlash(ProbeCaller(probe), **kwargs), device


def test_identifies_the_chip_and_reads_the_geometry_from_sfdp():
    flash, _ = connect(SpiNorFlash(size=1 << 20, jedec=(0xEF, 0x40, 0x14)))
    with flash:
        ident = flash.jedec_id()
        assert (ident.manufacturer, ident.memory_type, ident.capacity) == (0xEF, 0x40, 0x14)
        geo = flash.geometry
        assert (geo.size, geo.page_size, geo.address_bytes, geo.source) == (1 << 20, 256, 3, "sfdp")
        assert geo.erase_types == (EraseType(65536, 0xD8), EraseType(32768, 0x52), EraseType(4096, 0x20))
        assert geo.quad_enable == 0b101


def test_geometry_falls_back_to_the_jedec_capacity_without_sfdp():
    flash, _ = connect(SpiNorFlash(size=1 << 19, jedec=(0xC8, 0x40, 0x13), sfdp=False))
    with flash:
        geo = flash.geometry
        assert (geo.size, geo.page_size, geo.source) == (1 << 19, 256, "jedec")
        assert [e.size for e in geo.erase_types] == [65536, 32768, 4096]


def test_no_chip_is_reported():
    flash, device = connect(SpiNorFlash())
    device.slaves.clear()                                             # 何もつながっていない: MISO はプルアップ
    with pytest.raises(SpiFlashError, match="応答が無い"):
        flash.open()
    assert device.enable == 0                                         # 端子は離した


@pytest.mark.parametrize("lag,mode", [(0, 0), (1, 0), (1, 3)])
def test_read_returns_the_memory(lag, mode):
    chip = SpiNorFlash(size=1 << 16, mode=mode)
    rng = random.Random(1)
    chip.memory[:] = bytes(rng.getrandbits(8) for _ in range(len(chip.memory)))
    flash, _ = connect(chip, lag=lag, mode=mode, chunk=1000)
    with flash:
        assert flash.read(0x1234, 3000) == bytes(chip.memory[0x1234:0x1234 + 3000])


def test_program_splits_at_pages_and_skips_blank_pages():
    chip = SpiNorFlash(size=1 << 16)
    flash, _ = connect(chip)
    data = bytes(range(256)) * 2 + b"\xFF" * 256 + b"\x00\x11"
    with flash:
        flash.program(0x0F0, data)
    assert bytes(chip.memory[0x0F0:0x0F0 + len(data)]) == data
    assert chip.commands.count(0x02) == 4                            # 0xF0〜 の 5 ページのうち全部 0xFF の 1 つを飛ばす


def test_erase_uses_the_largest_aligned_type():
    chip = SpiNorFlash(size=1 << 20)
    chip.memory[:] = b"\x00" * len(chip.memory)
    flash, _ = connect(chip)
    with flash:
        flash.erase(0x0F000, 0x1A000)                                # 4K + 64K + 32K + 4K
        with pytest.raises(ValueError, match="4096"):
            flash.erase(0x100, 0x1000)
    erased = [c for c in chip.commands if c in (0x20, 0x52, 0xD8)]
    assert erased == [0x20, 0xD8, 0x52, 0x20]
    assert chip.memory[0x0F000:0x29000] == b"\xFF" * 0x1A000
    assert chip.memory[0x0EFFF] == 0 and chip.memory[0x29000] == 0


def test_erase_chip():
    chip = SpiNorFlash(size=1 << 16)
    chip.memory[:] = b"\x55" * len(chip.memory)
    flash, _ = connect(chip)
    with flash:
        flash.erase_chip()
    assert chip.memory == b"\xFF" * len(chip.memory) and 0xC7 in chip.commands


def test_write_keeps_the_rest_of_partly_written_sectors_and_verifies():
    chip = SpiNorFlash(size=1 << 16)
    rng = random.Random(2)
    old = bytes(rng.getrandbits(8) for _ in range(len(chip.memory)))
    chip.memory[:] = old
    flash, _ = connect(chip)
    data = bytes(rng.getrandbits(8) for _ in range(5000))
    with flash:
        result = flash.write(0x0800, data)
    expected = bytearray(old)
    expected[0x0800:0x0800 + len(data)] = data
    assert bytes(chip.memory) == bytes(expected)
    assert result["verified"] and result["erased"] == 0x2000 and result["bytes"] == 5000
    assert {"erase_s", "program_s", "verify_s", "kib_per_s"} <= set(result)


def test_verify_reports_the_first_difference():
    chip = SpiNorFlash(size=1 << 16)
    flash, _ = connect(chip)
    with flash:
        assert flash.verify(0x10, b"\xFF" * 64) is None
        chip.memory[0x30] = 0x12
        assert flash.verify(0x10, b"\xFF" * 64) == 0x30


def test_protected_chip_fails_verification_and_unprotect_clears_only_the_protection_bits():
    chip = SpiNorFlash(size=1 << 16, qer=0b101)
    chip.sr1, chip.sr2 = 0x9C, 0x02                                   # SRP0・BP0〜2、SR2 の QE
    flash, _ = connect(chip)
    with flash:
        with pytest.raises(SpiFlashError, match="保護"):
            flash.write(0, b"\x00" * 16)
        result = flash.unprotect()
        assert result["before"] == [0x9C, 0x02] and result["after"] == [0x00, 0x02] and result["unprotected"]
        flash.write(0, b"\x00" * 16)
    assert chip.memory[:16] == b"\x00" * 16


@pytest.mark.parametrize("qer,sr1,sr2,after", [
    (0b010, 0x7C, 0x00, [0x40, None]),                               # Macronix: bit 6 は QE（残す。35h は送らない）
    (0b001, 0x0C, 0x02, [0x00, 0x02]),                               # 1 バイトの書き込みで SR2 が消える型: 2 バイトで書く
    (0b000, 0x7C, 0x00, [0x00, None]),                               # QE なし: SR1 だけ
])
def test_unprotect_follows_the_quad_enable_rule(qer, sr1, sr2, after):
    chip = SpiNorFlash(size=1 << 16, qer=qer, jedec=(0xC2, 0x20, 0x10))
    chip.sr1, chip.sr2 = sr1, sr2
    flash, _ = connect(chip)
    with flash:
        result = flash.unprotect()
    assert result["after"] == after and result["unprotected"]
    assert chip.sr1 == after[0] and (after[1] is None or chip.sr2 == after[1])
    assert (0x35 in chip.commands) == (after[1] is not None)          # 35h は SR2 を読む型だけに送る（Macronix では QPI に入る）


def test_large_chips_use_four_byte_addresses():
    chip = SpiNorFlash(size=1 << 25, jedec=(0xEF, 0x40, 0x19))
    chip.memory[0x1800000:0x1800004] = b"\x01\x02\x03\x04"
    flash, _ = connect(chip)
    with flash:
        assert flash.geometry.address_bytes == 4
        assert flash.read(0x1800000, 4) == b"\x01\x02\x03\x04"
        flash.program(0x1800100, b"\xAA")
    assert chip.memory[0x1800100] == 0xAA
    assert chip.commands[-1] == 0xE9 and not chip.four_byte           # 閉じるとき 3 バイトの番地に戻す


def test_hold_pins_are_driven_during_the_session_and_released_after():
    flash, device = connect(SpiNorFlash(size=1 << 16), hold={2: 0, 4: 1})   # nRESET を Low、WP# を High
    with flash:
        assert device.enable & 0b10100 == 0b10100 and device.drive & 0b10100 == 0b10000
    assert device.enable == 0
