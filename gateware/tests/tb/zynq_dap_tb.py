"""zynq_frame_link + frame_mux + DAP 処理（zynq_dap_harness）。PS の中継（host/pynq/fprobe-relay/relay.c）と同じ手順の
AXI の読み書きで CMSIS-DAP のコマンドを送り、SWD のターゲットモデルが答える（PYNQ の P2 の経路のシミュレーション）。"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles

from tests.models.swd_target import DPIDR, SwdTarget
from tests.tb.axil_master import AxiLiteMaster
from tests.tb.zynq_frame_link_tb import RX_BUF, RX_COMMIT, RX_STATUS, TX_BUF, TX_DONE, TX_HEAD

CH_DAP = 1


async def setup(dut):
    dut.nreset_ext.value = "Z"
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    axi = AxiLiteMaster(dut, dut.clk)
    dut.rst.value = 1
    await ClockCycles(dut.clk, 10)
    dut.rst.value = 0
    await ClockCycles(dut.clk, 10)
    cocotb.start_soon(SwdTarget(dut).run())
    return axi


async def to_pl(axi, ch: int, payload: bytes):
    """relay_from_pc と同じ: busy でなくなるのを待ち、ch | payload を 4 バイトずつ書いて COMMIT"""
    while await axi.read(RX_STATUS) & 1:
        pass
    frame = bytes([ch]) + payload
    padded = frame + bytes(-len(frame) % 4)
    for i in range(0, len(padded), 4):
        await axi.write(RX_BUF + i, int.from_bytes(padded[i:i + 4], "little"))
    await axi.write(RX_COMMIT, len(frame))


async def from_pl(axi, timeout=20_000) -> bytes:
    """relay_to_pc と同じ: TX_HEAD を見て、あれば TX_BUF を読み TX_DONE"""
    for _ in range(timeout):
        n = await axi.read(TX_HEAD)
        if n:
            data = b""
            for i in range(0, n, 4):
                data += (await axi.read(TX_BUF + i)).to_bytes(4, "little")
            await axi.write(TX_DONE, 1)
            return data[:n]
    raise TimeoutError("PL からフレームが来ない")


async def dap(axi, command: bytes) -> bytes:
    await to_pl(axi, CH_DAP, command)
    frame = await from_pl(axi)
    assert frame[0] == CH_DAP, frame.hex()
    return frame[1:]


@cocotb.test()
async def dap_commands_through_the_axi_window(dut):
    axi = await setup(dut)
    assert (await dap(axi, bytes.fromhex("0200")))[:2] == bytes([0x02, 1])           # DAP_Connect（SWD）
    assert await dap(axi, bytes.fromhex("1233ffffffffffffff")) == bytes([0x12, 0])   # 線のリセット
    assert await dap(axi, bytes.fromhex("120800")) == bytes([0x12, 0])
    r = await dap(axi, bytes.fromhex("05000102"))                                    # DPIDR を読む
    assert r[:3] == bytes([0x05, 1, 1]) and int.from_bytes(r[3:7], "little") == DPIDR, r.hex()
    await to_pl(axi, 0, b"")                                                         # 空の CONTROL（宛先の結び付け）は捨てられる
    assert (await dap(axi, bytes.fromhex("0200")))[:2] == bytes([0x02, 1])
