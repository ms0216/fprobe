"""PynqLoader の堅牢さ: 実機なしで再現できる失敗の経路（引用・タイムアウト・コマンドが無い）。"""
import subprocess

import pytest

from fprobe_gw.toolchain.pynq_loader import PynqError, PynqLoader
from fprobe_gw.toolchain.vivado import VivadoToolchain


class FakeRun:
    def __init__(self):
        self.calls = []

    def __call__(self, args, **kw):
        self.calls.append(list(map(str, args)))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


def _bit(tmp_path):
    bit = tmp_path / "top.bit"
    bit.write_bytes(b"bit")
    return bit


@pytest.mark.parametrize("bad", ["", "abc; reboot", "a b", "$(id)", "ABC", "x\ny"])
def test_a_bad_design_id_is_refused_before_touching_the_board(tmp_path, bad):
    # id は SSH の遠隔のシェルにそのまま渡る。16 進以外（空を含む）は、PYNQ に触れる前に断る
    # （空だと JTAG で書いた後に補助が拒み、中継が止まったままになる）
    run = FakeRun()
    with pytest.raises(PynqError, match="id"):
        PynqLoader(VivadoToolchain(tmp_path / "v"), run=run).program(_bit(tmp_path), relay=True, design_id=bad)
    assert run.calls == []


def test_a_timeout_is_reported_as_pynq_error(tmp_path):
    def run(args, **kw):
        raise subprocess.TimeoutExpired(args, kw.get("timeout", 0))

    with pytest.raises(PynqError, match="時間"):
        PynqLoader(VivadoToolchain(tmp_path / "v"), run=run).ssh("true")


def test_a_vivado_timeout_leaves_the_relay_stopped_and_is_reported(tmp_path):
    calls = []

    def run(args, **kw):
        calls.append(list(map(str, args)))
        if "-source" in calls[-1]:
            raise subprocess.TimeoutExpired(args, 600)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    with pytest.raises(PynqError, match="Vivado"):
        PynqLoader(VivadoToolchain(tmp_path / "v"), run=run).program(_bit(tmp_path), relay=True, design_id="ab")
    assert len(calls) == 2 and not any("after" in c[-1] or c[-1].endswith("enable") for c in calls)


def test_a_missing_command_is_reported_as_pynq_error(tmp_path):
    def run(args, **kw):
        raise FileNotFoundError("ssh")

    with pytest.raises(PynqError, match="起動できない"):
        PynqLoader(VivadoToolchain(tmp_path / "v"), run=run).ssh("true")


@pytest.mark.parametrize("kw", [{"user": "x'; rm -rf /; '"}, {"user": "a b"}, {"host": "h; reboot"},
                                {"host": "-oProxyCommand=x"}])
def test_user_and_host_that_could_change_the_command_are_refused(tmp_path, kw):
    with pytest.raises(PynqError):
        PynqLoader(VivadoToolchain(tmp_path / "v"), **kw)


def test_ssh_has_a_connect_timeout_and_never_asks_for_a_password(tmp_path):
    run = FakeRun()
    PynqLoader(VivadoToolchain(tmp_path / "v"), run=run).ssh("true")
    args = run.calls[0]
    assert "BatchMode=yes" in args and "ConnectTimeout=10" in args
