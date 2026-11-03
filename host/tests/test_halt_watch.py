"""fprobe.halt_watch: FPGA が全コアの停止を見張り、1 つが止まったら残りを止める（docs/superpowers/specs/2026-10-29-halt-watch-design.md）。
CSR の書き方（サンプラ・値の一致・割り込み書き込みの語の列・クロストリガの経路）と、状態の読み方を、CSR の偽物で確かめる。"""

import pytest

from fprobe import generated as g
from fprobe.halt_watch import HaltWatch
from fprobe.xtrig import Source
from tests.probe_fakes import FakeDevice

DSCR = [0x80010088 + 0x2000 * n for n in range(4)]
DRCR = [(0x80010090 + 0x2000 * n, 1) for n in range(4)]


class LoggingDevice(FakeDevice):
    """書き込みの並びも残す（表への書き込みは PULSE なので、最後の値だけでは確かめられない）。"""

    def __init__(self):
        super().__init__()
        self.log = []
        self.trips = []                                   # CSR の往復（Device の 1 呼び出し = 1 往復）: (種類, 番地)

    def write(self, address, words):
        self.trips.append(("write", address))
        self.log.append((address, list(words)))
        for i, v in enumerate(words):
            self.csr[address + 4 * i] = v

    def write32(self, address, value):
        self.write(address, [value])

    def read(self, address, count=1, *, timeout_retries=0):
        self.trips.append(("read", address))
        return super().read(address, count)

    def read32(self, address):
        return self.read(address, 1)[0]

    def exchange(self, address, words):
        self.trips.append(("exchange", address))
        out = []
        for v in words:
            self.log.append((address, [v]))
            self.csr[address] = v
            out.append(self.csr[address])
        return out

    def time(self):
        self.trips.append(("time", None))
        return super().time()


def configured(ap=0):
    dev = LoggingDevice()
    w = HaltWatch(dev)
    w.configure(ap=ap, watch=DSCR, halted_mask=1, halted_value=1, halt_writes=DRCR)
    return dev, w


def test_configure_reads_each_watched_word_as_its_own_slot_and_disables_the_rest():
    dev, w = configured()
    slots = [v for a, v in dev.log if a == g.SAMPLER_SLOT_INDEX]
    enable = 1 << g.SAMPLER_SLOT_CONFIG_ENABLE[0]
    assert slots[:4] == [[k, a, enable, 1] for k, a in enumerate(DSCR)]       # か所 k = コア k、1 語
    assert slots[4:] == [[k, 0, 0, 1] for k in range(4, 8)]
    assert dev.read32(g.SAMPLER_PERIOD) == 1                                   # できるだけ速く
    assert dev.read32(g.SAMPLER_CONTROL) & 1 == 0                              # 設定だけ（まだ動かさない）


def test_configure_matches_every_watched_address_with_one_address_mask():
    dev, w = configured()
    assert dev.read32(g.SAMPLER_MATCH_ADDR) == DSCR[0]
    assert dev.read32(g.SAMPLER_MATCH_ADDR_MASK) == 0xFFFFFFFF & ~0x6000       # 4 つの番地で違うビットだけ見ない
    assert (dev.read32(g.SAMPLER_MATCH_MASK), dev.read32(g.SAMPLER_MATCH_VALUE)) == (1, 1)
    assert dev.read32(g.SAMPLER_MATCH_CONTROL) & 1 == 0


def test_configure_puts_the_halt_writes_in_the_injection_list():
    dev, w = configured(ap=1)
    assert dev.read(g.XTRIG_INJECT_AP, 3) == [1, DRCR[0][0], 1]               # 語 0
    entries = [v for a, v in dev.log if a == g.INJECT_ENTRY]
    assert entries == [[i, a, v, 1] for i, (a, v) in enumerate(DRCR) if i > 0]
    assert dev.read32(g.INJECT_CONTROL) == 3


@pytest.mark.parametrize("kw, why", [
    (dict(watch=[]), "watch"),
    (dict(watch=[0x1000 + 4 * i for i in range(9)]), "8"),
    (dict(halt_writes=[]), "halt_writes"),
    (dict(halt_writes=[(0x1000 + 4 * i, 1) for i in range(9)]), "8"),
    (dict(watch=[0x80010089]), "aligned"),
])
def test_configure_refuses_what_the_hardware_cannot_do(kw, why):
    args = dict(ap=0, watch=DSCR, halted_mask=1, halted_value=1, halt_writes=DRCR) | kw
    with pytest.raises(ValueError, match=why):
        HaltWatch(LoggingDevice()).configure(**args)


def configured_with_routes(routes):
    dev = LoggingDevice()
    dev.csr[g.XTRIG_ROUTE] = routes
    w = HaltWatch(dev)
    w.configure(ap=0, watch=DSCR, halted_mask=1, halted_value=1, halt_writes=DRCR)
    return dev, w


def test_configure_adds_only_its_own_route_after_the_match_is_off():
    """経路は configure で足し、release まで残す（arm・disarm の往復を減らす）。足すのは値の一致を無効にした後（disarm 済み）:
    有効でない値の一致は発火しないので、足した瞬間に何かが止まることはない。他の経路は変えない。"""
    others = int(Source.LA_TRIGGER) << g.XTRIG_ROUTE_TO_LA[0] | int(Source.HOST) << g.XTRIG_ROUTE_TO_INJECT[0]
    dev, w = configured_with_routes(others)
    route = dev.read32(g.XTRIG_ROUTE)
    assert route >> g.XTRIG_ROUTE_TO_INJECT[0] & 0x3F == int(Source.HOST | Source.SAMPLER_MATCH)
    assert route >> g.XTRIG_ROUTE_TO_LA[0] & 0x3F == int(Source.LA_TRIGGER)
    order = [a for a, _ in dev.log if a in (g.XTRIG_ROUTE, g.SAMPLER_MATCH_CONTROL)]
    assert order[0] == g.SAMPLER_MATCH_CONTROL and order[-1] == g.XTRIG_ROUTE   # 無効にしてから経路


def test_arm_restarts_the_match_and_runs_the_sampler_in_two_round_trips():
    dev, w = configured(ap=1)
    dev.trips.clear()
    dev.log.clear()
    w.arm()
    once = 1 << g.SAMPLER_MATCH_CONTROL_ONCE[0]
    # 0 → 1 で状態を消してから有効、1 回だけ発火（同じ番地に 2 語の EXCHANGE = 1 往復）。サンプラが最後: 最初の標本は
    # 値の一致を有効にした後
    assert dev.trips == [("exchange", g.SAMPLER_MATCH_CONTROL), ("write", g.SAMPLER_CONTROL)]
    assert [v for a, v in dev.log if a == g.SAMPLER_MATCH_CONTROL] == [[0], [1 | once]]
    assert dev.csr[g.SAMPLER_CONTROL] == 1 | 1 << g.SAMPLER_CONTROL_AP[0]


def test_disarm_stops_the_match_first_then_the_sampler_and_keeps_the_route():
    """値の一致を先に止める: その後は発火しない。前の発火は経路が残っているので必ず書き込みに届き、count に残る（外した後の
    poll で分かる）。経路を先に外すと、外した後・値の一致を止める前の発火が書き込みに届かず、count だけが残る。"""
    dev, w = configured()
    w.arm()
    dev.trips.clear()
    w.disarm()
    assert dev.trips == [("write", g.SAMPLER_MATCH_CONTROL), ("write", g.SAMPLER_CONTROL)]
    assert dev.csr[g.SAMPLER_MATCH_CONTROL] & 1 == 0 and dev.csr[g.SAMPLER_CONTROL] & 1 == 0
    assert dev.csr[g.XTRIG_ROUTE] >> g.XTRIG_ROUTE_TO_INJECT[0] & int(Source.SAMPLER_MATCH)


def test_release_removes_only_its_own_route():
    dev, w = configured_with_routes(int(Source.HOST) << g.XTRIG_ROUTE_TO_INJECT[0])
    w.arm()
    w.release()
    assert dev.read32(g.XTRIG_ROUTE) >> g.XTRIG_ROUTE_TO_INJECT[0] & 0x3F == int(Source.HOST)
    assert dev.read32(g.SAMPLER_MATCH_CONTROL) & 1 == 0 and dev.read32(g.SAMPLER_CONTROL) & 1 == 0


def unmatched_words(indexes):
    bits = 0
    for i in indexes:
        bits |= 1 << i
    return [bits >> 32 * k & 0xFFFFFFFF for k in range(4)]


def test_poll_is_one_read_and_says_which_watched_words_the_fpga_saw_running():
    """§3.4: 見張りの間の読みは 1 回（値の一致の count から unmatched3 まで）。seen_running[k] = か所 k の語 0（番号 16k）の
    不一致の標本を、有効にした後に FPGA が読んだ。"""
    dev, w = configured()
    w.arm()
    dev.write(g.SAMPLER_MATCH_UNMATCHED0, unmatched_words([0, 32, 33, 5]))   # 語 0 以外（33・5）は見張りの位置ではない
    dev.trips.clear()
    p = w.poll()
    assert dev.trips == [("read", g.SAMPLER_MATCH_COUNT)]
    assert p.seen_running == (True, False, True, False) and not p.matched


def test_status_without_a_fire_is_the_one_poll_read():
    dev, w = configured()
    w.arm()
    dev.trips.clear()
    s = w.status()
    assert not s.fired and dev.trips == [("read", g.SAMPLER_MATCH_COUNT)]


def test_status_after_a_fire_adds_one_read_of_the_injection():
    dev, w = configured()
    w.arm()
    dev.write32(g.SAMPLER_MATCH_COUNT, 1)
    dev.write32(g.SAMPLER_MATCH_FIRE, 16)
    dev.write32(g.INJECT_STATUS, 4)
    dev.write(g.SAMPLER_MATCH_TIME_LO, [1000, 0, 900, 0])
    dev.write(g.XTRIG_INJECT_TIME_LO, [1200, 0])
    dev.trips.clear()
    s = w.status()
    assert s.fired and s.core == 1
    assert dev.trips == [("read", g.SAMPLER_MATCH_COUNT), ("read", g.XTRIG_INJECT_STATUS)]   # 時刻の基準は最後の書き込み


def test_status_says_which_core_stopped_and_what_the_fpga_did():
    dev, w = configured()
    w.arm()                                                                    # 値の一致の数は arm で 0 に戻る
    assert not w.status().fired
    dev.write32(g.SAMPLER_MATCH_COUNT, 1)
    dev.write32(g.SAMPLER_MATCH_FIRE, (2 * 16) << g.SAMPLER_MATCH_FIRE_INDEX[0])
    dev.write32(g.XTRIG_INJECT_STATUS, 6 << g.XTRIG_INJECT_STATUS_COUNT[0])
    dev.write32(g.INJECT_STATUS, 4 << g.INJECT_STATUS_WRITTEN_WORDS[0])
    dev.write(g.SAMPLER_MATCH_TIME_LO, [1000, 0, 900, 0])
    dev.write(g.INJECT_FIRST_TIME_LO, [1100, 0])
    dev.write(g.XTRIG_INJECT_TIME_LO, [1200, 0])
    s = w.status()
    assert (s.fired, s.core, s.result, s.written) == (True, 2, "ok", 4)
    assert (s.t_prev, s.t_fire, s.t_first_write, s.t_last_write) == (900, 1000, 1100, 1200)


def test_the_48_bit_sample_times_are_unwrapped_like_the_64_bit_write_times():
    # A7: 値の一致の時刻（t_fire・t_prev）は 48 ビット。並ぶ t_first_write は 64 ビット: 後に読んだタイムベースで 64 ビットに
    dev, w = configured()
    w.arm()
    top = 1 << 48
    dev.ticks = top + 5_000
    dev.write32(g.SAMPLER_MATCH_COUNT, 1)
    dev.write(g.SAMPLER_MATCH_TIME_LO, [1000, 0, (top - 100) & 0xFFFF_FFFF, (top - 100) >> 32])   # prev は折り返しの前
    dev.write(g.INJECT_FIRST_TIME_LO, [1100, 1 << 16])
    s = w.status()
    assert (s.t_prev, s.t_fire, s.t_first_write) == (top - 100, top + 1000, top + 1100)


def test_status_reports_a_failed_injection_as_it_is():
    dev, w = configured()
    w.arm()
    dev.write32(g.SAMPLER_MATCH_COUNT, 1)                                      # 成功の回数は増えない（失敗）
    dev.write32(g.XTRIG_INJECT_STATUS, 1 << g.XTRIG_INJECT_STATUS_RESULT[0])
    dev.write32(g.INJECT_STATUS, 2 << g.INJECT_STATUS_WRITTEN_WORDS[0])
    s = w.status()
    assert s.fired and s.result == "swd_error" and s.written == 2


def test_status_waits_while_the_injection_is_still_running():
    dev, w = configured()
    w.arm()
    dev.write32(g.SAMPLER_MATCH_COUNT, 1)
    dev.write32(g.XTRIG_INJECT_STATUS, 1 << g.XTRIG_INJECT_STATUS_BUSY[0])
    assert not w.status().fired                                                # 書き終えるまで「終わった」としない


def test_configure_waits_for_a_running_injection_before_rewriting_its_list():
    """review: 書き込みが動いている最中に表を書き換えない（止めてから変える）。終わるのを待ち、終わらなければ誤りにする。"""
    from fprobe.halt_watch import HaltWatchError

    class Busy(LoggingDevice):
        def __init__(self, busy_reads):
            super().__init__()
            self.busy_reads = busy_reads

        def read32(self, address):
            if address == g.XTRIG_INJECT_STATUS and self.busy_reads:
                self.busy_reads -= 1
                return 1 << g.XTRIG_INJECT_STATUS_BUSY[0]
            return super().read32(address)

    dev = Busy(busy_reads=3)
    HaltWatch(dev).configure(ap=0, watch=DSCR, halted_mask=1, halted_value=1, halt_writes=DRCR)
    assert dev.busy_reads == 0 and any(a == g.INJECT_ENTRY for a, _ in dev.log)
    dev = Busy(busy_reads=10**9)
    with pytest.raises(HaltWatchError, match="busy"):
        HaltWatch(dev).configure(ap=0, watch=DSCR, halted_mask=1, halted_value=1, halt_writes=DRCR)
    assert not any(a == g.INJECT_ENTRY for a, _ in dev.log)                   # 表には触れていない


# 再開を FPGA が書く回路（ケイパビリティ watch_start。docs/superpowers/specs/2026-11-03-halt-watch-fpga-restart-design.md）
RESTART = [(0x80010090 + 0x2000 * n, 0b110) for n in range(4)]


def restart_device(watch_start=True):
    from caps_fakes import with_caps
    dev = LoggingDevice()
    dev.caps = with_caps(dev.capabilities(), watch_start=watch_start, sampler_match=True)
    return dev


def restart_configured():
    dev = restart_device()
    w = HaltWatch(dev)
    w.configure(ap=0, watch=DSCR, halted_mask=3, halted_value=3, halt_writes=DRCR, restart_writes=RESTART)
    return dev, w


def test_configure_puts_the_restart_requests_after_the_halt_requests_and_sets_the_start_range():
    dev, w = restart_configured()
    entries = [ws for a, ws in dev.log if a == g.INJECT_ENTRY]
    assert entries == [[i, a, v, 1] for i, (a, v) in enumerate(DRCR[1:] + RESTART, start=1)]
    assert dev.csr[g.INJECT_CONTROL] == 3 and w.restarts and w.can_restart


def test_restart_writes_need_the_capability_and_fit_the_list():
    with pytest.raises(ValueError, match="watch_start"):
        HaltWatch(restart_device(False)).configure(ap=0, watch=DSCR, halted_mask=3, halted_value=3, halt_writes=DRCR,
                                                   restart_writes=RESTART)
    with pytest.raises(ValueError, match="8"):
        HaltWatch(restart_device()).configure(ap=0, watch=DSCR, halted_mask=3, halted_value=3, halt_writes=DRCR,
                                              restart_writes=RESTART + [(0x80010090, 6)])


def test_arm_and_restart_gates_the_match_then_runs_the_sampler_then_asks_for_the_start_list():
    dev, w = restart_configured()
    dev.trips.clear()
    dev.log.clear()
    w.arm_and_restart()
    assert dev.trips == [("exchange", g.SAMPLER_MATCH_CONTROL), ("write", g.SAMPLER_CONTROL), ("write", g.INJECT_START)]
    mode = (1 | 1 << g.SAMPLER_MATCH_CONTROL_ONCE[0] | 1 << g.SAMPLER_MATCH_CONTROL_AFTER_START[0]
            | 1 << g.SAMPLER_MATCH_CONTROL_FIRST_SAMPLE[0])
    assert [v for a, v in dev.log if a == g.SAMPLER_MATCH_CONTROL] == [[0], [mode]]
    assert dev.csr[g.INJECT_START] == 4 | 7 << g.INJECT_START_LAST[0] | 1 << g.INJECT_START_GO[0]


def test_arm_and_restart_without_restart_writes_is_refused():
    from fprobe.halt_watch import HaltWatchError
    dev, w = configured()
    with pytest.raises(HaltWatchError):
        w.arm_and_restart()


def test_restart_status_is_one_read_with_the_facts():
    dev, w = restart_configured()
    dev.write(g.INJECT_START_STATUS, [1 << g.INJECT_START_STATUS_RESULT[0] | 2 << g.INJECT_START_STATUS_WRITTEN_WORDS[0]
                                      | 5 << g.INJECT_START_STATUS_COUNT[0], 0x10, 0, 0x20, 1])
    dev.trips.clear()
    r = w.restart_status()
    assert dev.trips == [("read", g.INJECT_START_STATUS)]
    assert (r.pending, r.result, r.written, r.count, r.t_first, r.t_last) == (False, "swd_error", 2, 5, 0x10, 1 << 32 | 0x20)


def test_poll_says_whether_the_fire_was_on_the_first_sample_after_the_gate():
    dev, w = restart_configured()
    w.arm_and_restart()
    dev.write(g.SAMPLER_MATCH_COUNT, [1])
    dev.write(g.SAMPLER_MATCH_FIRE, [16 | 1 << g.SAMPLER_MATCH_FIRE_FIRST[0]])
    p = w.poll()
    assert p.matched and p.index == 16 and p.fire_first
