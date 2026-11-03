"""2026-11-03 のレビュー（API のスキーマ。docs/reports/2026-11-03-cloud-A.md）。直す前は前の 2 件が落ちた: system.status の age_us の時刻の領域・xtrig.route の源の名前の大文字小文字・応答の照合が落ちるか。"""

from types import SimpleNamespace

import pytest
from caps_fakes import with_caps
from probe_fakes import FakeDevice

import api_result_check
from fprobe import generated as g
from fprobe.api.spec import InvalidResult
from fprobe.mcp_server import Probe
from fprobe.xtrig import Source
from test_fpga_obs_tools import DEFS, opened_status


@pytest.fixture
def probe(tmp_path, monkeypatch):
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path))
    p = Probe("unused")
    p._session = SimpleNamespace(device=FakeDevice())
    yield p
    p._la_record = None
    if p._recorder is not None:
        p.recording_stop()


def test_age_of_a_value_read_from_the_recording_after_the_timebase_passed_2_48(probe, monkeypatch):
    # ロジアナを記録へ流している間の fpga_obs_read は、共有の復号器（48 ビットのイベントの時刻のまま）から返す。
    # system_status の now_us は 64 ビットのタイムベース。2^48 刻みを越えた後（100 MHz で約 32.6 日）は、
    # 1000 刻み前の値の age_us が約 2^48 刻み（約 2.8e6 秒）になる。
    from test_fpga_obs import snapshot
    device = probe.session.device
    device.caps = with_caps(device.capabilities(), fpga_obs=True)
    hz = device.capabilities().timebase_hz
    monkeypatch.setattr(probe, "fpga_obs_status", opened_status)
    probe.fpga_obs_signals_set(DEFS)
    probe.recording_start(name="obs")
    probe._la_record = SimpleNamespace(running=True)
    base = 1 << 48
    device.ticks = base + 3000
    # 回路のイベントは時刻の下位 48 ビットを持つ（本物の機器と同じ）
    # 取り込みと同じ道（recording_stream・Collector._poll_events の add_events。イベントの後に読んだタイムベースを渡す）
    from fprobe.collector import add_events
    add_events(probe._collector.rec, [snapshot(1, 0x11, 1000), snapshot(1, 0x10, 2000)], base + 3000,
               obs=probe._collector.obs)
    latest = probe.system_status()["fpga"]["latest"]
    age_s = latest[0]["age_us"] / 1e6
    expected_s = (base + 3000 - (base + 2000)) / hz           # 2000 刻みの値を 3000 刻みで読んだ: 10 us
    assert age_s == pytest.approx(expected_s, abs=1e-3), f"age = {age_s} s（期待 {expected_s} s）"


def test_route_refuses_an_absent_source_given_in_upper_case(probe):
    # obs_match の無い構成: 小文字は断るが、大文字（flags は upper() で受ける）は能力の点検をすり抜けて経路を書き、
    # 応答は小文字に直して「つないだ」と返す
    device = probe.session.device
    device.caps = with_caps(device.capabilities(), obs_match=False)
    with pytest.raises(RuntimeError, match="obs_match"):
        probe.xtrig_route(to_la=["obs_match"])
    try:
        out = probe.xtrig_route(to_la=["OBS_MATCH"])
    except RuntimeError:
        return
    route = device.csr[g.XTRIG_ROUTE] >> g.XTRIG_ROUTE_TO_LA[0] & 0x3F
    pytest.fail(f"大文字の源は点検されずに通った: 応答 {out}、経路のレジスタ {Source(route)!r}")


def test_result_check_rejects_a_wrong_shaped_response():
    # 照合が本当に落ちるか（違う形の応答で InvalidResult）。この試験の違反は全体の失敗に数えないよう後で外す
    before = len(api_result_check._violations)
    try:
        with pytest.raises(InvalidResult):
            api_result_check.check("xtrig.route", {"to_la": ["nope"], "to_inject": []})
        with pytest.raises(InvalidResult):
            api_result_check.check("xtrig.route", {"to_la": [], "to_inject": [], "extra": 1})
        with pytest.raises(InvalidResult):
            api_result_check.check("profile.result", {"rows": "x"})
    finally:
        del api_result_check._violations[before:]


def test_result_check_wraps_the_impl_so_a_wrong_shape_fails_through_the_real_method(probe, monkeypatch):
    # 実装の道筋から: fpga_obs_status が欄を欠いた形を返すと system_status の応答が仕様に合わず落ちる
    before = len(api_result_check._violations)
    device = probe.session.device
    device.caps = with_caps(device.capabilities(), fpga_obs=True)
    monkeypatch.setattr(probe, "fpga_obs_status", lambda: {"running": True})
    monkeypatch.setattr(probe, "fpga_obs_read", lambda last: {"items": []})
    try:
        with pytest.raises(InvalidResult):
            probe.system_status()
    finally:
        del api_result_check._violations[before:]
