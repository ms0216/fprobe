"""dap_monitor のテストベンチ: ホストの転送（命令の受理 → 応答）を入れ、DRW のメモリの番地を追えたときだけ番地を出す
（ev_addr_known）こと、追えない DRW（CSW・TAR の前・別の AP・1 KB の境界の先・packed）は番地を出さず、番地一致も
起こさないことを確かめる。"""

import cocotb
from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge

from tests.tb.common import start_clock_and_reset

OK = 0b001
SELECT_W, CSW_W, TAR_W, DRW_W, DRW_R = 0x8, 0x1, 0x5, 0xD, 0xF     # 要求: A[3:2] << 2 | RnW << 1 | APnDP
CSW_WORD_INC = 0x12                                                # 32 ビット・単純増加
CSW_WORD_PACKED = 0x22                                             # 32 ビット・packed


async def transfer(dut, request, data=0, rsp=0, ack=OK):
    """1 転送。返り値 = (ev_addr, ev_addr_known, match)"""
    await FallingEdge(dut.clk)
    dut.cmd_fire.value, dut.cmd_op.value, dut.cmd_req.value, dut.cmd_data.value = 1, 0b10, request, data
    await FallingEdge(dut.clk)
    dut.cmd_fire.value = 0
    dut.rsp_valid.value, dut.rsp_ack.value, dut.rsp_data.value = 1, ack, rsp
    await RisingEdge(dut.clk)
    await ReadOnly()
    out = (int(dut.ev_addr.value), int(dut.ev_addr_known.value), int(dut.match.value))
    assert dut.ev_valid.value == 1
    await FallingEdge(dut.clk)
    dut.rsp_valid.value = 0
    return out


async def setup(dut):
    for name in ("cmd_fire", "cmd_op", "cmd_req", "cmd_data", "rsp_valid", "rsp_ack", "rsp_data", "rsp_parity",
                 "match_addr", "addr_mask", "match_data", "data_mask", "on_write", "on_read"):
        getattr(dut, name).value = 0
    await start_clock_and_reset(dut)


@cocotb.test()
async def drw_address_is_given_only_when_tracked(dut):
    await setup(dut)
    assert await transfer(dut, DRW_W, 1) == (0xC, 0, 0)                     # CSW・TAR の前: 番地は追えない
    await transfer(dut, SELECT_W, 0x00000000)                               # AP 0、bank 0
    await transfer(dut, CSW_W, CSW_WORD_INC)
    await transfer(dut, TAR_W, 0x20000000)
    assert (await transfer(dut, DRW_W, 1))[:2] == (0x20000000, 1)
    assert (await transfer(dut, DRW_R))[:2] == (0x20000004, 1)              # 単純増加
    await transfer(dut, SELECT_W, 0x01000000)                               # AP 1: CSW・TAR を書いていない
    assert (await transfer(dut, DRW_W, 1))[:2] == (0xC, 0)
    await transfer(dut, SELECT_W, 0x00000000)                               # AP 0 に戻ると追える（AP 1 の DRW は AP 0 の TAR を動かさない）
    assert (await transfer(dut, DRW_W, 1))[:2] == (0x20000008, 1)


@cocotb.test()
async def the_1kb_boundary_and_packed_increments_are_not_followed(dut):
    await setup(dut)
    await transfer(dut, SELECT_W, 0)
    await transfer(dut, CSW_W, CSW_WORD_INC)
    await transfer(dut, TAR_W, 0x200003FC)
    assert (await transfer(dut, DRW_W, 1))[:2] == (0x200003FC, 1)
    assert (await transfer(dut, DRW_W, 1))[:2] == (0xC, 0)                  # 境界の先は実装次第
    await transfer(dut, TAR_W, 0x20000100)                                  # TAR を書き直せば追える
    assert (await transfer(dut, DRW_W, 1))[:2] == (0x20000100, 1)
    await transfer(dut, CSW_W, CSW_WORD_PACKED)
    assert (await transfer(dut, DRW_W, 1))[:2] == (0xC, 0)                  # packed は追わない
    await transfer(dut, CSW_W, CSW_WORD_INC)
    assert (await transfer(dut, DRW_W, 1))[:2] == (0xC, 0)                  # packed の DRW の後の TAR は分からない


class AddressModel:
    """仕様（dap_monitor の冒頭）から書いた基準: ホストが書いた CSW・TAR を追って DRW のメモリの番地を求める。"""

    def __init__(self):
        self.sel = 0
        self.csw = None                                  # (AP, size, inc)
        self.tar = None                                  # (AP, 値)

    def step(self, request, data, ack, match_addr, on_write, on_read):
        ap, rnw, a = request & 1, request >> 1 & 1, request >> 2 & 3
        sel_ap, bank0 = self.sel >> 24, (self.sel >> 4 & 0xF) == 0
        drw = ap and a == 3 and bank0
        tracked = (self.csw is not None and self.tar is not None and self.csw[0] == sel_ap
                   and self.tar[0] == sel_ap and self.csw[2] < 2)
        addr, known = ((self.tar[1], 1) if drw and tracked else (a << 2, 0 if drw else 1))
        match = 0
        if ack == OK:
            if not ap and not rnw and a == 2:
                self.sel = data
            elif ap and bank0:
                if a == 0 and not rnw:
                    self.csw = (sel_ap, data & 7, data >> 4 & 3)
                elif a == 1 and not rnw:
                    self.tar = (sel_ap, data)
                elif a == 3 and tracked:
                    if (addr ^ match_addr) == 0 and ((not rnw and on_write) or (rnw and on_read)):
                        match = 1
                    if self.csw[2] == 1:
                        nxt = (self.tar[1] + (1 << (self.csw[1] & 3))) & 0xFFFFFFFF
                        self.tar = (self.tar[0], nxt) if nxt >> 10 == self.tar[1] >> 10 else None
                elif a == 3 and self.tar is not None and self.tar[0] == sel_ap:
                    self.tar = None
        return addr, known, match


@cocotb.test()
async def random_transfers_match_the_address_model(dut):
    """SELECT・CSW・TAR・DRW・他のレジスタのランダムな並び（AP・bank・サイズ・増加方式・1 KB の境界・WAIT / FAULT の応答を
    混ぜる）で、イベントの番地・追えたか・一致が基準と合う。"""
    import random
    await setup(dut)
    rng = random.Random(2468)
    model = AddressModel()
    dut.on_write.value, dut.on_read.value, dut.addr_mask.value, dut.data_mask.value = 1, 1, 0xFFFFFFFF, 0
    for i in range(1500):
        kind = rng.choice(["sel", "csw", "tar", "tar", "drw", "drw", "drw", "drw", "other"])
        ack = rng.choice([OK] * 8 + [0b010, 0b100])
        if kind == "sel":
            request, data = SELECT_W, rng.choice([0, 0, 0x01000000, 0x000000F0, 0x00000010]) | rng.choice([0, 0])
        elif kind == "csw":
            request, data = CSW_W, rng.choice([0x12, 0x02, 0x22, 0x10, 0x11, 0x00, 0x01]) | rng.getrandbits(1) << 24
        elif kind == "tar":
            request, data = TAR_W, rng.choice([0x20000000, 0x200003F8, 0x200003FC, 0x20000FF0, rng.getrandbits(32)])
        elif kind == "drw":
            request, data = rng.choice([DRW_W, DRW_R]), rng.getrandbits(32)
        else:
            request, data = rng.choice([0x2, 0x6, 0x3, 0x7, 0x0, 0xE, 0x4]), rng.getrandbits(32)
        match_addr = rng.choice([0x20000000, 0x20000004, 0x200003FC, 0x20000400])
        dut.match_addr.value = match_addr
        got = await transfer(dut, request, data, rsp=rng.getrandbits(32), ack=ack)
        want = model.step(request, data, ack, match_addr, True, True)
        assert got == want, (i, hex(request), hex(data), ack, got, want)


@cocotb.test()
async def address_match_only_on_tracked_drw(dut):
    await setup(dut)
    dut.match_addr.value, dut.addr_mask.value, dut.on_write.value = 0xC, 0xFFFFFFFF, 1
    assert (await transfer(dut, DRW_W, 1))[2] == 0                          # 追えない DRW の「番地 0xC」とは一致させない
    await transfer(dut, SELECT_W, 0)
    await transfer(dut, CSW_W, CSW_WORD_INC)
    await transfer(dut, TAR_W, 0x20000000)
    dut.match_addr.value = 0x20000000
    assert (await transfer(dut, DRW_W, 1))[2] == 1
