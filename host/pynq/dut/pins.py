#!/usr/bin/env python3
"""PYNQ-Z2 の RPi ヘッダのピン診断（zynq_dut の回路の dut_pins。語番地 8〜11）。PYNQ の上で root で実行する。

  sudo python3 pins.py          # 端子ごとに 0・1 を出して、読み戻しと、ほかの端子の変化（= 線でつながっている端子）を表にする
  sudo python3 pins.py idle     # 何も出さずに、各端子の値だけ読む（プル・外からの駆動を見る）
  sudo python3 pins.py set 10 1 # 端子 10（ヘッダ 15 番）を出力 1 にして保つ（相手側の読み戻しを見る用）。pins.py release で戻す

端子の添字 i = GPIO(12 + i) = RPi ヘッダ 32・33・8・10・36・11・12・35・38・40・15・16・18・22・37・13 番（FPGA の B20 … V7）。
読み戻しが出した値と違う端子は、外から別の駆動に負けている（短絡・衝突・別の出力）。ほかの端子が一緒に変わるなら、
その 2 本は線（ブレッドボードを含む）でつながっている。"""
import mmap
import os
import struct
import sys
import time

BASE, SIZE = 0x40000000, 4096
OE, OUT, IN, ID = 8, 9, 10, 11
ID_VALUE = 0x50494E01
N = 16
HEADER = (32, 33, 8, 10, 36, 11, 12, 35, 38, 40, 15, 16, 18, 22, 37, 13)
FPGA = ("B20", "W8", "V6", "Y6", "B19", "U7", "C20", "Y8", "A20", "Y9", "U8", "W6", "Y7", "F20", "W9", "V7")


def open_regs():
    fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
    mem = mmap.mmap(fd, SIZE, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=BASE)
    def rd(word): return struct.unpack_from("<I", mem, word * 4)[0]
    def wr(word, value): struct.pack_into("<I", mem, word * 4, value & 0xFFFFFFFF)
    return rd, wr


def bits(value):
    return "".join(str(value >> i & 1) for i in range(N))       # 添字 0 が左


def parse_set(argv):
    """set <添字> <0|1> の引数。添字は 0〜N-1、値は 0 か 1。違えば None（端子に触る前に断る）"""
    try:
        i, v = int(argv[2]), int(argv[3])
    except (IndexError, ValueError):
        return None
    return (i, v) if 0 <= i < N and v in (0, 1) else None


def main(argv):
    if argv[1:2] == ["set"] and parse_set(argv) is None:
        print(f"使い方: pins.py set <添字 0〜{N - 1}> <0|1>")
        return 2
    rd, wr = open_regs()
    if rd(ID) != ID_VALUE:
        print(f"ピン診断の ID が違う（0x{rd(ID):08x}）。zynq_dut の回路（dut_pins 入り）が載っているか確かめる")
        return 1
    if argv[1:2] != ["set"]:
        wr(OE, 0)
    time.sleep(0.01)
    print(f"何も出さない: IN = {bits(rd(IN))}（添字 0〜{N - 1} = ヘッダ {', '.join(map(str, HEADER))} 番）")
    if argv[1:] == ["idle"]:
        return 0
    if argv[1:2] == ["set"]:                       # set <添字> <0|1>: その端子を出力にして値を保つ（release で戻す）。ほかの端子は変えない
        i, v = parse_set(argv)
        wr(OUT, (rd(OUT) & ~(1 << i)) | (v << i))
        wr(OE, rd(OE) | (1 << i))
        print(f"端子 {i}（ヘッダ {HEADER[i]} 番）を {v} に出力した（保持）")
        return 0
    if argv[1:] == ["release"]:
        wr(OE, 0)
        print("全部の端子を解放した")
        return 0
    print("端子  ヘッダ  FPGA  出した値  読み戻し  ほかで変わった端子（= 線でつながっている）")
    try:
        for i in range(N):
            seen = {}
            for v in (0, 1):
                wr(OUT, v << i)
                wr(OE, 1 << i)
                time.sleep(0.01)
                seen[v] = rd(IN)
            wr(OE, 0)
            follows = (seen[0] >> i & 1) == 0 and (seen[1] >> i & 1) == 1
            others = [j for j in range(N) if j != i and (seen[0] ^ seen[1]) >> j & 1]
            names = ", ".join(f"ヘッダ{HEADER[j]}" for j in others) or "なし"
            print(f"{i}    {HEADER[i]:>4}   {FPGA[i]:>4}   0 / 1     {'OK  ' if follows else 'NG  '} ({bits(seen[0])} / {bits(seen[1])})  {names}")
    finally:
        wr(OE, 0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
