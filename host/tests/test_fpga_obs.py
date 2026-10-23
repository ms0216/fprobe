"""fprobe.fpga_obs: 対象の FPGA の観測の回路 v2 の記録（イベント 8）と印（イベント 9）を、fprobe の時刻の値の並びにする。
（docs/superpowers/specs/2026-10-20-fpga-observation-v2-design.md）"""

import random

import pytest

from fprobe import generated as g
from fprobe.caps import Capabilities
from fprobe.events import KIND_OBS_RECORD, KIND_OBS_SYNC, DapEvent, resolve
from fprobe.fpga_obs import FpgaObs, ObsDecoder, ObsSample, fit_sync, samples

T_VALUE, T_MARK, T_OVERFLOW, T_CONTROL = 0, 1, 2, 3


def record(kind: int, payload: int, time: int = 0, scan: int = 0) -> DapEvent:
    return DapEvent(time=time, kind=KIND_OBS_RECORD, ap=False, read=False, register=0, ack=0, parity_error=False,
                    address=scan, data=kind << 30 | payload)


def value(delta: int, v: int, **kw) -> DapEvent:
    return record(T_VALUE, delta << 16 | v, **kw)


def tick(delta: int = 16383, **kw) -> DapEvent:
    return record(T_MARK, delta << 16, **kw)


def mark(delta: int, seq: int, late: int = 0, **kw) -> DapEvent:
    return record(T_MARK, delta << 16 | 1 << 15 | seq << 7 | late, **kw)


def group_record(g: int, **kw) -> DapEvent:
    return record(T_CONTROL, g, **kw)


def response(addr: int, v: int, **kw) -> DapEvent:
    return record(T_CONTROL, 1 << 26 | addr << 16 | v, **kw)


def overflow(lost: int, **kw) -> DapEvent:
    return record(T_OVERFLOW, lost, **kw)


def sync(seq: int, fprobe: int) -> DapEvent:
    """印のイベント: 番号 seq の印を載せたスキャンの Update-DR の fprobe の時刻。"""
    return DapEvent(time=fprobe, kind=KIND_OBS_SYNC, ap=False, read=False, register=0, ack=0, parity_error=False,
                    address=0, data=seq)


def no_groups(out):
    return [s for s in out if s.kind != "group"]


def test_values_take_their_time_from_the_mark_backwards_and_forwards():
    # 鎖の始め（delta 0）から数える。印 1 は鎖の時刻 15 = fprobe 1000。傾きは申告の比（2）
    events = [group_record(0), value(0, 0x11), value(10, 0x22), mark(5, 1), sync(1, 1000),
              tick(), value(17, 0x33)]
    out = no_groups(samples(events, nominal_ratio=2.0))
    assert [(s.target_cycles, s.value) for s in out] == [(0, 0x11), (10, 0x22), (16_415, 0x33)]
    assert [s.time_fprobe for s in out] == pytest.approx([970, 990, 1000 + 2 * 16_400])


def test_values_wait_for_the_first_mark():
    # 印が来るまでは時刻が分からない: 取り込みは値を持っておき、印が来たら後ろ向きに決めて出す
    d = ObsDecoder(nominal_ratio=2.0)
    assert d.feed([group_record(0), value(0, 0x11), value(10, 0x22)]) == []
    out = no_groups(d.feed([mark(5, 7), sync(7, 1000)]))
    assert [(s.value, s.time_fprobe) for s in out] == [(0x11, pytest.approx(970)), (0x22, pytest.approx(990))]


def test_two_marks_give_the_slope_without_a_declared_clock():
    events = [group_record(0), value(0, 1), mark(100, 1), value(50, 2), mark(150, 2), sync(1, 1000), sync(2, 1400)]
    [a, b] = no_groups(samples(events))
    assert (a.time_fprobe, b.time_fprobe) == (pytest.approx(800), pytest.approx(1100))


def test_marks_and_events_pair_by_number_in_any_order():
    early = [sync(3, 1000), group_record(0), value(0, 1), mark(10, 3)]
    late = [group_record(0), value(0, 1), mark(10, 3), sync(3, 1000)]
    for events in (early, late):
        [s] = no_groups(samples(events, nominal_ratio=1.0))
        assert s.time_fprobe == pytest.approx(990)


def test_the_late_field_moves_the_mark_back():
    # 印は変化の無い周期まで待って書く。待った周期数 late を引いた瞬間が fprobe の時刻と組になる
    events = [group_record(0), value(0, 1), mark(10, 1, late=4), sync(1, 1000)]
    [s] = no_groups(samples(events, nominal_ratio=1.0))
    assert s.time_fprobe == pytest.approx(1000 - 6)


def test_declared_delays_move_the_samples_to_the_true_instants():
    # 印の瞬間は Update-DR より sync_delay 周期遅く、値の記録は変化より sample_delay 周期遅い（レジスタ 7・8）
    events = [group_record(0), value(0, 1), mark(10, 1), sync(1, 1000)]
    [raw] = no_groups(samples(events, nominal_ratio=2.0))
    [fixed] = no_groups(samples(events, nominal_ratio=2.0, sync_delay=3.5, sample_delay=1.0))
    assert fixed.target_cycles == raw.target_cycles == 0
    assert fixed.time_fprobe - raw.time_fprobe == pytest.approx(2 * (3.5 - 1.0))


def test_overflow_is_a_gap_and_starts_a_new_chain():
    events = [group_record(0), value(0, 1), value(5, 2), mark(5, 1), sync(1, 1000),
              overflow(7), group_record(0), value(0, 9), value(3, 10), mark(7, 2), sync(2, 5000)]
    out = samples(events, nominal_ratio=1.0)
    assert [(s.kind, s.value, s.lost) for s in out] == [
        ("group", None, 0), ("value", 1, 0), ("value", 2, 0), ("gap", None, 7), ("value", 9, 0), ("value", 10, 0)]
    assert [s.time_fprobe for s in out[3:]] == pytest.approx([4990, 4990, 4993])     # 新しい鎖は印 2 から


def test_the_group_comes_from_the_control_record():
    events = [group_record(0), value(0, 0x11), mark(5, 1), sync(1, 1000), group_record(1), value(4, 0x33)]
    out = samples(events, nominal_ratio=1.0)
    assert [(s.kind, s.group, s.value) for s in out] == [
        ("group", 0, None), ("value", 0, 0x11), ("group", 1, None), ("value", 1, 0x33)]
    assert out[2].time_fprobe == out[3].time_fprobe == pytest.approx(1004)          # 切り替えは次の値の時刻に


def test_feeding_in_pieces_gives_the_same_samples():
    events = [sync(1, 1000), group_record(0), value(0, 0x11), mark(5, 1), value(10, 0x22), group_record(1),
              value(3, 0x33), overflow(2), group_record(1), value(0, 0x44), mark(4, 2), sync(2, 9000)]
    whole = samples(events, nominal_ratio=1.0)
    for cut in range(1, len(events)):
        d = ObsDecoder(nominal_ratio=1.0)
        assert d.feed(events[:cut]) + d.feed(events[cut:]) + d.flush() == whole, cut


def test_without_any_mark_the_fprobe_time_is_unknown():
    out = no_groups(samples([group_record(0), value(0, 5)]))
    assert out == [ObsSample(time_fprobe=None, target_cycles=0, value=5)]


def test_after_lost_events_values_wait_for_the_next_mark():
    # イベントが欠けたら（記録のリングの追い越し等）delta の足し合わせが切れる。次の印から数え直す
    d = ObsDecoder(nominal_ratio=1.0)
    d.feed([group_record(0), value(0, 1), mark(5, 1), sync(1, 1000)])
    d.events_lost()
    assert d.feed([value(4, 2)]) == []
    out = d.feed([mark(6, 2), group_record(0), value(3, 3), sync(2, 2000)])
    assert [(s.value, s.time_fprobe) for s in no_groups(out)] == [(3, pytest.approx(2003))]


def test_values_before_the_group_is_known_are_not_reported():
    # 途中から読み始めた: 組の記録を見るまで値を出さない（組 0 と取り違えない）
    d = ObsDecoder(nominal_ratio=1.0)
    d.events_lost()
    assert d.feed([mark(5, 1), sync(1, 1000), value(4, 0x11)]) == []
    out = d.feed([group_record(3), value(2, 0x33)])
    assert [(s.kind, s.group, s.value) for s in out] == [("group", 3, None), ("value", 3, 0x33)]


def test_the_rewrite_after_a_mark_is_not_a_change():
    # 印の後、観測の回路は組と今の値を書き直す（途中から読む人のため）。変化ではない
    events = [group_record(0), value(0, 1), value(5, 2), mark(5, 1), sync(1, 1000), group_record(0), value(2, 2),
              value(3, 3)]
    assert [(s.target_cycles, s.value) for s in no_groups(samples(events, nominal_ratio=1.0))] == [
        (0, 1), (5, 2), (15, 3)]


def test_responses_are_not_values():
    events = [group_record(0), value(0, 1), response(4, 64), mark(5, 1), sync(1, 1000)]
    assert [s.value for s in no_groups(samples(events, nominal_ratio=1.0))] == [1]


def test_sync_fit_maps_target_time_to_fprobe_time():
    # 対象の水晶が 50 ppm 速い。組の揺れは ±1 周期（同期の遅れ）。直線に合わせて誤差 1 周期未満
    rng = random.Random(1)
    pairs = []
    for k in range(200):
        target = 1_000_000 + k * 50_000
        fprobe = 7_000 + (target - 1_000_000) * 2 * (1 - 50e-6) + rng.uniform(-1, 1)
        pairs.append((target, round(fprobe)))
    [seg] = fit_sync(pairs)
    for target in (1_000_000, 3_000_000, 10_950_000):
        truth = 7_000 + (target - 1_000_000) * 2 * (1 - 50e-6)
        assert abs(seg.to_fprobe(target) - truth) < 1.0


def test_obs_events_are_not_dap_failures():
    acc = resolve([value(3, 0x44, scan=9), sync(2, 77)])
    assert [(a.kind, a.address, a.value) for a in acc] == [("obs_record", 9, 3 << 16 | 0x44), ("obs_sync", 0, 2)]


class FakeDevice:
    """CSR の読み書きを覚え、観測の回路の役もする: enable で ID が読めた状態に、読みの命令で応答を read_value に。"""

    def __init__(self, fpga_obs=True, regs=None):
        self.caps = Capabilities(fpga_obs=fpga_obs)
        self.regs = {g.OBS_STATUS: 0, g.OBS_SCANS: 0, g.OBS_ID: 0, g.OBS_READ_VALUE: 0}
        self.core = {0: 0x4253, 1: 0x464F, 2: 2, 3: 1, 4: 16, 5: 0xF080, 6: 0x02FA, 7: 13, 8: 1, 9: 8, **(regs or {})}
        self.writes = []

    def capabilities(self):
        return self.caps

    def write32(self, address, v):
        self.writes.append((address, v))
        self.regs[address] = v
        if address == g.OBS_CONTROL and v & 1:
            self.regs[g.OBS_STATUS] = 1 << g.OBS_STATUS_ID_OK[0] | 1 << g.OBS_STATUS_RUNNING[0]
            self.regs[g.OBS_ID] = 0x464F4253
        if address == g.OBS_CMD_PUSH and v & 1:
            cmd = self.regs[g.OBS_CMD]
            if cmd & 0xF == 2:
                seq = (self.regs[g.OBS_READ_VALUE] >> 24) + 1 & 0xFF
                addr = cmd >> 4 & 0xF
                self.regs[g.OBS_READ_VALUE] = seq << 24 | addr << 16 | self.core.get(addr, 0)

    def read32(self, address):
        if address == g.OBS_SCANS:
            self.regs[address] += 1                    # 読むたびにスキャンが進む
        return self.regs.get(address, 0)


def test_missing_feature_is_a_clear_error():
    with pytest.raises(RuntimeError, match="FPGA の観測"):
        FpgaObs(FakeDevice(fpga_obs=False))


def test_open_writes_the_chain_and_waits_for_the_id():
    dev = FakeDevice()
    obs = FpgaObs(dev)
    info = obs.open(chain={"ir_len": 6, "ir_before": 4, "dr_before": 1}, user_ir=0x23, half_period=5, words=32,
                    sync_period=50)
    regs = dict(dev.writes)
    assert regs[g.OBS_CHAIN] == 6 | 4 << 6 | 1 << 18
    assert regs[g.OBS_USER_IR] == 0x23 and regs[g.OBS_HALF_PERIOD] == 5
    assert regs[g.OBS_WORDS] == 32 and regs[g.OBS_SYNC_PERIOD] == 50
    start = dev.writes.index((g.OBS_CONTROL, 1))                # 設定してから始める
    assert all(dev.writes.index((r, regs[r])) < start for r in (g.OBS_CHAIN, g.OBS_USER_IR, g.OBS_WORDS))
    assert info["id"] == 0x464F4253


def test_open_reads_the_declaration():
    obs = FpgaObs(FakeDevice())
    info = obs.open(chain={}, user_ir=2, half_period=5)
    assert info == {"id": 0x464F4253, "mode": "stream", "version": 2, "width": 16, "groups": 1, "fifo_depth": 16,
                    "clock_hz": 50_000_000, "sync_delay": 6.5, "sample_delay": 1.0, "lost_bits": 8}
    assert (obs.sync_delay, obs.sample_delay) == (6.5, 1.0)


def test_register_read_waits_for_its_own_response():
    # 応答は記録の列で来る。エンジンが写す read_value（番号 | 番地 | 値）の番号が変わり、番地が合うまで待つ
    dev = FakeDevice()
    obs = FpgaObs(dev)
    obs.open(chain={}, user_ir=2, half_period=5)
    assert obs.register_read(4) == 16 and obs.register_read(0) == 0x4253


def test_commands_are_packed_into_16_bits():
    dev = FakeDevice()
    obs = FpgaObs(dev)
    obs.open(chain={}, user_ir=2, half_period=5)
    obs.select(3)
    assert dev.regs[g.OBS_CMD] == 1 | 15 << 4 | 3 << 8


def test_open_reports_a_core_that_does_not_answer():
    dev = FakeDevice()
    orig = dev.write32

    def stuck(address, v):
        orig(address, v)
        if address == g.OBS_CONTROL and v & 1:
            dev.regs[g.OBS_STATUS] = 1 << g.OBS_STATUS_STUCK[0]
            dev.regs[g.OBS_ID] = 0xFFFF_FFFF
    dev.write32 = stuck
    with pytest.raises(RuntimeError, match="答えない"):
        FpgaObs(dev).open(chain={}, user_ir=2, half_period=5)


def test_close_waits_until_the_pins_are_released():
    # エンジンは今のスキャンを終えてから Test-Logic-Reset にして手放す。close はそれまで待つ（直後に別の道具をつながない）
    dev = FakeDevice()
    reads = {"left": 3}
    orig = dev.read32

    def read32(address):
        if address == g.OBS_STATUS and dev.regs.get(g.OBS_CONTROL) == 0:
            if reads["left"]:
                reads["left"] -= 1
                return 1 << g.OBS_STATUS_DRIVING[0]
            return 0
        return orig(address)
    dev.read32 = read32
    FpgaObs(dev).close()
    assert reads["left"] == 0


def snapshot(changes: int, v: int, time: int) -> DapEvent:
    """標本型の観測の回路（obs_snap_core）の標本: 0xA7 | 前の標本からの変化の回数 | 値。時刻 = Capture-DR の瞬間。"""
    return DapEvent(time=time, kind=KIND_OBS_RECORD, ap=False, read=False, register=0, ack=0, parity_error=False,
                    address=0, data=0xA7 << 24 | changes << 16 | v)


def test_snapshots_become_values_at_the_capture_time():
    # 標本の時刻は fprobe がスキャンした瞬間（時刻合わせは要らない）。標本の間の見えない変化は hidden（回数）にする
    # （溢れで失った gap とは別。信号の段を閉じず、警告にしない）
    events = [snapshot(0, 0x42, 1000), snapshot(0, 0x42, 2000), snapshot(1, 0x43, 3000), snapshot(3, 0x44, 4000),
              snapshot(2, 0x44, 5000)]
    out = samples(events)
    assert [(s.kind, s.time_fprobe, s.value, s.lost) for s in out] == [
        ("group", 1000, None, 0), ("value", 1000, 0x42, 0), ("value", 3000, 0x43, 0),
        ("hidden", 4000, None, 2), ("value", 4000, 0x44, 0), ("hidden", 5000, None, 2)]


def test_open_reports_a_snapshot_core():
    # 標本型はレジスタを持たない（エンジンは見出しの印 0xA7 で ID の照合を省き、id に "SNAP" を出す）
    dev = FakeDevice()
    orig = dev.write32

    def snap(address, v):
        orig(address, v)
        if address == g.OBS_CONTROL and v & 1:
            dev.regs[g.OBS_ID] = 0x534E4150
    dev.write32 = snap
    obs = FpgaObs(dev)
    info = obs.open(chain={}, user_ir=2, half_period=20)
    assert info["mode"] == "snapshot" and info["groups"] == 1 and info["width"] == 16
    assert not [w for w in dev.writes if w[0] == g.OBS_CMD_PUSH]                 # 読みの命令を出さない
    with pytest.raises(RuntimeError, match="標本型"):
        obs.register_read(4)


def test_a_rewrite_after_a_change_is_not_a_second_change():
    # 観測の回路は変化を書き直しより先に書く。印 → 変化 2 → 組 → 書き直し 2 と並んでも、2 は 1 回だけ
    events = [group_record(0), value(0, 1), mark(5, 1), sync(1, 1000), value(3, 2), group_record(0), value(4, 2),
              value(2, 3)]
    assert [s.value for s in no_groups(samples(events, nominal_ratio=1.0))] == [1, 2, 3]


def test_a_mark_whose_late_is_saturated_is_not_used_for_timing():
    # late は 127 で止まる（印を書けないまま長く待った）。その印の瞬間は分からないので時刻合わせに使わない
    events = [group_record(0), value(0, 1), mark(500, 1, late=127), sync(1, 1000), mark(100, 2), sync(2, 5000)]
    [s] = no_groups(samples(events, nominal_ratio=1.0))
    assert s.time_fprobe == pytest.approx(5000 - 600)


def test_after_lost_events_a_snapshot_is_not_compared_with_the_old_value():
    d = ObsDecoder()
    d.feed([snapshot(0, 0x42, 1000)])
    d.events_lost()
    out = d.feed([snapshot(0, 0x42, 2000)])
    assert [(s.kind, s.value) for s in out] == [("group", None), ("value", 0x42)]   # 欠けの後は読み直し


def test_a_condition_compiles_into_the_hardware_match():
    # システム解析の条件を、観測のエンジンの値の一致（CSR obs.match・match_change）のマスクに（瞬間の項は 1 つまで）
    from fprobe.fpga_obs import compile_match
    from fprobe.fpga_signals import FpgaSignals
    signals = FpgaSignals.from_dict({"groups": {"0": [
        {"name": "state", "lsb": 0, "width": 2, "format": "enum", "enum": {"0": "IDLE", "1": "BUSY", "2": "DONE"}},
        {"name": "restart", "lsb": 3, "width": 1}, {"name": "addr", "lsb": 5, "width": 3}]}})
    assert compile_match("restart changed and state=BUSY", signals) == (0x0003, 0x0001, 0x0008)
    assert compile_match("state=BUSY", signals) == (0x0003, 0x0001, 0)                 # 入った瞬間
    assert compile_match("restart rose", signals) == (0x0008, 0x0008, 0x0008)
    assert compile_match("restart fell and addr=5", signals) == (0x00E8, 0x00A0, 0x0008)
    for bad in ("restart changed and state changed", "state rose", "state=BUSY and state=DONE", "nothing changed",
                "restart changed and state=WAIT"):
        with pytest.raises(ValueError):
            compile_match(bad, signals)
