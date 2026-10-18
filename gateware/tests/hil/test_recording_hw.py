"""F3a 共通の時間軸の記録（実機）: ホストの操作・DAP の転送・ロジアナの波形が、デバイスのタイムベースの上で正しい順に並ぶ。"""

import time

import pytest

from fprobe.mcp_server import Probe
from tests.hil.program import SERIAL, program

pytestmark = pytest.mark.hil
DAP_BUSY = 9                                                     # ロジアナの ch9 = DAP の処理中


@pytest.fixture(scope="module")
def probe(tmp_path_factory):
    program("fprobe")
    import os
    os.environ["FPROBE_HOME"] = str(tmp_path_factory.mktemp("home"))
    p = Probe(SERIAL)
    p.target_connect("stm32f401xe", frequency_hz=4_000_000)
    yield p
    if p._recorder is not None:
        p.recording_stop()
    p.close()


def test_host_operation_dap_transfers_and_waveform_are_in_order_on_one_timebase(probe):
    probe.recording_start(name="hil")
    time.sleep(0.3)                                              # 時計の対応付けの点を取る
    probe.la_arm(rate_hz=25e6, pretrigger=512, rise=1 << DAP_BUSY)
    probe.record_host_event("host", "call", {"client": "test", "method": "target.registers.read"})
    probe.read_registers(["pc", "sp"])
    probe.la_collect(timeout_s=3)
    probe.recording_mark("レジスタを読んだ")
    time.sleep(0.5)                                              # イベントの取り込み
    status = probe.recording_status()
    stopped = probe.recording_stop()
    assert stopped["events"] > 0 and status["clock_uncertainty_ticks"] is not None

    hz = status["timebase_hz"]
    note = probe.recording_find({"stream": "host", "body": {"method": "target.registers.read"}})["events"][0]
    dap = [e for e in probe.recording_events(streams=["dap"], limit=10000)["events"] if e["t"] >= note["t"] - note["uncertainty"]]
    assert dap, "ホストの操作の後に DAP の転送が記録されていない"
    first = dap[0]["t"]
    # ホストの操作 → DAP の転送 の順（ホストの時刻の不確かさの中で）。USB の往復ぶん、DAP は少し後
    assert first >= note["t"] - note["uncertainty"]
    assert first - note["t"] < 0.05 * hz
    # DAP の転送の時刻は、ロジアナの波形の範囲にあり、そこで ch9（DAP 処理中）が動いている
    view = probe.recording_waveform(t0=first - 2000, t1=first + 2000, width=4)["levels"]
    assert len(view) > DAP_BUSY and any(c in "1x" for c in view[DAP_BUSY]), view[DAP_BUSY]
    marks = probe.recording_find({"stream": "annotation", "body": {"text": "レジスタを読んだ"}})["events"]
    assert marks and marks[0]["t"] > first
