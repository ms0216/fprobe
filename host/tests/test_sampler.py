"""fprobe.sampler（F4: DAP サンプラ）: か所の計画・CSR の設定・状態、イベント 種類 7 の取り込み。偽のデバイスで試験する。"""

from types import SimpleNamespace

import pytest

from fprobe import generated as g
from fprobe.collector import add_events
from fprobe.events import KIND_SAMPLE, DapEvent, resolve
from fprobe.recording import Recorder, Recording
from fprobe.sampler import Sampler, plan_slots, round_estimate, swclk_hz, swd_hz_for_rate


def test_ranges_become_word_slots_split_at_16_words_and_1kb():
    assert plan_slots([(0x2000_0000, 4)]) == [(0x2000_0000, 1)]
    assert plan_slots([(0x2000_0002, 4)]) == [(0x2000_0000, 2)]                      # 語の境界に広げる
    assert plan_slots([(0x2000_0000, 4), (0x2000_0004, 8)]) == [(0x2000_0000, 3)]    # 隣り合う範囲はまとめる
    assert plan_slots([(0x2000_0000, 80)]) == [(0x2000_0000, 16), (0x2000_0040, 4)]  # 1 か所 16 語まで
    assert plan_slots([(0x2000_03F8, 16)]) == [(0x2000_03F8, 2), (0x2000_0400, 2)]   # 1 KB をまたがない
    with pytest.raises(ValueError):
        plan_slots([(0x2000_0000 + 0x100 * k, 4) for k in range(9)])                 # 9 か所は入らない


class FakeDevice:
    """CSR の偽物。か所の表は slot_write の 1 で slot_index の位置へ書く（FPGA と同じ）。"""

    def __init__(self):
        self.csr = {}
        self.table = {}

    def write32(self, address, value):
        self.csr[address] = value
        if address == g.SAMPLER_SLOT_WRITE and value & 1:
            self.table[self.csr[g.SAMPLER_SLOT_INDEX]] = (self.csr[g.SAMPLER_SLOT_ADDR], self.csr[g.SAMPLER_SLOT_CONFIG])

    def write(self, address, values):
        for i, v in enumerate(values):
            self.write32(address + 4 * i, v)

    def read32(self, address):
        return self.csr.get(address, 0)

    def read(self, address, count=1):
        return [self.csr.get(address + 4 * i, 0) for i in range(count)]

    def capabilities(self):
        return SimpleNamespace(sampler=True, sys_clock_hz=100_000_000)


def test_configure_writes_slots_period_and_run():
    dev = FakeDevice()
    s = Sampler(dev)
    slots = s.configure([(0x2000_0000, 4), (0x2000_0100, 8)], period_s=1e-4, ap=0)
    assert slots == [(0x2000_0000, 1), (0x2000_0100, 2)]
    assert dev.table[0] == (0x2000_0000, 1) and dev.table[1] == (0x2000_0100, (1 << 8) | 1)
    assert all(dev.table[k][1] & 1 == 0 for k in range(2, 8))                        # 使わないか所は無効
    assert dev.csr[g.SAMPLER_PERIOD] == 10_000
    s.start()
    assert dev.csr[g.SAMPLER_CONTROL] & 1
    s.start(jitter=12)
    assert dev.csr[g.SAMPLER_CONTROL] >> 16 == 12 and dev.csr[g.SAMPLER_CONTROL] & 1
    with pytest.raises(ValueError):
        s.start(jitter=16)
    s.stop()
    assert dev.csr[g.SAMPLER_CONTROL] & 1 == 0


def test_status_and_a_device_without_a_sampler():
    dev = FakeDevice()
    dev.csr[g.SAMPLER_ROUNDS] = 5
    dev.csr[g.SAMPLER_OVERRUNS] = 1
    dev.csr[g.SAMPLER_STATUS] = 1 << 9 | 1                       # held（線を持っている）・ACK OK
    assert Sampler(dev).status() == {"rounds": 5, "errors": 0, "overruns": 1, "last_ack": 1, "busy": False,
                                     "held": True}
    dev.capabilities = lambda: SimpleNamespace(sampler=False, sys_clock_hz=1)
    with pytest.raises(RuntimeError):
        Sampler(dev)


def test_sample_events_are_resolved_and_recorded(tmp_path):
    e = DapEvent(100, KIND_SAMPLE, True, True, 0xC, 0b001, False, 0x2000_0010, 1234)
    assert [(a.kind, a.address, a.value) for a in resolve([e])] == [("sample", 0x2000_0010, 1234)]
    rec = Recorder(tmp_path / "r", timebase_hz=100_000_000)
    add_events(rec, [e], reference=1 << 40)
    rec.flush()
    got = Recording.open(rec.directory).events(streams=["sample"])
    assert [(x.kind, x.a, x.b) for x in got] == [("word", 0x2000_0010, 1234)]


# --- 速さの見積もり（B9） ------------------------------------------------------------------------

def test_swclk_follows_the_gateware_divider():
    sys_hz = 100_290_000                                         # Primer の sys
    assert swclk_hz(sys_hz, 4_000_000) == pytest.approx(3.857e6, rel=1e-3)     # 半周期 13
    assert swclk_hz(sys_hz, 10_000_000) == pytest.approx(10.03e6, rel=1e-3)    # 1% 超えてよい: 半周期 5
    assert swclk_hz(sys_hz, 25_000_000) == pytest.approx(25.07e6, rel=1e-3)    # 半周期 2（最小）
    assert swclk_hz(sys_hz, 100_000_000) == swclk_hz(sys_hz, 25_000_000)
    assert swclk_hz(sys_hz, 1_000) == pytest.approx(sys_hz // 2 / 49_649)
    assert swclk_hz(sys_hz, 500) == pytest.approx(sys_hz // 2 / 65535)         # 半周期 65535（最大）


def test_round_estimate_and_the_swd_speed_for_a_rate():
    sys_hz = 100_290_000
    one = [(0xE000_101C, 1)]                                     # DWT_PCSR の 1 語: DRW・RDBUFF・CTRL/STAT の 3 転送（B17）
    est = round_estimate(one, sys_hz, 4_000_000)
    assert est["transfers"] == 3 and est["swclk_hz"] == pytest.approx(3.857e6, rel=1e-3)
    assert est["round_us"] == pytest.approx(3 * 48 / 3.857, rel=1e-3)          # 約 37 µs（B17 の前は 9 転送 約 112 µs）
    assert est["max_rate_hz"] == pytest.approx(1e6 / est["round_us"])
    # 複数か所・複数語は か所ごとに TAR・DRW × n・RDBUFF・CTRL/STAT（SELECT・CSW は持ったまま書かない）
    assert round_estimate([(0x2000_0000, 4), (0x2000_1000, 1)], sys_hz, 4_000_000)["transfers"] == 7 + 4
    assert round_estimate([(0x2000_0000, 2)], sys_hz, 4_000_000)["transfers"] == 5
    need = swd_hz_for_rate(one, 50_000, sys_hz)                  # 50 kHz = 20 µs の巡回 → 144 / 20 µs = 7.2 MHz 以上
    assert need >= 7.2e6 and round_estimate(one, sys_hz, need)["max_rate_hz"] >= 50_000
    need = swd_hz_for_rate(one, 100_000, sys_hz)                 # 100 kHz: 14.4 MHz 以上（半周期 3 = 16.7 MHz）
    assert need >= 14.4e6 and round_estimate(one, sys_hz, need)["max_rate_hz"] >= 100_000
    assert swd_hz_for_rate(one, 200_000, sys_hz) is None         # SWD の上限（約 25 MHz）でも届かない
