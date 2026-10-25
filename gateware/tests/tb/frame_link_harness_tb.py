"""zynq_frame_link + frame_mux（PYNQ-Z2 のトップと同じつなぎ）のランダムな往復の試験。

PS 役（AXI4-Lite）が、ランダムなチャンネル・長さのフレームを渡し（受けのバッファが busy の間は待つ）、同時に各チャンネルの
入力からランダムな間隔でバイトを流し、PS が TX_HEAD / TX_BUF で読むフレームを集める。期待:
  * PS → コア: CONTROL / DAP は 1 フレーム = 1 メッセージ（last 付き）、UART はバイト列、LINE_CODING はボーレート、
    payload の無いフレーム・SWO / STREAM / 未知のチャンネルは捨てる
  * コア → PS: CONTROL はメッセージを 512 バイトのフレームに分け、それ未満のフレームで終える。DAP は 1 パケット = 1 フレーム。
    SWO / UART / STREAM はバイト列として欠けも重複もなく順に届く
"""
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from tests.tb.axil_master import AxiLiteMaster

RX_STATUS, RX_COMMIT, TX_HEAD, TX_DONE, DROPS = 0x008, 0x00C, 0x010, 0x014, 0x018
RX_BUF, TX_BUF = 0x400, 0x800
MAX = 512


class Sink:
    """コアの出力ストリーム（prefix_out_*）を、ランダムに ready を変えて受ける。last があればメッセージに分ける。"""

    def __init__(self, dut, prefix, rng, ready_p):
        self.v = getattr(dut, f"{prefix}_out_valid")
        self.r = getattr(dut, f"{prefix}_out_ready")
        self.d = getattr(dut, f"{prefix}_out_data")
        self.l = getattr(dut, f"{prefix}_out_last", None)
        self.rng, self.ready_p = rng, ready_p
        self.messages, self.cur, self.bytes = [], bytearray(), bytearray()
        cocotb.start_soon(self._run(dut.clk))

    async def _run(self, clk):
        while True:
            await RisingEdge(clk)
            self.r.value = int(self.rng.random() < self.ready_p)
            await ReadOnly()
            if self.v.value == 1 and self.r.value == 1:
                b = int(self.d.value)
                self.bytes.append(b)
                self.cur.append(b)
                if self.l is not None and self.l.value == 1:
                    self.messages.append(bytes(self.cur))
                    self.cur = bytearray()


class Source:
    """コアの入力ストリーム（prefix_in_*）へ、ランダムな間隔で items（バイト列のリスト）を流す。packet = last を付ける。"""

    def __init__(self, dut, prefix, items, rng, gap_p, packet):
        self.v = getattr(dut, f"{prefix}_in_valid")
        self.r = getattr(dut, f"{prefix}_in_ready")
        self.d = getattr(dut, f"{prefix}_in_data")
        self.l = getattr(dut, f"{prefix}_in_last", None)
        self.v.value = 0
        if self.l is not None:
            self.l.value = 0
        self.queue = [(b, int(packet and i == len(m) - 1)) for m in items for i, b in enumerate(m)]
        self.rng, self.gap_p = rng, gap_p
        self.cur = None
        self.done = not self.queue
        cocotb.start_soon(self._run(dut.clk))

    async def _run(self, clk):
        while True:
            await RisingEdge(clk)
            if self.cur is None and self.queue and self.rng.random() >= self.gap_p:
                self.cur = self.queue.pop(0)
            if self.cur is not None:
                self.v.value = 1
                self.d.value = self.cur[0]
                if self.l is not None:
                    self.l.value = self.cur[1]
            else:
                self.v.value = 0
            await ReadOnly()
            if self.cur is not None and self.r.value == 1:
                self.cur = None
            if self.cur is None and not self.queue:
                self.done = True


def rand_bytes(rng, n):
    return bytes(rng.getrandbits(8) for _ in range(n))


def lens(rng, limit):
    return rng.choice([1, 2, 3, 4, 5, 7, 8, 9, 31, 63, 64, 100, 200, 255, 256, 400, rng.randrange(1, limit + 1),
                       limit - 1, limit, limit])


@cocotb.test()
async def random_traffic_both_ways_1(dut):
    await one_run(dut, 1)


@cocotb.test()
async def random_traffic_both_ways_2(dut):
    await one_run(dut, 2)


@cocotb.test()
async def random_traffic_both_ways_3(dut):
    await one_run(dut, 3)


async def one_run(dut, seed):
    rng = random.Random(seed)
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    for name in ("awvalid", "wvalid", "arvalid"):
        getattr(dut, f"s_axi_{name}").value = 0
    dut.rst.value = 1
    await ClockCycles(dut.clk, 3)
    dut.rst.value = 0
    await ClockCycles(dut.clk, 2)
    axi = AxiLiteMaster(dut, dut.clk, delay=lambda: rng.choice([0, 0, 0, 1, 2, 3]))

    # --- PC → コア ---
    rx_frames = []
    for _ in range(60):
        ch = rng.choice([0, 0, 0, 1, 1, 1, 3, 3, 2, 4, 5, 5, 6, 9, 0xFF])
        n = rng.choice([0, 0, 1, 2, 3, 4, 5, 6, 10, 100, 511, 512]) if ch not in (0, 1, 3) else lens(rng, MAX)
        if ch == 5:
            n = rng.choice([0, 1, 3, 4, 4, 4, 5, 8])
        rx_frames.append(bytes([ch]) + rand_bytes(rng, n))
    exp_ctrl = [f[1:] for f in rx_frames if f[0] == 0 and len(f) >= 2]
    exp_dap = [f[1:] for f in rx_frames if f[0] == 1 and len(f) >= 2]
    exp_uart = b"".join(f[1:] for f in rx_frames if f[0] == 3)
    exp_rates = [int.from_bytes(f[1:5], "little") for f in rx_frames if f[0] == 5 and len(f) - 1 >= 4]

    sinks = {"ctrl": Sink(dut, "ctrl", rng, 0.5), "dap": Sink(dut, "dap", rng, 0.7), "uart": Sink(dut, "uart", rng, 0.3)}
    rates = []

    async def watch_rate():
        while True:
            await RisingEdge(dut.clk)
            await ReadOnly()
            if dut.line_changed.value == 1:
                rates.append(int(dut.line_rate.value))
    cocotb.start_soon(watch_rate())

    # --- コア → PC ---
    ctrl_msgs = [rand_bytes(rng, rng.choice([1, 4, 12, 100, 511, 512, 513, 700, 1024, 1100])) for _ in range(6)]
    dap_pkts = [rand_bytes(rng, rng.choice([1, 2, 3, 63, 64, 65, 255, 511, 512])) for _ in range(25)]
    swo = rand_bytes(rng, 700)
    uart_tx = rand_bytes(rng, 300)
    stream = rand_bytes(rng, 900)
    srcs = [
        Source(dut, "ctrl", ctrl_msgs, rng, rng.choice([0.0, 0.5]), True),
        Source(dut, "dap", dap_pkts, rng, rng.choice([0.0, 0.5]), True),
        Source(dut, "swo", [bytes([b]) for b in swo], rng, rng.choice([0.0, 0.3, 0.9]), False),
        Source(dut, "uart", [bytes([b]) for b in uart_tx], rng, rng.choice([0.0, 0.6]), False),
        Source(dut, "stream", [bytes([b]) for b in stream], rng, rng.choice([0.0, 0.8]), False),
    ]
    got = {0: [], 1: [], 2: bytearray(), 3: bytearray(), 4: bytearray()}
    ctrl_cur = bytearray()
    total_expected = len(swo) + len(uart_tx) + len(stream)

    async def ps_poll_once():
        nonlocal ctrl_cur
        n = await axi.read(TX_HEAD)
        if n == 0:
            return
        assert 1 <= n <= MAX + 1, n
        data = b""
        for i in range(0, n, 4):
            data += (await axi.read(TX_BUF + i)).to_bytes(4, "little")
        await axi.write(TX_DONE, 1)
        data = data[:n]
        ch, payload = data[0], data[1:]
        if ch == 0:
            ctrl_cur += payload
            if len(payload) < MAX:
                got[0].append(bytes(ctrl_cur))
                ctrl_cur = bytearray()
        elif ch == 1:
            got[1].append(payload)
        else:
            got[ch] += payload

    async def ps_send(frame):
        while await axi.read(RX_STATUS) & 1:
            pass
        padded = frame + bytes(-len(frame) % 4)
        for i in range(0, len(padded), 4):
            await axi.write(RX_BUF + i, int.from_bytes(padded[i:i + 4], "little"))
        await axi.write(RX_COMMIT, len(frame))

    pending = list(rx_frames)
    for _ in range(200000):
        if pending and rng.random() < 0.5:
            await ps_send(pending.pop(0))
        else:
            await ps_poll_once()
        tx_done = (len(got[0]) == len(ctrl_msgs) and len(got[1]) == len(dap_pkts)
                   and len(got[2]) + len(got[3]) + len(got[4]) == total_expected)
        if not pending and tx_done and all(s.done for s in srcs):
            break
    else:
        raise AssertionError(f"seed {seed}: 終わらない pending={len(pending)} ctrl={len(got[0])}/{len(ctrl_msgs)} "
                             f"dap={len(got[1])}/{len(dap_pkts)} bytes={len(got[2]) + len(got[3]) + len(got[4])}/{total_expected}")
    await ClockCycles(dut.clk, 3000)                     # 余計な出力・取りこぼしが後から出ないか
    for _ in range(5):
        await ps_poll_once()
    await ClockCycles(dut.clk, 300)

    assert got[0] == ctrl_msgs, f"seed {seed}: ctrl"
    assert got[1] == dap_pkts, f"seed {seed}: dap"
    assert bytes(got[2]) == swo, f"seed {seed}: swo"
    assert bytes(got[3]) == uart_tx, f"seed {seed}: uart"
    assert bytes(got[4]) == stream, f"seed {seed}: stream"
    assert sinks["ctrl"].messages == exp_ctrl, f"seed {seed}: rx ctrl"
    assert sinks["dap"].messages == exp_dap, f"seed {seed}: rx dap"
    assert bytes(sinks["uart"].bytes) == exp_uart, f"seed {seed}: rx uart"
    assert rates == exp_rates, f"seed {seed}: rates {rates} != {exp_rates}"
