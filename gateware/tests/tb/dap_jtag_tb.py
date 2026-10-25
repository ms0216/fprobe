"""dap_processor + debug_port の JTAG のテストベンチ（dap_harness）。チェーンは STM32 と同じ: 装置 0 = JTAG-DP（IR 4、
TDO 側）、装置 1 = バウンダリスキャン TAP（IR 5）。コマンドは pyOCD が JTAG で送る順。"""

import random
import struct

import cocotb
from cocotb.triggers import ReadOnly, RisingEdge

from tests.models.jtag_target import BSC_IDCODE, JTAG_DP_IDCODE, Bypass, JtagChain, JtagDp
from tests.models.swd_target import CDBGPWRUPACK
from tests.tb.common import start_clock_and_reset
from tests.tb.control_port import ControlPort
from tests.tb.dap_processor_tb import AP_DRW_R, AP_DRW_W, AP_TAR_W, DP_CTRL_R, DP_RDBUFF_R, OK, Dap

JTAG_SEQUENCE, JTAG_CONFIGURE, JTAG_IDCODE = 0x14, 0x15, 0x16


def sequence(bits: int, *, tms: int, tdi: int = 0, capture: bool = False) -> bytes:
    info = (bits % 64) | tms << 6 | capture << 7
    return bytes([info]) + tdi.to_bytes((bits + 7) // 8, "little")


async def setup(dut, **dp_args):
    dut.packet_size.value = 512
    dut.nreset_ext.value = "Z"
    dut.target_swdio.value = "Z"
    dp = JtagDp(**dp_args)
    chain = JtagChain(dut, [dp, Bypass()], tck="swclk", tms="swdio", tdi="tdi", tdo="tdo")
    port = ControlPort(dut, request="in", response="out", rng=random.Random(5), gap=0.2, stall=0.2)
    await start_clock_and_reset(dut)
    cocotb.start_soon(chain.run())
    dap = Dap(dut, port)
    assert await dap.command(bytes([0x02, 2])) == bytes([0x02, 2])                    # JTAG で接続
    assert await dap.command(bytes([0x11]) + struct.pack("<I", 10_000_000)) == bytes([0x11, 0])
    assert await dap.command(bytes([JTAG_CONFIGURE, 2, 4, 5])) == bytes([JTAG_CONFIGURE, 0])
    reset = bytes([JTAG_SEQUENCE, 2]) + sequence(6, tms=1, tdi=0x3F) + sequence(1, tms=0)
    assert await dap.command(reset) == bytes([JTAG_SEQUENCE, 0])                     # Run-Test/Idle へ
    return dap, dp


@cocotb.test()
async def sequences_capture_tdo(dut):
    dap, _ = await setup(dut)
    # リセット直後は両方 IDCODE: Shift-DR で 64 ビット読む
    r = await dap.command(bytes([JTAG_SEQUENCE, 3]) + sequence(1, tms=1) + sequence(2, tms=0)
                          + sequence(64, tms=0, tdi=(1 << 64) - 1, capture=True))
    assert r[:2] == bytes([JTAG_SEQUENCE, 0])
    tdo = int.from_bytes(r[2:10], "little")
    assert tdo & 0xFFFFFFFF == JTAG_DP_IDCODE and tdo >> 32 == BSC_IDCODE
    assert await dap.command(bytes([JTAG_SEQUENCE, 1]) + sequence(6, tms=1, tdi=0x3F)) == bytes([JTAG_SEQUENCE, 0])


@cocotb.test()
async def random_jtag_sequences_drive_and_capture_exactly(dut):
    """JTAG_Sequence の境界の長さ（0 = 64、1・31・32・33・63・64）とランダムな並びで、TCK ごとの TMS / TDI を要求と
    突き合わせ、Shift-DR で取った TDO を分割しても IDCODE の連なりと一致する。"""
    dap, _ = await setup(dut)
    rng = random.Random(4321)
    trace = []

    async def monitor():
        while True:
            await RisingEdge(dut.swclk)
            await ReadOnly()
            trace.append((str(dut.swdio.value), str(dut.tdi.value)))

    cocotb.start_soon(monitor())
    for _ in range(25):
        message, expected = bytearray(), []
        nseq = rng.randrange(1, 5)
        for _ in range(nseq):
            n = rng.choice([1, 2, 31, 32, 33, 63, 64, rng.randrange(1, 65)])
            tms, tdi = rng.getrandbits(1), rng.getrandbits(n)
            message += sequence(n, tms=tms, tdi=tdi)
            expected += [(str(tms), str(tdi >> i & 1)) for i in range(n)]
        trace.clear()
        r = await dap.command(bytes([JTAG_SEQUENCE, nseq]) + bytes(message))
        assert r == bytes([JTAG_SEQUENCE, 0]), r.hex()
        assert trace == expected, message.hex()
    reset = bytes([JTAG_SEQUENCE, 2]) + sequence(6, tms=1, tdi=0x3F) + sequence(1, tms=0)
    assert await dap.command(reset) == bytes([JTAG_SEQUENCE, 0])
    await dap.command(bytes([JTAG_SEQUENCE, 2]) + sequence(1, tms=1) + sequence(2, tms=0))     # Shift-DR
    got, pos = 0, 0
    total = JTAG_DP_IDCODE | BSC_IDCODE << 32
    while pos < 64:
        n = min(rng.choice([1, 3, 31, 32, 33]), 64 - pos)
        r = await dap.command(bytes([JTAG_SEQUENCE, 1]) + sequence(n, tms=0, tdi=(1 << n) - 1, capture=True))
        assert r[:2] == bytes([JTAG_SEQUENCE, 0]) and len(r) == 2 + (n + 7) // 8
        value = int.from_bytes(r[2:], "little")
        assert value >> n == 0, (n, hex(value))
        got |= value << pos
        pos += n
    assert got == total, hex(got)


@cocotb.test()
async def idcode_and_memory_access(dut):
    dap, dp = await setup(dut)
    assert await dap.command(bytes([JTAG_IDCODE, 0])) == bytes([JTAG_IDCODE, 0]) + struct.pack("<I", JTAG_DP_IDCODE)
    assert await dap.command(bytes([JTAG_IDCODE, 2])) == bytes([JTAG_IDCODE, 0xFF])  # 装置は 2 台
    await dap.mem_setup()
    count, resp, (ctrl,) = await dap.transfer((DP_CTRL_R, None))
    assert (count, resp) == (1, OK) and ctrl & CDBGPWRUPACK
    words = [random.Random(9).getrandbits(32) for _ in range(40)]
    await dap.transfer((AP_TAR_W, 0x20000000))
    assert (await dap.block(AP_DRW_W, len(words), words))[:2] == (40, OK)
    assert [dp.regs.memory[0x20000000 + 4 * i] for i in range(40)] == words
    await dap.transfer((AP_TAR_W, 0x20000000))
    assert await dap.block(AP_DRW_R, 40) == (40, OK, words)
    # Transfer の中で AP 読み → DP 読み → RDBUFF の混在
    await dap.transfer((AP_TAR_W, 0x20000004))
    count, resp, data = await dap.transfer((AP_DRW_R, None), (AP_DRW_R, None), (DP_CTRL_R, None),
                                           (DP_RDBUFF_R, None))
    assert (count, resp) == (4, OK) and data[:2] == words[1:3] and data[2] & CDBGPWRUPACK


@cocotb.test()
async def block_read_with_wait_returns_every_word_in_order(dut):
    """AP の転送の後に WAIT が続いても（Cortex-A7 の DCC の速い読み）、まとめ読み・まとめ書きの値がずれない"""
    dap, dp = await setup(dut, wait_after_ap=2)
    await dap.mem_setup()
    words = [random.Random(11).getrandbits(32) for _ in range(24)]
    await dap.transfer((AP_TAR_W, 0x20000000))
    assert (await dap.block(AP_DRW_W, len(words), words))[:2] == (24, OK)
    assert [dp.regs.memory[0x20000000 + 4 * i] for i in range(24)] == words
    await dap.transfer((AP_TAR_W, 0x20000000))
    assert await dap.block(AP_DRW_R, 24) == (24, OK, words)
    await dap.transfer((AP_TAR_W, 0x20000004))
    count, resp, data = await dap.transfer((AP_DRW_R, None), (AP_DRW_R, None), (DP_CTRL_R, None))
    assert (count, resp) == (3, OK) and data[:2] == words[1:3]
    assert dp.waits > 0
