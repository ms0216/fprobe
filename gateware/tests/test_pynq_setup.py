import subprocess

import pytest

from fprobe_gw.pynq_setup import install
from fprobe_gw.toolchain.pynq_loader import PynqError, PynqLoader
from fprobe_gw.toolchain.vivado import VivadoToolchain


def test_install_builds_on_the_board_and_enables_the_service(tmp_path):
    calls = []

    def run(args, **kw):
        calls.append((list(map(str, args)), kw.get("input")))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    loader = PynqLoader(VivadoToolchain(tmp_path), run=run)
    install(loader, run=run, env={"FPROBE_PYNQ_SUDO_PASSWORD": "pw"})
    (mkdir, _), (scp, _), (make, _), (root, password) = calls
    assert scp[0] == "scp" and any(a.endswith("fprobe-pl") for a in scp)
    assert "make" in make[-1]
    assert password == "pw\n" and "sudo -S" in root[-1]
    assert "systemctl enable fprobe-relay" in root[-1] and "visudo -cf" in root[-1]
    assert "NOPASSWD: /usr/local/bin/fprobe-pl" in root[-1]


def test_install_needs_the_sudo_password(tmp_path):
    with pytest.raises(PynqError, match="FPROBE_PYNQ_SUDO_PASSWORD"):
        install(PynqLoader(VivadoToolchain(tmp_path)), env={})


def test_reserve_puts_uenv_and_the_region_then_reboots(tmp_path):
    # P3: DDR の上端を Linux から外す（/boot/uEnv.txt の mem=480M）と、中継が読む領域（/etc/fprobe/mem）を置いて再起動する
    from fprobe_gw.pynq_setup import reserve
    calls = []

    def run(args, **kw):
        calls.append((list(map(str, args)), kw.get("input")))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    loader = PynqLoader(VivadoToolchain(tmp_path), run=run)
    reserve(loader, run=run, env={"FPROBE_PYNQ_SUDO_PASSWORD": "pw"})
    scp = [a for a, _ in calls if a[0] == "scp"][0]
    assert any(a.endswith("uEnv.txt") for a in scp) and any(a.endswith("fprobe-mem") for a in scp)
    root = calls[-1][0][-1]
    assert "/boot/uEnv.txt" in root and "/etc/fprobe/mem" in root and "reboot" in root
    assert root.index("fprobe-pl before") < root.index("reboot")        # 再起動の前に中継を止め、レベルシフタを切る


def test_unreserve_removes_uenv_and_the_region(tmp_path):
    from fprobe_gw.pynq_setup import reserve
    calls = []

    def run(args, **kw):
        calls.append(list(map(str, args)))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    reserve(PynqLoader(VivadoToolchain(tmp_path), run=run), run=run, env={"FPROBE_PYNQ_SUDO_PASSWORD": "pw"},
            undo=True)
    root = calls[-1][-1]
    assert "rm -f /boot/uEnv.txt /etc/fprobe/mem" in root and "reboot" in root
