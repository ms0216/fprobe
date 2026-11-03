"""実機: Primer の JTAG（PJTAG）で Zynq のメモリを読む。JTAG のクロックを上げても、読んだ値が 1 MHz の基準と一致するか（値が化けないか）。

FPROBE_ZYNQ_JTAG=1 で走る。読むのはカーネルのコード（走っていても変わらない）の 64 KB を、AHB-AP（AP 0）から。要求したクロックで、
実際の TCK は丸められる（sys ÷ (2 × 半周期)。2026-11-01 より前の回路は sys ÷ (2 × 半周期 + 1)）。**ここでの合格は「この配線・この個体で、基準と違う語が 0」であって、上限の保証ではない**
（配線が変われば測り直す）。2026-11-01 の測定（前の回路）: 2〜20 MHz で違う語 0。
"""
import os
import time

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("FPROBE_ZYNQ_JTAG"), reason="FPROBE_ZYNQ_JTAG が無い")
BASE, WORDS = 0x00100000, 16384
CLOCKS_HZ = (2_000_000, 5_000_000, 10_000_000, 15_000_000, 20_000_000)


def test_memory_read_is_identical_at_every_jtag_clock():
    from fprobe.arm_a_target import arm_a_part
    from fprobe.linux_halt.backend_arma import ArmACores
    from fprobe.session import Session
    session = Session.open(None)
    cores = ArmACores.open(session.dap_access(), arm_a_part("zynq7000"), frequency_hz=1_000_000,
                           page_offset=0xC0000000, phys_offset=0, linear_end=0xDE000000)

    def read(hz):
        cores._dap.set_clock(hz)
        out = []
        for off in range(0, WORDS, 64):
            out += cores._dap.mem_read32_many(0, [BASE + 4 * (off + i) for i in range(64)])
        return out
    try:
        ref = read(1_000_000)
        assert ref == read(1_000_000)
        for hz in CLOCKS_HZ:
            t = time.perf_counter()
            got = read(hz)
            wrong = sum(x != y for x, y in zip(got, ref))
            print(f"{hz / 1e6:g} MHz: 基準と違う語 {wrong}、{time.perf_counter() - t:.2f} s")
            assert wrong == 0, f"{hz} Hz で {wrong} 語が基準と違う"
    finally:
        cores._dap.set_clock(2_000_000)
        cores.disable_halting_debug()
        session.close()
