"""Primer 20K + Nucleo F401RE で、fprobe を CMSIS-DAP プローブとして pyOCD から使う（M4）。`pytest gateware --hil`。

前提（docs/boards/tang-primer-20k-dock.md「fprobe のデバッグコネクタ」）: Nucleo の SWD を J12 に配線し、CN2 の
ジャンパを外してある。fprobe は一意 ID（USB シリアル）で選び、ST-LINK には触れない。
Flash は Nucleo の既存プログラムが使っていない最後のセクタだけを書き、最後に消して戻す。
"""

import random
import time

import pytest

pyocd = pytest.importorskip("pyocd")
from pyocd.core.helpers import ConnectHelper  # noqa: E402

from tests.hil.program import program  # noqa: E402
from fprobe.pyocd_targets import register  # noqa: E402
from fprobe_gw.usb.fprobe_device import STRINGS, STR_SERIAL  # noqa: E402

pytestmark = pytest.mark.hil

PROBE_ID = STRINGS[STR_SERIAL - 1]
SRAM = 0x20004000                      # 先頭 16 KB は書き込みアルゴリズムが使うので避ける
LAST_SECTOR = 0x08060000               # セクタ 7（128 KB）
SPEED_TARGET = 1.5e6                   # バイト/秒（全体設計 §1.2）


@pytest.fixture(scope="module")
def session():
    program("fprobe")
    register()
    with ConnectHelper.session_with_chosen_probe(unique_id=PROBE_ID, target_override="stm32f401xe",
                                                 frequency=8_000_000, blocking=False,
                                                 options={"connect_mode": "attach", "reset_type": "hw"}) as s:
        yield s


def test_identifies_the_target(session):
    target = session.target
    assert target.read32(0xE000ED00) == 0x410FC241                 # CPUID: Cortex-M4 r0p1
    assert target.read32(0xE0042000) & 0xFFF == 0x433               # DBGMCU: STM32F401xD/E


def test_halt_registers_and_resume(session):
    target = session.target
    target.halt()
    assert target.get_state() == target.State.HALTED
    pc = target.read_core_register("pc")
    assert 0x08000000 <= pc < 0x08080000
    target.resume()
    assert target.get_state() == target.State.RUNNING


@pytest.mark.parametrize("clock_hz", [8_000_000, 25_000_000])
def test_sram_round_trip_and_speed(session, clock_hz):
    # 25 MHz（sys 100.29 MHz の半周期 2）はジャンパ線の反射で SWCLK が二重に数えられやすい（読みが 1 ビットずれる）。
    # ピンの出力電流を 4 mA に抑えて防いでいる。その回帰検査を兼ねる
    target = session.target
    session.probe.set_clock(clock_hz)
    target.halt()
    rng = random.Random(1)
    try:
        for _ in range(3):
            data = rng.randbytes(32 * 1024)
            start = time.perf_counter()
            target.write_memory_block8(SRAM, data)
            write_rate = len(data) / (time.perf_counter() - start)
            start = time.perf_counter()
            back = bytes(target.read_memory_block8(SRAM, len(data)))
            read_rate = len(data) / (time.perf_counter() - start)
            assert back == data
    finally:
        session.probe.set_clock(8_000_000)
        target.resume()
    print(f"{clock_hz / 1e6:.0f} MHz: SRAM 書き込み {write_rate / 1e3:.0f} KB/s、読み出し {read_rate / 1e3:.0f} KB/s")


def test_flash_program_verify_and_restore(session):
    target = session.target
    flash = target.memory_map.get_boot_memory()
    assert flash is not None
    rng = random.Random(2)
    data = bytes(rng.randrange(256) for _ in range(4096))
    # 書き込みアルゴリズムは既定のクロック設定を前提にするので、動作中のプログラムを止めるのではなく
    # nRESET でリセットして止める（pyOCD の通常の書き込み手順と同じ）
    target.reset_and_halt()
    from pyocd.flash.eraser import FlashEraser
    from pyocd.flash.loader import MemoryLoader
    loader = MemoryLoader(session, chip_erase="sector")
    loader.add_data(LAST_SECTOR, data)
    loader.commit()
    assert bytes(target.read_memory_block8(LAST_SECTOR, len(data))) == data
    FlashEraser(session, FlashEraser.Mode.SECTOR).erase([f"{LAST_SECTOR:#x}"])
    assert bytes(target.read_memory_block8(LAST_SECTOR, 64)) == b"\xff" * 64
    target.reset()                                             # 既存プログラムを動かし直す
