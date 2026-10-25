"""dap_processor + swd_phy（ハーネス経由）のテストベンチ。CMSIS-DAP のコマンドパケットを送り、
相手は SWD ターゲットモデル。パケットの形は CMSIS-DAP v2 仕様（pyOCD が送るものと同じ）。"""

import random
import struct

import cocotb
from cocotb.triggers import ClockCycles, ReadOnly, RisingEdge

from fprobe_gw import dap_info
from tests.models.swd_target import (AP_IDR, CDBGPWRUPACK, CDBGPWRUPREQ, CSYSPWRUPREQ, DPIDR, STICKYERR,
                                     STKERRCLR, SwdTarget)
from tests.tb.common import start_clock_and_reset
from tests.tb.control_port import ControlPort

# 転送要求（bit0 APnDP, bit1 RnW, bit2-3 A[3:2], bit4 値一致, bit5 一致マスク）
DP_IDCODE_R, DP_ABORT_W, DP_CTRL_W, DP_CTRL_R, DP_SELECT_W, DP_RDBUFF_R = 0x02, 0x00, 0x04, 0x06, 0x08, 0x0E
AP_CSW_W, AP_TAR_W, AP_DRW_W, AP_DRW_R = 0x01, 0x05, 0x0D, 0x0F
MATCH_VALUE, MATCH_MASK = 0x10, 0x20
OK, WAIT, FAULT, MISMATCH = 1, 2, 4, 0x10


class Dap:
    def __init__(self, dut, port):
        self.dut, self.port = dut, port

    async def command(self, packet: bytes) -> bytes:
        response = await self.port.transact(packet, timeout=1_000_000)   # SWD 10 MHz で 100 ワードなら 5 万周期
        assert response[0] == packet[0] or response == b"\xff", (packet.hex(), response.hex())
        return response

    async def connect(self):
        assert await self.command(bytes([0x02, 1])) == bytes([0x02, 1])
        assert await self.command(bytes([0x11]) + struct.pack("<I", 10_000_000)) == bytes([0x11, 0])
        ones = b"\xff" * 7
        assert await self.command(bytes([0x12, 51]) + ones) == bytes([0x12, 0])
        assert await self.command(bytes([0x12, 16]) + struct.pack("<H", 0xE79E)) == bytes([0x12, 0])
        assert await self.command(bytes([0x12, 51]) + ones) == bytes([0x12, 0])
        assert await self.command(bytes([0x12, 8, 0])) == bytes([0x12, 0])

    async def transfer(self, *requests) -> tuple[int, int, list[int]]:
        """requests: (request, value) の並び（読み出しは value = None、値一致は値を渡す）。
        戻り値: (処理数, 応答, 読んだ値)。"""
        body = b""
        for request, value in requests:
            body += bytes([request])
            if value is not None:
                body += struct.pack("<I", value)
        r = await self.command(bytes([0x05, 0, len(requests)]) + body)
        count, resp = r[1], r[2]
        data = list(struct.unpack(f"<{(len(r) - 3) // 4}I", r[3:]))
        return count, resp, data

    async def block(self, request: int, count: int, values=()) -> tuple[int, int, list[int]]:
        body = b"".join(struct.pack("<I", v) for v in values)
        r = await self.command(bytes([0x06, 0]) + struct.pack("<H", count) + bytes([request]) + body)
        done, resp = struct.unpack_from("<HB", r, 1)
        return done, resp, list(struct.unpack(f"<{(len(r) - 4) // 4}I", r[4:]))

    async def mem_setup(self):
        await self.transfer((DP_ABORT_W, 0x1E), (DP_CTRL_W, CDBGPWRUPREQ | CSYSPWRUPREQ), (DP_SELECT_W, 0),
                            (AP_CSW_W, 0x23000012))                # 32 ビット、自動インクリメント


async def setup(dut, *, packet_size=512, seed=1, **target):
    dut.packet_size.value = packet_size
    dut.nreset_ext.value = "Z"
    port = ControlPort(dut, request="in", response="out", rng=random.Random(seed), gap=0.2, stall=0.2)
    model = SwdTarget(dut, **target)
    await start_clock_and_reset(dut)
    cocotb.start_soon(model.run())
    return Dap(dut, port), model


def info_string(r: bytes) -> str:
    assert r[0] == 0x00 and len(r) == 2 + r[1]
    return r[2:2 + r[1]].rstrip(b"\0").decode()


@cocotb.test()
async def info_and_unknown_commands(dut):
    dap, _ = await setup(dut)
    for info_id, text in dap_info.STRINGS.items():
        assert info_string(await dap.command(bytes([0x00, info_id]))) == text
    assert await dap.command(bytes([0x00, 0xF0])) == bytes([0x00, 1, dap_info.capabilities(jtag=True)])
    assert await dap.command(bytes([0x00, 0xFE])) == bytes([0x00, 1, dap_info.PACKET_COUNT])
    assert await dap.command(bytes([0x00, 0xFF])) == bytes([0x00, 2]) + struct.pack("<H", 512)
    assert await dap.command(bytes([0x00, 0x05])) == bytes([0x00, 0])          # 対象デバイス名: 無し
    assert await dap.command(bytes([0x1E, 0])) == b"\xff"                       # SWO_ExtendedStatus: 未対応
    assert await dap.command(bytes([0x01, 0, 1])) == bytes([0x01, 0])           # HostStatus
    assert await dap.command(bytes([0x02, 3])) == bytes([0x02, 0])              # 不明なポート: 失敗
    assert await dap.command(bytes([0x0A])) == bytes([0x0A, 0, 0])              # ResetTarget: 実装なし


@cocotb.test()
async def connect_and_read_dpidr(dut):
    dap, model = await setup(dut)
    count, resp, data = await dap.transfer((DP_IDCODE_R, None))
    assert (count, resp) == (0, 7)                                             # 接続前（ピン解放）は無応答
    await dap.connect()
    assert await dap.transfer((DP_IDCODE_R, None)) == (1, OK, [DPIDR])


@cocotb.test()
async def memory_through_transfer_and_block(dut):
    dap, model = await setup(dut, seed=2)
    await dap.connect()
    await dap.mem_setup()
    assert (await dap.transfer((DP_CTRL_R, None)))[2][0] & CDBGPWRUPACK
    rng = random.Random(3)
    words = [rng.getrandbits(32) for _ in range(100)]
    await dap.transfer((AP_TAR_W, 0x20000000))
    assert (await dap.block(AP_DRW_W, len(words), words))[:2] == (100, OK)
    assert [model.memory[0x20000000 + 4 * i] for i in range(100)] == words
    await dap.transfer((AP_TAR_W, 0x20000000))
    assert await dap.block(AP_DRW_R, 100) == (100, OK, words)
    # Transfer の中で AP 読み出しを続ける（ポスティングの連鎖）、DP 読み出し、書き込みの混在
    await dap.transfer((AP_TAR_W, 0x20000000))
    count, resp, data = await dap.transfer((AP_DRW_R, None), (AP_DRW_R, None), (AP_DRW_R, None),
                                           (DP_CTRL_R, None), (AP_TAR_W, 0x20000010), (AP_DRW_R, None))
    assert (count, resp) == (6, OK)
    assert data[:3] == words[:3] and data[3] & CDBGPWRUPACK and data[4] == words[4]
    await dap.transfer((DP_SELECT_W, 0xF0))
    assert (await dap.transfer((AP_DRW_R, None)))[2] == [AP_IDR]               # バンク 0xF の 0xFC
    await dap.transfer((DP_SELECT_W, 0))


@cocotb.test()
async def wait_retry_match_and_fault(dut):
    dap, model = await setup(dut, seed=4)
    await dap.connect()
    await dap.mem_setup()
    await dap.command(bytes([0x04, 0]) + struct.pack("<HH", 5, 3))             # WAIT 5 回、一致 3 回
    model.wait_next = 4
    assert (await dap.transfer((AP_TAR_W, 0x20000000)))[:2] == (1, OK)         # 再試行で通る
    await dap.command(bytes([0x04, 0]) + struct.pack("<HH", 1, 3))
    model.wait_next = 5
    assert (await dap.transfer((AP_TAR_W, 0x1), (AP_TAR_W, 0x2)))[:2] == (0, WAIT)
    model.wait_next = 0
    # 値一致: CTRL/STAT の電源応答ビットを待つ（一致する）/ 一致しない
    assert (await dap.transfer((MATCH_MASK, CDBGPWRUPACK),
                               (DP_CTRL_R | MATCH_VALUE, CDBGPWRUPACK)))[:2] == (2, OK)
    count, resp, _ = await dap.transfer((MATCH_MASK, 0xF), (DP_CTRL_R | MATCH_VALUE, 0x5))
    assert (count, resp) == (1, OK | MISMATCH)
    await dap.transfer((MATCH_MASK, 0xFFFFFFFF))
    # FAULT: バスエラーで STICKYERR、以後の AP アクセスは FAULT。WriteABORT で戻る
    model.fault_addresses.add(0x30000000)
    await dap.transfer((AP_TAR_W, 0x30000000))
    count, resp, _ = await dap.transfer((AP_DRW_W, 1), (AP_DRW_W, 2))
    assert resp == FAULT and model.ctrl_stat & STICKYERR
    assert await dap.command(bytes([0x08, 0]) + struct.pack("<I", STKERRCLR)) == bytes([0x08, 0])
    assert (await dap.transfer((AP_TAR_W, 0x20000000)))[:2] == (1, OK)
    # パリティ異常
    model.corrupt_next_parity = True
    assert (await dap.transfer((DP_IDCODE_R, None)))[1] == 0x08


@cocotb.test()
async def sequences_pins_delay_and_disconnect(dut):
    dap, model = await setup(dut, seed=5)
    await dap.connect()
    # SWD_Sequence: 出力 8 ビット（アイドル）+ 入力 5 ビット（誰も駆動しない → 1）
    assert await dap.command(bytes([0x1D, 2, 8, 0x00, 0x80 | 5])) == bytes([0x1D, 0, 0x1F])
    # SWJ_Pins: nRESET を Low（選択 bit7）にして読む → 0。解放して読む → 1
    r = await dap.command(bytes([0x10, 0x00, 0x80]) + struct.pack("<I", 0))
    assert r[1] & 0x80 == 0 and str(dut.nreset.value) == "0"
    r = await dap.command(bytes([0x10, 0x80, 0x80]) + struct.pack("<I", 100))
    assert r[1] & 0x80
    # ターゲット側がリセットを保持していれば、待ち時間を使い切って 0 を返す
    dut.nreset_ext.value = "0"
    r = await dap.command(bytes([0x10, 0x80, 0x80]) + struct.pack("<I", 3))
    assert r[1] & 0x80 == 0
    dut.nreset_ext.value = "Z"
    # Delay（10 µs ≒ 1000 周期以上かかる）
    start = cocotb.utils.get_sim_time("ns")
    assert await dap.command(bytes([0x09]) + struct.pack("<H", 10)) == bytes([0x09, 0])
    assert cocotb.utils.get_sim_time("ns") - start >= 10_000
    assert await dap.command(bytes([0x03])) == bytes([0x03, 0])                # Disconnect
    await ClockCycles(dut.clk, 5)
    assert str(dut.swclk.value) == "1"                                          # 解放（プルアップ）


@cocotb.test()
async def full_speed_packet_size_and_short_packets(dut):
    dap, model = await setup(dut, packet_size=64, seed=6)
    assert await dap.command(bytes([0x00, 0xFF])) == bytes([0x00, 2, 64, 0])
    await dap.connect()
    await dap.mem_setup()
    await dap.transfer((AP_TAR_W, 0x20000100))
    words = list(range(14))                                                    # 8 + 14 * 4 = 64 バイト
    assert (await dap.block(AP_DRW_W, 14, words))[:2] == (14, OK)
    # 引数の足りない要求（途中で終わったパケット）も応答を返し、次のコマンドに影響しない
    r = await dap.command(bytes([0x05, 0, 2, AP_TAR_W]))
    assert r[0] == 0x05
    assert await dap.transfer((DP_IDCODE_R, None)) == (1, OK, [DPIDR])


@cocotb.test()
async def swj_clock_picks_the_fastest_not_above_request(dut):
    dap, _ = await setup(dut, seed=7)
    # CLK_HZ = 100 MHz: SWCLK = 50 MHz / half_period。要求の 1% 増し以下で最も速い（切り上げ）、2〜65535 に制限
    for hz, half in ((10_000_000, 5), (9_000_000, 6), (1_000_000, 50), (40_000_000, 2), (500, 65535),
                     (9_950_000, 5), (9_850_000, 6)):
        assert await dap.command(bytes([0x11]) + struct.pack("<I", hz)) == bytes([0x11, 0])
        assert int(dut.dap_i.config_i.half_r.value) == half, hz
    assert await dap.command(bytes([0x11]) + struct.pack("<I", 0)) == bytes([0x11, 0xFF])


@cocotb.test()
async def pyocd_connect_sequence_returns_dp_read_data(dut):
    # 実機（Ethernet）で DP の読みの値が返らなかった順序をそのまま（pyOCD の接続と同じ）
    dap, model = await setup(dut, seed=7)
    assert await dap.command(bytes([0x02, 0])) == bytes([0x02, 1])
    ones = b"\xff" * 7
    assert await dap.command(bytes([0x12, 51]) + ones) == bytes([0x12, 0])
    assert await dap.command(bytes([0x12, 8, 0])) == bytes([0x12, 0])
    assert await dap.block(DP_IDCODE_R, 1) == (1, OK, [DPIDR])
    count, resp, data = await dap.transfer((DP_ABORT_W, 0x1E), (DP_SELECT_W, 0),
                                           (DP_CTRL_W, CDBGPWRUPREQ | CSYSPWRUPREQ), (DP_CTRL_R, None))
    assert (count, resp, len(data)) == (4, OK, 1)
    count, resp, data = await dap.transfer((DP_CTRL_R, None))
    assert (count, resp, len(data)) == (1, OK, 1)


# RP2040 の DP の TARGETSEL（core0・core1・救済の DP）と、それぞれの DPIDR（試験で見分けるため別の値にする）
RP2040_DPS = {0x01002927: 0x0BC12477, 0x11002927: 0x1BC12477, 0xF1002927: 0x2BC12477}


def targetsel_command(value: int) -> bytes:
    """pyOCD の RP2040 のターゲット（target_rp2.py の select_dp）と同じ DAP_SWD_Sequence: 要求 8 ビット（0x99）、
    入力 5 周期（ターンアラウンド・ACK・ターンアラウンド。誰も駆動しない）、値とパリティの 33 ビット、休み 2 周期。"""
    parity = bin(value).count("1") & 1
    data = (value | parity << 32).to_bytes(5, "little")
    return bytes([0x1D, 4, 8, 0x99, 0x80 | 5, 33]) + data + bytes([2, 0x00])


@cocotb.test()
async def multidrop_targetsel_selects_one_dp(dut):
    """複数ドロップの SWD（U31）: DAP_SWD_Sequence で TARGETSEL を書くと、選んだ DP だけが応答する。"""
    dap, model = await setup(dut, seed=8, multidrop=RP2040_DPS)
    await dap.connect()
    ones = b"\xff" * 7
    for targetsel, dpidr in RP2040_DPS.items():
        assert await dap.command(bytes([0x12, 51]) + ones) == bytes([0x12, 0])     # 線のリセット: 選び直す
        assert await dap.command(bytes([0x12, 2, 0])) == bytes([0x12, 0])
        # 応答: 状態 OK と、入力 5 周期の値（誰も駆動しない → 1）
        assert await dap.command(targetsel_command(targetsel)) == bytes([0x1D, 0, 0x1F])
        assert model.transactions[-1] == (0, 0, 0xC, None, targetsel)
        assert await dap.transfer((DP_IDCODE_R, None)) == (1, OK, [dpidr])
        await dap.mem_setup()                                                  # 選んだ DP の上で AP も使える
        assert (await dap.transfer((DP_CTRL_R, None)))[2][0] & CDBGPWRUPACK
    # 一致しない TARGETSEL: どの DP も応答しない
    assert await dap.command(bytes([0x12, 51]) + ones) == bytes([0x12, 0])
    assert await dap.command(targetsel_command(0x12345678)) == bytes([0x1D, 0, 0x1F])
    assert (await dap.transfer((DP_IDCODE_R, None)))[:2] == (0, 7)


@cocotb.test()
async def random_transfers_match_a_sequential_memory_model(dut):
    """TAR 書き・DRW 読み書き（自動増加）のランダムな並びを Transfer で送り、読んだ値・処理数が「要求を順に実行した
    メモリ」と一致する（ポスティングの連鎖・書き込みの前の回収・最後の読み出しの回収を含む）。"""
    dap, model = await setup(dut, seed=9)
    await dap.connect()
    await dap.mem_setup()
    rng = random.Random(77)
    mem = {}
    for batch in range(25):
        base = 0x20000000 + 0x400 * rng.randrange(0, 4)
        tar = base + 4 * rng.randrange(0, 64)
        requests, expected, ops = [(AP_TAR_W, tar)], [], 0
        for _ in range(rng.choice([1, 2, 3, 8, 20])):
            kind = rng.choice(["r", "r", "w", "t"])
            if kind == "t":
                tar = base + 4 * rng.randrange(0, 64)
                requests.append((AP_TAR_W, tar))
            elif kind == "w":
                value = rng.getrandbits(32)
                requests.append((AP_DRW_W, value))
                mem[tar] = value
                tar += 4
            else:
                requests.append((AP_DRW_R, None))
                expected.append(mem.get(tar, 0))
                tar += 4
        count, resp, data = await dap.transfer(*requests)
        assert (count, resp) == (len(requests), OK), (batch, count, resp)
        assert data == expected, (batch, requests)


class PinTrace:
    """SWCLK の立ち上がりごとの SWDIO の値を記録する（ピンに出たビット列を見る）。"""

    def __init__(self, dut):
        self.dut, self.bits = dut, []
        cocotb.start_soon(self._run())

    async def _run(self):
        while True:
            await RisingEdge(self.dut.swclk)
            await ReadOnly()
            self.bits.append(str(self.dut.swdio.value))


def bits_of(value: int, n: int) -> list[str]:
    return [str(value >> i & 1) for i in range(n)]


@cocotb.test()
async def random_sequences_drive_exactly_the_requested_bits(dut):
    """SWJ_Sequence / SWD_Sequence を境界の長さ（1・63・64・65・128・255・256 ビット、バイト境界）とランダムな長さで送り、
    ピンに出たビット列（順序・個数）と入力の応答を、要求から決めた期待値と突き合わせる。"""
    # ターゲットモデルは起動しない（ランダムなビットを開始ビットと見て SWDIO を駆動し、衝突するため）。線はプルアップ
    dut.packet_size.value = 512
    dut.nreset_ext.value = "Z"
    dut.target_swdio.value = "Z"
    dap = Dap(dut, ControlPort(dut, request="in", response="out", rng=random.Random(21), gap=0.2, stall=0.2))
    await start_clock_and_reset(dut)
    assert await dap.command(bytes([0x02, 1])) == bytes([0x02, 1])
    assert await dap.command(bytes([0x11]) + struct.pack("<I", 25_000_000)) == bytes([0x11, 0])
    trace = PinTrace(dut)
    rng = random.Random(1234)
    counts = [1, 2, 7, 8, 9, 15, 16, 17, 31, 32, 33, 63, 64, 65, 66, 127, 128, 129, 191, 192, 193, 255, 0]
    counts += [rng.randrange(1, 257) for _ in range(20)]
    for n in counts:
        total = n or 256
        value = rng.getrandbits(total)
        trace.bits.clear()
        r = await dap.command(bytes([0x12, n]) + value.to_bytes((total + 7) // 8, "little"))
        assert r == bytes([0x12, 0]), (n, r.hex())
        assert trace.bits == bits_of(value, total), (n, "".join(trace.bits))
    for _ in range(30):
        seqs, expect_bits, expect_resp = bytearray(), [], bytearray()
        nseq = rng.randrange(0, 5)
        for _ in range(nseq):
            n = rng.choice([1, 8, 31, 32, 33, 63, 64, rng.randrange(1, 65)])
            info = n % 64
            if rng.random() < 0.5:
                seqs.append(info | 0x80)                                     # 入力: 誰も駆動しない → 1
                expect_bits += ["1"] * n
                expect_resp += ((1 << n) - 1).to_bytes((n + 7) // 8, "little")
            else:
                value = rng.getrandbits(n)
                seqs.append(info)
                seqs += value.to_bytes((n + 7) // 8, "little")
                expect_bits += bits_of(value, n)
        trace.bits.clear()
        r = await dap.command(bytes([0x1D, nseq]) + bytes(seqs))
        assert r == bytes([0x1D, 0]) + bytes(expect_resp), (nseq, seqs.hex(), r.hex())
        assert trace.bits == expect_bits, (seqs.hex(), "".join(trace.bits))


@cocotb.test()
async def truncated_and_garbage_packets_keep_the_framing(dut):
    """全コマンド ID に、途中で切れた・余分に続く・ランダムな内容の要求を送っても、応答が 1 つ返り（先頭はコマンド ID か
    0xFF、長さは packet_size 以下）、直後の DAP_Info が正しく返る（枠がずれない・止まらない）。"""
    dap, _ = await setup(dut, seed=31, packet_size=64)
    rng = random.Random(555)
    await dap.connect()
    await dap.mem_setup()
    safe = {
        0x00: lambda: bytes([rng.choice([1, 2, 3, 4, 5, 6, 7, 8, 9, 0xF0, 0xFD, 0xFE, 0xFF, 0x42])]),
        0x01: lambda: bytes([rng.getrandbits(2), rng.getrandbits(1)]),
        0x02: lambda: bytes([rng.choice([0, 1, 2, 3])]),
        0x04: lambda: bytes([rng.randrange(0, 4)]) + struct.pack("<HH", rng.randrange(0, 20), rng.randrange(0, 20)),
        0x05: lambda: bytes([0, rng.randrange(0, 7)]) + rng.randbytes(rng.randrange(0, 30)),
        0x06: lambda: bytes([0, rng.randrange(0, 6), 0, rng.choice([AP_DRW_R, AP_DRW_W, DP_IDCODE_R, DP_CTRL_R])])
                      + rng.randbytes(rng.randrange(0, 30)),
        0x08: lambda: rng.randbytes(4),
        0x09: lambda: struct.pack("<H", rng.randrange(0, 5)),
        0x0A: lambda: b"",
        0x10: lambda: bytes([rng.getrandbits(8) & 0x7F, rng.getrandbits(8) & 0x7F]) + struct.pack("<I", rng.randrange(0, 3)),
        0x11: lambda: struct.pack("<I", rng.choice([1_000_000, 10_000_000, 25_000_000])),
        0x12: lambda: bytes([rng.randrange(0, 80)]) + rng.randbytes(rng.randrange(0, 12)),
        0x13: lambda: bytes([rng.getrandbits(3)]),
        0x15: lambda: bytes([rng.randrange(0, 4)]) + rng.randbytes(rng.randrange(0, 4)),
        0x16: lambda: bytes([rng.randrange(0, 3)]),
        0x17: lambda: bytes([rng.randrange(0, 3)]),
        0x18: lambda: bytes([rng.randrange(0, 4)]),
        0x19: lambda: struct.pack("<I", rng.choice([0, 115200, 1_000_000, 3_000_000])),
        0x1A: lambda: bytes([rng.getrandbits(1)]),
        0x1B: lambda: b"",
        0x1C: lambda: struct.pack("<H", rng.randrange(0, 600)),
        0x1D: lambda: bytes([rng.randrange(0, 4)]) + rng.randbytes(rng.randrange(0, 12)),
    }
    ids = list(safe) + [0x1E, 0x1F, 0x20, 0x7F, 0x80, 0xFE]
    for i in range(160):
        cmd = rng.choice(ids)
        body = safe[cmd]() if cmd in safe else rng.randbytes(rng.randrange(0, 70))
        if rng.random() < 0.3 and cmd in safe:
            body = body[:rng.randrange(0, len(body) + 1)]                   # 途中で切れた要求
        if rng.random() < 0.2:
            body += rng.randbytes(rng.randrange(1, 40))                     # 余分に続く要求
        packet = (bytes([cmd]) + body)[:64]
        r = await dap.command(packet)
        assert len(r) <= 64 and (r[0] == cmd or r == b"\xff"), (i, packet.hex(), r.hex())
        assert await dap.command(bytes([0x00, 0xFF])) == bytes([0x00, 2, 64, 0]), (i, packet.hex())
