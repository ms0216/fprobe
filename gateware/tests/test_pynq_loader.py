import subprocess
from pathlib import Path

import pytest

from fprobe_gw.toolchain.pynq_loader import PynqError, PynqLoader, render_jtag_tcl
from fprobe_gw.toolchain.vivado import VivadoToolchain


class FakeRun:
    def __init__(self, fail_on=None):
        self.calls, self.fail_on = [], fail_on

    def __call__(self, args, **kw):
        args = list(map(str, args))
        self.calls.append(args)
        rc = 1 if self.fail_on and any(self.fail_on in a for a in args) else 0
        return subprocess.CompletedProcess(args, rc, stdout="out", stderr="err")


@pytest.fixture
def loader(tmp_path):
    run = FakeRun()
    return PynqLoader(VivadoToolchain(tmp_path / "v"), run=run), run


def test_jtag_tcl_selects_the_tul_cable_and_checks_the_idcode(tmp_path):
    tcl = render_jtag_tcl(tmp_path / "top.bit")
    assert "*1234-TUL*" in tcl and "3727093" in tcl
    assert (tmp_path / "top.bit").resolve().as_posix() in tcl
    assert tcl.index("IDCODE") < tcl.index("program_hw_devices")


def test_program_stops_the_relay_then_writes_by_jtag_then_starts_it(loader, tmp_path):
    pynq, run = loader
    bit = tmp_path / "top.bit"
    bit.write_bytes(b"bit")
    pynq.program(bit, relay=True, design_id="abc")
    ssh_before, vivado, ssh_after = run.calls
    assert ssh_before[0] == "ssh" and ssh_before[-1] == "sudo -n /usr/local/bin/fprobe-pl before"
    assert vivado[0].endswith("vivado.bat") and "-source" in vivado
    assert ssh_after[-1] == "sudo -n /usr/local/bin/fprobe-pl after abc"


def test_a_design_without_the_frame_link_does_not_start_the_relay(loader, tmp_path):
    pynq, run = loader
    bit = tmp_path / "top.bit"
    bit.write_bytes(b"bit")
    pynq.program(bit, relay=False)
    assert len(run.calls) == 2 and "before" in run.calls[0][-1]


def test_jtag_failure_does_not_start_the_relay(tmp_path):
    run = FakeRun(fail_on="-source")
    bit = tmp_path / "top.bit"
    bit.write_bytes(b"bit")
    with pytest.raises(PynqError, match="JTAG"):
        PynqLoader(VivadoToolchain(tmp_path / "v"), run=run).program(bit, relay=True, design_id="ab")
    assert len(run.calls) == 2


def test_ssh_failure_is_reported_in_japanese(tmp_path):
    run = FakeRun(fail_on="before")
    bit = tmp_path / "top.bit"
    bit.write_bytes(b"bit")
    with pytest.raises(PynqError, match="SSH"):
        PynqLoader(VivadoToolchain(tmp_path / "v"), run=run).program(bit, relay=False)
    assert len(run.calls) == 1        # JTAG まで進まない


def test_a_relative_bitstream_path_is_passed_to_vivado_as_absolute(loader, tmp_path, monkeypatch):
    # cwd を置き場所に移して Vivado を起動するので、相対のままだと TCL が見つからない（2026-10-20 に実機で失敗）
    pynq, run = loader
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "top.bit").write_bytes(b"bit")
    monkeypatch.chdir(tmp_path)
    pynq.program(Path("b") / "top.bit", relay=False)
    tcl = Path(run.calls[1][run.calls[1].index("-source") + 1])
    assert tcl.is_absolute() and tcl.is_file()


def test_shifter_is_enabled_after_writing_for_a_design_the_ps_reaches_over_axi(loader, tmp_path):
    # PS7 を持つが中継を持たない回路（Zynq DUT）: 書いた後にレベルシフタを有効にしないと PS の AXI が PL に届かない
    pynq, run = loader
    bit = tmp_path / "top.bit"
    bit.write_bytes(b"bit")
    pynq.program(bit, relay=False, shifter=True)
    before, vivado, enable = run.calls
    assert before[-1].endswith("fprobe-pl before") and vivado[0].endswith("vivado.bat")
    assert enable[0] == "ssh" and enable[-1] == "sudo -n /usr/local/bin/fprobe-pl enable"


def test_shifter_stays_disabled_by_default_and_without_a_successful_write(loader, tmp_path):
    pynq, run = loader
    bit = tmp_path / "top.bit"
    bit.write_bytes(b"bit")
    pynq.program(bit, relay=False)
    assert not any(c[-1].endswith("enable") for c in run.calls)
    failing = FakeRun(fail_on="-source")
    with pytest.raises(PynqError):
        PynqLoader(VivadoToolchain(tmp_path / "v"), run=failing).program(bit, relay=False, shifter=True)
    assert not any(c[-1].endswith("enable") for c in failing.calls)
