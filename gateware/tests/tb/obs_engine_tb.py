"""obs_harness: obs_jtag_engine（fprobe 側）↔ JTAG の線 ↔ obs_soft_core（対象側。観測の回路 v2）。
fprobe の clk 100 MHz、対象の clk 50 MHz（非同期）、TCK = clk / (2 × half_period)。
イベント: 記録（ev_sync 0、種類 8）と印（ev_sync 1、種類 9。data = 番号）。"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge, RisingEdge, with_timeout

OBS_ID = 0x464F4253
T_VALUE, T_MARK, T_OVERFLOW, T_CONTROL = 0, 1, 2, 3


def command(op=0, addr=0, data=0) -> int:
    return op | addr << 4 | data << 8


START = command(1, 14, 1)              # CONTROL.start = 1
HALF_PERIOD = {"0": 3, "1": 10}        # 1 クロック版は TCK ≤ 対象の clk / 8


class Engine:
    def __init__(self, dut):
        self.dut = dut
        self.events = []               # (sync, data, addr, time)
        cocotb.start_soon(self._collect())

    async def _collect(self):
        dut = self.dut
        while True:
            await RisingEdge(dut.clk)
            if dut.ev_valid.value == 1:
                self.events.append((int(dut.ev_sync.value), int(dut.ev_data.value), int(dut.ev_addr.value),
                                    int(dut.ev_time.value)))

    def records(self):
        return [(d >> 30, d & (1 << 30) - 1, a) for s, d, a, _ in self.events if s == 0]

    def sync_events(self):
        """印のイベント: {番号: fprobe の時刻}。"""
        return {d: t for s, d, a, t in self.events if s == 1}

    async def push(self, cmd: int):
        dut = self.dut
        await FallingEdge(dut.clk)
        dut.cmd_data.value = cmd
        dut.cmd_push.value = 1
        await FallingEdge(dut.clk)
        dut.cmd_push.value = 0

    async def wait_for(self, cond, cycles=200_000):
        for _ in range(cycles):
            await RisingEdge(self.dut.clk)
            if cond():
                return
        d = self.dut
        raise AssertionError(f"timeout: stuck={d.stuck.value} id_ok={d.id_ok.value} id={int(d.id.value):#x} "
                             f"scans={int(d.scans.value)} oe={d.oe.value}")

    async def scans(self, n: int):
        start = int(self.dut.scans.value)
        await self.wait_for(lambda: int(self.dut.scans.value) >= start + n)


async def setup(dut, *, enable=True, sync_period=0, words=8, chain=False):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    cocotb.start_soon(Clock(dut.tgt_clk, 20, unit="ns").start(start_high=False))
    for name in ("enable", "free_pins", "stuck_tdo", "stuck_low", "cmd_push", "cmd_data", "probes", "ir_before", "ir_after",
                 "dr_before", "dr_after", "match_mask", "match_value", "match_change", "match_on"):
        getattr(dut, name).value = 0
    import os
    dut.half_period.value = HALF_PERIOD[os.environ.get("OBS_ONE_CLOCK", "0")]
    dut.ir_len.value = 4
    dut.user_ir.value = 0x2
    dut.words.value = words
    dut.sync_period.value = sync_period
    if chain:
        dut.ir_before.value, dut.ir_after.value, dut.dr_before.value, dut.dr_after.value = 4, 4, 1, 1
    dut.rst.value = 1
    dut.tgt_rst.value = 1
    await ClockCycles(dut.clk, 10)
    dut.rst.value = 0
    dut.tgt_rst.value = 0
    eng = Engine(dut)
    if enable:
        dut.enable.value = 1
        await eng.wait_for(lambda: dut.id_ok.value == 1)
    return eng


async def values_flow(dut, eng):
    """開始して値を変え、記録の列から値の並びを作り直す。"""
    dut.probes.value = 0x00A0
    await eng.push(START)
    await eng.scans(3)
    expected = [0x00A0]
    for v in (0x00A1, 0x0B00, 0xC0DE, 0x0001):
        await FallingEdge(dut.tgt_clk)
        dut.probes.value = v
        expected.append(v)
        await ClockCycles(dut.tgt_clk, 300)
    await eng.scans(4)
    recs = eng.records()
    assert recs[0][:2] == (T_CONTROL, 0) and recs[1][:2] == (T_VALUE, 0x00A0)          # 組 0・鎖の始めの値
    assert [d & 0xFFFF for t, d, _ in recs if t == T_VALUE] == expected
    deltas = [d >> 16 for t, d, _ in recs if t == T_VALUE][2:]
    assert deltas == [300, 300, 300]
    scans = [a for *_, a in recs]
    assert scans == sorted(scans) and scans[-1] < int(dut.scans.value)    # スキャンの番号の順
    assert dut.stuck.value == 0 and dut.running.value == 1


@cocotb.test()
async def records_flow_into_events(dut):
    eng = await setup(dut)
    assert int(dut.id.value) == OBS_ID
    await values_flow(dut, eng)


@cocotb.test()
async def records_flow_through_a_chain(dut):
    # CHAIN = true の回だけ（test_obs_engine.py）。対象の前後に BYPASS の装置が 1 つずつ
    eng = await setup(dut, chain=True)
    await values_flow(dut, eng)


@cocotb.test()
async def sync_pairs_are_recorded(dut):
    # 印のイベント（番号・fprobe の時刻）と印の記録（番号・鎖の時刻）を番号で組にすると、間隔が対象の clk で合う
    eng = await setup(dut, sync_period=2)
    await eng.push(START)
    await eng.scans(16)
    t, marks = None, {}
    for kind, d, _ in eng.records():
        if kind == T_VALUE:
            t = 0 if d >> 16 & 0x3FFF == 0 else t + (d >> 16 & 0x3FFF)
        elif kind == T_MARK:
            t += d >> 16 & 0x3FFF
            if d >> 15 & 1:
                marks[d >> 7 & 0xFF] = t - (d & 0x7F)
    events = eng.sync_events()
    pairs = sorted((marks[s], events[s]) for s in marks if s in events)
    assert len(pairs) >= 4
    for (t0, f0), (t1, f1) in zip(pairs, pairs[1:]):
        # 対象の clk は fprobe の半分の速さ。同期の揺れ ±1 周期
        assert abs((t1 - t0) - (f1 - f0) / 2) <= 1.5, (t0, f0, t1, f1)


@cocotb.test()
async def register_read_via_cmd(dut):
    # 応答の記録が read_value（番号 | 番地 | 値）に写る。番号は応答のたびに増える
    eng = await setup(dut)
    before = int(dut.read_value.value) >> 24
    await eng.push(command(2, 4))               # FIFO_DEPTH
    await eng.wait_for(lambda: dut.cmd_pending.value == 0)
    await eng.scans(3)
    v = int(dut.read_value.value)
    assert v >> 24 == (before + 1) & 0xFF and v & 0xFFFFF == 4 << 16 | 64
    await eng.push(command(1, 15, 0))           # SELECT = 0（書き）
    await eng.push(command(2, 5))               # CLOCK_HZ の下位
    await eng.push(command(2, 1))               # ID の上位
    await eng.wait_for(lambda: dut.cmd_pending.value == 0)
    await eng.scans(3)
    v = int(dut.read_value.value)
    assert v >> 24 == (before + 3) & 0xFF and v & 0xFFFFF == 1 << 16 | 0x464F
    assert int(dut.id.value) == OBS_ID


@cocotb.test()
async def commands_beyond_the_queue_are_dropped(dut):
    eng = await setup(dut)
    dut.half_period.value = 50                  # スキャンを遅くして待ち行列をあふれさせる
    for k in range(6):
        await eng.push(command(2, 0x00))
    assert dut.cmd_dropped.value == 1 and dut.cmd_pending.value == 1


@cocotb.test()
async def commands_of_a_previous_session_are_not_sent_after_reopening(dut):
    # R17: 止めて開き直したら、前の観測で積んだまま送っていない命令は捨てる（新しい観測に古い命令を送らない）
    eng = await setup(dut)
    dut.half_period.value = 50
    for k in range(6):
        await eng.push(command(2, 0x00))
    assert dut.cmd_pending.value == 1 and dut.cmd_dropped.value == 1
    dut.enable.value = 0
    await eng.wait_for(lambda: dut.oe.value == 0)
    dut.half_period.value = 2
    dut.enable.value = 1
    await ClockCycles(dut.clk, 20)
    assert dut.cmd_pending.value == 0 and dut.cmd_dropped.value == 0


@cocotb.test()
async def engine_stops_when_the_core_disappears(dut):
    eng = await setup(dut)
    await eng.push(START)
    await eng.scans(3)
    dut.stuck_tdo.value = 1                     # 対象が答えなくなる（TDO = 1）
    await eng.wait_for(lambda: dut.stuck.value == 1)
    assert dut.id_ok.value == 1                 # 答えていた後に止まった（id_ok は残る。初めから答えない場合と区別できる）
    n = len(eng.events)
    await eng.wait_for(lambda: dut.oe.value == 0)
    assert dut.running.value == 0
    scans = int(dut.scans.value)
    await ClockCycles(dut.clk, 5000)
    assert int(dut.scans.value) == scans and len(eng.events) == n          # 止まっている
    # 始めから答えない
    dut.enable.value = 0
    await ClockCycles(dut.clk, 10)
    dut.enable.value = 1
    await eng.wait_for(lambda: dut.stuck.value == 0)       # 始めると消える
    await eng.wait_for(lambda: dut.stuck.value == 1)
    assert dut.id_ok.value == 0


@cocotb.test()
async def disable_returns_the_tap_to_reset_and_releases_the_pins(dut):
    eng = await setup(dut)
    tms_at_rise = []

    async def watch():
        while True:
            await RisingEdge(dut.tck)
            tms_at_rise.append(int(dut.tms.value))
    cocotb.start_soon(watch())
    await eng.scans(2)
    dut.enable.value = 0
    await eng.wait_for(lambda: dut.oe.value == 0)
    assert tms_at_rise[-5:] == [1] * 5
    n = len(tms_at_rise)
    await ClockCycles(dut.clk, 2000)
    assert len(tms_at_rise) == n                # 線を手放した後は TCK を動かさない
    # free_pins はすぐ手放す
    dut.enable.value = 1
    await eng.wait_for(lambda: dut.running.value == 1)
    dut.free_pins.value = 1
    await RisingEdge(dut.clk)
    await RisingEdge(dut.clk)
    assert dut.oe.value == 0


@cocotb.test()
async def a_tdo_stuck_low_is_also_a_missing_core(dut):
    # TDO が 0（線が外れて引き下げ・IR が BYPASS 等）でも見出しの印が無いので答えないと分かる（0 の記録を流さない）
    eng = await setup(dut)
    await eng.push(START)
    await eng.scans(3)
    assert dut.target_running.value == 1                       # 見出しの「clk 側が記録中」
    dut.stuck_low.value = 1
    await eng.wait_for(lambda: dut.stuck.value == 1)
    n = len(eng.events)
    await ClockCycles(dut.clk, 5000)
    assert len(eng.events) == n and dut.oe.value == 0


@cocotb.test()
async def a_quick_disable_and_enable_starts_over(dut):
    # 止める指示がスキャンの途中ですぐ戻されても、区切りで止めて Test-Logic-Reset・IR・ID の照合からやり直す
    # （PC の close → open。新しい鎖の位置・USER の命令が IR に入る）。止まるまで driving = 1
    eng = await setup(dut)
    dut.half_period.value = 20                                  # 1 スキャンが長い
    await eng.scans(2)
    assert dut.driving.value == 1
    dut.enable.value = 0
    await ClockCycles(dut.clk, 2)
    dut.enable.value = 1
    await eng.wait_for(lambda: int(dut.scans.value) == 0)        # 始め直した（数え直し）
    await eng.wait_for(lambda: dut.id_ok.value == 1)
    dut.enable.value = 0
    await eng.wait_for(lambda: dut.driving.value == 0)
    assert dut.oe.value == 0


@cocotb.test()
async def snapshots_carry_the_capture_time(dut):
    # 標本型（見出しの印 0xA7）: ID の照合なしで動き、スキャンごとに語 0 を記録のイベントにする。時刻は Capture-DR を
    # 出る TCK の立ち上がり（値はその瞬間の probes）。印（時刻合わせ）のイベントは出さない
    eng = await setup(dut, sync_period=2)
    assert int(dut.id.value) == 0x534E4150                                  # "SNAP"
    dut.probes.value = 0x0042
    await eng.scans(4)
    eng.events.clear()
    await eng.scans(2)
    await FallingEdge(dut.tgt_clk)
    dut.probes.value = 0x0043
    await eng.scans(4)
    snaps = [(d, t) for s, d, a, t in eng.events if s == 0]
    assert all(d >> 24 == 0xA7 for d, _ in snaps) and not [e for e in eng.events if e[0] == 1]
    # 前と同じ標本（値も変化の回数も同じ）はイベントにしない: 値 0x42 の間の 6 スキャン → 0 件、0x43 になって 1 件
    assert [d & 0xFFFF for d, _ in snaps] == [0x0043]
    assert snaps[0][0] >> 16 & 0xFF == 1                                     # 前の標本からの変化 1 回
    assert int(dut.scans.value) >= 10


@cocotb.test()
async def reopening_with_old_records_left_in_the_fifo_still_finds_the_id(dut):
    # 前の観測の読まれていない記録が FIFO（64 語）に残ったまま、1 スキャン 1 語で開き直す。ID の応答はその後ろに並ぶ
    # ので、残りを吸い出し終わるまでは「ID が来ない」と数えない（16 スキャンで stuck にしていた）
    eng = await setup(dut, words=1)
    await eng.push(START)
    await eng.scans(2)
    for v in range(1, 2000):                                  # 吸い出しより速く変えて FIFO を埋める
        await FallingEdge(dut.tgt_clk)
        dut.probes.value = v
    dut.enable.value = 0
    await eng.wait_for(lambda: dut.oe.value == 0)
    dut.enable.value = 1
    await eng.wait_for(lambda: dut.id_ok.value == 1 or dut.stuck.value == 1, cycles=2_000_000)
    assert dut.id_ok.value == 1 and dut.stuck.value == 0


async def _count_pulses(dut, out: list):
    while True:
        await RisingEdge(dut.clk)
        if dut.match.value == 1:
            out.append(int(dut.scans.value))


async def _step(dut, eng, value: int, scans: int = 3):
    await FallingEdge(dut.tgt_clk)
    dut.probes.value = value
    await eng.scans(scans)


async def _match_sequence(dut, eng):
    """FPGA の値の一致（クロストリガの源 bit4）: restart（bit3）が変わり かつ state（1:0）= BUSY(1) で 1 周期のパルス。
    change = 0 なら一致に入った瞬間。最初の値は前が無いので成り立たない。回数は match_count"""
    pulses: list = []
    cocotb.start_soon(_count_pulses(dut, pulses))
    dut.match_mask.value, dut.match_value.value, dut.match_change.value = 0x0003, 0x0001, 0x0008
    dut.match_on.value = 1
    for v in (0x0001, 0x0009, 0x0002, 0x000A, 0x0001, 0x0009, 0x0008):
        await _step(dut, eng, v)
    # 0x0001 → 0x0009（BUSY で restart が変わった: 成り立つ）→ 0x0002（DONE）→ 0x000A → 0x0001（restart が戻った・BUSY:
    # 成り立つ）→ 0x0009（成り立つ）→ 0x0008（IDLE）
    assert len(pulses) == 3 and int(dut.match_count.value) == 3
    pulses.clear()
    dut.match_change.value = 0                                    # 一致に入った瞬間
    for v in (0x0000, 0x0001, 0x0009, 0x0002, 0x0001):
        await _step(dut, eng, v)
    assert len(pulses) == 2
    dut.match_on.value = 0
    for v in (0x0000, 0x0001):
        await _step(dut, eng, v)
    assert len(pulses) == 2                                       # 無効の間は出さない


@cocotb.test()
async def snapshot_values_fire_the_match(dut):
    eng = await setup(dut)
    dut.probes.value = 0x0000
    await eng.scans(3)
    await _match_sequence(dut, eng)


@cocotb.test()
async def record_values_fire_the_match(dut):
    eng = await setup(dut)
    dut.probes.value = 0x0000
    await eng.push(START)
    await eng.scans(3)
    await _match_sequence(dut, eng)
