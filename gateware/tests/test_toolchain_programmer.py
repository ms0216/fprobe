import subprocess

import pytest

from fprobe_gw.toolchain.ftdi import FtdiDeviceInfo
from fprobe_gw.toolchain.gowin import GowinToolchain
from fprobe_gw.toolchain.programmer import (GowinProgrammer, JtagIdentity, ProgrammerError,
                                            UnsafeTargetError)

PRIMER = JtagIdentity(board="Tang Primer 20K Dock", usb_id=0x04036010, usb_serial="FACTORYAIOT_PRO",
                      ftdi_description="USB Debugger A", gowin_cable="USB Debugger A",
                      gowin_device="GW2A-18C", idcode=0x0000081B)
DEVICES = [FtdiDeviceInfo(2, 0, 4, 0x04036010, 8481, "A", "USB Debugger A")]

# programmer_cli 1.9.11.03 の実出力
IDCODE_OK = (" Target Cable: USB Debugger A/0/8481/null@2.5MHz\n"
             " Target Device: GW2A-18C(0x0000081B)\n"
             " Operation \"Read Device Codes\" for device#1...\n"
             " ID Code is: 0x0000081B\n User Code is: 0x0000C53E\n"
             " Status Code is: 0x00006020\n Finished.\n Cost 0.14 second(s)\n")
IDCODE_OTHER = IDCODE_OK.replace("ID Code is: 0x0000081B", "ID Code is: 0x23727093")
PROGRAM_OK = (" Target Cable: USB Debugger A/0/8481/null@2.5MHz\n"
              " Operation \"SRAM Program\" for device#1...\n"
              "Programming...: [#########################] 100%\n"
              " User Code is: 0x0000E264\n Status Code is: 0x00006020\n Finished.\n")


class FakeRun:
    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls = []

    def __call__(self, args, **kwargs):
        self.calls.append([str(a) for a in args])
        return subprocess.CompletedProcess(args, 0, stdout=self.outputs.pop(0), stderr="")


@pytest.fixture
def bitstream(tmp_path):
    fs = tmp_path / "top.fs"
    fs.write_bytes(b"x")
    return fs


def programmer(tmp_path, run):
    return GowinProgrammer(GowinToolchain(tmp_path), PRIMER, list_devices=lambda: DEVICES,
                           find_port_chain=lambda usb_id, serial: (1, 2), run=run)


def arg(call, name):
    return call[call.index(name) + 1]


def test_program_sram_verifies_idcode_first(tmp_path, bitstream):
    run = FakeRun(IDCODE_OK, PROGRAM_OK)
    programmer(tmp_path, run).program(bitstream)
    read, prog = run.calls
    assert arg(read, "--location") == "8481"
    assert arg(read, "--cable") == "USB Debugger A"
    assert arg(read, "--run") == "0"
    assert arg(prog, "--run") == "2"
    assert arg(prog, "--fsFile") == str(bitstream)
    assert arg(prog, "--location") == "8481"


def test_bitstream_path_is_passed_as_absolute(tmp_path, bitstream, monkeypatch):
    # programmer_cli は相対パスを自身の場所基準で解釈するため、常に絶対パスで渡す
    monkeypatch.chdir(tmp_path)
    run = FakeRun(IDCODE_OK, PROGRAM_OK)
    programmer(tmp_path, run).program(bitstream.relative_to(tmp_path))
    assert arg(run.calls[1], "--fsFile") == str(bitstream.resolve())


def test_persistent_uses_flash_operation(tmp_path, bitstream):
    run = FakeRun(IDCODE_OK, PROGRAM_OK)
    programmer(tmp_path, run).program(bitstream, persistent=True)
    assert arg(run.calls[1], "--run") == "36"


def test_refuses_on_idcode_mismatch(tmp_path, bitstream):
    run = FakeRun(IDCODE_OTHER)
    with pytest.raises(UnsafeTargetError, match="0x23727093"):
        programmer(tmp_path, run).program(bitstream)
    assert len(run.calls) == 1  # 書き込みコマンドは実行されない


def test_reports_unreadable_idcode(tmp_path, bitstream):
    run = FakeRun(" Error: cable open failed\n")
    with pytest.raises(ProgrammerError, match="IDCODE"):
        programmer(tmp_path, run).program(bitstream)


def test_reports_failed_programming(tmp_path, bitstream):
    run = FakeRun(IDCODE_OK, " Error: verify failed\n")
    with pytest.raises(ProgrammerError, match="verify failed"):
        programmer(tmp_path, run).program(bitstream)


def test_hung_programmer_is_reported_as_programmer_error(tmp_path, bitstream):
    def hang(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs.get("timeout", 0))

    with pytest.raises(ProgrammerError, match="応答しない"):
        programmer(tmp_path, hang).program(bitstream)


def test_missing_bitstream_is_rejected_before_any_jtag_access(tmp_path):
    run = FakeRun()
    with pytest.raises(FileNotFoundError):
        programmer(tmp_path, run).program(tmp_path / "none.fs")
    assert run.calls == []
