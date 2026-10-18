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
    assert cli.main(["detect"]) == 0
    assert "8481" in capsys.readouterr().out.splitlines()[-1]


def test_detect_reports_ambiguity_as_error(monkeypatch, capsys):
    monkeypatch.setattr(cli, "list_ftdi_devices", lambda: [])
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
