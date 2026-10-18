"""Flash の操作（U03。fprobe.flash・fprobe.probe_flash）の実機試験: Nucleo F401RE。

Nucleo の Flash を全部書き換えることはしない（既存のファームを壊さない）。書く・消すのは最後のセクタ
（0x08060000、128 KiB。F401RE の Flash は 16 KiB × 4 + 64 KiB + 128 KiB × 3）だけで、試験の前に中身を読んで
保存し、終わったら書き戻す。読み出し・検証は Flash の全体。

速度（書き込み・読み出し・RAM）は結果を表示する（-s で見る）。本流の担当が記録して公表する。
"""

import time

import pytest

from fprobe.flash import FlashMemory
from fprobe.mcp_server import Probe
from tests.hil.program import SERIAL, program

pytestmark = pytest.mark.hil

FLASH, FLASH_SIZE = 0x08000000, 0x80000
LAST = 0x08060000                        # 最後のセクタ（試験で書いてよい唯一の場所）
LAST_SIZE = 0x20000
F401_SECTORS = [{"start": 0x08000000, "count": 4, "size": 0x4000}, {"start": 0x08010000, "count": 1, "size": 0x10000},
                {"start": 0x08020000, "count": 3, "size": 0x20000}]


@pytest.fixture(scope="module")
def probe():
    program("fprobe")
    p = Probe(SERIAL)
    p.target_connect("stm32f401xe", frequency_hz=4_000_000)
    yield p
    p.target_reset()
    p.close()


@pytest.fixture
def last_sector(probe, tmp_path):
    """最後のセクタの元の中身を保存し、試験の後に書き戻す（中身が同じページは書かない）。"""
    saved = tmp_path / "last_sector.bin"
    probe.flash_read(str(saved), address=LAST, length=LAST_SIZE)
    yield saved
    probe.flash(str(saved), address=LAST, reset=False)
    assert probe.flash_verify(str(saved), address=LAST)["match"]


def test_regions_show_the_f401_sector_layout(probe):
    # pyOCD の定義（STM32F412xE を流用）はセクタの大きさごとに 3 つの領域
    regions = [r for r in probe.flash_regions()["regions"] if FLASH <= r["start"] < FLASH + FLASH_SIZE]
    assert [s for r in regions for s in r["sectors"]] == F401_SECTORS, regions
    assert sum(r["size"] for r in regions) == FLASH_SIZE and regions[0]["boot"]
    assert all(r["algorithm"] == "builtin" and r["erased_value"] == 0xFF for r in regions)


def test_whole_flash_reads_back_and_verifies_against_the_file(probe, tmp_path):
    out = probe.flash_read(str(tmp_path / "all.bin"))
    assert out["address"] == FLASH and out["bytes"] == FLASH_SIZE, out
    print(f"\nFlash の読み出し: {out['read']['bytes_per_s'] / 1024:.1f} KiB/s（{out['read']['seconds']:.3f} s）")
    assert probe.flash_verify(str(tmp_path / "all.bin"), address=FLASH)["match"]
    probe.flash_read(str(tmp_path / "head.hex"), address=FLASH, length=0x400)
    assert probe.flash_verify(str(tmp_path / "head.hex"))["match"]


def test_benchmark_on_the_last_sector_restores_everything(probe, last_sector):
    probe.target_resume()
    out = probe.flash_benchmark(ram_length=0x4000, read_length=0x10000, program_address=LAST, program_length=LAST_SIZE)
    prog = out["flash_program"]
    assert prog["verified"] and prog["restored"], out
    assert out["ram"]["verified"], out
    assert probe.target_status()["state"] == "RUNNING"                    # 走っていたので走らせ直す
    print(f"\nSWD {out['frequency_hz'] / 1e6:.1f} MHz:"
          f" RAM 書き {out['ram']['write']['bytes_per_s'] / 1024:.1f} KiB/s・読み {out['ram']['read']['bytes_per_s'] / 1024:.1f} KiB/s,"
          f" Flash 読み {out['flash_read']['bytes_per_s'] / 1024:.1f} KiB/s,"
          f" 消去 {prog['erase']['bytes_per_s'] / 1024:.1f} KiB/s・書き込み {prog['program']['bytes_per_s'] / 1024:.1f} KiB/s"
          f"（全体 {prog['total']['seconds']:.2f} s / 128 KiB）")


def test_program_erase_and_verify_on_the_last_sector(probe, last_sector, tmp_path):
    sent = []
    probe.attach_publisher(lambda topic, data: sent.append(data))
    try:
        image = tmp_path / "pattern.bin"
        image.write_bytes(bytes((i * 13 + 7) & 0xFF for i in range(0x1000)))
        out = probe.flash(str(image), address=LAST, erase="sector", reset=False)
        assert out["verify"]["match"] and out["program"]["erase_method"] == "sector", out
        assert {d["phase"] for d in sent} >= {"erase", "program", "verify"} and sent[-1]["fraction"] == 1.0
        assert probe.flash_verify(str(image), address=LAST)["match"]
        erased = probe.flash_erase(address=LAST, length=1)
        assert erased["start"] == LAST and erased["end"] == LAST + LAST_SIZE and erased["sectors"] == 1, erased
        assert bytes.fromhex(probe.read_memory(LAST, 16)) == b"\xFF" * 16
        mismatch = probe.flash_verify(str(image), address=LAST)
        assert not mismatch["match"] and mismatch["differences"][0]["address"] == LAST
    finally:
        probe.attach_publisher(None)


def test_patch_keeps_the_rest_of_the_sector_and_the_halted_program(probe, last_sector):
    # U28（Flash ブレークポイント）が使う口: 数バイトの書き換え。止めたプログラムのレジスタとアルゴリズムが使う RAM を戻す
    probe.target_halt()
    with probe._lock:
        target = probe.target
        memory = FlashMemory(target)
        names = ["r0", "r1", "r2", "r3", "r12", "lr", "pc", "xpsr", "msp", "psp", "control", "primask"]
        regs = target.read_core_registers_raw(names)
        ram = bytes(target.read_memory_block8(0x20000000, 0x5000))       # 内蔵アルゴリズムが使う（命令・バッファ・CRC 解析）
        before = bytes(target.read_memory_block8(LAST, 0x200))
        started = time.monotonic()
        report = memory.patch([(LAST + 0x100, b"\x00\xBE")])               # BKPT #0
        seconds = time.monotonic() - started
        after = bytes(target.read_memory_block8(LAST, 0x200))
        assert after[0x100:0x102] == b"\x00\xBE"
        assert after[:0x100] == before[:0x100] and after[0x102:] == before[0x102:]
        assert target.read_core_registers_raw(names) == regs
        assert bytes(target.read_memory_block8(0x20000000, 0x5000)) == ram
        assert target.get_state().name == "HALTED"
    print(f"\n2 バイトの書き換え（128 KiB のセクタを読み・消し・書き戻す）: {seconds:.2f} s, {report.as_dict()}")
