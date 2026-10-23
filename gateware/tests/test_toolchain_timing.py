from pathlib import Path

import pytest

from fprobe_gw.toolchain.timing import TimingReportError, parse_timing_report

# Gowin 1.9.11.03 が primer20k bringup（VHDL）のビルドで実際に出力した
# project_tr_content.html の要約部分
REAL = (Path(__file__).parent / "data" / "gowin_timing_summary.html").read_text(encoding="utf-8")

SYS_HOLD_ROW = "<td>sys_clk</td>\n<td>Hold</td>\n<td>0.000</td>\n<td>0</td>\n</tr>"

VIOLATED = REAL.replace("<td>Setup</td>\n<td>0.000</td>\n<td>0</td>",
                        "<td>Setup</td>\n<td>-1.250</td>\n<td>3</td>")


def test_parse_real_report():
    s = parse_timing_report(REAL)
    assert s.met
    clocks = {c.name: c for c in s.clocks}
    assert clocks["sys_clk"].constraint_mhz == pytest.approx(100.281)
    assert clocks["sys_clk"].fmax_mhz == pytest.approx(158.727)
    # 経路の無いクロックは Fmax 表に載らないが、TNS 表から判定対象になる
    assert clocks["clk27"].fmax_mhz is None
    assert clocks["clk27"].setup_tns == 0.0


def test_parse_violated_report():
    assert VIOLATED != REAL
    s = parse_timing_report(VIOLATED)
    assert not s.met
    assert "-1.250" in s.describe()


def test_extra_tns_only_clock_is_checked():
    assert SYS_HOLD_ROW in REAL
    report = REAL.replace(
        SYS_HOLD_ROW,
        SYS_HOLD_ROW + "\n<tr>\n<td>usb_clk</td>\n<td>Setup</td>\n<td>-0.100</td>\n<td>1</td>\n</tr>")
    s = parse_timing_report(report)
    assert "usb_clk" in [c.name for c in s.clocks]
    assert not s.met


def test_unrecognised_report_is_an_error():
    with pytest.raises(TimingReportError):
        parse_timing_report("<html>nothing</html>")


# 経路の表（Setup / Hold Paths Table）。set_max_delay の経路は TNS の要約に現れない（R24、2026-10-22）
PATHS = (Path(__file__).parent / "data" / "gowin_path_slacks.html").read_text(encoding="utf-8")


def test_a_negative_path_slack_fails_even_when_the_tns_summary_is_zero():
    s = parse_timing_report(REAL + PATHS)
    assert all(c.met for c in s.clocks)                       # 要約の TNS はどれも 0
    assert not s.met
    (path,) = s.failing_paths
    assert (path.kind, path.slack, path.start, path.end) == (
        "Setup", -0.312, "device_i/cdc_requests_i/brk_ms_0_s1/Q", "core_i/host_break_sync_i/stage_0_s0/D")
    assert "-0.312" in s.describe() and "host_break_sync_i" in s.describe()


def test_non_negative_path_slacks_pass():
    s = parse_timing_report(REAL + PATHS.replace("<td>-0.312</td>", "<td>0.012</td>"))
    assert s.met and s.failing_paths == ()


def test_reports_without_path_tables_are_judged_by_the_summary():
    assert parse_timing_report(REAL).failing_paths == ()
