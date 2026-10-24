"""クロストリガの動作「出力の端子」: 符号化（fprobe.xtrig.PinOutputs）と Probe の道具（fprobe.probe_xtrig_out）。
偽の機器（CSR は番地 → 値）。"""

from types import SimpleNamespace

import pytest

from fprobe import generated as g
from fprobe.caps import Output
from fprobe.mcp_server import Probe
from fprobe.xtrig import OutputConfig, OutputMode, PinOutputs, Source, pulse_range_s, pulse_ticks
from probe_fakes import HZ, FakeDevice

PINS = (Output(0, 4, False), Output(1, 5, True), Output(2, 6, True), Output(3, 7, False))


class OutputDevice(FakeDevice):
    def __init__(self, pins=PINS, actions=2):
        super().__init__()
        self.pins, self.actions = pins, actions

    def capabilities(self):
        caps = super().capabilities()
        caps.outputs, caps.output_actions = self.pins, self.actions
        return caps


def field(word, f):
    return word >> f[0] & (1 << f[1]) - 1


@pytest.fixture
def device():
    return OutputDevice()


@pytest.fixture
def probe(device):
    p = Probe("unused")
    p._session = SimpleNamespace(device=device)
    return p


# --- 符号化 -------------------------------------------------------------------------------------

def test_configure_writes_the_condition_and_the_action_disabled_first(device):
    writes = []
    device.write32 = lambda a, v: (writes.append((a, v)), FakeDevice.write32(device, a, v))
    PinOutputs(device).configure(1, 3, OutputConfig(mode=OutputMode.PULSE, mask=0b110, value=0b010, any=True,
                                                    sources=Source.HOST | Source.LA_TRIGGER, high=False,
                                                    length=200, unit=2), enable=True)
    (a0, ctrl_off), (a1, when), (a2, ctrl_on) = writes
    assert (a0, a1, a2) == (g.XTRIG_OUT_CTRL1, g.XTRIG_OUT_WHEN1, g.XTRIG_OUT_CTRL1)
    assert field(ctrl_off, g.XTRIG_OUT_CTRL1_ENABLE) == 0 and field(ctrl_on, g.XTRIG_OUT_CTRL1_ENABLE) == 1
    assert (field(when, g.XTRIG_OUT_WHEN1_MASK), field(when, g.XTRIG_OUT_WHEN1_VALUE),
            field(when, g.XTRIG_OUT_WHEN1_ANY), field(when, g.XTRIG_OUT_WHEN1_SOURCES)) == (0b110, 0b010, 1, 0b101)
    assert (field(ctrl_on, g.XTRIG_OUT_CTRL1_MODE), field(ctrl_on, g.XTRIG_OUT_CTRL1_LEVEL),
            field(ctrl_on, g.XTRIG_OUT_CTRL1_CLAIM), field(ctrl_on, g.XTRIG_OUT_CTRL1_LENGTH),
            field(ctrl_on, g.XTRIG_OUT_CTRL1_UNIT), field(ctrl_on, g.XTRIG_OUT_CTRL1_PIN)) == (0, 0, 1, 200, 2, 3)


def test_status_reads_back_the_configuration(device):
    out = PinOutputs(device)
    cfg = OutputConfig(mode=OutputMode.HOLD, mask=1, value=1, sources=Source.SEQUENCE)
    out.configure(0, 2, cfg, enable=True)
    device.csr[g.XTRIG_OUT_STATUS] = 0b01
    st = out.status()
    assert [s.index for s in st] == [0, 1]
    assert (st[0].pin, st[0].enabled, st[0].active, st[0].config) == (2, True, True, cfg)
    assert not st[1].in_use


def test_pulse_ticks_choose_the_smallest_unit_and_report_the_range():
    assert pulse_ticks(1e-6, HZ) == (100, 0)                    # 1 µs = 100 周期（正確）
    assert pulse_range_s(100, 0, HZ) == (1e-6, 1e-6)
    length, unit = pulse_ticks(50e-3, HZ)                       # 50 ms = 5,000,000 周期
    assert unit == 4 and length == -(-5_000_000 // 65536)
    low, high = pulse_range_s(length, unit, HZ)
    assert low < 50e-3 <= high and (high - low) / high < 1 / 16
    with pytest.raises(ValueError, match="長すぎる"):
        pulse_ticks(1e4, HZ)


def test_config_is_validated():
    with pytest.raises(ValueError):
        OutputConfig(mask=0x100)
    with pytest.raises(ValueError):
        OutputConfig(length=0)


# --- Probe の道具 ---------------------------------------------------------------------------------

def test_set_allocates_an_action_per_channel_and_reuses_it(probe, device):
    out = probe.xtrig_output_set(channel=4, mode="while", pattern={"mask": 0b10, "value": 0b10}, enable=True)
    assert out == {"channel": 4, "mode": "while", "level": "high", "pull_up": False, "enabled": True}
    ctrl0 = device.csr[g.XTRIG_OUT_CTRL0]
    assert field(ctrl0, g.XTRIG_OUT_CTRL0_PIN) == 0 and field(ctrl0, g.XTRIG_OUT_CTRL0_MODE) == 2
    probe.xtrig_output_set(channel=7, sources=["host"], mode="pulse", pulse_us=2)
    assert field(device.csr[g.XTRIG_OUT_CTRL1], g.XTRIG_OUT_CTRL1_PIN) == 3
    probe.xtrig_output_set(channel=4, mode="hold", sources=["dap_match"])      # 同じ端子は同じ動作
    assert field(device.csr[g.XTRIG_OUT_CTRL0], g.XTRIG_OUT_CTRL0_MODE) == 1
    with pytest.raises(RuntimeError, match="同時に 2 本まで"):
        probe.xtrig_output_set(channel=5, sources=["host"])
    probe.xtrig_output_release(4)
    assert device.csr[g.XTRIG_OUT_WHEN0] == 0 and field(device.csr[g.XTRIG_OUT_CTRL0], g.XTRIG_OUT_CTRL0_CLAIM) == 0
    probe.xtrig_output_set(channel=5, sources=["host"])                      # 空いた動作を使う
    assert field(device.csr[g.XTRIG_OUT_CTRL0], g.XTRIG_OUT_CTRL0_PIN) == 1


def test_enable_and_disable_flip_only_the_enable_bit(probe, device):
    probe.xtrig_output_set(channel=7, sources=["host"], mode="hold", level="low")
    before = device.csr[g.XTRIG_OUT_CTRL0]
    assert probe.xtrig_output_enable(7) == {"channel": 7, "enabled": True}
    assert device.csr[g.XTRIG_OUT_CTRL0] == before | 1
    probe.xtrig_output_disable(7)
    assert device.csr[g.XTRIG_OUT_CTRL0] == before
    with pytest.raises(RuntimeError, match="設定されていない"):
        probe.xtrig_output_enable(4)


def test_pulse_result_and_pull_up_note(probe):
    out = probe.xtrig_output_set(channel=5, mode="pulse", pulse_us=1000, sources=["la_trigger"])
    assert out["pull_up"] is True and any("プルアップ" in n for n in out["notes"])
    assert out["pulse_us"]["min"] <= 1000 <= out["pulse_us"]["max"]
    assert "notes" not in probe.xtrig_output_set(channel=5, mode="while", level="low", pattern={"mask": 1, "value": 0})


def test_set_rejects_bad_arguments(probe):
    with pytest.raises(ValueError, match="使えない"):
        probe.xtrig_output_set(channel=0, sources=["host"])               # SWCLK は候補に無い
    with pytest.raises(ValueError, match="条件が無い"):
        probe.xtrig_output_set(channel=4)
    with pytest.raises(ValueError, match="pulse_us"):
        probe.xtrig_output_set(channel=4, mode="pulse", sources=["host"])
    with pytest.raises(ValueError, match="源"):
        probe.xtrig_output_set(channel=4, sources=["keyboard"])
    with pytest.raises(ValueError, match="mode"):
        probe.xtrig_output_set(channel=4, mode="press", sources=["host"])


def test_status_lists_every_pin_and_the_free_actions(probe, device):
    probe.xtrig_output_set(channel=7, mode="while", pattern={"mask": 0b11, "value": 0b01, "any": True}, enable=True)
    device.csr[g.XTRIG_OUT_STATUS] = 0b01
    st = probe.xtrig_output_status()
    assert (st["actions"], st["free"]) == (2, 1)
    by = {o["channel"]: o for o in st["outputs"]}
    assert set(by) == {4, 5, 6, 7} and by[4] == {"channel": 4, "pull_up": False, "configured": False}
    assert by[7]["active"] and by[7]["enabled"] and by[7]["mode"] == "while"
    assert by[7]["pattern"] == {"mask": 0b11, "value": 0b01, "any": True}


def test_an_undefined_mode_read_back_is_reported_as_read(probe, device):
    # 読み戻したモードの値が定義の外（3）なら WHILE に丸めず、読んだ値をそのまま返す（?75）
    probe.xtrig_output_set(channel=7, mode="while", pattern={"mask": 1, "value": 1}, enable=True)
    lo, width = g.XTRIG_OUT_CTRL0_MODE                                          # (最下位のビット, 幅)
    device.csr[g.XTRIG_OUT_CTRL0] = device.csr[g.XTRIG_OUT_CTRL0] & ~(((1 << width) - 1) << lo) | 3 << lo
    out = {o["channel"]: o for o in probe.xtrig_output_status()["outputs"]}[7]
    assert out["mode"] is None and out["mode_raw"] == 3


def test_without_outputs_the_tools_say_so(device):
    p = Probe("unused")
    p._session = SimpleNamespace(device=OutputDevice(pins=(), actions=0))
    assert p.xtrig_output_status() == {"outputs": [], "actions": 0, "free": 0}
    with pytest.raises(RuntimeError, match="出力の端子が無い"):
        p.xtrig_output_set(channel=4, sources=["host"])


def test_claimed_pins_are_claimed_again_disabled_after_the_fpga_restarts(probe, device):
    """B29: FPGA が立ち上がり直すと設定が消え、端子が普段の機能（UART TX は High を駆動）に戻る。開き直した後に
    出力に使っていた端子をすぐ取り戻す（駆動はしない: 有効だったものも無効で戻す）。"""
    probe.xtrig_output_set(channel=4, mode="while", pattern={"mask": 0b100000, "value": 0b100000}, enable=True)
    probe.xtrig_output_set(channel=5, sources=["host"])
    probe.xtrig_output_release(5)
    probe.xtrig_output_set(channel=7, mode="pulse", sources=["host"], pulse_us=200)
    fresh = OutputDevice()                                   # 書き直した FPGA: 何も設定されていない
    probe._session = SimpleNamespace(device=fresh)
    assert probe._restore_outputs() == {"restored": [4, 7], "failed": {}}
    st = {o["channel"]: o for o in probe.xtrig_output_status()["outputs"]}
    assert st[4]["claim"] and not st[4]["enabled"] and st[4]["mode"] == "while"
    assert st[4]["pattern"] == {"mask": 0b100000, "value": 0b100000, "any": False}
    assert st[7]["claim"] and not st[7]["enabled"] and st[7]["mode"] == "pulse"
    assert not st[5]["configured"]                           # 戻した端子は取り戻さない


def test_restoring_reports_a_pin_that_cannot_be_claimed(probe, device):
    probe.xtrig_output_set(channel=4, sources=["host"])
    probe._session = SimpleNamespace(device=OutputDevice(pins=PINS[1:]))     # 別の構成: ch4 が無い
    out = probe._restore_outputs()
    assert out["restored"] == [] and "ch4" in out["failed"][4]
