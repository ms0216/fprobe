"""obs_core v2（対象の FPGA に入れる観測の回路。docs/superpowers/specs/2026-10-20-fpga-observation-v2-design.md）。
ユーザーの DR の口（BSCANE2 と同じ意味）を Python で駆動する。TCK 10 MHz・clk 50 MHz（非同期）。"""

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge, RisingEdge, Timer
from cocotb.utils import get_sim_time

OP_NOP, OP_WRITE, OP_READ, OP_SYNC = 0, 1, 2, 3
REG_ID_LO, REG_ID_HI, REG_VERSION, REG_GROUPS, REG_FIFO_DEPTH, REG_CLOCK_LO, REG_CLOCK_HI = range(7)
REG_SYNC_DELAY, REG_SAMPLE_DELAY, REG_LOST_BITS = 7, 8, 9
REG_CONTROL, REG_SELECT = 14, 15
T_VALUE, T_MARK, T_OVERFLOW, T_CONTROL = 0, 1, 2, 3
MARK_SYNC = 1 << 15
CTRL_GROUP, CTRL_RESPONSE = 0, 1
CLK_NS = 20


def command(op=OP_NOP, addr=0, data=0) -> int:
    return op | addr << 4 | data << 8


class UserDr:
    """TCK を自分で出し、Run-Test/Idle → Select-DR-Scan → Capture-DR → Shift-DR（n ビット）→ Exit1-DR → Update-DR →
    Run-Test/Idle を 1 回のスキャンとして行う（TAP と同じ数の TCK を出す）。"""

    def __init__(self, dut, period_ns=100):
        self.dut, self.half = dut, period_ns / 2
        self.update_ns = 0
        for name in ("tck", "tdi", "sel", "capture", "shift", "update", "tlr"):
            getattr(dut, name).value = 0

    async def pulse(self):
        await Timer(self.half, "ns")
        self.dut.tck.value = 1
        await Timer(self.half, "ns")
        self.dut.tck.value = 0

    async def reset(self):
        self.dut.tlr.value = 1
        await self.pulse()
        self.dut.tlr.value = 0

    async def scan(self, cmd: int = 0, words: int = 0, bits: int | None = None) -> list[int]:
        """命令 cmd（最後の 16 ビット）を送り、見出し 1 語 + 記録 words 語を受け取る（bits で送るビット数を変えられる）。"""
        dut = self.dut
        n = 32 * (1 + words) if bits is None else bits
        dut.sel.value = 1
        await self.pulse()                                       # Run-Test/Idle → Select-DR-Scan
        await self.pulse()                                       # → Capture-DR
        dut.capture.value = 1
        await self.pulse()
        dut.capture.value = 0
        dut.shift.value = 1
        out = []
        for i in range(n):
            dut.tdi.value = (cmd >> (i - (n - 16))) & 1 if i >= n - 16 else 0
            await Timer(1, "ns")
            out.append(int(dut.tdo.value))                  # 立ち上がりの前に TDO を読む
            await self.pulse()
        dut.shift.value = 0
        await self.pulse()                                       # Exit1-DR → Update-DR
        dut.update.value = 1
        await Timer(self.half, "ns")
        self.update_ns = get_sim_time("ns") + self.half          # Update-DR を出る立ち上がり
        await Timer(self.half, "ns")
        dut.tck.value = 1
        await Timer(self.half, "ns")
        dut.tck.value = 0
        dut.update.value = 0
        dut.sel.value = 0
        return [sum(out[32 * w + k] << k for k in range(32)) for w in range(n // 32)]


class Target:
    """試験の相手: DR と、受けた記録の列（drain で吸い出してためる）。"""

    def __init__(self, dut):
        self.dut, self.dr, self.records = dut, UserDr(dut), []

    async def drain(self, max_words=64):
        """有効な記録を全部吸い出して self.records に足す（見出しの count が 0 になるまで）。"""
        got = []
        for _ in range(40):
            r = await self.dr.scan(0, max_words)
            assert r[0] >> 24 == 0xA6                                 # v2 の印
            count = r[0] & 0xFFFF
            if count == 0:
                self.records += got
                return got
            got += r[1:1 + min(count, max_words)]
            assert not any(r[1 + count:])                             # 有効な記録の後は 0
        raise AssertionError("records never ran out")

    async def read_reg(self, addr: int) -> int:
        await self.dr.scan(command(OP_READ, addr))
        for _ in range(5):
            for w in await self.drain():
                if is_response(w) and w >> 16 & 0xF == addr:
                    return w & 0xFFFF
        raise AssertionError(f"no response for register {addr}")

    async def write_reg(self, addr: int, data: int):
        await self.dr.scan(command(OP_WRITE, addr, data))

    async def start(self):
        await self.write_reg(REG_CONTROL, 1)


CLOCK_START = {}                        # clk を動かし始めた時刻（辺の位相。試験ごとに違う）


async def setup(dut, probes=0) -> Target:
    CLOCK_START["ns"] = get_sim_time("ns")
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    dut.rst.value = 1
    dut.probes.value = probes
    target = Target(dut)
    await ClockCycles(dut.clk, 5)
    await target.dr.reset()                            # 前の試験の CONTROL を消してから（明けに写し直すので）
    await ClockCycles(dut.clk, 5)
    dut.rst.value = 0
    await ClockCycles(dut.clk, 5)
    return target


def cycle_at(t_ns: float) -> float:
    """シミュレーションの時刻 t_ns を、clk の辺の番号（連続の値。最初の立ち上がり = 0）にする。"""
    return (t_ns - CLOCK_START["ns"]) / CLK_NS


def kind(w):
    return w >> 30


def is_response(w):
    return kind(w) == T_CONTROL and w >> 26 & 0xF == CTRL_RESPONSE


def is_group(w, g=None):
    return kind(w) == T_CONTROL and w >> 26 & 0xF == CTRL_GROUP and (g is None or w & 0xFF == g)


def is_sync(w):
    return kind(w) == T_MARK and w & MARK_SYNC


def value_of(w):
    return w & 0xFFFF


def delta_of(w):
    return w >> 16 & 0x3FFF                                     # 時刻のある記録はどれも 29:16


def chains(records):
    """記録の列を PC と同じ規則で読む: [(鎖の時刻, 種類, 中身)]。鎖の時刻は鎖の始め（delta 0 の値）からの周期数、
    溢れで切れる（None）。中身: 値 → 値、印 → (番号, 印の瞬間 = 時刻 − late)、組 → 番号、刻み → None。"""
    t, out = None, []
    for w in records:
        k = kind(w)
        if k == T_OVERFLOW:
            t = None
            out.append((None, "overflow", w & 0xFFFF))
        elif k == T_CONTROL:
            if is_group(w):
                out.append((t, "group", w & 0xFF))
        elif k == T_VALUE:
            d = delta_of(w)
            t = 0 if d == 0 else (None if t is None else t + d)
            out.append((t, "value", value_of(w)))
        elif is_sync(w):
            t = None if t is None else t + delta_of(w)
            out.append((t, "sync", (w >> 7 & 0xFF, None if t is None else t - (w & 0x7F))))
        else:
            t = None if t is None else t + delta_of(w)
            out.append((t, "tick", None))
    return out


def values(records):
    return [(t, v) for t, k, v in chains(records) if k == "value"]


def changes(records):
    """値の変化だけ（印の後の書き直し = 前と同じ値を除く）。"""
    out = []
    for t, v in values(records):
        if not out or out[-1][1] != v:
            out.append((t, v))
    return out


@cocotb.test()
async def id_and_registers(dut):
    tg = await setup(dut)
    assert await tg.read_reg(REG_ID_LO) == 0x4253 and await tg.read_reg(REG_ID_HI) == 0x464F     # "FOBS"
    assert await tg.read_reg(REG_VERSION) == 2
    assert await tg.read_reg(REG_GROUPS) == 2
    assert await tg.read_reg(REG_FIFO_DEPTH) == 16
    assert await tg.read_reg(REG_CLOCK_LO) | await tg.read_reg(REG_CLOCK_HI) << 16 == 50_000_000
    assert await tg.read_reg(REG_SAMPLE_DELAY) == 1
    assert await tg.read_reg(REG_LOST_BITS) == len(dut.lost)
    assert await tg.read_reg(REG_CONTROL) == 0
    await tg.write_reg(REG_SELECT, 1)
    assert await tg.read_reg(REG_SELECT) == 1


@cocotb.test()
async def value_changes_become_records(dut):
    tg = await setup(dut, probes=0x1111)
    await tg.start()
    await ClockCycles(dut.clk, 30)
    for v, wait in ((0x2222, 10), (0x3333, 7), (0x4444, 100)):
        await FallingEdge(dut.clk)
        dut.probes.value = v
        await ClockCycles(dut.clk, wait)
    rec = await tg.drain()
    assert is_group(rec[0], 0) and kind(rec[1]) == T_VALUE and delta_of(rec[1]) == 0   # 始め: 組・鎖の始めの値
    vs = values(rec)
    assert [v for _, v in vs] == [0x1111, 0x2222, 0x3333, 0x4444]
    assert [b[0] - a[0] for a, b in zip(vs[1:], vs[2:])] == [10, 7]                    # 変化の間の周期数


@cocotb.test()
async def long_quiet_times_are_counted_with_ticks(dut):
    tg = await setup(dut, probes=0x0001)
    await tg.start()
    await ClockCycles(dut.clk, 20)
    await FallingEdge(dut.clk)
    dut.probes.value = 0x0002
    await ClockCycles(dut.clk, 40000)
    await FallingEdge(dut.clk)
    dut.probes.value = 0x0003
    await ClockCycles(dut.clk, 5)
    rec = await tg.drain()
    assert sum(1 for _, k, _ in chains(rec) if k == "tick") == 2                      # 16383 周期ごとに刻み
    t = dict((v, t) for t, v in values(rec))
    assert t[0x0003] - t[0x0002] == 40000


@cocotb.test()
async def overflow_is_reported_and_restarts_the_chain(dut):
    tg = await setup(dut, probes=0)
    await tg.start()
    await ClockCycles(dut.clk, 5)
    for v in range(1, 41):                                     # FIFO 16 語に 40 回の変化
        await FallingEdge(dut.clk)
        dut.probes.value = v
        await ClockCycles(dut.clk, 2)
    await ClockCycles(dut.clk, 20)
    rec = await tg.drain()
    k = next(k for k, w in enumerate(rec) if kind(w) == T_OVERFLOW)
    assert rec[k] & 0xFFFF > 0
    assert is_group(rec[k - 1], 0) and rec[k + 1] == value_of(rec[k + 1]) == 40       # 組 → 溢れ → 鎖の始め（delta 0）の今の値
    r = await tg.dr.scan()
    assert r[0] >> 16 & 1                                                             # 見出しの溢れの印


@cocotb.test()
async def select_switches_the_group_without_breaking_the_chain(dut):
    tg = await setup(dut, probes=0x0001_0000)
    await tg.start()
    await ClockCycles(dut.clk, 20)
    await tg.write_reg(REG_SELECT, 1)
    await ClockCycles(dut.clk, 20)
    await FallingEdge(dut.clk)
    dut.probes.value = 0x0002_0000
    await ClockCycles(dut.clk, 10)
    rec = await tg.drain()
    assert [v for t, k, v in chains(rec) if k == "group"] == [0, 1]
    assert [v for _, v in values(rec)] == [0x0000, 0x0001, 0x0002]
    assert all(t is not None for t, _ in values(rec))                                # 組を変えても鎖は続く


@cocotb.test()
async def a_partly_shifted_record_is_not_lost(dut):
    # 記録は最後のビットまで送り出したときに取り除く。途中で終わったスキャンの記録は次のスキャンでまた出る
    tg = await setup(dut, probes=0x0101)
    await tg.start()
    await ClockCycles(dut.clk, 20)
    r = await tg.dr.scan(0, 2, bits=32 * 2 + 16)
    assert r[0] & 0xFFFF == 2 and is_group(r[1], 0)
    rest = await tg.drain()                                                           # 送り切った組は消え、途中の値は残る
    assert rest == [0x0101]


@cocotb.test()
async def a_command_shorter_than_16_bits_does_nothing(dut):
    tg = await setup(dut)
    await tg.dr.scan(command(OP_WRITE, REG_CONTROL, 1), bits=15)
    await ClockCycles(dut.clk, 10)
    assert await tg.read_reg(REG_CONTROL) == 0


@cocotb.test()
async def the_sync_mark_is_where_the_core_declares(dut):
    # 印の瞬間（鎖の時刻 − late）は Update-DR の瞬間より SYNC_DELAY / 2 周期（平均）遅い。クロックの位相で ±0.5 周期。
    # 鎖の時刻を clk の辺の番号に結ぶには、辺の直後に変えた値（記録は SAMPLE_DELAY 周期後）を使う
    tg = await setup(dut, probes=0)
    declared = await tg.read_reg(REG_SYNC_DELAY) / 2
    sample = await tg.read_reg(REG_SAMPLE_DELAY)
    await tg.start()
    await ClockCycles(dut.clk, 20)
    await RisingEdge(dut.clk)
    await Timer(1, "ns")
    dut.probes.value = 7
    edge = round(cycle_at(get_sim_time("ns") - 1))
    rng = random.Random(3)
    updates = []
    for seq in range(1, 41):
        await Timer(rng.randrange(1, 997), "ns")
        await tg.dr.scan(command(OP_SYNC, 0, seq))
        updates.append((seq, cycle_at(tg.dr.update_ns)))
        await tg.drain()                                           # 印・組・値の 3 語ずつ（FIFO を溢れさせない）
    ch = chains(tg.records)
    t7 = next(t for t, k, v in ch if k == "value" and v == 7)
    offset = edge + sample - t7                                    # 鎖の時刻 → 辺の番号
    marks = {v[0]: v[1] + offset for t, k, v in ch if k == "sync"}
    lag = [marks[s] - u for s, u in updates]
    assert len(lag) == 40
    assert abs(sum(lag) / len(lag) - declared) < 0.1 and declared - 0.5 <= min(lag) and max(lag) <= declared + 0.5


@cocotb.test()
async def records_are_sample_delay_after_the_change(dut):
    # 同じ clk の登録の出力（辺の直後に変わる）の変化の記録は、変化より SAMPLE_DELAY 周期後の時刻（鎖の時刻の差で見る）
    tg = await setup(dut, probes=0)
    await tg.start()
    await ClockCycles(dut.clk, 20)
    edges = []
    for v in range(1, 6):
        await RisingEdge(dut.clk)
        await Timer(1, "ns")
        dut.probes.value = v
        edges.append(round(cycle_at(get_sim_time("ns") - 1)))
        await ClockCycles(dut.clk, 37)
    t = dict((v, t) for t, v in values(await tg.drain()))
    assert [t[v] - t[1] for v in range(1, 6)] == [e - edges[0] for e in edges]


@cocotb.test()
async def after_a_sync_the_group_and_value_are_written_again(dut):
    # 記録の途中（印）から読み始めても、組と今の値が分かる。どれも変化の無い周期に書く（値の時刻は正確なまま）
    tg = await setup(dut, probes=0x0042)
    await tg.start()
    await ClockCycles(dut.clk, 20)
    await tg.drain()
    await tg.dr.scan(command(OP_SYNC, 0, 9))
    await ClockCycles(dut.clk, 20)
    rec = await tg.drain()
    k = next(k for k, w in enumerate(rec) if is_sync(w))
    assert rec[k] >> 7 & 0xFF == 9
    assert any(is_group(w, 0) for w in rec[k + 1:]) and any(kind(w) == T_VALUE and value_of(w) == 0x42 for w in rec[k + 1:])


@cocotb.test()
async def responses_and_syncs_never_shift_a_value(dut):
    # 毎周期変わる間に読み・印を頼んでも、値の記録は毎周期（delta 1）のまま。応答・印は変化の無い周期に書く
    tg = await setup(dut, probes=0)
    await tg.start()
    await ClockCycles(dut.clk, 20)

    async def count_up():
        for v in range(1, 12):
            await FallingEdge(dut.clk)
            dut.probes.value = v
    task = cocotb.start_soon(count_up())
    await tg.dr.scan(command(OP_SYNC, 0, 3))
    await task
    await tg.dr.scan(command(OP_READ, REG_GROUPS))
    await ClockCycles(dut.clk, 30)
    rec = await tg.drain()
    vs = changes(rec)
    assert [v for _, v in vs] == list(range(0, 12))
    assert all(b[0] - a[0] == 1 for a, b in zip(vs[1:], vs[2:]))
    assert any(is_sync(w) for w in rec) and any(is_response(w) for w in rec)


@cocotb.test()
async def the_header_carries_a_mark_and_the_running_flag(dut):
    tg = await setup(dut)
    r = await tg.dr.scan()
    assert r[0] >> 24 == 0xA6 and not r[0] >> 17 & 1
    await tg.start()
    await ClockCycles(dut.clk, 10)
    r = await tg.dr.scan()
    assert r[0] >> 24 == 0xA6 and r[0] >> 17 & 1


@cocotb.test()
async def recording_resumes_after_the_target_clock_domain_is_reset(dut):
    # 対象の回路のリセット（clk 側）の後も、PC が CONTROL を書き直さなくても記録を続ける（組・鎖の始めの値から）
    tg = await setup(dut, probes=0x0010)
    await tg.start()
    await ClockCycles(dut.clk, 20)
    await tg.drain()
    dut.rst.value = 1
    await ClockCycles(dut.clk, 3)
    dut.rst.value = 0
    await ClockCycles(dut.clk, 20)
    await FallingEdge(dut.clk)
    dut.probes.value = 0x0020
    await ClockCycles(dut.clk, 20)
    rec = await tg.drain()
    assert is_group(rec[0], 0) and rec[1] == 0x0010                   # delta 0 = 鎖の始め
    assert [v for _, v in values(rec)] == [0x0010, 0x0020]


@cocotb.test()
async def test_logic_reset_toggles_the_register_handshake_once(dut):
    # TAP が Test-Logic-Reset に何周期いても、clk 側への合図（トグル）は入ったときの 1 回だけ（2 周期で打ち消していた）
    tg = await setup(dut)
    await tg.dr.scan()                                 # Test-Logic-Reset から出る TCK（実際の TAP と同じ）
    before = int(dut.t_reg_tog.value)
    dut.tlr.value = 1
    await tg.dr.pulse()
    await tg.dr.pulse()
    dut.tlr.value = 0
    await tg.dr.pulse()
    assert int(dut.t_reg_tog.value) != before


@cocotb.test()
async def the_lost_count_stops_at_its_declared_maximum(dut):
    # 失った数は LOST_BITS で数え、上限で止まる（PC は「上限以上」と読む）。試験は LOST_BITS を小さくして溢れさせる
    tg = await setup(dut, probes=0)
    maximum = (1 << await tg.read_reg(REG_LOST_BITS)) - 1
    await tg.start()
    await ClockCycles(dut.clk, 20)
    for v in range(1, 200):                                   # FIFO（16 語）を吸い出さずに変え続ける
        await FallingEdge(dut.clk)
        dut.probes.value = v
        await ClockCycles(dut.clk, 2)
    await ClockCycles(dut.clk, 20)
    rec = await tg.drain()
    assert [w & 0xFFFF for w in rec if kind(w) == T_OVERFLOW] == [maximum]


async def _overflow(dut, tg):
    for v in range(1, 41):                                     # FIFO 16 語に 40 回の変化
        await FallingEdge(dut.clk)
        dut.probes.value = v
        await ClockCycles(dut.clk, 2)


@cocotb.test()
async def a_select_after_a_recovered_overflow_writes_no_overflow(dut):
    # R17: 溢れから立ち直った後の SELECT で、溢れていないのに溢れの記録（前の数 + 組の差）を書かない
    tg = await setup(dut, probes=0)
    await tg.start()
    await ClockCycles(dut.clk, 5)
    await _overflow(dut, tg)
    await ClockCycles(dut.clk, 20)
    await tg.drain()
    await ClockCycles(dut.clk, 50)
    await tg.drain()
    await tg.write_reg(REG_SELECT, 1)
    await ClockCycles(dut.clk, 30)
    rec = await tg.drain()
    assert not any(kind(w) == T_OVERFLOW for w in rec), [hex(w) for w in rec]


@cocotb.test()
async def a_change_while_restarting_after_an_overflow_is_counted(dut):
    # R17: 溢れの後の立て直し（組の記録を書く周期）に立った 1 周期の脈も、失った変化として数える
    tg = await setup(dut, probes=0)
    await tg.start()
    await ClockCycles(dut.clk, 5)
    await _overflow(dut, tg)

    async def pulse_at_group():
        while True:
            await FallingEdge(dut.clk)
            if int(dut.state.value) == 3:                         # S_GROUP（この立ち上がりで組を書く）
                dut.probes.value = 0x100
                await FallingEdge(dut.clk)
                dut.probes.value = 40
                return
    task = cocotb.start_soon(pulse_at_group())
    rec = []
    for _ in range(10):
        rec += await tg.drain()
        await ClockCycles(dut.clk, 20)
    await task
    k = next(i for i, w in enumerate(rec) if kind(w) == T_OVERFLOW)
    before = [value_of(w) for w in rec[:k] if kind(w) == T_VALUE]
    lost = sum(w & 0xFFFF for w in rec if kind(w) == T_OVERFLOW)
    assert lost + (len(before) - 1) == 40 + 2, (lost, before)      # 起こした変化 = 記録した変化 + 失った数


@cocotb.test()
async def select_while_running_keeps_watching_the_old_group_until_the_switch(dut):
    # R17: 記録中の SELECT は再同期しない。切り替えの記録を書く周期まで前の組を見続ける（その間の前の組の脈も記録に残る）
    tg = await setup(dut, probes=0x0010_0001)
    await tg.start()
    await ClockCycles(dut.clk, 20)
    await tg.drain()
    task = cocotb.start_soon(tg.write_reg(REG_SELECT, 1))
    while True:                                                    # 書いたことを clk 側が見る直前の周期
        await FallingEdge(dut.clk)
        s = int(dut.reg_sync.value)
        if (s >> 2 & 1) != (s >> 1 & 1):
            break
    dut.probes.value = 0x0010_0002                                 # その周期に前の組（0）に 1 周期の脈
    await FallingEdge(dut.clk)
    dut.probes.value = 0x0010_0001
    await task
    await ClockCycles(dut.clk, 20)
    rec = await tg.drain()
    ch = chains(rec)
    k = next(i for i, (_, kd, v) in enumerate(ch) if kd == "group" and v == 1)
    assert [v for _, kd, v in ch[:k] if kd == "value"][-2:] == [2, 1], ch       # 前の組の脈が残る
    assert [v for _, kd, v in ch[k:] if kd == "value"][:1] == [0x10]
    assert not any(kd == "overflow" for _, kd, _ in ch)


@cocotb.test()
async def select_takes_effect_even_if_the_old_group_changes_every_cycle(dut):
    # R17 のレビュー: 前の組が毎周期変わって「変化の無い周期」が来なくても、溢れの立て直しで切り替わる
    tg = await setup(dut, probes=0x0010_0000)
    await tg.start()
    await ClockCycles(dut.clk, 10)
    await tg.drain()

    async def count():
        v = 0
        while True:
            await FallingEdge(dut.clk)
            v = (v + 1) & 0xFFFF
            dut.probes.value = 0x0010_0000 | v
    task = cocotb.start_soon(count())
    await tg.write_reg(REG_SELECT, 1)
    rec = []
    for _ in range(6):
        rec += await tg.drain()
        await ClockCycles(dut.clk, 50)
    task.cancel()
    assert 1 in [v for _, kd, v in chains(rec) if kd == "group"]

