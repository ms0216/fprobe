import dataclasses
import subprocess
from pathlib import Path

import pytest

from fprobe_gw.toolchain.build import BuildError, GowinBuilder
from fprobe_gw.toolchain.gowin import GowinToolchain
from fprobe_gw.toolchain.project import ClockConstraint, GowinIp, GowinProject, Pin

REPORT_OK = (Path(__file__).parent / "data" / "gowin_timing_summary.html").read_text(
    encoding="utf-8")
REPORT_BAD = REPORT_OK.replace("<td>Setup</td>\n<td>0.000</td>", "<td>Setup</td>\n<td>-0.500</td>")


@pytest.fixture
def project(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "top.vhd").write_text("-- top")
    return GowinProject(name="top", part="GW2A-LV18PG256C8/I7", family="GW2A-18C", top="top",
                        sources=(src / "top.vhd",), pins=(Pin("clk27", "H11"),),
                        clocks=(ClockConstraint("clk27", "clk27", "port", 27_000_000),
                                ClockConstraint("sys_clk", "sys_clk", "net", 100_285_714)))


class FakeGwSh:
    def __init__(self, report=REPORT_OK, rc=0, produce=True, stdout="gw_sh output"):
        self.report, self.rc, self.produce, self.stdout = report, rc, produce, stdout
        self.calls = []

    def __call__(self, args, *, cwd, **kwargs):
        self.calls.append((list(map(str, args)), Path(cwd)))
        cwd = Path(cwd)
        if self.produce:
            pnr = cwd / "impl" / "pnr"
            pnr.mkdir(parents=True)
            (pnr / "project_tr_content.html").write_text(self.report, encoding="utf-8")
            (cwd / "top.fs").write_bytes(b"bitstream")
        return subprocess.CompletedProcess(args, self.rc, stdout=self.stdout, stderr="")


def build(tmp_path, gw_sh, project, build_dir=None):
    builder = GowinBuilder(GowinToolchain(tmp_path / "gw"), run=gw_sh)
    return builder.build(project, build_dir or tmp_path / "b")


def test_successful_build(tmp_path, project):
    gw_sh = FakeGwSh()
    result = build(tmp_path, gw_sh, project)
    assert result.bitstream == tmp_path / "b" / "top.fs"
    assert (tmp_path / "b" / "top.tcl").is_file() and (tmp_path / "b" / "top.sdc").is_file()
    assert result.timing.met
    assert result.log.read_text(encoding="utf-8") == "gw_sh output"
    (args, cwd), = gw_sh.calls
    assert args == [str(tmp_path / "gw" / "IDE" / "bin" / "gw_sh.exe"), "top.tcl"]
    assert cwd == tmp_path / "b"


def test_unchanged_inputs_reuse_the_verified_build(tmp_path, project):
    gw_sh = FakeGwSh()
    build(tmp_path, gw_sh, project)
    assert build(tmp_path, gw_sh, project).timing.met and len(gw_sh.calls) == 1     # 再検証して使う
    project.sources[0].write_text("-- changed")
    build(tmp_path, gw_sh, project)
    assert len(gw_sh.calls) == 2


def test_failed_timing_is_never_reused(tmp_path, project):
    gw_sh = FakeGwSh(REPORT_BAD)
    for _ in range(2):
        with pytest.raises(BuildError):
            build(tmp_path, gw_sh, project)
    assert len(gw_sh.calls) == 2


def test_stale_bitstream_is_never_reused(tmp_path, project):
    build_dir = tmp_path / "b"
    build_dir.mkdir()
    (build_dir / "top.fs").write_bytes(b"stale")
    with pytest.raises(BuildError, match="ビットストリーム"):
        build(tmp_path, FakeGwSh(produce=False), project, build_dir)
    assert not (build_dir / "top.fs").exists()


def test_rebuild_over_read_only_outputs(tmp_path, project):
    # Gowin は project.fs / top.fs などを読み取り専用で出力する。再ビルドで確実に消せること
    build_dir = tmp_path / "b"
    stale_report = build_dir / "impl" / "pnr" / "project_tr_content.html"
    stale_report.parent.mkdir(parents=True)
    stale_report.write_text(REPORT_BAD, encoding="utf-8")
    (build_dir / "top.fs").write_bytes(b"stale")
    for path in (stale_report, build_dir / "top.fs"):
        path.chmod(0o444)
    result = build(tmp_path, FakeGwSh(), project, build_dir)
    assert result.bitstream.read_bytes() == b"bitstream"
    assert result.timing.met   # 古い（違反した）レポートが残っていない


def test_nonzero_exit_is_an_error(tmp_path, project):
    with pytest.raises(BuildError, match="gw_sh.log"):
        build(tmp_path, FakeGwSh(rc=1), project)


def test_timing_violation_is_an_error(tmp_path, project):
    with pytest.raises(BuildError, match="タイミング"):
        build(tmp_path, FakeGwSh(report=REPORT_BAD), project)
    # タイミングを満たさないビットストリームは残さない（配置の別の候補を誤って書き込み、配置で壊れると誤解した。2026-10-17）
    assert not (tmp_path / "b" / "top.fs").exists()


def test_constrained_clock_missing_from_report_is_an_error(tmp_path, project):
    # SDC のネット名が外れると、そのクロックはレポートに現れず「違反なし」に見えてしまう
    lost = GowinProject(**{**project.__dict__, "clocks": project.clocks + (
        ClockConstraint("usb_clk", "usb_clk", "net", 60_000_000),)})
    with pytest.raises(BuildError, match="usb_clk"):
        build(tmp_path, FakeGwSh(), lost)


def test_constraint_frequency_mismatch_is_an_error(tmp_path, project):
    wrong = GowinProject(**{**project.__dict__, "clocks": (
        ClockConstraint("sys_clk", "sys_clk", "net", 50_000_000),)})
    with pytest.raises(BuildError, match="sys_clk"):
        build(tmp_path, FakeGwSh(), wrong)


def test_timing_failure_message_names_the_report(tmp_path, project):
    with pytest.raises(BuildError, match="project_tr_content.html"):
        build(tmp_path, FakeGwSh(report=REPORT_BAD), project)


class FakeIpGwSh(FakeGwSh):
    """IP 生成（generate.tcl）では生成物を作り、本体のビルドでは FakeGwSh と同じ。"""

    def __call__(self, args, *, cwd, **kwargs):
        if args[-1] == "generate.tcl":
            self.calls.append((list(map(str, args)), Path(cwd)))
            out = Path(cwd) / "MEM" / "src" / "MEM" / "MEM.v"
            out.parent.mkdir(parents=True)
            out.write_text("// encrypted")
            return subprocess.CompletedProcess(args, 0, stdout="generated", stderr="")
        return super().__call__(args, cwd=cwd, **kwargs)


def test_ip_is_generated_once_per_configuration(tmp_path, project):
    ip = GowinIp(module="MEM", kind="ddr3", config=(("CLK_Ratio", "1:4"), ("Memory_Clock", "400")))
    with_ip = dataclasses.replace(project, ips=(ip,))
    gw_sh = FakeIpGwSh()
    build(tmp_path, gw_sh, with_ip)
    script = (tmp_path / "b" / "ip" / "MEM" / "generate.tcl").read_text(encoding="utf-8")
    assert "create_ipc -name ddr3 -module_name MEM" in script
    assert script.index("CONFIG.CLK_Ratio -value {1:4}") < script.index("CONFIG.Memory_Clock -value {400}")
    assert "-device_version C" in script
    tcl = (tmp_path / "b" / "top.tcl").read_text(encoding="utf-8")
    assert "add_file -type verilog" in tcl and "ip/MEM/MEM/src/MEM/MEM.v" in tcl
    build(tmp_path, gw_sh, with_ip)                                        # 同じ設定: 生成し直さない
    assert [c for c, _ in gw_sh.calls].count([str(tmp_path / "gw" / "IDE" / "bin" / "gw_sh.exe"),
                                              "generate.tcl"]) == 1
    changed = dataclasses.replace(with_ip, ips=(dataclasses.replace(ip, config=(("Memory_Clock", "300"),)),))
    build(tmp_path, gw_sh, changed)                                        # 設定が変われば生成し直す
    assert sum(1 for c, _ in gw_sh.calls if c[-1] == "generate.tcl") == 2


def test_prebuilt_ip_is_copied_from_the_toolchain(tmp_path, project):
    netlist = tmp_path / "gw" / "IDE" / "ip" / "MEM.v"
    netlist.parent.mkdir(parents=True)
    netlist.write_text("// prebuilt")
    ip = GowinIp(module="MEM", kind="ddr3", config=(), prebuilt="IDE/ip/MEM.v")
    gw_sh = FakeIpGwSh()
    build(tmp_path, gw_sh, dataclasses.replace(project, ips=(ip,)))
    assert ip.source(tmp_path / "b").read_text() == "// prebuilt"
    assert not any(c[-1] == "generate.tcl" for c, _ in gw_sh.calls)      # 生成はしない


class PerVariantGwSh(FakeGwSh):
    """配置の版（ビルドディレクトリ名 p<n>）ごとにレポートを変える偽の gw_sh。"""

    def __init__(self, reports):
        super().__init__()
        self.reports = reports

    def __call__(self, args, *, cwd, **kwargs):              # 並列に呼ばれるので共有の状態を使わない
        self.calls.append((list(map(str, args)), Path(cwd)))
        pnr = Path(cwd) / "impl" / "pnr"
        pnr.mkdir(parents=True)
        (pnr / "project_tr_content.html").write_text(self.reports[Path(cwd).name], encoding="utf-8")
        (Path(cwd) / "top.fs").write_bytes(b"bitstream")
        return subprocess.CompletedProcess(args, 0, stdout="gw_sh output", stderr="")


def test_build_best_tries_placements_in_parallel_and_skips_failures(tmp_path, project):
    project = dataclasses.replace(project, options=("-place_option 1 -route_option 1",))
    gw_sh = PerVariantGwSh({"p1": REPORT_BAD, "p0": REPORT_OK, "p2": REPORT_BAD, "p3": REPORT_OK, "p4": REPORT_OK})
    builder = GowinBuilder(GowinToolchain(tmp_path / "gw"), run=gw_sh)
    result = builder.build_best(project, tmp_path / "b")
    assert result.bitstream.parent.name == "p0" and result.timing.met
    assert sorted(cwd.name for _, cwd in gw_sh.calls) == ["p0", "p1", "p2"]        # 最初の 3 本で足りた
    assert "-place_option 0 -route_option 1" in (tmp_path / "b" / "p0" / "top.tcl").read_text()
    assert builder.build_best(project, tmp_path / "b").bitstream == result.bitstream and len(gw_sh.calls) == 3
    # 採用した版は決まった場所（build/<design>/top.fs）にも、成功したビルドの印と一緒に写す
    published = tmp_path / "b" / "top.fs"
    assert published.read_bytes() == result.bitstream.read_bytes()
    assert (tmp_path / "b" / "inputs.sha256").read_text() == (result.bitstream.parent / "inputs.sha256").read_text()


def test_build_best_reports_when_no_placement_meets_timing(tmp_path, project):
    gw_sh = PerVariantGwSh({f"p{n}": REPORT_BAD for n in range(3)})        # Gowin 1.9.11 の配置は 0〜2
    with pytest.raises(BuildError, match="どの配置でも"):
        GowinBuilder(GowinToolchain(tmp_path / "gw"), run=gw_sh).build_best(project, tmp_path / "b")
    assert len(gw_sh.calls) == 3


def test_line_endings_do_not_force_a_rebuild(tmp_path, project):
    project.sources[0].write_bytes(b"-- top\nentity top is\n")
    gw_sh = FakeGwSh()
    build(tmp_path, gw_sh, project)
    project.sources[0].write_bytes(b"-- top\r\nentity top is\r\n")          # 中身は同じで改行だけ CRLF
    build(tmp_path, gw_sh, project)
    assert len(gw_sh.calls) == 1
    project.sources[0].write_bytes(b"-- top\r\nentity top2 is\r\n")         # 中身が変われば作り直す
    build(tmp_path, gw_sh, project)
    assert len(gw_sh.calls) == 2


def test_a_hung_build_is_an_error_and_other_placements_still_count(tmp_path, project):
    project = dataclasses.replace(project, options=("-place_option 1",))

    class Hangs(PerVariantGwSh):
        def __call__(self, args, *, cwd, timeout=None, **kwargs):
            if Path(cwd).name == "p1":                       # 配置 1 は終わらない
                raise subprocess.TimeoutExpired(args, timeout)
            return super().__call__(args, cwd=cwd, **kwargs)

    gw_sh = Hangs({f"p{n}": REPORT_OK for n in range(5)})
    result = GowinBuilder(GowinToolchain(tmp_path / "gw"), run=gw_sh, timeout_s=1).build_best(project, tmp_path / "b")
    assert result.bitstream.parent.name in ("p0", "p2")


UNDRIVEN = (r"""WARN  (EX1998) : Net 'u_done[0]' does not have a driver("C:\x\rtl\dap_processor.vhd":85)""" + "\n"
            r"""WARN  (EX1998) : Net 'u_get_req[0]' does not have a driver("C:\x\rtl\dap_processor.vhd":85)""" + "\n")


def test_a_net_without_a_driver_fails_the_build(tmp_path, project):
    # 駆動されない配線は実機で値が定まらず（配置で変わる）、シミュレーションでは初期値で隠れる（2026-10-17 の
    # Ethernet 版の DAP の化けの原因）。警告で済ませずビルドを失敗にし、次も使い回さない
    with pytest.raises(BuildError, match=r"u_done\[0\].*dap_processor.vhd:85") as e:
        build(tmp_path, FakeGwSh(stdout="ok\n" + UNDRIVEN), project)
    assert "u_get_req[0]" in str(e.value)
    assert not (tmp_path / "b" / "top.fs").exists()
    gw_sh = FakeGwSh()
    build(tmp_path, gw_sh, project)
    assert len(gw_sh.calls) == 1                                    # 失敗したビルドは使い回さない


@pytest.fixture
def stamped(tmp_path, project):
    """版のパッケージ（U49）を持つプロジェクト。"""
    from fprobe_gw.toolchain.project import BuildStamp
    default = tmp_path / "src" / "build_info_pkg.vhd"
    default.write_text("-- no version")

    def render(info):
        return f"-- {info.digest} {info.time} {info.git} {info.dirty}"

    return dataclasses.replace(project, sources=(*project.sources, default),
                               stamp=BuildStamp(source=default, render=render, repo=tmp_path))


def stamped_build(tmp_path, gw_sh, project, *, now=1_790_000_000.5, git=("abc123", True)):
    builder = GowinBuilder(GowinToolchain(tmp_path / "gw"), run=gw_sh, now=lambda: now, git=lambda repo: git)
    return builder.build(project, tmp_path / "b")


def test_build_writes_the_version_package_and_uses_it_instead_of_the_default(tmp_path, stamped):
    stamped_build(tmp_path, FakeGwSh(), stamped)
    digest = (tmp_path / "b" / "inputs.sha256").read_text(encoding="utf-8")
    assert (tmp_path / "b" / "build_info_pkg.vhd").read_text(encoding="utf-8") == \
        f"-- {digest} 1790000000 abc123 True"
    tcl = (tmp_path / "b" / "top.tcl").read_text(encoding="utf-8")
    assert (tmp_path / "b" / "build_info_pkg.vhd").resolve().as_posix() in tcl
    assert (tmp_path / "src" / "build_info_pkg.vhd").resolve().as_posix() not in tcl


def test_version_does_not_force_a_rebuild_and_survives_reuse(tmp_path, stamped):
    gw_sh = FakeGwSh()
    stamped_build(tmp_path, gw_sh, stamped)
    first = (tmp_path / "b" / "build_info_pkg.vhd").read_text(encoding="utf-8")
    stamped_build(tmp_path, gw_sh, stamped, now=1_800_000_000, git=("def456", False))
    assert len(gw_sh.calls) == 1                                    # 時刻・git が変わっても入力が同じなら作り直さない
    assert (tmp_path / "b" / "build_info_pkg.vhd").read_text(encoding="utf-8") == first   # 版はそのビットストリームのもの
    stamped.stamp.source.write_text("-- edited default")             # 既定のパッケージは入力に含めない
    stamped_build(tmp_path, gw_sh, stamped)
    assert len(gw_sh.calls) == 1
    (tmp_path / "b" / "build_info_pkg.vhd").unlink()                 # 版が無ければ作り直す
    stamped_build(tmp_path, gw_sh, stamped)
    assert len(gw_sh.calls) == 2


def test_stamp_must_be_one_of_the_sources(tmp_path, project):
    from fprobe_gw.toolchain.project import BuildStamp
    with pytest.raises(ValueError, match="sources"):
        dataclasses.replace(project, stamp=BuildStamp(source=tmp_path / "x.vhd", render=str, repo=tmp_path))


def test_git_state_of_a_directory_outside_git(tmp_path):
    from fprobe_gw.toolchain.build import git_state
    assert git_state(tmp_path) == (None, False)


def test_fprobe_designs_stamp_their_version():
    from fprobe_gw import build_info
    from fprobe_gw.designs import DESIGNS
    for name in ("fprobe", "fprobe_jtag", "fprobe_serial", "fprobe_eth", "memtest"):
        assert DESIGNS[name].project().stamp.source == build_info.DEFAULT_PACKAGE, name
    assert DESIGNS["bringup"].project().stamp is None
