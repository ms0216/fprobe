"""DAP_Transfer を ARM の参照実装（DAP.c の DAP_SWD_Transfer）の Python の写しと突き合わせる（差分の試験）。

ランダムな要求の並び（DP / AP の読み書き・値一致の読み・一致マスク・TAR / CSW の書き・タイムスタンプ・WAIT・WAIT の使い切り・
FAULT・パリティ異常）を送り、(1) 応答のバイト列、(2) 線に出た転送の並び（ターゲットの模型が記録する (APnDP, RnW, A, ACK, 値)）が、
同じ状態から写しを走らせた結果と一致すること。写しのターゲットはピンの模型と同じレジスタの振る舞い（SwdTarget の
_ack / _read / _write）を使う。速さのために転送を先に出す回路でも、線に出る転送とその順は参照実装と同じでなければならない。
"""

import copy
import random
import struct

import cocotb
from cocotb.triggers import ReadOnly, RisingEdge

from tests.models.swd_target import ACK_FAULT, ACK_OK, ACK_WAIT, STICKYERR, STKERRCLR, SwdTarget
from tests.tb.dap_processor_tb import AP_TAR_W, DP_ABORT_W, setup
from tests.tb.review_dapline_jtag_block_tb import setup as jtag_setup

RNW, APNDP, MATCH_VALUE, MATCH_MASK, TIMESTAMP = 0x02, 0x01, 0x10, 0x20, 0x80
DP_RDBUFF_R = 0x0E
RESP_OK, RESP_ERROR, RESP_MISMATCH = 0x01, 0x08, 0x10


class ShadowTarget:
    """ピンを持たないターゲットの写し（SwdTarget のレジスタの振る舞いをそのまま使う）。"""

    def __init__(self, live: SwdTarget):
        self.t = copy.copy(live)
        for name in ("memory", "fault_addresses", "transactions"):
            setattr(self.t, name, copy.deepcopy(getattr(live, name)))

    def transfer(self, request: int, wdata: int | None):
        """1 回の転送（線の上の 1 つ）。戻り値: (ACK, 読んだ値 or None, パリティ異常)。"""
        t = self.t
        ap, rnw, addr = request & 1, request >> 1 & 1, (request >> 2 & 3) << 2
        if t.wait_select_writes and not ap and rnw == 0 and addr == 0x8:
            t.wait_select_writes -= 1
            ack = ACK_WAIT
        else:
            ack = t._ack(ap)
        if ack != ACK_OK:
            t.transactions.append((ap, rnw, addr, ack, None))
            return ack, None, False
        if rnw:
            value = t._read(ap, addr)
            bad = t.corrupt_next_parity or t.corrupt_parity_at == len(t.transactions)
            if t.corrupt_next_parity:
                t.corrupt_next_parity = False
            t.transactions.append((ap, rnw, addr, ack, value))
            return ack, value, bad
        t._write(ap, addr, wdata)
        t.transactions.append((ap, rnw, addr, ack, wdata))
        return ack, None, False


def reference_transfer(shadow: ShadowTarget, requests, *, wait_retry: int, match_retry: int, match_mask: int):
    """DAP.c の DAP_SWD_Transfer。requests: (request, value or None)。
    戻り値: (応答のバイト列（コマンド ID の後。タイムスタンプの所は 0）, 更新後の match_mask, タイムスタンプの位置の並び)。"""
    out = bytearray()
    stamps = []

    def xfer(request, wdata=None):
        retry = wait_retry
        while True:
            ack, value, bad = shadow.transfer(request, wdata)
            if ack == ACK_WAIT and retry > 0:
                retry -= 1
                continue
            if ack == ACK_OK and bad:
                return RESP_ERROR, value
            return (ack if ack in (ACK_OK, ACK_WAIT, ACK_FAULT) else 0x07), value

    def store(v):
        out.extend(struct.pack("<I", v))

    def stamp(request):                                  # DAP_Data.timestamp（直前の転送の時刻）を返す所
        if request & TIMESTAMP:
            stamps.append(2 + len(out))
            out.extend(bytes(4))

    post_read = check_write = 0
    count, resp = 0, 0
    for request, value in requests:
        if request & RNW:
            if post_read:
                if request & (APNDP | MATCH_VALUE) == APNDP:
                    resp, data = xfer(request)
                else:
                    resp, data = xfer(DP_RDBUFF_R)
                    post_read = 0
                if resp != RESP_OK:
                    break
                store(data)
                if post_read:
                    stamp(request)                       # 次の AP の読みのポストの時刻（前の値の後）
            if request & MATCH_VALUE:
                retry = match_retry
                if request & APNDP:
                    resp, _ = xfer(request)
                    if resp != RESP_OK:
                        break
                while True:
                    resp, data = xfer(request)
                    if resp != RESP_OK:
                        break
                    if data & match_mask == value or retry == 0:
                        break
                    retry -= 1
                if resp != RESP_OK:
                    break
                if data & match_mask != value:
                    resp |= RESP_MISMATCH
                    break
            elif post_read == 0:
                if request & APNDP:
                    resp, _ = xfer(request)
                    if resp != RESP_OK:
                        break
                    stamp(request)
                    post_read = 1
                else:
                    resp, data = xfer(request)
                    if resp != RESP_OK:
                        break
                    stamp(request)
                    store(data)
            check_write = 0
        else:
            if post_read:
                resp, data = xfer(DP_RDBUFF_R)
                if resp != RESP_OK:
                    break
                store(data)
                post_read = 0
            if request & MATCH_MASK:
                match_mask = value
                resp = RESP_OK
            else:
                resp, _ = xfer(request, value)
                if resp != RESP_OK:
                    break
                stamp(request)
                check_write = 1
        count += 1
    if resp == RESP_OK:
        if post_read:
            resp, data = xfer(DP_RDBUFF_R)
            if resp == RESP_OK:
                store(data)
        elif check_write:
            resp, _ = xfer(DP_RDBUFF_R)
    return bytes([count, resp]) + bytes(out), match_mask, stamps


class WireTimes:
    """debug_port の応答（どの転送でも）の線上の点の時刻（rsp_time の下位 32 ビット）を集める。"""

    def __init__(self, dut):
        self.dut, self.times = dut, []
        cocotb.start_soon(self._run())

    async def _run(self):
        while True:
            await RisingEdge(self.dut.clk)
            await ReadOnly()
            if int(self.dut.rsp_valid_out.value):
                self.times.append(int(self.dut.rsp_time.value) & 0xFFFFFFFF)


def check_reply(got: bytes, expected: bytes, stamps, wire: list[int], context):
    """タイムスタンプの所を除いて一致し、タイムスタンプは順に増え、どれもこのコマンドの間の転送の線上の点の時刻であること。"""
    body = bytearray(got)
    values = []
    for at in stamps:
        values.append(struct.unpack_from("<I", body, at)[0])
        body[at:at + 4] = bytes(4)
    assert bytes(body) == expected, (*context, got.hex(), expected.hex())
    assert values == sorted(values) and len(set(values)) == len(values), (*context, values)
    assert set(values) <= set(wire), (*context, values, wire)


def encode(requests) -> bytes:
    body = b""
    for request, value in requests:
        body += bytes([request])
        if value is not None:
            body += struct.pack("<I", value)
    return bytes([0x05, 0, len(requests)]) + body


def random_requests(rng: random.Random, base: int, *, jtag: bool = False):
    reqs = []
    for _ in range(rng.choice([1, 2, 3, 5, 8, 16, 30])):
        k = rng.random()
        if k < 0.25:
            reqs.append((0x0F, None))                                   # AP DRW の読み
        elif k < 0.40:
            reqs.append((0x0D, rng.getrandbits(32)))                    # AP DRW の書き
        elif k < 0.50:
            reqs.append((AP_TAR_W, base + 4 * rng.randrange(0, 16)))    # TAR
        elif k < 0.58:
            # DP の読み（DPIDR・CTRL/STAT・RDBUFF。JTAG は CTRL/STAT だけ: JTAG-DP の模型の 0 番は IDCODE、RDBUFF は JTAG の作り）
            reqs.append((0x06 if jtag else rng.choice([0x02, 0x06, 0x0E]), None))
        elif k < 0.63:
            reqs.append((0x07, None))                                   # AP TAR の読み
        elif k < 0.68:
            reqs.append((0x01, rng.choice([0x23000012, 0x23000002])))  # CSW（32 ビット、増加あり / なし）
        elif k < 0.76:
            reqs.append((0x0F | MATCH_VALUE, rng.choice([0, 0x1234, rng.getrandbits(32)])))   # AP の値一致
        elif k < 0.80:
            reqs.append((0x06 | MATCH_VALUE, rng.choice([0, 0xF0000000])))                     # DP の値一致
        elif k < 0.86:
            reqs.append((MATCH_MASK, rng.choice([0xFFFFFFFF, 0x0000FFFF, 0])))                 # 一致マスク
        else:
            reqs.append((0x08, 0))                                      # DP SELECT（bank 0）
    # タイムスタンプ（どの要求にも付けてよい。値一致・一致マスクでは DAP.c も返さない）
    return [(r | TIMESTAMP if rng.random() < 0.3 else r, v) for r, v in reqs]


async def swd_differential(dut, *, hz: int | None, seed: int, batches: int):
    """SWD: ランダムな並びを batches 組送り、応答と線に出た転送の並びを参照実装の写しと突き合わせる（hz = None は既定の速さ）。"""
    dap, model = await setup(dut, seed=seed, output_delay_ns=3)
    await dap.connect()
    if hz is not None:
        assert await dap.command(bytes([0x11]) + struct.pack("<I", hz)) == bytes([0x11, 0])
    await dap.mem_setup()
    rng = random.Random(seed * 7919 + (hz or 0))
    base = 0x20000000
    for i in range(16):
        model.memory[base + 4 * i] = rng.choice([0, 0x1234, rng.getrandbits(32)])
    wait_retry, match_retry, match_mask = 100, 0, 0                # 一致マスクの初期値は 0（DAP.c の DAP_Setup）
    wire = WireTimes(dut)
    outcomes = set()
    for batch in range(batches):
        if batch % 20 == 0:                                             # 再試行の回数を変える（TransferConfigure）
            wait_retry, match_retry = rng.choice([(100, 0), (3, 2), (0, 5), (2, 1), (1, 0)])
            assert await dap.command(bytes([0x04, 0]) + struct.pack("<HH", wait_retry, match_retry)) == bytes([0x04, 0])
        # 前の誤り（STICKYERR）を消して始める
        assert (await dap.transfer((DP_ABORT_W, STKERRCLR)))[:2] == (1, 1)
        model.fault_addresses = {base + 4 * 13} if rng.random() < 0.3 else set()
        model.wait_next = rng.choice([0] * 6 + [1, 2, 4, 120])
        model.wait_at = len(model.transactions) + rng.randrange(1, 12) if rng.random() < 0.25 else None
        model.corrupt_parity_at = len(model.transactions) + rng.randrange(1, 12) if rng.random() < 0.12 else None
        requests = random_requests(rng, base)
        shadow = ShadowTarget(model)
        start = len(model.transactions)
        expected, mask_after, stamps = reference_transfer(shadow, requests, wait_retry=wait_retry,
                                                          match_retry=match_retry, match_mask=match_mask)
        wire.times.clear()
        got = await dap.command(encode(requests))
        check_reply(got[1:], expected, stamps, wire.times, (hz, batch, requests))
        assert model.transactions[start:] == shadow.t.transactions[start:], (
            hz, batch, requests, model.transactions[start:], shadow.t.transactions[start:])
        match_mask = mask_after
        outcomes.add(expected[1])
        model.wait_next, model.wait_at, model.corrupt_parity_at = 0, None, None
    # 失敗の扱いも突き合わせたこと（どの終わり方も一度は出る）
    assert {RESP_OK, ACK_WAIT, ACK_FAULT, RESP_ERROR, RESP_OK | RESP_MISMATCH} <= outcomes, sorted(outcomes)


@cocotb.test()
async def transfer_matches_the_reference_implementation(dut):
    """SWD・既定の速さ（線の時間に対して先出しの余裕が大きい）。"""
    await swd_differential(dut, hz=None, seed=21, batches=160)


@cocotb.test()
async def transfer_matches_the_reference_implementation_at_sys_half(dut):
    """SWD・50 MHz（half_period 1。次の転送を出す時間の余裕がいちばん小さい）。"""
    await swd_differential(dut, hz=50_000_000, seed=31, batches=160)


@cocotb.test()
async def zero_requests_answer_zero_and_touch_nothing(dut):
    """要求 0 個: 処理数 0・応答 0 で、線には何も出さない（書きの後でも確認の RDBUFF は前のコマンドのもので、ここでは出さない）。"""
    dap, model = await setup(dut, seed=3)
    await dap.connect()
    await dap.mem_setup()
    n = len(model.transactions)
    for _ in range(2):
        assert await dap.command(bytes([0x05, 0, 0])) == bytes([0x05, 0, 0])
    assert len(model.transactions) == n


async def jtag_differential(dut, *, hz: int, sample_delay: int, tdo_delay_ns: float, wait_after_ap: int):
    """JTAG（JTAG-DP の模型。レジスタの振る舞いは SWD の模型と同じ）: 応答のバイト列を参照実装の写しと突き合わせる。
    線の上のスキャンの並びは JTAG の作り（jtag_phy）なので比べない。"""
    dap, dp = await jtag_setup(dut, hz, sample_delay, tdo_delay_ns, wait_after_ap=wait_after_ap)
    await dap.mem_setup()
    base = 0x20000000
    rng = random.Random(hz + 17 * wait_after_ap + sample_delay)
    for i in range(16):
        dp.regs.memory[base + 4 * i] = rng.choice([0, 0x1234, rng.getrandbits(32)])
    match_retry, match_mask = 2, 0
    wire = WireTimes(dut)
    assert await dap.command(bytes([0x04, 0]) + struct.pack("<HH", 100, match_retry)) == bytes([0x04, 0])
    for batch in range(60):
        requests = random_requests(rng, base, jtag=True)
        shadow = ShadowTarget(dp.regs)
        expected, match_mask_after, stamps = reference_transfer(shadow, requests, wait_retry=100,
                                                                match_retry=match_retry, match_mask=match_mask)
        wire.times.clear()
        got = await dap.command(encode(requests))
        check_reply(got[1:], expected, stamps, wire.times, (hz, batch, requests))
        match_mask = match_mask_after


@cocotb.test()
async def jtag_transfer_matches_the_reference_at_sys_half_with_wait(dut):
    await jtag_differential(dut, hz=50_000_000, sample_delay=0, tdo_delay_ns=0, wait_after_ap=2)


@cocotb.test()
async def jtag_transfer_matches_the_reference_with_a_late_tdo(dut):
    await jtag_differential(dut, hz=50_000_000, sample_delay=6, tdo_delay_ns=55, wait_after_ap=1)


@cocotb.test()
async def disconnected_transfers_answer_like_the_dummy_and_touch_nothing(dut):
    """未接続（DAP_Disconnect の後）: Transfer は処理数 0・応答 0、TransferBlock は処理数 0・応答 0、WriteAbort・JTAG_IDCODE は
    DAP_ERROR（DAP.c の DAP_Dummy_Transfer・DAP_WriteAbort・DAP_JTAG_IDCODE）。線には何も出さない。"""
    dap, model = await setup(dut, seed=3)
    await dap.connect()
    await dap.mem_setup()
    assert await dap.command(bytes([0x03])) == bytes([0x03, 0])
    n = len(model.transactions)
    assert await dap.command(encode([(0x0F, None), (0x0D, 1), (0x02 | TIMESTAMP, None)])) == bytes([0x05, 0, 0])
    assert await dap.command(bytes([0x06, 0, 3, 0, 0x0F])) == bytes([0x06, 0, 0, 0])
    assert await dap.command(bytes([0x08, 0]) + struct.pack("<I", STKERRCLR)) == bytes([0x08, 0xFF])
    assert await dap.command(bytes([0x16, 0])) == bytes([0x16, 0xFF])
    assert len(model.transactions) == n
    # 繋ぎ直せば元どおり
    await dap.connect()
    assert await dap.command(bytes([0x08, 0]) + struct.pack("<I", STKERRCLR)) == bytes([0x08, 0])


@cocotb.test()
async def info_reports_the_timestamp_clock(dut):
    """Info 0x08 = タイムスタンプの時計（clk の周波数）、Capabilities の bit5 = タイムスタンプがある。"""
    dap, _ = await setup(dut, seed=3)
    assert await dap.command(bytes([0x00, 0x08])) == bytes([0x00, 4]) + struct.pack("<I", 100_000_000)
    caps = await dap.command(bytes([0x00, 0xF0]))
    assert caps[1] == 1 and caps[2] & 0x20
