"""補助マイコン（Pico）の ADC を、ESP32（初代、8 ビット DAC）で作った既知の電圧で確かめる。

配線: ESP32 の GPIO25（DAC1）→ Pico の GP26、ESP32 の GND → Pico の AGND。Pico の USB を先につなぐ（逆流の防止）。
ESP32 には MicroPython を入れておく。COM ポートは USB シリアル番号（環境変数 FPROBE_ESP32_SERIAL）で選ぶ
（他の機器のポートは開かない）。未設定なら飛ばす。
ESP32 の DAC の実際の出力は約 0.06〜2.5 V（公称の VDD × 値 / 256 より利得が小さい。2026-10-17 の実測で傾き 9.5 mV / 値、
値 128 でテスタ 1.26 V と fprobe 1.271 V）なので、比べるのは単調性・直線性と、傾きが DAC として妥当な範囲にあること。
ボードは USB 版（fprobe）で開く。環境変数 FPROBE_ETH_HOST があれば Ethernet 版（fprobe_eth）で開く。
"""

import os
import subprocess
import sys
import time

import pytest
from serial.tools import list_ports

from fprobe.aux_mcu import Aux
from fprobe.session import Session
from tests.hil.program import SERIAL, program

ESP32_SERIAL = os.environ.get("FPROBE_ESP32_SERIAL")
pytestmark = [pytest.mark.hil,
              pytest.mark.skipif(not ESP32_SERIAL, reason="FPROBE_ESP32_SERIAL（ESP32 の USB シリアル番号）が未設定")]


def esp32_port() -> str:
    ports = [p.device for p in list_ports.comports() if p.serial_number == ESP32_SERIAL]
    assert len(ports) == 1, ports
    return ports[0]


ETH_HOST = os.environ.get("FPROBE_ETH_HOST")


def hold_dac(port: str, value: int, seconds: float) -> subprocess.Popen:
    """ESP32 をリセットしてから DAC を value にし、seconds の間保つ（MicroPython の exec が終わると DAC も止まるため）。"""
    subprocess.run([sys.executable, "-m", "mpremote", "connect", port, "reset"], check=True, capture_output=True)
    time.sleep(1.5)
    return subprocess.Popen([sys.executable, "-m", "mpremote", "connect", port, "exec",
                             f"import time; from machine import DAC, Pin; d = DAC(Pin(25)); d.write({value}); "
                             f"time.sleep({seconds}); d.write(0)"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.fixture(scope="module")
def session():
    if ETH_HOST:
        subprocess.run([sys.executable, "-m", "fprobe_gw", "build", "fprobe_eth", "--program"], check=True)
        with Session.open_eth(ETH_HOST) as s:
            yield s
    else:
        program("fprobe")
        with Session.open(SERIAL) as s:
            yield s


def test_pico_adc_follows_the_esp32_dac(session):
    port = esp32_port()
    codes = [0, 32, 64, 96, 128, 160, 192, 224, 255]
    readings = []
    aux = Aux(session.device)
    for code in codes:
        dac = hold_dac(port, code, 4)
        time.sleep(2.5)                                   # mpremote の接続と DAC の設定を待つ
        r = aux.read()
        dac.wait()
        assert r.live and not r.over_range, (code, r)
        readings.append(r.target_millivolts)
    print("DAC → mV:", list(zip(codes, readings)))
    assert all(b > a for a, b in zip(readings, readings[1:])), readings          # 単調に増える
    n = len(codes)
    mx, my = sum(codes) / n, sum(readings) / n
    slope = sum((x - mx) * (y - my) for x, y in zip(codes, readings)) / sum((x - mx) ** 2 for x in codes)
    residual = max(abs(y - (my + slope * (x - mx))) for x, y in zip(codes, readings))
    assert 7.0 <= slope <= 13.5, slope                    # ESP32 の DAC の利得の幅（実測 9.5、公称 12.9）
    assert residual < 30, residual                        # 実測 20 mV 未満
