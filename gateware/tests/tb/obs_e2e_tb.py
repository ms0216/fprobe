"""FPGA 観測の端から端までの差分試験（obs_harness: obs_jtag_engine ↔ JTAG ↔ 対象の観測の回路）。

対象側の probes をランダムな間隔で変え、エンジンが出すイベント（ev_valid / ev_sync / ev_data / ev_time）を集め、本物のホストの復号器
（fprobe.obs_decoder.samples）に通して、復号した (fprobe の時刻, 値) を、シミュレーションが知っている正解（値を変えた対象の
クロックの瞬間の fprobe の時刻）と突き合わせる。

時刻の物差し: fprobe の時刻は time_count（clk の立ち上がりごとに 1 増える）。連続な時刻 f(T) は、clk の立ち下がりで読んだ値 tc と
その立ち下がりの時刻 Tf から f(T) = tc + (T − (Tf − 半周期)) / 周期 とする（count n は立ち上がり n から次の立ち上がりまで）。
正解の時刻 = 対象の clk の立ち上がりで probes を替えた瞬間の f（登録の出力と同じ。obs_core の SAMPLE_DELAY の前提）。

設定は環境変数 E2E_*（test_obs_e2e.py が与える）。結果の統計は E2E_LOG のファイルに JSON の行で追記する。
"""

import json
import math
import os
import random
from types import SimpleNamespace

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge, RisingEdge
from cocotb.utils import get_sim_time

from fprobe.events import KIND_OBS_RECORD, KIND_OBS_SYNC
from fprobe.obs_decoder import ObsDecoder, samples

FCLK_PS = 10_000                       # fprobe の clk（100 MHz）
OBS_ID = 0x464F4253
T_VALUE, T_MARK, T_OVERFLOW, T_CONTROL = 0, 1, 2, 3
START = 1 | 14 << 4 | 1 << 8


def cmd(op=0, addr=0, data=0) -> int:
    return op | addr << 4 | data << 8


def env_int(name, default):
    return int(os.environ.get(name, default))


class Bench:
    """端から端までの試験台。events = [(sync, data, addr, time)]。"""

    def __init__(self, dut):
        self.dut = dut
        self.events = []
        self.falls = []                # (Tf, tc): clk の立ち下がりの時刻と、そのとき読んだ time_now
        self.scan_log = []
        cocotb.start_soon(self._collect())
        cocotb.start_soon(self._falls())
        self.tgt_ps = env_int("E2E_TGT_PS", 20_000)

    async def _collect(self):
        d = self.dut
        while True:
            await RisingEdge(d.clk)
            if d.ev_valid.value == 1:
                self.events.append((int(d.ev_sync.value), int(d.ev_data.value), int(d.ev_addr.value), int(d.ev_time.value)))

    async def _falls(self):
        d = self.dut
        while True:
            await FallingEdge(d.clk)
            self.falls.append((get_sim_time("ps"), int(d.time_now.value)))

    def now_f(self) -> float:
        """今の瞬間の fprobe の時刻（連続）。"""
        T = get_sim_time("ps")
        Tf, tc = self.falls[-1]
        return tc + (T - (Tf - FCLK_PS // 2)) / FCLK_PS

    def decoder_events(self, sub=None):
        evs = self.events if sub is None else sub
        return [SimpleNamespace(kind=KIND_OBS_SYNC if s else KIND_OBS_RECORD, data=d, address=a, time=t) for s, d, a, t in evs]

    async def push(self, c):
        d = self.dut
        await FallingEdge(d.clk)
        d.cmd_data.value = c
        d.cmd_push.value = 1
        await FallingEdge(d.clk)
        d.cmd_push.value = 0

    async def wait_for(self, cond, cycles=400_000):
        for _ in range(cycles):
            await RisingEdge(self.dut.clk)
            if cond():
                return
        d = self.dut
        raise AssertionError(f"timeout: stuck={d.stuck.value} id_ok={d.id_ok.value} scans={int(d.scans.value)} oe={d.oe.value}")

    async def scans(self, n):
        start = int(self.dut.scans.value)
        await self.wait_for(lambda: int(self.dut.scans.value) >= start + n)

    async def read_register(self, addr):
        """host の FpgaObs.register_read と同じ待ち方: 番号が変わり、番地が合うまで。"""
        before = int(self.dut.read_value.value) >> 24
        await self.push(cmd(2, addr))
        await self.wait_for(lambda: self.dut.cmd_pending.value == 0)
        await self.wait_for(lambda: int(self.dut.read_value.value) >> 24 != before and int(self.dut.read_value.value) >> 16 & 0xF == addr)
        return int(self.dut.read_value.value) & 0xFFFF


async def bring_up(dut, *, enable=True):
    """クロックを動かしてリセットを明け、設定は環境変数から。"""
    one_clock = os.environ.get("E2E_ONE_CLOCK") == "1"
    snap = os.environ.get("E2E_SNAP") == "1"
    chain = os.environ.get("E2E_CHAIN") == "1"
    tgt_ps = env_int("E2E_TGT_PS", 20_000)
    cocotb.start_soon(Clock(dut.clk, FCLK_PS, unit="ps").start())
    cocotb.start_soon(Clock(dut.tgt_clk, tgt_ps, unit="ps").start(start_high=False))
    for name in ("enable", "free_pins", "stuck_tdo", "stuck_low", "cmd_push", "cmd_data", "probes", "ir_before", "ir_after",
                 "dr_before", "dr_after", "match_mask", "match_value", "match_change", "match_on"):
        getattr(dut, name).value = 0
    dut.half_period.value = env_int("E2E_HALF", 10 if (one_clock or snap) else 3)
    dut.ir_len.value = 4
    dut.user_ir.value = 0x2
    dut.words.value = env_int("E2E_WORDS", 8)
    dut.sync_period.value = env_int("E2E_SYNC", 2)
    dut.id_scans.value = 16
    if chain:
        dut.ir_before.value, dut.ir_after.value, dut.dr_before.value, dut.dr_after.value = 4, 4, 1, 1
    dut.probes.value = env_int("E2E_INIT", 0x00A0)
    dut.rst.value = 1
    dut.tgt_rst.value = 1
    await ClockCycles(dut.clk, 10)
    dut.rst.value = 0
    dut.tgt_rst.value = 0
    b = Bench(dut)
    if enable:
        dut.enable.value = 1
        await b.wait_for(lambda: dut.id_ok.value == 1 or snap)
    return b


def scan_tgt_cycles(words, half, tgt_ps):
    """1 スキャンの対象の clk の周期数（おおよそ。間隔を決めるためだけに使う）。"""
    return int((32 * (1 + words) + 30) * 2 * half * FCLK_PS / tgt_ps)


def make_schedule(rng, n, words, half, tgt_ps, *, burst_max=6):
    """[(待つ対象の clk の周期数, 新しい値)]。溢れない条件: 短い間隔（1〜8 周期）の連続は burst_max 回まで、その後は記録を
    吸い出せるだけ（記録 1 語ずつなら burst_max × 4 スキャン）空ける。値は前と違う。"""
    scan = scan_tgt_cycles(words, half, tgt_ps)
    out, prev = [], None
    while len(out) < n:
        k = rng.randint(1, burst_max)
        for i in range(k):
            gap = rng.choice([1, 1, 2, 3, 5, 8]) if i else scan * (2 + math.ceil(burst_max * 4 / words)) + rng.randrange(scan)
            out.append((gap, None))
    out = out[:n]
    return out


def fill_values(rng, schedule, initial):
    prev, out = initial, []
    for gap, _ in schedule:
        v = rng.randrange(0x10000)
        while v == prev:
            v = rng.randrange(0x10000)
        out.append((gap, v))
        prev = v
    return out


class Truth:
    """値を替えた瞬間の記録。changes = [(f, 値)]。"""

    def __init__(self, b):
        self.b = b
        self.changes = []

    async def run(self, schedule):
        d = self.b.dut
        for gap, v in schedule:
            await ClockCycles(d.tgt_clk, gap)         # 対象の clk の立ち上がり gap 回後
            d.probes.value = v                         # この立ち上がりの直後に替わる（登録の出力と同じ）
            self.changes.append((self.b.now_f(), v))


def logged(fn):
    """失敗の理由を E2E_LOG にも残す（並列実行の出力は読みにくいので）。"""
    import functools

    @functools.wraps(fn)
    async def wrapper(dut):
        try:
            await fn(dut)
        except BaseException as e:      # noqa: BLE001
            log_result(fn.__name__ + "_FAILED", error=repr(e)[:1500])
            raise
    return wrapper


def stats(errors):
    n = len(errors)
    if n == 0:
        return {"n": 0}
    s = sorted(errors)
    mean = sum(errors) / n
    var = sum((e - mean) ** 2 for e in errors) / n
    return {"n": n, "min": s[0], "max": s[-1], "mean": mean, "std": math.sqrt(var), "p50": s[n // 2],
            "p05": s[int(n * 0.05)], "p95": s[min(n - 1, int(n * 0.95))],
            "abs_max": max(abs(e) for e in errors)}


def log_result(name, **fields):
    path = os.environ.get("E2E_LOG")
    fields = {"test": name, **{k: os.environ[k] for k in os.environ if k.startswith("E2E_") and k != "E2E_LOG"}, **fields}
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(fields, ensure_ascii=False) + "\n")


async def declared(b):
    """観測の回路の申告（周期）。SYNC_DELAY は半周期の単位。"""
    sync_halves = await b.read_register(7)
    sample_delay = await b.read_register(8)
    clock_lo, clock_hi = await b.read_register(5), await b.read_register(6)
    return sync_halves / 2, float(sample_delay), clock_hi << 16 | clock_lo


def compare(b, truth, decoded, *, label=""):
    """復号した値の並びと正解を突き合わせる。(値の食い違い, 時刻の誤差の一覧, 時刻なしの数) を返す。"""
    values = [s for s in decoded if s.kind == "value"]
    expect = [v for _, v in truth.changes]
    return values, expect


async def drive_and_decode(dut, b, rng, *, n, initial, burst_max=6, extra_wait=0):
    """START → 変化を n 回 → 吸い出しを待つ。(復号した列, Truth, 申告の遅れ) を返す。"""
    sync_delay, sample_delay, clock_hz = await declared(b)
    await b.push(START)
    await b.wait_for(lambda: dut.target_running.value == 1)
    await b.scans(2)
    tr = Truth(b)
    sched = fill_values(rng, make_schedule(rng, n, int(dut.words.value), int(dut.half_period.value), b.tgt_ps, burst_max=burst_max), initial)
    await tr.run(sched)
    nominal = b.tgt_ps / FCLK_PS                  # fprobe の刻み / 対象の clk の周期（申告の CLOCK_HZ が正しいとき）
    # 吸い出されるまで待つ（上限つき）
    for _ in range(600):
        await b.scans(1)
        dec = samples(b.decoder_events(), nominal, sync_delay, sample_delay)
        if len([s for s in dec if s.kind == "value"]) >= n + 1:
            break
    await b.scans(2)
    dec = samples(b.decoder_events(), nominal, sync_delay, sample_delay)
    return dec, tr, (sync_delay, sample_delay, clock_hz, nominal)


def check_session(name, dec, tr, delays, *, initial, require_no_gap=True):
    gaps = [s for s in dec if s.kind == "gap"]
    values = [s for s in dec if s.kind == "value"]
    got = [s.value for s in values]
    want = [initial] + [v for _, v in tr.changes]
    if require_no_gap:
        assert not gaps, f"{name}: 溢れの切れ目 {[(g.lost) for g in gaps]}（溢れない条件のはず）"
    assert got == want, f"{name}: 値の列が違う（復号 {len(got)} 件、正解 {len(want)} 件）。最初の違い: " \
        f"{next(((i, hex(a), hex(b)) for i, (a, b) in enumerate(zip(got, want)) if a != b), None)}"
    untimed = [s for s in values if s.time_fprobe is None]
    errors = [s.time_fprobe - f for s, (f, _) in zip(values[1:], tr.changes) if s.time_fprobe is not None]
    st = stats(errors)
    if len(errors) <= 20:
        st["errors"] = [round(e, 3) for e in errors]
    st["untimed"] = len(untimed)
    st["declared_sync_delay"], st["declared_sample_delay"] = delays[0], delays[1]
    bases = {}
    for s in values:
        bases[str(s.time_basis)] = bases.get(str(s.time_basis), 0) + 1
    st["bases"] = bases
    # 時刻は鎖の中で単調（同じ値の列の順序）
    ts = [s.time_fprobe for s in values if s.time_fprobe is not None]
    st["monotonic"] = all(a <= b for a, b in zip(ts, ts[1:]))
    log_result(name, **st)
    return st


@cocotb.test()
@logged
async def random_changes_match_the_decoded_values(dut):
    """(1)(2)(3): ランダムな変化を復号して正解と比べる。時刻の誤差は統計として記録する。"""
    initial = env_int("E2E_INIT", 0x00A0)
    b = await bring_up(dut)
    rng = random.Random(env_int("E2E_SEED", 1))
    dec, tr, delays = await drive_and_decode(dut, b, rng, n=env_int("E2E_N", 150), initial=initial)
    st = check_session("random", dec, tr, delays, initial=initial)
    assert st["untimed"] == 0, st
    assert st["monotonic"], st
    assert dut.overflow_seen.value == 0


def unique_values(rng, n, avoid):
    pool = [v for v in range(0x10000) if v != avoid]
    rng.shuffle(pool)
    return pool[:n]


@cocotb.test()
@logged
async def lost_counts_in_a_gap_equal_the_changes_actually_lost(dut):
    """(5): 溢れる条件（密に変える）。復号した値は正解の部分列で、隣り合う復号した値の正解での番号の差 = その間の切れ目の
    lost（切れ目が無ければ 1）。切れ目なしに飛んでいれば静かな欠け。"""
    initial = env_int("E2E_INIT", 0x00A0)
    b = await bring_up(dut)
    rng = random.Random(env_int("E2E_SEED", 1))
    sync_delay, sample_delay, clock_hz = await declared(b)
    await b.push(START)
    await b.wait_for(lambda: dut.target_running.value == 1)
    await b.scans(2)
    tr = Truth(b)
    n = env_int("E2E_N", 400)
    vals = unique_values(rng, n, initial)
    sched = [(rng.choice([1, 1, 2, 3, 5, 40, 300]), v) for v in vals]
    await tr.run(sched)
    for _ in range(600):
        await b.scans(1)
        if int(dut.scans.value) > 3 and len(b.events) and dut.cmd_pending.value == 0:
            dec = samples(b.decoder_events(), b.tgt_ps / FCLK_PS, sync_delay, sample_delay)
            if dec and dec[-1].value == vals[-1]:
                break
    await b.scans(2)
    dec = samples(b.decoder_events(), b.tgt_ps / FCLK_PS, sync_delay, sample_delay)
    index = {initial: 0, **{v: i + 1 for i, v in enumerate(vals)}}
    seq = [s for s in dec if s.kind in ("value", "gap")]
    last, pending_lost, problems, total_lost, silent = None, 0, [], 0, 0
    nvalues = 0
    for s in seq:
        if s.kind == "gap":
            pending_lost += s.lost
            total_lost += s.lost
            continue
        nvalues += 1
        i = index.get(s.value)
        if i is None:
            problems.append(("value not in truth", hex(s.value)))
            continue
        if last is not None:
            want = i - last
            if want <= 0:
                problems.append(("order", last, i))
            elif pending_lost == 0 and want != 1:
                silent += want - 1
                problems.append(("silent loss", last, i))
            elif pending_lost and want != pending_lost:
                problems.append(("lost mismatch", last, i, "lost", pending_lost))
        last, pending_lost = i, 0
    log_result("overflow", n=n, decoded_values=nvalues, total_lost=total_lost, silent=silent, problems=problems[:10],
               gaps=len([s for s in seq if s.kind == "gap"]), overflow_seen=int(dut.overflow_seen.value))
    assert dut.overflow_seen.value == 1 or not [s for s in seq if s.kind == "gap"]
    assert not problems, problems[:10]


@cocotb.test()
@logged
async def reopening_repeatedly_keeps_every_session_correct(dut):
    """(4): 止めて開き直すのを繰り返す。各回の復号は、その回に変えた値だけ（前の回の読み残しが値に混ざらない）。"""
    initial = env_int("E2E_INIT", 0x00A0)
    b = await bring_up(dut)
    rng = random.Random(env_int("E2E_SEED", 1))
    cur = initial
    for session in range(env_int("E2E_SESSIONS", 4)):
        if session:
            dut.enable.value = 0
            await b.wait_for(lambda: dut.oe.value == 0 and dut.running.value == 0)
            await ClockCycles(dut.clk, 20)
            dut.enable.value = 1
            b.events.clear()
            await b.wait_for(lambda: dut.id_ok.value == 1)
        sync_delay, sample_delay, clock_hz = await declared(b)
        await b.push(START)
        await b.wait_for(lambda: dut.target_running.value == 1)
        await b.scans(2)
        start_value = int(dut.probes.value)
        tr = Truth(b)
        sched = fill_values(rng, make_schedule(rng, 12, int(dut.words.value), int(dut.half_period.value), b.tgt_ps), start_value)
        await tr.run(sched)
        for _ in range(300):
            await b.scans(1)
            dec = samples(b.decoder_events(), b.tgt_ps / FCLK_PS, sync_delay, sample_delay)
            if len([s for s in dec if s.kind == "value"]) >= 13:
                break
        await b.scans(2)
        dec = samples(b.decoder_events(), b.tgt_ps / FCLK_PS, sync_delay, sample_delay)
        got = [s.value for s in dec if s.kind == "value"]
        want = [start_value] + [v for _, v in tr.changes]
        log_result("reopen", session=session, got=len(got), want=len(want), ok=got == want)
        assert got == want, f"session {session}: 復号 {[hex(x) for x in got]} 正解 {[hex(x) for x in want]}"


@cocotb.test()
@logged
async def a_session_closed_with_unread_records_does_not_leak_into_the_next(dut):
    """(4): 最後に変えた直後に止める（記録が対象の FIFO に残る）→ 開き直して、新しい観測の値の列に前の記録が混ざらない。"""
    initial = env_int("E2E_INIT", 0x00A0)
    b = await bring_up(dut)
    rng = random.Random(env_int("E2E_SEED", 1))
    await b.push(START)
    await b.wait_for(lambda: dut.target_running.value == 1)
    await b.scans(2)
    # 変えた直後に止める（words が少ないので吸い出しが追いつかない）
    for k in range(300):                          # 密に変えて FIFO を溢れさせる（残りに「組・溢れ・値」の鎖の始めが入る）
        await ClockCycles(dut.tgt_clk, 1)
        dut.probes.value = 0x1000 + k
    dut.enable.value = 0
    await b.wait_for(lambda: dut.oe.value == 0 and dut.running.value == 0)
    await ClockCycles(dut.clk, 20)
    dut.probes.value = 0x7777
    await ClockCycles(dut.tgt_clk, 4)
    dut.enable.value = 1
    b.events.clear()
    await b.wait_for(lambda: dut.id_ok.value == 1, cycles=2_000_000)
    sync_delay, sample_delay, clock_hz = await declared(b)
    await b.push(START)
    await b.wait_for(lambda: dut.target_running.value == 1)
    await b.scans(2)
    start_value = int(dut.probes.value)
    tr = Truth(b)
    sched = fill_values(rng, make_schedule(rng, 10, int(dut.words.value), int(dut.half_period.value), b.tgt_ps), start_value)
    await tr.run(sched)
    for _ in range(300):
        await b.scans(1)
    dec = samples(b.decoder_events(), b.tgt_ps / FCLK_PS, sync_delay, sample_delay)
    got = [s.value for s in dec if s.kind == "value"]
    want = [start_value] + [v for _, v in tr.changes]
    stale = [hex(x) for x in got if x not in want]
    log_result("leak", got=len(got), want=len(want), stale=stale[:8], ok=got == want)
    assert got == want, f"前の観測の読み残しが値に混ざった: {stale[:8]}（復号 {len(got)} 件、正解 {len(want)} 件）"


@cocotb.test()
@logged
async def a_long_quiet_period_keeps_the_time(dut):
    """delta の上限（16383 周期）を超える静かな区間（刻みの記録）をはさんでも、値と時刻が合う。"""
    initial = env_int("E2E_INIT", 0x00A0)
    b = await bring_up(dut)
    rng = random.Random(env_int("E2E_SEED", 1))
    sync_delay, sample_delay, clock_hz = await declared(b)
    await b.push(START)
    await b.wait_for(lambda: dut.target_running.value == 1)
    await b.scans(2)
    tr = Truth(b)
    gaps = [100, 20000, 300, 50000, 40000, 200]
    sched = [(g, v) for g, v in zip(gaps, unique_values(rng, len(gaps), initial))]
    await tr.run(sched)
    for _ in range(300):
        await b.scans(1)
        dec = samples(b.decoder_events(), b.tgt_ps / FCLK_PS, sync_delay, sample_delay)
        if len([s for s in dec if s.kind == "value"]) >= len(gaps) + 1:
            break
    await b.scans(2)
    dec = samples(b.decoder_events(), b.tgt_ps / FCLK_PS, sync_delay, sample_delay)
    if os.environ.get("E2E_DUMP"):
        with open(os.environ["E2E_DUMP"], "w", encoding="utf-8") as f:
            json.dump({"events": b.events, "changes": tr.changes, "delays": [sync_delay, sample_delay]}, f)
    st = check_session("long_quiet", dec, tr, (sync_delay, sample_delay, clock_hz, 2.0), initial=initial)
    assert st["untimed"] == 0


@cocotb.test()
@logged
async def a_target_domain_reset_is_visible_and_recording_can_resume(dut):
    """(4): 対象の clk 側のリセット（tgt_rst）。記録が止まったことが target_running に出て、START し直すと値の列が続く。"""
    initial = env_int("E2E_INIT", 0x00A0)
    b = await bring_up(dut)
    rng = random.Random(env_int("E2E_SEED", 1))
    sync_delay, sample_delay, clock_hz = await declared(b)
    await b.push(START)
    await b.wait_for(lambda: dut.target_running.value == 1)
    await b.scans(2)
    tr = Truth(b)
    await tr.run(fill_values(rng, make_schedule(rng, 8, int(dut.words.value), int(dut.half_period.value), b.tgt_ps), initial))
    await b.scans(8)
    dut.tgt_rst.value = 1
    await ClockCycles(dut.tgt_clk, 4)
    dut.tgt_rst.value = 0
    await b.scans(3)
    stopped = int(dut.target_running.value) == 0
    # 止まっている間の変化は記録されない（値の列に出ない）
    mid = int(dut.probes.value)
    dut.probes.value = 0x5A5A
    await ClockCycles(dut.tgt_clk, 50)
    await b.scans(3)
    await b.push(START)
    await b.wait_for(lambda: dut.target_running.value == 1)
    await b.scans(2)
    tr2 = Truth(b)
    await tr2.run(fill_values(rng, make_schedule(rng, 8, int(dut.words.value), int(dut.half_period.value), b.tgt_ps), 0x5A5A))
    await b.scans(12)
    dec = samples(b.decoder_events(), b.tgt_ps / FCLK_PS, sync_delay, sample_delay)
    got = [s.value for s in dec if s.kind == "value"]
    want1 = [initial] + [v for _, v in tr.changes]
    want2 = [0x5A5A] + [v for _, v in tr2.changes]
    # 対象のリセットが明けると、TCK 側の CONTROL（開始）を写し直して自動で記録を再開する（組 → 今の値）。その値は変化ではない
    want = want1 + [want1[-1]] + want2
    kinds = [(s.kind, s.value) for s in dec if s.kind in ("value", "group", "gap", "hidden")]
    log_result("tgt_reset", stopped=stopped, got=[hex(x) for x in got], want=[hex(x) for x in want],
               times_none=len([s for s in dec if s.kind == "value" and s.time_fprobe is None]),
               kinds_around_reset=[(k, hex(v) if v is not None else None) for k, v in kinds[len(want1) - 1:len(want1) + 5]])
    assert got == want, (got, want)


@cocotb.test()
@logged
async def snapshot_values_have_the_capture_time(dut):
    """標本型: 復号した各標本の値は、その fprobe の時刻に probes が持っていた値。見えなかった変化は hidden の lost に出る
    （変化の総数 = 見えた変化 + hidden の lost）。"""
    b = await bring_up(dut)
    rng = random.Random(env_int("E2E_SEED", 1))
    await b.scans(3)
    b.events.clear()
    tr = Truth(b)
    vals = unique_values(rng, env_int("E2E_N", 60), 0x00A0)
    sched = [(rng.choice([1, 2, 5, 40, 300, 900, 3000]), v) for v in vals]
    await tr.run(sched)
    await b.scans(6)
    dec = samples(b.decoder_events(), None, 0.0, 0.0)
    hist = [(f, v) for f, v in tr.changes]

    def truth_at(t):
        cur = int(dut.probes.value) if False else 0x00A0
        for f, v in hist:
            if f <= t:
                cur = v
        return cur
    values = [s for s in dec if s.kind == "value"]
    wrong = [(s.time_fprobe, hex(s.value), hex(truth_at(s.time_fprobe))) for s in values[1:] if truth_at(s.time_fprobe) != s.value]
    hidden = sum(s.lost for s in dec if s.kind == "hidden")
    # 最初の標本の値 → 以降の変化の数
    n_changes = len(vals)
    seen = len(values)                  # 最初の標本は変化の回数が付かない（前の標本が無い）
    log_result("snapshot", values=len(values), hidden=hidden, changes=n_changes, wrong=wrong[:5], seen_plus_hidden=seen + hidden)
    assert not wrong, wrong[:5]
    assert seen + hidden == n_changes, (seen, hidden, n_changes)


@cocotb.test()
@logged
async def changes_made_during_a_target_reset_are_reported_as_lost(dut):
    """(4): 対象の clk 側のリセットの間に probes が変わると、その変化は記録できない。記録は自動で再開する（CONTROL を写し直す）
    ので、復号は「リセット前の値 → 再開後の値」と続けて見え、失った変化の切れ目（gap）が出ない（BUG-E2E-001）。"""
    b = await bring_up(dut)
    sync_delay, sample_delay, clock_hz = await declared(b)
    await b.push(START)
    await b.wait_for(lambda: dut.target_running.value == 1)
    await b.scans(2)
    await ClockCycles(dut.tgt_clk, 5)
    dut.probes.value = 0x0AAA
    await b.scans(6)                                   # 記録が吸い出されて落ち着く
    dut.tgt_rst.value = 1
    for v in (0x1111, 0x2222, 0x3333):                 # リセットの間に 3 回変える
        await ClockCycles(dut.tgt_clk, 2)
        dut.probes.value = v
    dut.tgt_rst.value = 0
    await b.scans(8)
    dec = samples(b.decoder_events(), b.tgt_ps / FCLK_PS, sync_delay, sample_delay)
    seq = [(s.kind, s.value, s.lost) for s in dec if s.kind in ("value", "gap", "hidden")]
    log_result("reset_loss", seq=[(k, hex(v) if v is not None else None, lost) for k, v, lost in seq])
    i = max(i for i, (k, v, _) in enumerate(seq) if v == 0x0AAA)
    after = seq[i + 1:]
    assert any(v == 0x3333 for _, v, _ in after), after            # 再開後の今の値は見える
    assert any(k == "gap" for k, _, _ in after[:2]), f"0x0AAA → 0x3333 の間に失った変化（0x1111・0x2222）の切れ目が無い: {after[:3]}"
