# Shrike-Lite のデモの MCU 側（RP2040、MicroPython）: FPGA の演算器を SPI で使い、結果を検証し続ける。
#
#   起動: FPGA にデモの回路（fprobe_demo.bin）を書く → SPI0（GPIO0 = MISO、1 = CS、2 = SCK、3 = MOSI。1 MHz、モード 0）
#   ループ: A・B（8 ビット、乱数）を書き込んで開始 → STATUS を最大 POLLS 回ポーリング → 完了なら RESULT を読んで A × B と
#           比較。完了しなければリトライ（待ち回数は倍に。FPGA の演算器はこのとき途中の和をクリアしない = 仕込んだ不具合）。
#   マーカー: GPIO26（fprobe のロジアナ。PYNQ の PMOD JB の 1 番）= リクエスト中 1。リトライのたびに一度 0 に戻す。
#   メールボックス: 0x20040800 から 8 ワード（fprobe が SWD で非停止で読む。コア 1 のスタック領域で、MicroPython は
#     _thread を使わなければ触らない）:
#       0 マジック 0x5D0B0001、1 requests、2 errors、3 retries、4 最後の A、5 最後の B、6 最後の結果、7 最後の期待値
import time

import machine
import shrike
from machine import SPI, Pin

BITSTREAM = "fprobe_demo.bin"
POLLS = 8                     # 完了待ちのポーリング回数（MicroPython で約 0.6 ms。B が大きいと足りない）
MAILBOX = 0x20040800
MAGIC = 0x5D0B0001

ID, CTRL, A, B, RESULT, STATUS = 0x00, 0x01, 0x02, 0x03, 0x04, 0x06

shrike.reset()
shrike.flash(BITSTREAM)
cs = Pin(1, Pin.OUT, value=1)
spi = SPI(0, baudrate=1_000_000, polarity=0, phase=0, bits=8, sck=Pin(2), mosi=Pin(3), miso=Pin(0))
mark = Pin(26, Pin.OUT, value=0)
buf = bytearray(3)


def write(addr, value):
    cs(0)
    spi.write(bytes((addr, value >> 8 & 0xFF, value & 0xFF)))
    cs(1)


def read(addr):
    cs(0)
    spi.write_readinto(bytes((0x80 | addr, 0, 0)), buf)
    cs(1)
    return buf[1] << 8 | buf[2]


def box(k, v):
    machine.mem32[MAILBOX + 4 * k] = v & 0xFFFFFFFF


for k in range(8):
    box(k, 0)
box(0, MAGIC)
requests = errors = retries = 0
fpga_id = read(ID)
print("fpga id", hex(fpga_id))
seed = time.ticks_us()


def rand8():
    global seed
    seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
    return seed >> 16 & 0xFF


while True:
    a, b = rand8(), rand8()
    mark(1)
    write(A, a)
    write(B, b)
    write(CTRL, 1)
    polls = POLLS
    while True:
        for _ in range(polls):
            if read(STATUS) & 2:
                break
        else:
            retries += 1                      # タイムアウトしたのでリトライ（次は倍まで待つ）
            box(3, retries)
            polls *= 2
            mark(0)
            mark(1)
            write(CTRL, 1)
            continue
        break
    result = read(RESULT)
    mark(0)
    requests += 1
    if result != a * b:
        errors += 1
        print("NG", requests, "a", a, "b", b, "result", result, "expected", a * b)
    box(1, requests)
    box(2, errors)
    box(4, a)
    box(5, b)
    box(6, result)
    box(7, a * b)
    time.sleep_ms(5)
