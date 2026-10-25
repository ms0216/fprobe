"""「条件で MCU を止める」経路の不具合の再現試験（台帳: docs/reports/bugs/rtl-xtrig.md）。
各試験は「あるべき動作」を tests/tb/rtl_bugs_xtrig_tb.py に書いてあり、修正前の RTL では失敗した。直ったので普通の試験。"""

import pytest

from fprobe_gw.toolchain.sim import run_cocotb
from tests.test_dap_processor import SOURCES

TB = "tests.tb.rtl_bugs_xtrig_tb"


def run(name, tmp_path):
    run_cocotb(toplevel="dap_harness", sources=SOURCES, test_module=TB, build_dir=tmp_path, test_filter=rf".{name}$")


def test_bug_inj_1(tmp_path):
    run("bug_inj_1_fault_leaves_csw_and_tar_modified", tmp_path)


def test_bug_inj_2(tmp_path):
    run("bug_inj_2_select_restore_is_not_retried_after_a_failure", tmp_path)


def test_bug_inj_3(tmp_path):
    run("bug_inj_3_injection_into_another_ap_writes_the_hosts_csw_tar", tmp_path)


def test_bug_smp_1(tmp_path):
    run("bug_smp_1_sampler_on_another_ap_writes_the_hosts_csw_tar", tmp_path)


def test_injection_during_sampling_and_host_traffic(tmp_path):
    run("injection_during_sampling_and_host_traffic_keeps_host_state", tmp_path)


def test_bug_mon_1(tmp_path):
    run("bug_mon_1_shadow_survives_a_disconnect_and_reconnect", tmp_path)


def test_injection_fuzz_with_waits_and_many_slots(tmp_path):
    run("injection_fuzz_with_waits_and_many_slots", tmp_path)
