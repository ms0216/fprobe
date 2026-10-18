"""Primer 20K: クロストリガの動作「出力の端子を動かす」（U41。仕様 2026-10-18-u41-xtrig-pin-output-design.md §8）。

配線（試験の前に。**Primer の USB をつないでから**ジャンパをつなぐ）:
- J12 の pmod1[0]（ch0 = SWCLK）以外のターゲットはつながない（Nucleo 等は外す）。
- ジャンパ A: pmod1[2]（ch2、nRESET。gpio で駆動して条件の入力に使う）→ pmod1[1]（ch1、TDI。条件を見る端子）。
  どちらも 3.3 V の FPGA の端子同士で、逆流の心配は無い（ch1 は SWD の構成では使わない入力）。
- 抵抗: pmod1[4]（ch4、出力の候補 0、プル無し）と GND の間に 10 kΩ（「動いていない = 高インピーダンス」を確かめる）。
ロジアナは深いバッファ（100 MS/s）で出力そのものを見る（ロジアナはいつも端子そのものを見る）。
"""

import time

import pytest

from fprobe import generated as g
from fprobe.la import DeepAnalyzer
from fprobe.mcp_server import Probe
from tests.hil.program import SERIAL, program

pytestmark = pytest.mark.hil
CLOCK_HZ = 100_285_714
COND, OUT, OUT_PULLUP, UART_TX = 1, 4, 5, 7


@pytest.fixture(scope="module")
def probe():
    program("fprobe")
    p = Probe(SERIAL)
    yield p
    for ch in (OUT, OUT_PULLUP, UART_TX):
        p.xtrig_output_release(ch)
    p.csr_write(g.GPIO_ENABLE, [0])
    p.close()


def gpio_drive(probe, ch: int, level: int) -> None:
    probe.csr_write(g.GPIO_DRIVE, [level << ch])
    probe.csr_write(g.GPIO_ENABLE, [1 << ch])


def level(probe, ch: int) -> int:
    return probe.csr_read(g.GPIO_LEVEL, 1)[0] >> ch & 1


def test_capabilities_declare_the_output_pins(probe):
    st = probe.xtrig_output_status()
    assert st["actions"] == 2
    assert [(o["channel"], o["pull_up"]) for o in st["outputs"]] == [(4, False), (5, True), (6, True), (7, False)]


def test_inactive_output_is_high_impedance(probe):
    # プル無しの ch4 は 10 kΩ のプルダウンで Low、プルアップの ch5 は High（申告と一致）
    probe.xtrig_output_set(channel=OUT, mode="hold", sources=["host"])
    assert level(probe, OUT) == 0
    assert level(probe, OUT_PULLUP) == 1
    probe.xtrig_output_enable(OUT)
    probe.xtrig_fire()
    assert level(probe, OUT) == 1                                       # 保持: 上げたまま
    time.sleep(0.1)
    assert level(probe, OUT) == 1
    probe.xtrig_output_disable(OUT)                                     # 無効で戻る（高インピーダンス → Low）
    assert level(probe, OUT) == 0


def test_pulse_width_on_the_analyzer(probe):
    probe.xtrig_output_set(channel=OUT, mode="pulse", pulse_us=1.0, sources=["host"], enable=True)
    la = DeepAnalyzer(probe.session.device)
    la.start(rise=1 << OUT, post_beats=2_000)
    probe.xtrig_fire()
    la.wait(5)
    cap = la.read(before_beats=10)
    ch = cap.channel(OUT)
    high = sum(ch[cap.trigger:cap.trigger + 1000])
    assert high == pytest.approx(round(1e-6 * CLOCK_HZ), abs=1), high      # 刻み 1 周期: 正確


def test_while_follows_the_condition_within_a_few_cycles(probe):
    gpio_drive(probe, 2, 0)
    probe.xtrig_output_set(channel=OUT, mode="while", pattern={"mask": 1 << COND, "value": 1 << COND}, enable=True)
    la = DeepAnalyzer(probe.session.device)
    la.start(rise=1 << COND, post_beats=2_000)
    gpio_drive(probe, 2, 1)                                             # ジャンパで ch1 が上がる
    la.wait(5)
    cap = la.read(before_beats=10)
    rise_out = next(i for i in range(cap.trigger, len(cap.samples)) if cap.channel(OUT)[i])
    delay_ns = (rise_out - cap.trigger) * 1e9 / CLOCK_HZ
    print(f"while: 条件 → 出力 {delay_ns:.0f} ns")
    assert delay_ns <= 60
    gpio_drive(probe, 2, 0)
    time.sleep(0.01)
    assert level(probe, OUT) == 0


def test_claimed_uart_tx_is_not_driven_by_the_uart(probe):
    probe.xtrig_output_set(channel=UART_TX, mode="hold", sources=["host"])      # claim: UART TX の休止 High を止める
    la = DeepAnalyzer(probe.session.device)
    la.start(post_beats=20_000, rle=True)
    probe.uart_write("UUUU")
    la.wait(5)
    tr = la.read_transitions(before_beats=0)
    assert len(tr.edges(UART_TX)) == 0                                   # 送信しても端子は動かない
    probe.xtrig_output_release(UART_TX)
    assert level(probe, UART_TX) == 1                                    # UART に戻り、休止の High


def test_keyboard_plugin_drives_a_row(probe):
    """XIAO nRF52840（ZMK）での確認は手で行う: 列（74HC595 の出力）を ch0〜3 のどれかに、行を ch4 につなぎ、
    KeyMatrix.tap でキーが入ることをホストの USB HID で見る。ここでは ch1（ジャンパ A）を列の代わりにする。"""
    from fprobe.plugins import ProbeCaller
    from fprobe.plugins.keymatrix import KeyMatrix
    gpio_drive(probe, 2, 1)
    with KeyMatrix(ProbeCaller(probe)) as km:
        km.add_key("A", column=COND, row=OUT)
        km.press("A")
        time.sleep(0.01)
        assert level(probe, OUT) == 1
        gpio_drive(probe, 2, 0)                                          # 列が落ちると行も離れる
        time.sleep(0.01)
        assert level(probe, OUT) == 0
        km.release("A")
