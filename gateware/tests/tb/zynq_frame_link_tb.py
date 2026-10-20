"""zynq_frame_link: AXI4-Lite のレジスタ表どおりにフレームを受け渡し、どの番地にも必ず応答する。

frame_mux の側は Python の模型（受けのバッファへの書き・commit・busy、送りのバッファの 1 周期の同期読み・tx_done）。
"""
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

from tests.tb.axil_master import AxiLiteMaster

ID, VERSION, RX_STATUS, RX_COMMIT, TX_HEAD, TX_DONE, DROPS, MEM_BASE, MEM_BYTES, MEM_ERRORS = (
    0x000, 0x004, 0x008, 0x00C, 0x010, 0x014, 0x018, 0x01C, 0x020, 0x024)
RX_BUF, TX_BUF = 0x400, 0x800


class MuxModel:
    def __init__(self, dut):
        self.dut = dut
        self.rx_ram = {}
        self.frames = []
        self.tx_frame = b""
        self.tx_done = 0
        dut.rx_busy.value = 0
        dut.tx_valid.value = 0
        dut.tx_len.value = 1
        dut.tx_rdata.value = 0
        cocotb.start_soon(self._run())

    def offer(self, frame: bytes):
        self.tx_frame = frame
        self.dut.tx_len.value = len(frame)
        self.dut.tx_valid.value = 1

    async def _run(self):
        dut = self.dut
        while True:
            await RisingEdge(dut.clk)
            if dut.rst.value == 1:
                continue
            if dut.rx_we.value == 1:
                self.rx_ram[int(dut.rx_waddr.value)] = int(dut.rx_wdata.value)
            if dut.rx_commit.value == 1:
                n = int(dut.rx_len.value)
                self.frames.append(bytes(self.rx_ram.get(i, 0) for i in range(n)))
            a = int(dut.tx_raddr.value)
            dut.tx_rdata.value = self.tx_frame[a] if a < len(self.tx_frame) else 0
            if dut.tx_done.value == 1:
                self.tx_done += 1
                dut.tx_valid.value = 0


async def setup(dut, delay=lambda: 0):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst.value = 1
    dut.mem_errors.value = 0
    model = MuxModel(dut)
    axi = AxiLiteMaster(dut, dut.clk, delay=delay)
    await ClockCycles(dut.clk, 3)
    dut.rst.value = 0
    await ClockCycles(dut.clk, 2)
    return axi, model


async def send(axi, frame: bytes):
    padded = frame + bytes(-len(frame) % 4)
    for i in range(0, len(padded), 4):
        await axi.write(RX_BUF + i, int.from_bytes(padded[i:i + 4], "little"))
    await axi.write(RX_COMMIT, len(frame))


async def receive(axi) -> bytes:
    n = await axi.read(TX_HEAD)
    data = b""
    for i in range(0, n, 4):
        data += (await axi.read(TX_BUF + i)).to_bytes(4, "little")
    await axi.write(TX_DONE, 1)
    return data[:n]


@cocotb.test()
async def id_and_version(dut):
    axi, _ = await setup(dut)
    assert await axi.read(ID) == 0x46504C4B
    assert await axi.read(VERSION) == 1


@cocotb.test()
async def frame_from_pc_reaches_frame_mux(dut):
    axi, model = await setup(dut)
    frame = bytes([1]) + bytes(range(10, 21))
    await send(axi, frame)
    await ClockCycles(dut.clk, 2)
    assert model.frames == [frame]


@cocotb.test()
async def frame_to_pc_is_read_from_tx_buf(dut):
    axi, model = await setup(dut)
    frame = bytes([0]) + bytes(random.randrange(256) for _ in range(512))
    model.offer(frame)
    assert await receive(axi) == frame
    model.offer(bytes([0x01, 0xAA]))              # 短いフレーム: 長さより後ろは前のフレームの中身でなく 0
    model.tx_frame = bytes([0x01, 0xAA]) + bytes([0xEE]) * 10
    assert await axi.read(TX_BUF) == 0x0000AA01
    await axi.write(TX_DONE, 1)
    await ClockCycles(dut.clk, 2)
    assert model.tx_done == 2
    assert await axi.read(TX_HEAD) == 0          # 読み終えたら次のフレームは無い


@cocotb.test()
async def rx_backpressure(dut):
    axi, model = await setup(dut)
    dut.rx_busy.value = 1
    assert await axi.read(RX_STATUS) & 1 == 1
    await axi.write(RX_BUF, 0xDDCCBBAA)           # busy の間の書きは無視
    await axi.write(RX_COMMIT, 4)                 # busy の間の COMMIT は渡さず数える
    await ClockCycles(dut.clk, 2)
    assert model.frames == [] and model.rx_ram == {}
    assert await axi.read(DROPS) == 1
    dut.rx_busy.value = 0
    await send(axi, b"\x03abc")
    await ClockCycles(dut.clk, 2)
    assert model.frames == [b"\x03abc"]


@cocotb.test()
async def max_and_zero_length(dut):
    axi, model = await setup(dut)
    await axi.write(RX_COMMIT, 514)               # MAX_PAYLOAD + 2: 範囲外
    await axi.write(RX_COMMIT, 0)
    await axi.write(RX_COMMIT, 1)                 # ch だけ: 渡す（frame_mux が捨てる）
    await axi.write(RX_COMMIT, 513)               # ちょうど最大: 渡す
    await ClockCycles(dut.clk, 2)
    assert [len(f) for f in model.frames] == [1, 513]
    assert await axi.read(DROPS) == 2


@cocotb.test()
async def every_address_answers(dut):
    rng = random.Random(1)
    axi, model = await setup(dut, delay=lambda: rng.choice((0, 0, 1, 3)))
    model.offer(b"\x02" + bytes(8))
    for addr in range(0, 0x1000, 4):
        value = await axi.read(addr)
        if addr not in (ID, VERSION, RX_STATUS, TX_HEAD, DROPS, MEM_BASE, MEM_BYTES) and not TX_BUF <= addr < 0xC00:
            assert value == 0, hex(addr)
    for addr in range(0, 0x1000, 4):
        if addr in (RX_COMMIT, TX_DONE, MEM_BASE):
            continue
        await axi.write(addr, 0xFFFFFFFF)
    assert await axi.read(ID) == 0x46504C4B


@cocotb.test()
async def mem_base_is_written_by_the_ps_and_read_back(dut):
    # PS の中継が DDR の領域の物理番地を書く（P3 の深いバッファ）。リセットの後は 0（= 使えない）
    axi, _ = await setup(dut)
    assert await axi.read(MEM_BASE) == 0 and int(dut.mem_base.value) == 0
    await axi.write(MEM_BASE, 0x1E000000)
    assert await axi.read(MEM_BASE) == 0x1E000000 and int(dut.mem_base.value) == 0x1E000000


@cocotb.test()
async def mem_bytes_and_errors_are_reported_to_the_ps(dut):
    # 中継は PL が実際に使う大きさ（generic MEM_BYTES）の範囲が空いているかを確かめ、AXI の誤りの数を見せる
    axi, _ = await setup(dut)
    assert await axi.read(MEM_BYTES) == 32 * 1024 * 1024
    dut.mem_errors.value = 7
    assert await axi.read(MEM_ERRORS) == 7
