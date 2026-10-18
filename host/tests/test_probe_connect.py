"""Probe の接続の手順の上書きとロック解除の道具（fprobe.probe_connect）: スクリプト・target_connect の script・
オプションバイト（STM32 の FLASH の模型）・target_unlock（STM32 の RDP、nRF の CTRL-AP）。機器は使わない。"""

from types import SimpleNamespace

import pytest
from dap_fakes import FakeDapLink, FakeMemAp, FakeTarget
from pyocd.core.target import Target
from stm32_flash_fake import FakeStm32Flash

from fprobe.mcp_server import Probe
from probe_fakes import FakeDevice


class Stm32(FakeStm32Flash):
    """オプションバイトの模型を、pyOCD のターゲットの口（状態・止める・リセット）でも使えるように。"""

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.state = Target.State.RUNNING
        self.resets = 0

    def get_state(self):
        return self.state

    def halt(self):
        self.state = Target.State.HALTED

    def reset_and_halt(self):
        self.resets += 1
        self.state = Target.State.HALTED


def connected(flash):
    p = Probe("unused")
    closed = []
    p._debug = SimpleNamespace(target=flash, close=lambda: closed.append(True))
    p._session = SimpleNamespace(device=None)
    return p, closed


def test_script_check_and_list(tmp_path):
    p = Probe("unused")
    assert p.script_check("builtin:reset_with_nreset")["ok"]
    listing = p.script_list()
    assert any(s["name"] == "builtin:stm32_debug_low_power" for s in listing["builtin"])
    assert any(h["name"] == "will_reset" for h in listing["hooks"])


def test_target_connect_passes_the_script_to_pyocd(tmp_path):
    p = Probe("unused")
    seen = {}

    class S:
        def open(self):
            pass

        def close(self):
            pass
    session = S()
    session.target = SimpleNamespace(get_state=lambda: Target.State.RUNNING)

    def debug(**options):
        seen.update(options)
        return session
    p._session = SimpleNamespace(device=FakeDevice(), debug=debug)
    status = p.target_connect("cortex_m", script="builtin:reset_with_nreset")
    assert seen["user_script"].endswith("reset_with_nreset.py") and status["script"]["hooks"][0]["name"] == "will_reset"
    bad = tmp_path / "bad.py"
    bad.write_text("def will_reset(core\n", encoding="utf-8")
    with pytest.raises(ValueError, match="構文"):
        p.target_connect("cortex_m", script=str(bad))


def test_status_tells_the_connected_target_speed_and_wire():
    """画面のステータス バーはターゲットの状態から名前・線・速さを出す（操作の記録の最後の接続に頼らない）。"""
    p = Probe("unused")

    class S:
        target = SimpleNamespace(get_state=lambda: Target.State.RUNNING)

        def open(self):
            pass

        def close(self):
            pass
    p._session = SimpleNamespace(device=FakeDevice(), debug=lambda **options: S())
    p.target_connect("nrf52840", frequency_hz=8_000_000)
    assert p.target_status() == {"state": "RUNNING", "target": "nrf52840", "frequency_hz": 8_000_000, "wire": "swd"}
    p.target_disconnect()
    p.target_connect("stm32f401xe", frequency_hz=1_000_000, jtag_chain=[4, 5])     # cortex_a は pyOCD を使わない（U32）
    assert p.target_status()["wire"] == "jtag"


def test_option_bytes_are_read_by_name():
    p, _ = connected(Stm32("optcr", 0x0FFFAAED))
    ob = p.option_bytes_read()
    assert ob["part"]["name"] == "STM32F401xD/E" and ob["family"] == "f4"
    assert ob["fields"]["RDP"] == 0xAA and ob["rdp_level"] == 0 and ob["fields"]["nWRP"] == 0xFF


def test_option_bytes_write_halts_programs_and_resets():
    flash = Stm32("optcr", 0x0FFFAAED)
    p, _ = connected(flash)
    out = p.option_bytes_write({"nWRP": 0xFE})
    assert out["after"]["fields"]["nWRP"] == 0xFE and flash.state == Target.State.HALTED and flash.resets == 1
    assert "コアを止めた" in out["notes"] and not out["reconnect_required"]


def test_rdp_regression_is_refused_without_confirmation_and_nothing_is_written():
    flash = Stm32("optcr", 0x0FFFBBED)
    p, _ = connected(flash)
    with pytest.raises(ValueError, match="confirm_mass_erase"):
        p.option_bytes_write({"RDP": 0xAA})
    with pytest.raises(ValueError, match="confirm_mass_erase"):
        p.target_unlock()
    with pytest.raises(ValueError, match="Level 2"):
        p.option_bytes_write({"RDP": 0xCC}, confirm_mass_erase=True, confirm_protect=True)
    assert flash.writes == [] and flash.state == Target.State.RUNNING


def test_unlock_stm32_with_confirmation():
    flash = Stm32("optcr", 0x0FFFBBED)
    p, _ = connected(flash)
    out = p.target_unlock(confirm_mass_erase=True)
    assert out["kind"] == "stm32_rdp" and not out["already_unlocked"] and flash.mass_erased
    assert out["after"]["rdp_level"] == 0
    assert p.target_unlock()["already_unlocked"]                                # 2 度目は何もしない


def test_optr_families_launch_and_disconnect():
    flash = Stm32("optr", 0xFFEFF8BB, dev_id=0x435)
    p, closed = connected(flash)
    out = p.target_unlock(confirm_mass_erase=True)
    assert out["reconnect_required"] and flash.launched and flash.mass_erased
    assert out["planned"]["rdp_level"] == 0 and closed and p._debug is None


def test_unsupported_parts_are_explained():
    flash = Stm32("optcr", dev_id=0x410)                                       # F1
    p, _ = connected(flash)
    with pytest.raises(Exception, match="扱えない"):
        p.option_bytes_read()


def nrf_target(protected=True):
    ctrl = FakeMemAp(idr=0x02880000, extra={0x0C: 0 if protected else 1, 0x08: 0})
    events = []

    def on_write(offset, value):
        events.append((offset, value))
        if offset == 0x04 and value:
            ctrl.extra[0x08] = 0                                                # すぐに消し終える
        if offset == 0x00 and value == 0:
            ctrl.extra[0x0C] = 1
        return True
    ctrl.on_write = on_write
    return FakeTarget(aps={0: FakeMemAp(csw=0x03000000), 1: ctrl}), events


def test_nrf_approtect_is_cleared_through_ctrl_ap_when_disconnected():
    target, events = nrf_target()
    p = Probe("unused")
    p._session = SimpleNamespace(device=None, dap_access=lambda: FakeDapLink(target))
    with pytest.raises(ValueError, match="confirm_mass_erase"):
        p.target_unlock()
    assert events == []
    out = p.target_unlock(confirm_mass_erase=True)
    assert out["kind"] == "nrf_ctrl_ap" and out["ap"] == 1 and out["unlocked"]
    assert events == [(0x04, 1), (0x00, 1), (0x00, 0)]


def test_nrf_already_unlocked_and_other_parts_when_disconnected():
    target, _ = nrf_target(protected=False)
    p = Probe("unused")
    p._session = SimpleNamespace(device=None, dap_access=lambda: FakeDapLink(target))
    assert p.target_unlock()["already_unlocked"]
    q = Probe("unused")
    q._session = SimpleNamespace(device=None, dap_access=lambda: FakeDapLink(FakeTarget()))
    with pytest.raises(RuntimeError, match="nRF52"):
        q.target_unlock(confirm_mass_erase=True)
