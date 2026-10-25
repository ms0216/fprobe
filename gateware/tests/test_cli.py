from pathlib import Path

from fprobe_gw import cli
from fprobe_gw.boards.primer20k import PRIMER20K
from fprobe_gw.toolchain.ftdi import FtdiDeviceInfo


def test_parser_accepts_build_and_program():
    p = cli.make_parser()
    a = p.parse_args(["build", "bringup", "--program"])
    assert a.design == "bringup" and a.program and not a.persistent
    a = p.parse_args(["program", "x.fs", "--persistent"])
    assert a.bitstream == "x.fs" and a.persistent and a.board == "primer20k"


def test_boards_registry():
    assert cli.BOARDS["primer20k"] is PRIMER20K


def test_build_uses_the_designs_project(monkeypatch, tmp_path, capsys):
    built = {}

    class FakeBuilder:
        def __init__(self, toolchain):
            pass

        def build(self, project, build_dir):
            built["project"], built["dir"], built["how"] = project, build_dir, "single"
            raise cli.BuildError("stop here")

        def build_best(self, project, build_dir):
            built["project"], built["dir"], built["how"] = project, build_dir, "best"
            raise cli.BuildError("stop here")

    monkeypatch.setattr(cli, "GowinBuilder", FakeBuilder)
    monkeypatch.setattr(cli.GowinToolchain, "locate", classmethod(lambda c: c(tmp_path)))
    assert cli.main(["build", "bringup", "--build-dir", str(tmp_path / "b")]) == 1
    assert built["project"].top == "primer20k_bringup_top" and built["how"] == "best"   # 既定は配置を変えた並列ビルド
    assert "stop here" in capsys.readouterr().err
    assert cli.main(["build", "bringup", "--single", "--build-dir", str(tmp_path / "b")]) == 1
    assert built["how"] == "single"


def test_detect_prints_selected_location(monkeypatch, capsys):
    devices = [FtdiDeviceInfo(0, 2, 6, 0x04036010, 545, "1234-tulA", "TUL A"),
               FtdiDeviceInfo(1, 0, 4, 0x04036010, 8481, "A", "USB Debugger A")]
    monkeypatch.setattr(cli, "list_ftdi_devices", lambda: devices)
    monkeypatch.setattr(cli, "usb_port_chain", lambda usb_id, serial: devices[1].port_chain)   # 実機の USB に依らない
    assert cli.main(["detect"]) == 0
    assert "8481" in capsys.readouterr().out.splitlines()[-1]


def test_detect_reports_ambiguity_as_error(monkeypatch, capsys):
    monkeypatch.setattr(cli, "list_ftdi_devices", lambda: [])
    monkeypatch.setattr(cli, "usb_port_chain", lambda usb_id, serial: (2, 1))
    assert cli.main(["detect"]) == 1
    assert "見つからない" in capsys.readouterr().err


def test_gen_writes_artifacts(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "write_all", lambda: [tmp_path / "x.vhd"])
    assert cli.main(["gen"]) == 0
    assert "x.vhd" in capsys.readouterr().out


def test_program_refuses_a_bitstream_whose_build_did_not_succeed(monkeypatch, tmp_path, capsys):
    # 成功したビルド（タイミングを満たした）の印 inputs.sha256 が隣に無いビットストリームは、--unverified なしでは書かない
    written = []

    class FakeProgrammer:
        def __init__(self, toolchain, identity):
            pass

        def program(self, bitstream, persistent=False):
            written.append(bitstream)

    monkeypatch.setattr(cli, "GowinProgrammer", FakeProgrammer)
    monkeypatch.setattr(cli.GowinToolchain, "locate", classmethod(lambda c: c(tmp_path)))
    fs = tmp_path / "p1" / "top.fs"
    fs.parent.mkdir()
    fs.write_bytes(b"x")
    assert cli.main(["program", str(fs)]) == 1 and written == []
    assert "inputs.sha256" in capsys.readouterr().err
    assert cli.main(["program", str(fs), "--unverified"]) == 0 and written == [fs]
    (fs.parent / "inputs.sha256").write_text("ok")
    assert cli.main(["program", str(fs)]) == 0 and written == [fs, fs]


def test_pynq_relay_without_a_build_stamp_is_refused_before_writing(monkeypatch, tmp_path, capsys):
    # 印（inputs.sha256）が無いと中継の印の id が空になり、書いた後に fprobe-pl が拒む。書く前に断る
    bit = tmp_path / "top.bit"
    bit.write_bytes(b"bit")
    calls = []
    monkeypatch.setattr(cli, "PynqLoader", lambda *a, **k: calls.append("loader"))
    assert cli.main(["program", str(bit), "--board", "pynq_z2", "--relay", "--unverified"]) == 1
    assert calls == [] and "inputs.sha256" in capsys.readouterr().err


def test_shifter_is_refused_for_a_bitstream_that_is_not_a_ps_axi_design(monkeypatch, tmp_path, capsys):
    # レビューの指摘: PS7 の無い回路でシフタを有効にすると Linux が固まった。--shifter は ps_axi の構成のビルドの置き場所のときだけ
    for directory in ("blinky_elsewhere", "pynq_blinky", "zynq_dut"):
        bit = tmp_path / directory / "top.bit"
        bit.parent.mkdir()
        bit.write_bytes(b"bit")
    calls = []
    monkeypatch.setattr(cli, "PynqLoader", lambda *a, **k: calls.append("loader") or type("L", (), {"program": lambda *a, **k: calls.append(k)})())
    for refused in ("blinky_elsewhere", "pynq_blinky"):
        assert cli.main(["program", str(tmp_path / refused / "top.bit"), "--board", "pynq_z2", "--shifter", "--unverified"]) == 1
        assert "--shifter" in capsys.readouterr().err
    assert calls == []
    assert cli.main(["program", str(tmp_path / "zynq_dut" / "top.bit"), "--board", "pynq_z2", "--shifter", "--unverified"]) == 0
    assert calls[-1]["shifter"] is True


def test_detect_on_a_board_without_gowin_jtag_is_an_error_message(capsys):
    assert cli.main(["detect", "--board", "pynq_z2"]) == 1
    assert "1234-TUL" in capsys.readouterr().err


def test_build_program_persistent_on_pynq_is_refused(monkeypatch, capsys):
    built = []
    monkeypatch.setattr(cli, "VivadoBuilder", lambda *a, **k: built.append("builder"))
    monkeypatch.setattr(cli.VivadoToolchain, "locate", classmethod(lambda c: c(Path("."))))
    assert cli.main(["build", "fprobe_pynq", "--program", "--persistent"]) == 1
    assert built == [] and "persistent" in capsys.readouterr().err
