"""解析の道具（U13・U19・U24）の実機試験: Primer 20K + Nucleo F401RE。

- la.measure: SWD の通信（SWCLK 1 MHz）を取り、SWCLK の周波数・デューティと、SWCLK の立ち上がりに対する
  SWDIO のセットアップ / ホールド（プローブは立ち下がりで変える = 約半周期）を測る。
- 記録の SQL 問い合わせと Perfetto への書き出し: 記録中に DAP を動かし、events を SQL で数え、Chrome JSON に書く。
"""

import json
import time

import pytest

from fprobe.mcp_server import Probe
from fprobe_gw.boards.primer20k import PRIMER20K
from tests.hil.program import SERIAL, program

pytestmark = pytest.mark.hil
PINS = PRIMER20K.debug_connector
PROBE_DRIVES_SWDIO = 8
SWD_HZ = 1_000_000


@pytest.fixture(scope="module")
def probe(tmp_path_factory):
    program("fprobe")
    import os
    os.environ["FPROBE_HOME"] = str(tmp_path_factory.mktemp("home"))
    p = Probe(SERIAL)
    p.target_connect("stm32f401xe", frequency_hz=SWD_HZ)
    yield p
    if p._recorder is not None:
        p.recording_stop()
    p.close()


def test_la_measure_swclk_frequency_and_swdio_setup_hold(probe):
    probe.la_arm(rate_hz=50e6, pretrigger=1024, fall=1 << PROBE_DRIVES_SWDIO)
    probe.read_memory(0xE000ED00, 4)
    probe.la_collect(timeout_s=3)
    clk, dio = PINS["swclk"], PINS["swdio"]
    out = probe.la_measure(channels=[clk], relations=[
        {"type": "setup_hold", "clock": clk, "clock_edge": "rise", "data": dio, "setup_min_ns": 100}])
    ch = out["channels"][clk]
    # SWCLK はトランザクションの間だけ動く: 周期の最小値（連続したクロック）が 1 MHz の周期（±2 サンプル）
    assert ch["period"]["count"] >= 40, ch
    assert abs(ch["period"]["min_ns"] - 1e9 / SWD_HZ) <= 2 * out["resolution_ns"], ch["period"]
    assert 0.35 < ch["duty_min"] <= ch["duty_max"] < 0.65, ch
    rel = out["relations"][0]
    # プローブは SWCLK の立ち下がりで SWDIO を変える: 立ち上がりの前の約半周期（ターゲットが返す区間は別）
    assert rel["setup"]["count"] > 0 and rel["setup"]["max_ns"] >= 400, rel["setup"]
    print("setup/hold:", rel["setup"], rel["hold"], "violations:", rel["violation_count"])


def test_recording_query_and_perfetto_export(probe, tmp_path):
    probe.recording_start(name="analysis")
    time.sleep(0.3)                                               # 時計の対応付けの点
    probe.la_arm(rate_hz=25e6, pretrigger=512, fall=1 << PROBE_DRIVES_SWDIO)
    for _ in range(10):
        probe.read_memory(0xE000ED00, 4)
    probe.la_collect(timeout_s=3)
    probe.recording_mark("十回読んだ")
    time.sleep(0.5)                                               # イベントの取り込み
    counted = probe.recording_query(sql="SELECT kind, COUNT(*) FROM events WHERE stream = 'dap' GROUP BY kind")
    kinds = dict(map(tuple, counted["rows"]))
    assert kinds.get("ap_read", 0) >= 10, kinds
    assert probe.recording_query(sql="SELECT COUNT(*) FROM chunks")["rows"][0][0] >= 1
    with pytest.raises(ValueError):
        probe.recording_query(sql="DELETE FROM events")
    path = tmp_path / "trace.json"
    out = probe.recording_export(format="perfetto", path=str(path))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert out["instants"] >= 10 and "DAP" in out["tracks"] and "ロジアナ" in out["tracks"], out
    assert any(e["ph"] == "i" and e["name"] == "十回読んだ" for e in data["traceEvents"])
    measured = probe.recording_waveform_measure(channels=[PINS["swclk"]])
    assert measured["channels"][PINS["swclk"]]["rises"] > 0
    probe.recording_stop()
    print("Perfetto のトレース:", path, "（ui.perfetto.dev で開いて、DAP の点とロジアナのカウンタが重なることを目で確かめる）")
