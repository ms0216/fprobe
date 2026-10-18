"""Primer の構成（ビットストリーム）の選択と自動の書き替え（fprobe.bitstreams・fprobe.probe_design）。機器は使わない:
書き込みは偽物、USB の再列挙は偽のセッションの開き直しで模す。"""

import subprocess
from types import SimpleNamespace

import pytest
import usb.core
from pyocd.core.target import Target

from fprobe import bitstreams as bs
from fprobe import generated as g
from fprobe import mcp_server
from fprobe.mcp_server import Probe


def caps(design, *, jtag=None, sampler=None, inject=None, build=None):
    standard = design == g.DESIGN_FOR_SWD
    return SimpleNamespace(design=design, board="Tang Primer 20K Dock", jtag=not standard if jtag is None else jtag,
                           sampler=standard if sampler is None else sampler,
                           inject=standard if inject is None else inject, xtrig=True,
                           sys_clock_hz=100_000_000, timebase_hz=100_000_000, build=build)


def test_the_designs_come_from_the_gateware():
    assert bs.PROTOCOLS == {"swd": "fprobe", "jtag": "fprobe_jtag"}
    assert bs.FEATURES == {"la_inputs": "fprobe_la", "la_stream_events": "fprobe_la"}   # U22・R1
    assert bs.SWITCHABLE == {"fprobe", "fprobe_jtag", "fprobe_la"}
    assert bs.protocols_of("fprobe_jtag") == ["jtag"]
    assert bs.protocols_of("fprobe_la") == ["swd"] and bs.protocols_of("fprobe_eth") == []


@pytest.mark.parametrize("target, chain, protocol", [
    ("stm32f401xe", None, "swd"), ("cortex_m", None, "swd"), ("cortex_m", [4, 5], "jtag"),
    ("cortex_a7", None, "jtag"), ("bcm2836", None, "jtag"), ("arm1176", None, "jtag"), ("raspberry_pi_zero", None, "jtag")])
def test_the_line_a_target_needs(target, chain, protocol):
    assert bs.protocol_for_target(target, chain) == protocol


def test_which_design_to_switch_to():
    assert bs.wanted_design(caps("fprobe"), "jtag") == "fprobe_jtag"
    assert bs.wanted_design(caps("fprobe"), "swd") is None
    assert bs.wanted_design(caps("fprobe_jtag"), "jtag") is None
    assert bs.wanted_design(caps("fprobe_jtag"), "swd") == "fprobe"           # 標準（サンプラ・故障注入）に戻す
    la = caps("fprobe_la", jtag=False, sampler=False, inject=False)
    assert bs.wanted_design(la, "swd") is None                                 # 機能のために選んだ構成はそのまま
    assert bs.wanted_design(la, "jtag") == "fprobe_jtag"
    assert bs.wanted_design(caps("fprobe_eth", jtag=True), "swd") is None   # 組の外には触らない
    assert bs.wanted_design(caps("fprobe_eth", jtag=True), "jtag") is None
    with pytest.raises(bs.DesignSwitchError, match="JTAG が無く"):
        bs.wanted_design(caps("other", jtag=False), "jtag")


def test_bitstream_location_is_configurable(tmp_path, monkeypatch):
    monkeypatch.delenv(bs.ENV_ROOT, raising=False)
    assert bs.bitstream_path("fprobe") == bs.DEFAULT_ROOT / "fprobe" / "top.fs"
    assert bs.DEFAULT_ROOT.parts[-2:] == ("gateware", "build")
    monkeypatch.setenv(bs.ENV_ROOT, str(tmp_path))
    assert bs.bitstream_path("fprobe_jtag") == tmp_path / "fprobe_jtag" / "top.fs"
    assert bs.bitstream_path("fprobe", tmp_path / "x") == tmp_path / "x" / "fprobe" / "top.fs"   # 引数が優先


def test_program_uses_the_safe_gateware_tool(tmp_path):
    seen = {}

    def run(command, **kw):
        seen.update(command=command, **kw)
        return subprocess.CompletedProcess(command, 0, stdout="書き込み完了", stderr="")
    assert bs.program(tmp_path / "top.fs", run=run) == "書き込み完了"
    assert seen["command"][1:] == ["-m", "fprobe_gw", "program", str(tmp_path / "top.fs"), "--board", "primer20k"]

    def fails(command, **kw):
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="IDCODE が一致しない")
    with pytest.raises(bs.DesignSwitchError, match="IDCODE"):
        bs.program(tmp_path / "top.fs", run=fails)


# --- Probe の書き替えの流れ -------------------------------------------------------------------------


class Board:
    """偽の Primer: 書き込まれた構成を覚え、書き込みの後しばらく USB に現れない（再列挙）。"""

    def __init__(self, design="fprobe", absent_polls=2):
        self.design = design
        self.programmed = []
        self.absent = 0
        self.absent_polls = absent_polls
        self.opened = 0
        self.connected = {}
        self.plugged = 0                        # 抜けるたびに増える（それより前に開いた口は使えない）
        self.features = {}                      # 立ち上がった版の機能（caps の引数）

    def unplug(self, absent_polls=2, **features):
        """USB が切れてつながり直す（電源が落ち、Flash の版で立ち上がる等）。"""
        self.plugged += 1
        self.absent = absent_polls
        self.features = features

    def program(self, path):
        self.programmed.append(path)
        self.design = path.parent.name
        self.absent = self.absent_polls

    def open(self, serial=None):
        if self.absent:
            self.absent -= 1
            raise RuntimeError("fprobe が見つからない")
        self.opened += 1
        design = self.design
        board = self

        def debug(**options):
            board.connected = options
            target = SimpleNamespace(get_state=lambda: Target.State.RUNNING)
            return SimpleNamespace(target=target, open=lambda: None, close=lambda: None, options=options)
        plugged, features = self.plugged, dict(self.features)

        def capabilities():
            if board.plugged != plugged:
                raise usb.core.USBError("Input/Output Error", errno=5)
            device.last_capabilities = caps(design, **features)
            return device.last_capabilities
        device = SimpleNamespace(capabilities=capabilities, time=lambda: 0, write32=lambda a, v: None)
        return SimpleNamespace(device=device, debug=debug, close=lambda: None, link=None)


@pytest.fixture
def board(tmp_path, monkeypatch):
    b = Board()
    for design in ("fprobe", "fprobe_jtag"):
        (tmp_path / design).mkdir()
        (tmp_path / design / "top.fs").write_bytes(b"bitstream")
    monkeypatch.setattr(mcp_server.Session, "open", classmethod(lambda cls, serial=None: b.open(serial)))
    monkeypatch.delenv(bs.ENV_AUTO, raising=False)
    return b


def make_probe(board, tmp_path):
    p = Probe("0001", bitstreams=str(tmp_path))
    p._init_design(str(tmp_path), program=board.program, sleep=lambda s: None)
    return p


def test_a_jtag_connection_switches_to_the_jtag_design_and_back(board, tmp_path):
    p = make_probe(board, tmp_path)
    try:
        status = p.target_connect("cortex_m", jtag_chain=[4, 5])
        switch = status["design_switch"]
        assert (switch["before"], switch["after"]) == ("fprobe", "fprobe_jtag")
        assert board.programmed == [tmp_path / "fprobe_jtag" / "top.fs"] and board.connected["jtag_chain"] == [4, 5]
        assert p.probe_design_get()["design"] == "fprobe_jtag"
        status = p.target_connect("stm32f401xe")                 # SWD: 標準に戻す
        assert status["design_switch"]["after"] == "fprobe" and len(board.programmed) == 2
        assert "design_switch" not in p.target_connect("stm32f401xe")    # もう要らない
    finally:
        p.close()


def test_switching_explicitly_and_what_is_reported(board, tmp_path):
    p = make_probe(board, tmp_path)
    try:
        info = p.probe_design_get()
        assert info["design"] == "fprobe" and info["features"] == {"swd": True, "jtag": False, "sampler": True,
                                                                  "inject": True, "la_inputs": 0,
                                                                  "la_stream_events": False}
        assert info["switchable"] and info["auto_switch"]
        assert [(d["design"], d["protocols"], d["for"], d["current"]) for d in info["designs"]] == [
            ("fprobe", ["swd"], [], True), ("fprobe_jtag", ["jtag"], [], False),
            ("fprobe_la", ["swd"], ["la_inputs", "la_stream_events"], False)]
        assert [d["available"] for d in info["designs"]][:2] == [True, True]
        assert p.probe_design_switch(protocol="swd") == {"switched": False, "design": "fprobe"}
        out = p.probe_design_switch(design="fprobe_jtag")
        assert out["switched"] and out["after"] == "fprobe_jtag" and board.opened >= 2
        with pytest.raises(ValueError, match="どちらか"):
            p.probe_design_switch()
        with pytest.raises(ValueError, match="切り替えられる"):
            p.probe_design_switch(design="fprobe_eth")
        p.target_connect("cortex_m", jtag_chain=[4])
        with pytest.raises(RuntimeError, match="接続中"):
            p.probe_design_switch(protocol="swd")
    finally:
        p.close()


def test_without_auto_switch_jtag_is_refused_and_swd_stays(board, tmp_path, monkeypatch):
    monkeypatch.setenv(bs.ENV_AUTO, "0")
    p = make_probe(board, tmp_path)
    try:
        with pytest.raises(bs.DesignSwitchError, match="自動の書き替えは切ってある"):
            p.target_connect("cortex_m", jtag_chain=[4, 5])
        assert board.programmed == []
        board.design = "fprobe_jtag"
        p.close()
        assert "design_switch" not in p.target_connect("stm32f401xe")    # SWD はどちらの構成にもある
        assert board.programmed == []
    finally:
        p.close()


def test_a_missing_bitstream_or_a_device_that_does_not_come_back_is_an_error(board, tmp_path):
    p = make_probe(board, tmp_path)
    try:
        (tmp_path / "fprobe_jtag" / "top.fs").unlink()
        with pytest.raises(bs.DesignSwitchError, match="fprobe_gw build fprobe_jtag"):
            p.target_connect("cortex_a7")
        assert board.programmed == []
        (tmp_path / "fprobe_jtag" / "top.fs").write_bytes(b"bitstream")
        board.absent_polls = 10 ** 6
        p._reenumerate_s = 0.0
        with pytest.raises(bs.DesignSwitchError, match="USB に現れない"):
            p.target_connect("cortex_a7")
    finally:
        p.close()


# --- USB がつながり直したとき（B28） --------------------------------------------------------------

def lost(p):
    """USB が切れた後の呼び出しの誤り。"""
    with pytest.raises(usb.core.USBError) as e:
        p.probe_info()
    return e.value


def test_a_probe_that_comes_back_on_usb_is_reopened_and_says_what_changed(board, tmp_path):
    p = make_probe(board, tmp_path)
    try:
        p.target_connect("stm32f401xe")
        board.unplug(sampler=False, jtag=True)          # 電源が落ち、Flash の古い版で立ち上がった
        out = p.recover_link(lost(p))
        assert "つなぎ直した" in out["message"] and "ターゲット" in out["message"]
        assert out["changed"] == {"jtag": [False, True], "sampler": [True, False]}   # 書き込んだ版でないかもしれない
        assert "sampler: True → False" in out["message"]
        assert p._debug is None
        assert p.probe_info()["capabilities"]["sampler"] is False
        board.unplug(sampler=False, jtag=True)          # 同じ版でつながり直しただけ
        out = p.recover_link(lost(p))
        assert out["changed"] == {} and "変わった" not in out["message"]
    finally:
        p.close()


def test_the_version_tells_for_sure_whether_the_probe_came_back_with_another_bitstream(board, tmp_path):
    """U49: 版（ビルドの入力の要約）を比べて、Flash の古い版で立ち上がったかを確実に言う（機能が同じでも）。"""
    from fprobe.caps import Build
    developed = Build(id="1111222233334444", time=1_790_000_000, git="abcdef01", dirty=False)
    flashed = Build(id="aaaabbbbccccdddd", time=1_780_000_000, git="12345678", dirty=False)
    (tmp_path / "fprobe" / "inputs.sha256").write_text(developed.id + "0" * 48, encoding="utf-8")
    board.features = {"build": developed}
    p = make_probe(board, tmp_path)
    try:
        assert p.probe_info()["version"] == developed.describe()
        board.unplug(build=flashed)                      # 機能は同じ・版が違う
        out = p.recover_link(lost(p))
        assert out["version"] == {"before": developed.describe(), "after": flashed.describe(), "changed": True,
                                  "latest_build": False}
        assert "版が変わった" in out["message"] and "fprobe_gw program" in out["message"]
        assert "build" not in out["changed"]             # 版は changed（機能の違い）とは別に返す
        board.unplug(build=flashed)                      # 同じ版でつながり直しただけ
        out = p.recover_link(lost(p))
        assert out["version"]["changed"] is False and "版が変わった" not in out["message"]
        board.unplug(build=developed)                    # ビルド済みの最新に戻った
        out = p.recover_link(lost(p))
        assert out["version"]["changed"] is True and out["version"]["latest_build"] is True
        assert "最新" in out["message"]
    finally:
        p.close()


def test_without_versions_the_features_are_the_only_hint(board, tmp_path):
    p = make_probe(board, tmp_path)
    try:
        p.probe_info()
        board.unplug(sampler=False, jtag=True)
        out = p.recover_link(lost(p))
        assert out["version"] == {"before": None, "after": None, "changed": None, "latest_build": None}
        assert "かもしれない" in out["message"]
    finally:
        p.close()


def test_only_a_lost_link_is_recovered(board, tmp_path):
    p = make_probe(board, tmp_path)
    try:
        p.probe_info()
        assert p.recover_link(RuntimeError("違う誤り")) is None
        assert p.recover_link(usb.core.USBTimeoutError("待ちすぎ", errno=110)) is None
        assert board.opened == 1
        wrapped = RuntimeError("pyOCD の誤り")
        wrapped.__cause__ = usb.core.USBError("No such device", errno=19)
        assert p.recover_link(wrapped) is not None     # 包まれていても USB の切断なら直す
    finally:
        p.close()


def test_a_probe_that_does_not_come_back_says_so(board, tmp_path):
    p = make_probe(board, tmp_path)
    try:
        p.probe_info()
        board.unplug(absent_polls=10 ** 6)
        p._reenumerate_s = 0.0
        with pytest.raises(bs.DesignSwitchError, match="USB に現れない"):
            p.recover_link(lost(p))
        board.absent = 0
        assert p.probe_info()["capabilities"]["design"] == "fprobe"    # 後で現れれば、次の呼び出しで開く
    finally:
        p.close()


def test_output_pins_are_taken_back_when_the_probe_comes_back_or_is_switched(board, tmp_path, monkeypatch):
    """B29: 開き直し・書き替えの後、出力に使っていた端子をすぐ取り戻す（普段の機能でターゲットを駆動しないよう）。"""
    p = make_probe(board, tmp_path)
    calls = []
    monkeypatch.setattr(p, "_restore_outputs", lambda: calls.append(1) or {"restored": [4], "failed": {7: "ch7: 無い"}})
    try:
        p.probe_info()
        board.unplug()
        out = p.recover_link(lost(p))
        assert calls and out["outputs"] == {"restored": [4], "failed": {7: "ch7: 無い"}}
        assert "ch7: 無い" in out["message"]                 # 取り戻せない端子は知らせる
        assert p.probe_design_switch(design="fprobe_jtag")["outputs"]["restored"] == [4]
        assert len(calls) == 2
    finally:
        p.close()


def test_reopening_the_device_keeps_watching_the_elf(board, tmp_path):
    """開き直し（B28）・構成の書き替えは機器を閉じるだけ: ELF の更新の見張り（機器と無関係）は続ける。
    2026-10-18 に実機で、開き直しの後に ELF を作り直しても読み込み直さなくなっていた（close が見張りを止めていた）。"""
    p = make_probe(board, tmp_path)
    try:
        p._ensure_elf_watcher()
        p.probe_info()
        board.unplug()
        p.recover_link(lost(p))
        p.probe_design_switch(design="fprobe_jtag")
        assert not p._elf_closing.is_set() and p._elf_thread is not None and p._elf_thread.is_alive()
    finally:
        p.close()
    assert p._elf_closing.is_set()                       # 本当に閉じるときは止める
