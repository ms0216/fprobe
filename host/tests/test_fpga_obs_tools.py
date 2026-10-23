"""Probe の FPGA の観測の道具（信号の定義の共有・記録の付帯情報）。機器は使わない。"""

import json
from types import SimpleNamespace

import pytest
from probe_fakes import FakeDevice

from fprobe.mcp_server import Probe

DEFS = {"groups": {"0": [{"name": "mark", "lsb": 0, "width": 1},
                         {"name": "state", "lsb": 4, "width": 2, "format": "enum", "enum": {"1": "RUN"}}]}}


@pytest.fixture
def probe(tmp_path, monkeypatch):
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path))
    p = Probe("unused")
    p._session = SimpleNamespace(device=FakeDevice())
    yield p
    if p._recorder is not None:
        p.recording_stop()


def meta_of(path):
    return json.loads((path / "meta.json").read_text(encoding="utf-8"))


def test_definitions_are_shared_and_copied_into_a_new_recording(probe):
    out = probe.fpga_obs_signals_set(DEFS)
    assert out["signals"]["groups"]["0"][1]["enum"] == {"1": "RUN"} and out["recording"] is False
    assert probe.fpga_obs_signals_get()["signals"] == out["signals"]
    started = probe.recording_start(name="obs")
    from pathlib import Path
    assert meta_of(Path(started["path"]))["fpga.signals"] == out["signals"]         # 記録を始めると写す


def test_changing_the_definitions_rewrites_the_recording_even_after_it_ended(probe):
    from pathlib import Path
    started = probe.recording_start(name="obs")
    path = Path(started["path"])
    probe.fpga_obs_signals_set(DEFS)                                               # 記録中: 記録にも
    assert meta_of(path)["fpga.signals"]["groups"]["0"][0]["name"] == "mark"
    probe.recording_stop()
    probe.recording_open(started["recording"])
    renamed = {"groups": {"0": [{"name": "pulse", "lsb": 0, "width": 1}]}}
    assert probe.fpga_obs_signals_set(renamed)["recording"] is True                # 開いている記録（後から）にも
    assert meta_of(path)["fpga.signals"]["groups"]["0"][0]["name"] == "pulse"
    assert probe.fpga_obs_signals_get()["recording"]["groups"]["0"][0]["name"] == "pulse"


def test_bad_definitions_are_refused(probe):
    with pytest.raises(ValueError, match="重なる"):
        probe.fpga_obs_signals_set({"groups": {"0": [{"name": "a", "lsb": 0, "width": 2},
                                                     {"name": "b", "lsb": 1, "width": 1}]}})


def test_system_status_reports_both_sides_even_without_a_target(probe):
    # MCU が未接続でも FPGA 側（ここでは観測の無い構成）を返す。どちらかが無くても他方は返す
    out = probe.system_status(addresses=[0x20040800])
    assert out["mcu"] == {"connected": False, "connect": None}
    assert out["fpga"] == {"available": False}
    assert out["time_us"] > 0


def test_system_status_keeps_the_last_known_fpga_value_after_it_leaves_the_window(probe, monkeypatch):
    # MCU が長く止まると値が変わらず、イベントの窓から値が消える。止まった瞬間の FPGA こそ見たいので、最後の値を出し続ける
    device = probe.session.device
    caps = SimpleNamespace(**vars(device.capabilities()), fpga_obs=True)
    monkeypatch.setattr(device, "capabilities", lambda: caps)
    monkeypatch.setattr(probe, "fpga_obs_status", lambda: {"state": "running"})
    value = {"kind": "value", "group": 0, "value": 1, "time_us": 5.0, "fields": {"state": "BUSY"}}
    reads = iter([[value], [{"kind": "gap", "group": 0, "value": None, "time_us": None}]])
    monkeypatch.setattr(probe, "fpga_obs_read", lambda last: {"items": next(reads)})
    first = probe.system_status()["fpga"]["latest"]
    assert [(v["group"], v["value"], v["time_us"]) for v in first] == [(0, 1, 5.0)]
    assert probe.system_status()["fpga"]["latest"] == first                        # 窓に値が無くても最後の値


def test_read_while_the_logic_analyzer_streams_into_the_recording(probe):
    # ロジアナを記録へ流している間はイベントのリングを流れが読む: 観測の値は記録の取り込み（共有の復号器）から返す
    from test_fpga_obs import snapshot
    probe.fpga_obs_signals_set(DEFS)
    probe.recording_start(name="obs")
    probe._la_record = SimpleNamespace(running=True)
    try:
        _check_read_from_the_recording(probe)
    finally:
        probe._la_record = None


def _check_read_from_the_recording(probe):
    from test_fpga_obs import snapshot
    probe._collector.obs.feed([snapshot(1, 0x11, 1000), snapshot(1, 0x10, 2000), snapshot(3, 0x11, 3000)])
    items = probe.fpga_obs_read(last=3)["items"]
    assert [(it["kind"], it["value"]) for it in items] == [("value", 0x10), ("hidden", None), ("value", 0x11)]
    assert items[-1]["fields"] == {"mark": 1, "state": "RUN"}
    with pytest.raises(RuntimeError, match="since"):
        probe.fpga_obs_read(since=0)


def test_status_reports_the_settings_it_was_opened_with(probe, monkeypatch):
    # 別の窓・開き直した画面が、いま動いている設定（と申告）を見られる。開き直す（対象の FPGA を書き直した後等）にも使う
    class StubObs:
        mode = "snapshot"

        def __init__(self, dev):
            self.dev = dev

        def open(self, chain, user_ir, half_period, words, sync_period):
            return {"id": 1, "mode": "snapshot", "sync_delay": 0, "sample_delay": 0, "clock_hz": 0}

        def status(self):
            return {"running": True}

    monkeypatch.setattr("fprobe.mcp_server.FpgaObs", StubObs)
    assert probe.fpga_obs_status()["config"] is None
    probe.fpga_obs_open(chain={"ir_len": 4}, user_ir=2, half_period=20, words=1, sync_period=0)
    out = probe.fpga_obs_status()
    assert out["config"] == {"chain": {"ir_len": 4}, "user_ir": 2, "half_period": 20, "words": 1, "sync_period": 0}
    assert out["declared"]["mode"] == "snapshot"


def test_auto_reopen_opens_again_with_the_same_settings_when_stuck(probe, monkeypatch):
    # 対象の FPGA を書き直すと観測の回路が一時答えず、エンジンは線を手放して止まる（stuck）。auto_reopen なら
    # 同じ設定で開き直す（失敗しても次の周期でまた試す）。閉じたら止める
    opened = []

    class StubObs:
        mode = "snapshot"
        stuck = False
        fail = False

        def __init__(self, dev):
            self.dev = dev

        def open(self, chain, user_ir, half_period, words, sync_period):
            if StubObs.fail:
                raise RuntimeError("no answer")
            opened.append((chain, user_ir, half_period, words, sync_period))
            StubObs.stuck = False
            return {"id": 1, "mode": "snapshot", "sync_delay": 0, "sample_delay": 0, "clock_hz": 0}

        def status(self):
            return {"running": not StubObs.stuck, "stuck": StubObs.stuck}

        def close(self):
            pass

    monkeypatch.setattr("fprobe.mcp_server.FpgaObs", StubObs)
    probe.fpga_obs_open(chain={"ir_len": 4}, user_ir=2, half_period=20, words=1, sync_period=0, auto_reopen=True)
    assert "fpga_obs.reopen" in probe._jobs
    probe._fpga_obs_reopen()                                    # 答えている: 何もしない
    assert len(opened) == 1
    StubObs.stuck, StubObs.fail = True, True
    probe._fpga_obs_reopen()                                    # まだ答えない: 次の周期で
    assert len(opened) == 1
    StubObs.fail = False
    probe._fpga_obs_reopen()
    assert opened[-1] == ({"ir_len": 4}, 2, 20, 1, 0) and len(opened) == 2
    st = probe.fpga_obs_status()
    assert st["auto_reopen"] is True and st["reopened"] == 1
    probe.fpga_obs_close()
    assert "fpga_obs.reopen" not in probe._jobs


def test_system_status_reports_how_the_target_was_connected(probe, monkeypatch):
    # 対象を書き直す・リセットすると MCU 側の SWD がはずれる（複数ドロップの選択等）。同じ設定でつなぎ直せるように、
    # 最後に成功した target_connect の引数を返す
    monkeypatch.setattr(type(probe), "_connect_target", lambda self, *a: {"state": "RUNNING"})
    monkeypatch.setattr(type(probe), "_ensure_design", lambda self, protocol: None)
    assert probe.system_status()["mcu"].get("connect") is None
    probe.target_connect(target="rp2040", frequency_hz=1_000_000, connect_mode="attach", core=0)
    connect = probe.system_status()["mcu"]["connect"]
    assert connect == {"target": "rp2040", "frequency_hz": 1_000_000, "connect_mode": "attach", "jtag_chain": None,
                       "script": None, "core": 0}


def test_a_hardware_trigger_on_an_fpga_condition(probe):
    # FPGA の条件でハードのクロストリガ（obs_match = 源 bit4）: 条件を CSR obs.match のマスクに直し、経路の bit4 を
    # 割り込み書き込み（MCU の停止）・ロジアナへ足す。やめると外す（ほかの源の経路は触らない）
    from fprobe import generated as g
    from fprobe.xtrig import Source
    dev = probe.session.device
    caps = dev.capabilities()
    dev.capabilities = lambda: SimpleNamespace(**{**vars(caps), "fpga_obs": True, "obs_match": True, "xtrig": True})
    probe.fpga_obs_signals_set({"groups": {"0": [
        {"name": "state", "lsb": 0, "width": 2, "format": "enum", "enum": {"1": "BUSY"}},
        {"name": "restart", "lsb": 3, "width": 1}]}})
    dev.csr[g.XTRIG_ROUTE] = int(Source.HOST) << g.XTRIG_ROUTE_TO_LA[0]             # 先にある経路
    out = probe.fpga_obs_trigger_set("restart changed and state=BUSY", halt=True, la=True)
    assert (out["mask"], out["value"], out["change"]) == (0x0003, 0x0001, 0x0008)
    assert dev.csr[g.OBS_MATCH] == 0x0003 | 0x0001 << 16
    assert dev.csr[g.OBS_MATCH_CHANGE] == 0x0008 | 1 << 16
    route = dev.csr[g.XTRIG_ROUTE]
    assert route >> g.XTRIG_ROUTE_TO_INJECT[0] & 0x1F == int(Source.OBS_MATCH)
    assert route >> g.XTRIG_ROUTE_TO_LA[0] & 0x1F == int(Source.OBS_MATCH | Source.HOST)
    dev.csr[g.OBS_MATCH_COUNT] = 7
    assert probe.fpga_obs_trigger_status() == {"condition": "restart changed and state=BUSY", "halt": True, "la": True,
                                               "count": 7}
    probe.fpga_obs_trigger_clear()
    assert dev.csr[g.OBS_MATCH_CHANGE] == 0
    assert dev.csr[g.XTRIG_ROUTE] == int(Source.HOST) << g.XTRIG_ROUTE_TO_LA[0]
    assert probe.fpga_obs_trigger_status() == {"condition": None}


def test_an_fpga_condition_cannot_halt_without_the_injector(probe):
    # 故障注入の無い構成（fprobe_core_m 等）では halt の経路を立てても MCU は止まらない: 断る
    dev = probe.session.device
    caps = dev.capabilities()
    dev.capabilities = lambda: SimpleNamespace(**{**vars(caps), "fpga_obs": True, "obs_match": True, "xtrig": True,
                                                  "inject": False})
    probe.fpga_obs_signals_set({"groups": {"0": [{"name": "flag", "lsb": 0, "width": 1}]}})
    with pytest.raises(RuntimeError, match="inject"):
        probe.fpga_obs_trigger_set("flag rose", halt=True, la=False)


def test_a_hardware_trigger_left_armed_by_an_earlier_daemon_is_reported_and_can_be_cleared(probe):
    # 条件の文字はデーモンにしか無い。デーモンを再起動しても FPGA には条件が残り、MCU を止め続ける: レジスタから
    # 持ち主の無い条件（orphan）として見せ、解除できるようにする
    from fprobe import generated as g
    from fprobe.xtrig import Source
    dev = probe.session.device
    caps = dev.capabilities()
    dev.capabilities = lambda: SimpleNamespace(**{**vars(caps), "fpga_obs": True, "obs_match": True, "xtrig": True})
    assert probe.fpga_obs_trigger_status() == {"condition": None}
    dev.csr[g.OBS_MATCH] = 0x0003 | 0x0001 << 16
    dev.csr[g.OBS_MATCH_CHANGE] = 0x0008 | 1 << g.OBS_MATCH_CHANGE_ENABLE[0]
    dev.csr[g.XTRIG_ROUTE] = int(Source.OBS_MATCH) << g.XTRIG_ROUTE_TO_INJECT[0]
    dev.csr[g.OBS_MATCH_COUNT] = 2
    assert probe.fpga_obs_trigger_status() == {"condition": None, "orphan": True, "mask": 0x0003, "value": 0x0001,
                                               "change": 0x0008, "halt": True, "la": False, "count": 2}
    probe.fpga_obs_trigger_clear()
    assert dev.csr[g.OBS_MATCH_CHANGE] == 0 and dev.csr[g.XTRIG_ROUTE] == 0
    assert probe.fpga_obs_trigger_status() == {"condition": None}


def test_the_hardware_trigger_needs_the_capability(probe):
    with pytest.raises(RuntimeError, match="obs_match"):
        probe.fpga_obs_trigger_set("restart changed")


def test_switching_recordings_closes_the_previous_one(probe):
    # 読む記録を替えたら前の記録を閉じる（開いたままだと Windows で記録のフォルダを消せず、ファイルが溜まる）
    first = probe.recording_start(name="a")["recording"]
    probe.recording_stop()
    old = probe._view
    second = probe.recording_start(name="b")["recording"]
    probe.recording_stop()
    with pytest.raises(Exception, match="closed"):
        old.count()
    reopened = probe._view
    probe.recording_open(first)
    with pytest.raises(Exception, match="closed"):
        reopened.count()
    assert probe.recording_status()["name"] == first and second != first


def test_the_last_fpga_value_follows_the_current_definitions_and_is_forgotten_on_reopen(probe, monkeypatch):
    # 保持するのは値だけ: 信号の定義を変えたら今の名前で出す。観測を開き直す（別の回路かもしれない）・閉じると忘れる
    device = probe.session.device
    caps = SimpleNamespace(**vars(device.capabilities()), fpga_obs=True)
    monkeypatch.setattr(device, "capabilities", lambda: caps)
    monkeypatch.setattr(probe, "fpga_obs_status", lambda: {"state": "running"})
    value = {"kind": "value", "group": 0, "value": 1, "time_us": 5.0, "fields": {"mark": 1}}
    monkeypatch.setattr(probe, "fpga_obs_read", lambda last: {"items": [value]})
    probe.fpga_obs_signals_set({"groups": {"0": [{"name": "mark", "lsb": 0, "width": 1}]}})
    assert probe.system_status()["fpga"]["latest"][0]["fields"] == {"mark": 1}
    monkeypatch.setattr(probe, "fpga_obs_read", lambda last: {"items": []})
    probe.fpga_obs_signals_set({"groups": {"0": [{"name": "pulse", "lsb": 0, "width": 1}]}})
    assert probe.system_status()["fpga"]["latest"][0]["fields"] == {"pulse": 1}     # 今の定義で
    monkeypatch.setattr(probe, "_obs", lambda: SimpleNamespace(close=lambda: None))
    probe.fpga_obs_close()
    assert probe.system_status()["fpga"]["latest"] == []


def test_the_fpga_trigger_state_follows_the_registers(probe):
    # 仕掛けた記憶があってもレジスタで外れていれば（FPGA を書き直した・別のクライアントが経路を変えた）無いと返す。
    # 書く途中で失敗したら記憶を残さない。halt も la も無い条件は意味が無いので断る
    from fprobe import generated as g
    dev = probe.session.device
    caps = dev.capabilities()
    dev.capabilities = lambda: SimpleNamespace(**{**vars(caps), "fpga_obs": True, "obs_match": True, "xtrig": True})
    probe.fpga_obs_signals_set({"groups": {"0": [{"name": "flag", "lsb": 0, "width": 1}]}})
    probe.fpga_obs_trigger_set("flag rose", halt=True)
    assert probe.fpga_obs_trigger_status()["condition"] == "flag rose"
    dev.csr[g.OBS_MATCH_CHANGE] = 0                                               # 回路が書き直された
    assert probe.fpga_obs_trigger_status() == {"condition": None}
    with pytest.raises(ValueError, match="halt"):
        probe.fpga_obs_trigger_set("flag rose", halt=False, la=False)
    probe.fpga_obs_trigger_set("flag rose", halt=True)
    original = dev.write32
    dev.write32 = lambda a, v: (_ for _ in ()).throw(OSError("DAP")) if a == g.XTRIG_ROUTE else original(a, v)
    with pytest.raises(OSError):
        probe.fpga_obs_trigger_set("flag rose", halt=True, la=True)
    dev.write32 = original
    assert getattr(probe, "_obs_trigger", None) is None


def test_a_watch_running_before_the_recording_names_its_rows(probe):
    # 記録の前から動いていた watch（名前つき）も、記録の変数の名前になる（AI の試験で recording.correlate の
    # "errors changed" が「変数 'errors' が記録に無い」になった）
    from pathlib import Path
    probe._watched = [SimpleNamespace(name="errors", address=0x20040808), SimpleNamespace(name="x", address=None)]
    probe._watch_running = False                          # 止めた watch の名前は写さない（値が入らないので誤らせる）
    stopped = probe.recording_start(name="s")
    assert "sample.names" not in meta_of(Path(stopped["path"]))
    probe.recording_stop()
    probe._watch_running = True
    started = probe.recording_start(name="w")
    assert meta_of(Path(started["path"]))["sample.names"] == {"0x20040808": "errors"}
    assert probe._view.meta["sample.names"] == {"0x20040808": "errors"}


def test_recording_events_give_fpga_values_by_signal_name(probe):
    # FPGA の値は生の 16 ビットに加え、記録の信号の定義で名前ごとに（AI が lsb・width で手で分けなくてよい）
    probe.fpga_obs_signals_set(DEFS)
    probe.recording_start(name="ev")
    probe._recorder.add("fpga", "value", 100, a=0, b=0x0011)
    probe.recording_stop()
    events = probe.recording_events(streams=["fpga"])["events"]
    assert events[0]["fields"] == {"mark": 1, "state": "RUN"}


def test_the_fpga_trigger_condition_survives_a_daemon_restart(probe, tmp_path):
    # 仕掛けた条件の文字を FPROBE_HOME に残し、再起動後（新しい Probe）にレジスタと一致すれば条件の文字で見せる
    # （一致しなければ持ち主の無い条件 orphan のまま。別の道具が書き換えた等）
    from fprobe import generated as g
    dev = probe.session.device
    caps = dev.capabilities()
    dev.capabilities = lambda: SimpleNamespace(**{**vars(caps), "fpga_obs": True, "obs_match": True, "xtrig": True})
    probe.fpga_obs_signals_set({"groups": {"0": [{"name": "flag", "lsb": 0, "width": 1}]}})
    probe.fpga_obs_trigger_set("flag rose", halt=True)
    again = Probe("unused")
    again._session = SimpleNamespace(device=dev)
    status = again.fpga_obs_trigger_status()
    assert status["condition"] == "flag rose" and status["restored"] is True and status["halt"] is True
    dev.csr[g.OBS_MATCH] ^= 1 << 16                                            # 別のものが値を書き換えた
    other = Probe("unused")
    other._session = SimpleNamespace(device=dev)
    assert other.fpga_obs_trigger_status()["orphan"] is True
    other.fpga_obs_trigger_clear()
    assert Probe.fpga_obs_trigger_status(other)["condition"] is None


def test_a_failed_recording_start_leaves_no_half_recording(probe, monkeypatch):
    # 記録を始める途中で失敗したら、記録中の状態を残さない（残ると以後ずっと「記録中」で断られる）
    import fprobe.mcp_server as m
    monkeypatch.setattr(m.Collector, "start", lambda self: (_ for _ in ()).throw(TimeoutError("UDP")))
    with pytest.raises(TimeoutError):
        probe.recording_start(name="half")
    assert probe._recorder is None and probe._collector is None
    monkeypatch.undo()
    probe.recording_start(name="ok")                                             # 次は始められる


def test_watch_names_go_only_to_the_recording_being_made(probe, tmp_path):
    # 記録中に別の（古い）記録を開いて watch の名前を付けても、古い記録の情報は書き換えない
    from pathlib import Path
    old = probe.recording_start(name="old")["recording"]
    probe.recording_stop()
    live = Path(probe.recording_start(name="live")["path"])
    probe.recording_open(old)
    probe._name_watches({0x20040808: "errors"})
    assert meta_of(live)["sample.names"] == {"0x20040808": "errors"}
    assert "sample.names" not in meta_of(probe._recordings_dir() / old)


def test_read_returns_raw_values_when_the_saved_definition_is_broken(probe):
    # 保存された信号の定義が壊れていても（手で書き換えた等）読み出し全体は失敗しない: 生の値を返し、定義の誤りを添える（R17）
    from test_fpga_obs import snapshot
    probe.fpga_obs_signals_set(DEFS)
    probe.recording_start(name="obs")
    probe._la_record = SimpleNamespace(running=True)
    try:
        probe._recorder.update_meta(**{"fpga.signals": {"groups": {"0": [{"name": "a", "lsb": 0, "width": 99}]}}})
        probe._view.meta["fpga.signals"] = {"groups": {"0": [{"name": "a", "lsb": 0, "width": 99}]}}
        probe._collector.obs.feed([snapshot(1, 0x11, 1000)])
        out = probe.fpga_obs_read(last=1)
        assert out["items"][-1]["value"] == 0x11 and "fields" not in out["items"][-1]
        assert out["signals_error"]
    finally:
        probe._la_record = None
