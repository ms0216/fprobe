"""ビルドの報告から階層別の資源を読む（モジュール化の段階 0。機器は使わない）。"""

from pathlib import Path

from fprobe_gw.toolchain.resources import ModuleUse, read_gowin_hierarchy, read_vivado_hierarchy, table

DATA = Path(__file__).parent / "data" / "resources"


def test_gowin_hierarchy_sums_to_the_top():
    mods = {m.path: m for m in read_gowin_hierarchy(DATA / "gowin_rsc.xml")}
    assert mods["top"] == ModuleUse("top", 0, lut=530, ff=230, alu=45, bsram=3)
    assert mods["top/core_i/la_i"] == ModuleUse("top/core_i/la_i", 2, lut=300, ff=100, alu=20, bsram=3)
    assert mods["top/pll_i"].lut == 0
    core = mods["top/core_i"]
    assert core.lut == 200 + mods["top/core_i/la_i"].lut                      # 総計 = 自分の分 + 子の総計


def test_vivado_hierarchy_table():
    mods = {m.path: m for m in read_vivado_hierarchy(DATA / "vivado_util_hier.rpt")}
    assert mods["top/core_i/la_i"] == ModuleUse("top/core_i/la_i", 2, lut=300, ff=400, alu=0, bsram=2)
    assert mods["top"].bsram == 5 and "top/(top)" not in mods                 # RAMB36 + RAMB18、自分の分の行は数えない


def test_table_limits_depth_and_shows_shares():
    mods = read_gowin_hierarchy(DATA / "gowin_rsc.xml")
    text = table(mods, depth=1)
    assert "core_i" in text and "la_i" not in text and "94.3%" in text           # 500 / 530


def test_cli_prints_the_table_and_the_fmax_of_each_variant(tmp_path, capsys):
    import shutil
    from fprobe_gw.cli import main
    syn = tmp_path / "p0" / "impl" / "gwsynthesis"
    syn.mkdir(parents=True)
    shutil.copy(DATA / "gowin_rsc.xml", syn / "project_syn_rsc.xml")
    (tmp_path / "p1" / "impl" / "gwsynthesis").mkdir(parents=True)
    assert main(["resources", "fprobe", "--build-dir", str(tmp_path), "--depth", "1"]) == 0
    out = capsys.readouterr().out
    assert "core_i" in out and "la_i" not in out
    assert "p0: Fmax の報告なし" in out and "p1: Fmax の報告なし" in out        # 報告が無い版は無いと書く
