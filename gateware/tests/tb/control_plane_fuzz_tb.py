"""control_plane（ハーネス経由）に壊れた要求も含むランダムなメッセージの列を流し、仕様から書いた基準モデル
（状態・done・読んだ語・メモリの中身）と突き合わせる。ハーネス: 窓 0 = ワード 0..63 の RW メモリ、窓 1 = 64..67 が常にエラー、
他は未割り当て（エラー）。"""
import random
import struct

import cocotb

from fprobe import generated as g
from fprobe.control import decode_response
from tests.tb.common import start_clock_and_reset
from tests.tb.control_port import ControlPort

OK, BAD_OP, BAD_LEN, BUS_ERR = g.STATUS_OK, g.STATUS_BAD_OP, g.STATUS_BAD_LENGTH, g.STATUS_BUS_ERROR


class Model:
    def __init__(self):
        self.mem = [0] * 64

    def bus(self, word_addr, write=None):
        """(err, rdata)"""
        if 0 <= word_addr < 64:
            if write is not None:
                self.mem[word_addr] = write
            return False, self.mem[word_addr]
        return True, 0

    def respond(self, m: bytes):
        tag = m[1] if len(m) >= 2 else 0
        if len(m) < 8:
            return tag, BAD_LEN, 0, []
        op, count = m[0], m[2] | m[3] << 8
        addr = int.from_bytes(m[4:8], "little")
        data = m[8:]
        if op not in (g.OP_READ, g.OP_WRITE, g.OP_EXCHANGE):
            return tag, BAD_OP, 0, []
        if count == 0 or addr % 4:
            return tag, BAD_LEN, 0, []
        wa = addr >> 2
        if op == g.OP_READ:
            if data:
                return tag, BAD_LEN, 0, []
            words = []
            for j in range(count):
                err, r = self.bus(wa + j)
                if err:
                    return tag, BUS_ERR, j, words
                words.append(r)
            return tag, OK, count, words
        words = []
        for j in range(count):
            chunk = data[4 * j:4 * j + 4]
            if len(chunk) < 4:
                return tag, BAD_LEN, j, words
            w = int.from_bytes(chunk, "little")
            a = wa if op == g.OP_EXCHANGE else wa + j
            err, _ = self.bus(a, write=w)
            if err:
                return tag, BUS_ERR, j, words
            if op == g.OP_EXCHANGE:
                _, r = self.bus(a)
                words.append(r)
            if j == count - 1:
                return tag, (BAD_LEN if len(data) > 4 * count else OK), count, words
            if len(data) <= 4 * (j + 1):
                return tag, BAD_LEN, j + 1, words
        raise AssertionError


def random_message(rng: random.Random) -> bytes:
    op = rng.choice([g.OP_READ, g.OP_WRITE, g.OP_EXCHANGE, g.OP_WRITE, g.OP_READ, g.OP_EXCHANGE, 0, 0x7F])
    count = rng.choice([0, 1, 1, 2, 3, 5, 8, 66, 70])
    addr = rng.choice([0, 4, 8, 4 * 60, 4 * 62, 4 * 63, 4 * 64, 4 * 66, 4 * 67, 4 * 68, 0x200, 0xFFFFFFFC,
                       rng.randrange(0, 4 * 64, 4), rng.randrange(0, 4 * 64) | 1, 0x80000000])
    nwords = rng.choice([0, 1, 2, 3, 4, 5, 9, count, count, count, count + 1])
    tail = rng.choice([0, 0, 0, 0, 1, 2, 3, 5])
    tag = rng.randrange(256)
    msg = struct.pack("<BBHI", op, tag, count, addr) + rng.randbytes(4 * nwords + tail)
    cut = rng.random()
    if cut < 0.08:
        msg = msg[:rng.randrange(0, 9)]
    elif cut < 0.14:
        msg = msg[:rng.randrange(len(msg) + 1)]
    return msg or b"\x00"            # 空のメッセージは last を立てられない


@cocotb.test()
async def random_messages_match_the_reference_model(dut):
    rng = random.Random(2024)
    port = ControlPort(dut, request="in", response="out", rng=random.Random(5), gap=0.2, stall=0.3)
    await start_clock_and_reset(dut)
    model = Model()
    for batch in range(300):
        port.gap, port.stall = rng.choice([(0.0, 0.0), (0.3, 0.3), (0.6, 0.6), (0.1, 0.8)])
        messages = [random_message(rng) for _ in range(rng.randrange(1, 9))]
        expected = [model.respond(m) for m in messages]
        responses = await port.pipeline(messages)
        got = [decode_response(r) for r in responses]
        assert got == expected, f"batch {batch}: {messages!r}\n got {got}\n exp {expected}"
