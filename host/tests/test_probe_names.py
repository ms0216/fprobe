"""fprobe.probe_names: プローブの個体名（U49）。"""

import argparse

import pytest

from fprobe import probe_names as pn


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path))
    return tmp_path


USB = {"link": "usb", "address": "0001"}
ETH = {"link": "eth", "address": "192.168.77.2"}


def test_names_are_saved_and_resolved(home):
    assert pn.load() == {}
    pn.set_name("bench-a", USB)
    pn.set_name("remote", ETH)
    assert pn.resolve("bench-a") == USB
    assert pn.name_of(ETH) == "remote" and pn.name_of({"link": "usb", "address": "9999"}) is None
    assert (home / "probes.json").is_file()


def test_one_name_per_probe_and_renaming(home):
    pn.set_name("old", USB)
    pn.set_name("new", USB)
    assert pn.load() == {"new": USB}
    pn.set_name("new", ETH)                          # 同じ名前を別の機器に付け直す
    assert pn.load() == {"new": ETH}


def test_invalid_names_and_unknown_names(home):
    with pytest.raises(pn.ProbeNameError):
        pn.set_name("has space", USB)
    with pytest.raises(pn.ProbeNameError):
        pn.set_name("ok", {"link": "pcie", "address": "x"})
    with pytest.raises(pn.ProbeNameError, match="登録されていない"):
        pn.resolve("nobody")
    with pytest.raises(pn.ProbeNameError):
        pn.remove("nobody")
    pn.set_name("a", USB)
    pn.remove("a")
    assert pn.load() == {}


def test_probe_info_reports_the_device_name_and_version(home, monkeypatch):
    from types import SimpleNamespace

    from fprobe import mcp_server
    from fprobe.caps import Build, Capabilities
    build = Build(id="0123456789abcdef", time=1_790_000_000, git="89abcdef", dirty=False)
    caps = Capabilities(design="fprobe", board="Tang Primer 20K Dock", sampler=False, build=build)
    device = SimpleNamespace(capabilities=lambda: caps, time=lambda: 5)
    session = SimpleNamespace(device=device, address=USB, close=lambda: None, link=None)
    monkeypatch.setattr(mcp_server.Session, "open", classmethod(lambda cls, serial=None: session))
    p = mcp_server.Probe()
    try:
        info = p.probe_info()
        assert (info["device"], info["name"], info["version"]) == (USB, None, build.describe())
        assert "0123456789abcdef" in info["version"] and "git 89abcdef" in info["version"]
        assert p.probe_name_set("bench") == {"device": USB, "name": "bench"}
        assert p.probe_info()["name"] == "bench" and pn.resolve("bench") == USB
        assert p.probe_name_set(None)["name"] is None and pn.load() == {}
    finally:
        p.close()


def test_cli_lists_and_removes_names(home, capsys):
    from fprobe.__main__ import main
    pn.set_name("a", USB)
    with pytest.raises(SystemExit) as e:
        main(["name", "list"])
    assert e.value.code == 0 and "a\tusb\t0001" in capsys.readouterr().out
    with pytest.raises(SystemExit) as e:
        main(["name", "remove", "a"])
    assert e.value.code == 0 and pn.load() == {}
    with pytest.raises(SystemExit) as e:
        main(["name", "remove", "a"])
    assert e.value.code == 1


def test_command_line_name_fills_the_address(home):
    pn.set_name("lab", ETH)
    args = argparse.Namespace(name="lab", serial=None, port=None, eth=None)
    pn.apply_to_args(args)
    assert (args.serial, args.port, args.eth) == (None, None, "192.168.77.2")
    pn.set_name("desk", USB)
    args = argparse.Namespace(name="desk", serial=None, port=None, eth=None)
    pn.apply_to_args(args)
    assert args.serial == "0001"
    with pytest.raises(pn.ProbeNameError, match="一緒に"):
        pn.apply_to_args(argparse.Namespace(name="desk", serial="0002", port=None, eth=None))
    untouched = argparse.Namespace(name=None, serial="0003", port=None, eth=None)
    pn.apply_to_args(untouched)
    assert untouched.serial == "0003"
