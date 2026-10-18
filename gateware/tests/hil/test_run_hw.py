"""CI 実行コマンド `python -m fprobe run`（U07）の実機試験: Nucleo F401RE に RAM で読み込む rtt_demo。
セミホスティングの SYS_EXIT の終了コード、時間切れ（124）とその時のスナップショット、故障（125）と残すもの、JUnit XML。"""

import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from fprobe.mcp_server import Probe
from fprobe.run import EXIT_ABNORMAL, EXIT_TIMEOUT, RunOptions, Runner
from tests.hil.program import SERIAL, program

pytestmark = pytest.mark.hil
RTT_DEMO = Path(__file__).resolve().parents[3] / "host" / "firmware" / "rtt_demo" / "rtt_demo.elf"
TARGET = "stm32f401xe"


@pytest.fixture(scope="module", autouse=True)
def bitstream():
    program("fprobe")


def fprobe_run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "fprobe", "--serial", SERIAL, "run", str(RTT_DEMO),
                           "--target", TARGET, *args], capture_output=True, text=True, encoding="utf-8", timeout=60)


def test_semihosting_exit_code_is_the_exit_status(tmp_path):
    junit = tmp_path / "junit.xml"
    # rtt_demo に main は無い: .bss を 0 にした後の行（VTOR の設定）で書く
    done = fprobe_run("--set", "semihosting_enabled=1", "--set", "semihosting_exit=4", "--set-at", "rtt_demo.c:344",
                      "--timeout", "10",
                      "--junit", str(junit), "--out", str(tmp_path / "out"))
    assert done.returncode == 3, done.stderr                                # 終了コード = semihosting_exit - 1
    result = json.loads((tmp_path / "out" / "result.json").read_text(encoding="utf-8"))
    assert result["outcome"] == "exit" and result["target_exit_code"] == 3
    assert ET.parse(junit).getroot().find("testsuite/testcase/failure") is not None


def test_a_timeout_keeps_the_output_and_a_snapshot(tmp_path):
    done = fprobe_run("--timeout", "1.5", "--out", str(tmp_path / "out"))
    assert done.returncode == EXIT_TIMEOUT, done.stderr
    assert "tick" in done.stdout                                            # RTT のチャンネル 0
    out = tmp_path / "out"
    assert (out / "snapshot.fsnap").is_file() and (out / "snapshot.core").is_file()
    assert json.loads((out / "backtrace.json").read_text(encoding="utf-8"))


def test_a_fault_is_reported_with_its_cause(tmp_path):
    probe = Probe(SERIAL)
    try:
        probe.target_connect(TARGET)
        probe.program_ram(str(RTT_DEMO))
        probe.write_register("xpsr", 0)                                     # Thumb のビットを消す → INVSTATE
        result = Runner(probe, RunOptions(elf=RTT_DEMO, out_dir=tmp_path / "out", connect=False, load="none",
                                          reset=False, timeout_s=5)).run()
    finally:
        probe.close()
    assert result.exit_code == EXIT_ABNORMAL and result.outcome == "fault", result.reason
    assert "INVSTATE" in result.reason
    assert {"fault", "backtrace", "snapshot", "core"} <= set(result.artifacts)
