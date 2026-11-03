"""Probe の後始末（A9）: どの段も必ず試し、1 つの段の失敗で後ろの段（機器の口・リンク）を閉じ残さない。失敗は集めて返す。"""

from types import SimpleNamespace

import pytest

from fprobe.probe import Probe
from fprobe.session import Session


@pytest.fixture
def probe(tmp_path, monkeypatch):
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path))
    return Probe("unused")


def test_every_step_runs_even_when_earlier_steps_fail(probe, monkeypatch):
    called = []

    def ok(name):
        return lambda: called.append(name)

    def boom(name):
        def f():
            called.append(name)
            raise OSError(f"{name} が切れた")
        return f

    monkeypatch.setattr(probe, "_stop_la_record", boom("la_record"))
    monkeypatch.setattr(probe, "_close_streams", ok("streams"))
    monkeypatch.setattr(probe, "_close_formats", boom("formats"))
    monkeypatch.setattr(probe, "_stop_observing", ok("observing"))
    monkeypatch.setattr(probe, "_close_testpoint", ok("testpoint"))
    monkeypatch.setattr(probe, "_close_linux_halt", boom("linux_halt"))
    monkeypatch.setattr(probe, "target_disconnect", boom("target"))
    monkeypatch.setattr(probe, "_close_lowlevel", ok("lowlevel"))
    probe._uart = SimpleNamespace(close=boom("uart"))
    probe._session = SimpleNamespace(close=ok("session"))
    failures = probe._release_device()
    assert called == ["la_record", "streams", "formats", "observing", "testpoint", "linux_halt", "target", "lowlevel",
                      "uart", "session"]
    assert [f["step"] for f in failures] == ["la_record", "formats", "linux_halt", "target", "uart"]
    assert failures[0]["error"] == "OSError: la_record が切れた"
    assert probe._session is None and probe._uart is None              # 閉じるのに失敗した口も手放す


def test_close_returns_nothing_when_everything_closes(probe):
    assert probe.close() == []


def test_session_close_releases_the_link_even_if_the_device_close_fails():
    released = []

    class Dev:
        def close(self):
            raise OSError("切れた")
    s = Session(Dev(), lambda: None, lambda: released.append(True))
    with pytest.raises(OSError):
        s.close()
    assert released == [True]
