"""プラグイン fprobe.plugins.keymatrix: 本体の汎用の API（ProbeCaller 経由）だけでキーの行列をまねる。
偽の機器（CSR は番地 → 値）。プラグインは本体から読み込まれない。"""

import subprocess
import sys
from types import SimpleNamespace

import pytest

from fprobe import generated as g
from fprobe.mcp_server import Probe
from fprobe.plugins import ProbeCaller
from fprobe.plugins.keymatrix import KeyMatrix
from test_probe_xtrig_out import OutputDevice, field


@pytest.fixture
def device():
    return OutputDevice()


@pytest.fixture
def km(device):
    probe = Probe("unused")
    probe._session = SimpleNamespace(device=device)
    return KeyMatrix(ProbeCaller(probe))


def action(device, k):
    when, ctrl = device.csr.get(g.XTRIG_OUT_WHEN0 + 8 * k, 0), device.csr.get(g.XTRIG_OUT_CTRL0 + 8 * k, 0)
    return {"mask": field(when, g.XTRIG_OUT_WHEN0_MASK), "value": field(when, g.XTRIG_OUT_WHEN0_VALUE),
            "any": field(when, g.XTRIG_OUT_WHEN0_ANY), "enable": field(ctrl, g.XTRIG_OUT_CTRL0_ENABLE),
            "mode": field(ctrl, g.XTRIG_OUT_CTRL0_MODE), "level": field(ctrl, g.XTRIG_OUT_CTRL0_LEVEL),
            "claim": field(ctrl, g.XTRIG_OUT_CTRL0_CLAIM), "pin": field(ctrl, g.XTRIG_OUT_CTRL0_PIN)}


def test_press_drives_the_row_while_the_column_is_high(km, device):
    km.add_key("A", column=1, row=4)
    km.add_key("B", column=2, row=4)
    km.press("A")
    assert action(device, 0) == {"mask": 0b10, "value": 0b10, "any": 1, "enable": 1, "mode": 2, "level": 1,
                                 "claim": 1, "pin": 0}
    km.press("B")                                                      # 同じ行の 2 つ目: どちらかの列で
    assert action(device, 0)["mask"] == 0b110 and action(device, 0)["value"] == 0b110
    km.release("A")
    assert action(device, 0)["mask"] == 0b100
    km.release("B")
    assert action(device, 0)["enable"] == 0 and action(device, 0)["claim"] == 1   # 離した: 高インピーダンス


def test_rows_use_separate_actions_and_close_returns_the_pins(km, device):
    km.add_key("A", column=1, row=4)
    km.add_key("Z", column=0, row=7, active_high=False)
    km.press("A")
    km.press("Z")
    assert action(device, 1) == {"mask": 0b1, "value": 0, "any": 1, "enable": 1, "mode": 2, "level": 0, "claim": 1,
                                 "pin": 3}
    km.close()
    assert action(device, 0)["claim"] == 0 and action(device, 1)["claim"] == 0


def test_tap_presses_for_a_while(km, device, monkeypatch):
    km.add_key("A", column=3, row=7)
    seen = []
    monkeypatch.setattr("fprobe.plugins.keymatrix.time.sleep", lambda s: seen.append((s, action(device, 0)["enable"])))
    km.tap("A", 0.02)
    assert seen == [(0.02, 1)] and action(device, 0)["enable"] == 0


def test_rows_must_be_usable_outputs_without_pull_up_for_active_high(km):
    with pytest.raises(ValueError, match="出力に使えない"):
        km.add_key("A", column=1, row=0)
    with pytest.raises(ValueError, match="プルアップ"):
        km.add_key("A", column=1, row=5)
    km.add_key("L", column=1, row=5, active_high=False)                # プルアップの行は Low で返す型なら使える
    km.add_key("A", column=1, row=4)
    with pytest.raises(ValueError, match="極性"):
        km.add_key("B", column=2, row=4, active_high=False)


def test_caller_validates_params_like_the_daemon(km):
    from fprobe.api.spec import InvalidParams
    with pytest.raises(InvalidParams):
        km.api.call("xtrig.output_set", channel=4, mode="press")


def test_the_core_does_not_import_plugins():
    code = ("import sys, fprobe.mcp_server, fprobe.api.daemon; "
            "print(any(m.startswith('fprobe.plugins') for m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                         env={**__import__("os").environ, "PYTHONPATH": __import__("os").pathsep.join(sys.path)})
    assert out.stdout.strip() == "False"
