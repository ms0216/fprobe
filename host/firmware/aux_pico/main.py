# fprobe の補助マイコン（Raspberry Pi Pico、MicroPython）。
# 仕様: docs/superpowers/specs/2026-10-17-m11-aux-pico-design.md
#
# ADC0（GP26）= ターゲット電圧（ch 0）と ADC3（GP29）= Pico 自身の VSYS（基板上で 1/3 に分圧、ch 1。経路の自己確認用）を
# AVERAGE 回平均して mV に換算し、PERIOD_US ごとに UART0（GP0 = TX、1 Mbaud）で
# FPGA（Primer の J14 の 5 番）へ送る。1 件 = 3 バイト: 1 | ch(7)、0 | mV[13:7]、0 | mV[6:0]。
# 書き込み: python -m fprobe.aux_flash（Pico の USB シリアル番号を指定）。電源投入で自動的に動く（main.py）。
from machine import ADC, UART, Pin
import sys
import time

# Pico W（WH）では基板の LED と GP29（VSYS/3）が無線チップの線と共用なので使わない（ch 1 は送らない）
PICO_W = "Pico W" in sys.implementation._machine

BAUD = 1_000_000
PERIOD_US = 1000
AVERAGE = 16
# ADC の基準電圧（Pico の 3V3 = ADC_VREF）。テスタで測った 3V3 に合わせて校正する
# （補助の Pico WH <Pico WH のシリアル> で 3.30 V を確認、2026-10-17）
VREF_MV = 3300
# GP26 の手前の分圧比（ターゲット電圧 / GP26 の電圧）。同じ値の抵抗 2 本の 1/2 分圧なら 2。分圧なしは 1
# （分圧なしでは 3.3V 系のターゲットは ADC の基準と同じで振り切れる。範囲外として送る）
DIVIDER = 1
OVER_RANGE = 0x3FFF                                   # 範囲外の印（ADC が上限に張り付いた）
FULL_SCALE = 65535 - 64                               # read_u16 がこれ以上なら張り付きとみなす

uart = UART(0, baudrate=BAUD, tx=Pin(0), rx=Pin(1))
adc = ADC(26)
vsys = None if PICO_W else ADC(29)
VSYS_DIVIDER = 3                                      # Pico の基板上の分圧（R5/R6）
led = None if PICO_W else Pin(25, Pin.OUT)


def frame(ch, mv):
    mv = max(0, min(mv, 0x3FFF))
    return bytes((0x80 | ch, (mv >> 7) & 0x7F, mv & 0x7F))


def measure(a, divider):
    total = 0
    top = 0
    for _ in range(AVERAGE):
        v = a.read_u16()                                # 12 ビットを 16 ビットに広げた値
        total += v
        top = max(top, v)
    return OVER_RANGE if top >= FULL_SCALE else total * VREF_MV * divider // (AVERAGE * 65535)


def run():
    next_t = time.ticks_us()
    n = 0
    while True:
        uart.write(frame(0, measure(adc, DIVIDER)))
        if vsys is not None and n % 10 == 0:
            uart.write(frame(1, measure(vsys, VSYS_DIVIDER)))   # 10 ms ごと
        n += 1
        if led is not None and n % 500 == 0:
            led.toggle()                                # 動いている印（0.5 秒ごと）
        next_t = time.ticks_add(next_t, PERIOD_US)
        while time.ticks_diff(next_t, time.ticks_us()) > 0:
            pass


run()
