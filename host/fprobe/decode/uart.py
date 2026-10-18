"""UART の解読とボーレートの推定。仕様: docs/superpowers/specs/2026-10-17-f7-protocol-decode-design.md"""

import math

from ..la import Transitions
from .core import Annotation, ChannelLevels, Decoding

__all__ = ["STANDARD_BAUDS", "decode_uart", "estimate_baud"]

STANDARD_BAUDS = (300, 600, 1200, 2400, 4800, 9600, 14400, 19200, 28800, 38400, 57600, 76800, 115200, 128000,
                  230400, 250000, 256000, 460800, 500000, 576000, 921600, 1_000_000, 1_152_000, 1_500_000,
                  2_000_000, 2_500_000, 3_000_000, 3_500_000, 4_000_000, 6_000_000, 8_000_000, 10_000_000, 12_000_000)
SNAP = 0.03                     # 標準のボーレートとこの比率以内なら標準値に寄せる
PARITIES = ("none", "even", "odd", "mark", "space")


def estimate_baud(tr: Transitions, channel: int, *, snap: bool = True) -> float | None:
    """同じ値が続いた長さから 1 ビットの長さを推定する。変化が足りなければ None。
    最短の 1 ビットの並びが 1 つも無いデータ（例: 2 ビットずつしか変わらない）では倍の長さを 1 ビットと見る。"""
    edges = ChannelLevels(tr, channel).edges
    runs = sorted(edges[i + 1] - edges[i] for i in range(len(edges) - 1))   # 両端の切れた区間は使わない
    if len(runs) < 2:
        return None
    k = max(2, math.ceil(0.01 * len(runs)))           # これより少ない短い区間は雑音とみなす
    if len(runs) < k:
        return None
    rough = runs[k - 1]
    ones = [r for r in runs if 0.5 * rough <= r <= 1.5 * rough]
    unit = sum(ones) / len(ones)                      # 1 ビットの区間の平均
    total, bits = 0, 0
    for r in runs:                                    # 整数ビットに近い区間だけを、ビット数の合計で割る（精度を上げる）
        n = round(r / unit)                           # 半端な長さ（停止ビット 1.5・フレームの間の休止）は除く
        if 1 <= n <= 10 and abs(r / unit - n) <= 0.2:
            total += r
            bits += n
    baud = bits / (total * tr.sample_period_s)
    if snap:
        nearest = min(STANDARD_BAUDS, key=lambda b: abs(b - baud))
        if abs(nearest - baud) <= SNAP * nearest:
            return nearest
    return baud


def decode_uart(tr: Transitions, channel: int, *, baud: float | None = None, data_bits: int = 8,
                parity: str = "none", stop_bits: float = 1, invert: bool = False, lsb_first: bool = True) -> Decoding:
    """1 本の UART の線を解読する。baud を省くと推定する。"""
    if not 5 <= data_bits <= 9:
        raise ValueError(f"データビットは 5〜9（{data_bits}）")
    if parity not in PARITIES:
        raise ValueError(f"パリティは {PARITIES} のどれか（{parity!r}）")
    if stop_bits not in (1, 1.5, 2):
        raise ValueError(f"停止ビットは 1 / 1.5 / 2（{stop_bits}）")
    if baud is not None and baud <= 0:
        raise ValueError(f"ボーレートが正でない（{baud}）")
    estimated = baud is None
    if estimated:
        baud = estimate_baud(tr, channel)
    params = {"channel": channel, "baud": baud, "baud_estimated": estimated, "data_bits": data_bits,
              "parity": parity, "stop_bits": stop_bits, "invert": invert, "lsb_first": lsb_first}
    anns = [] if baud is None else _frames(tr, ChannelLevels(tr, channel), 1 / (baud * tr.sample_period_s),
                                            data_bits, parity, stop_bits, int(invert), lsb_first)
    return Decoding("uart", tuple(anns), tr.sample_period_s, tr.trigger, tr.length, params)


def _frames(tr, lv: ChannelLevels, p: float, data_bits: int, parity: str, stop_bits: float, invert: int,
            lsb_first: bool) -> list[Annotation]:
    """フレームを順に読む。値の参照は二分探索でなく、前にだけ進む変化点のカーソルで行う（毎フレーム O(ビット数)）。"""
    length, out = tr.length, []
    edges, n, initial = lv.edges, len(lv.edges), lv.initial
    nbits = data_bits + (parity != "none")
    j = 0                                             # カーソル: edges[:j] は今のサンプル以前の変化点
    while True:
        # 開始ビットの始まり: 線が論理 0（線の上では invert）に変わる変化点。edges[j] の後の値は initial ^ (j + 1) の偶奇
        if j < n and initial ^ ((j + 1) & 1) != invert:
            j += 1
        if j >= n:
            break
        s = edges[j]
        base = s - 0.5 + 0.5 * p + 0.5                # 本当の変化は s の直前の半サンプル。+0.5 で floor を丸めに
        bits, truncated = [], False
        for i in range(nbits + 2):                    # 開始・データ・パリティ・最初の停止ビットの中央
            idx = int(base + i * p)
            if idx >= length:
                truncated = True
                break
            while j < n and edges[j] <= idx:
                j += 1
            bits.append(initial ^ (j & 1) ^ invert)
            if i == 0 and bits[0]:                    # 開始ビットの中央が 1: 雑音。次の 0 への変化点を探す
                break
        if bits and bits[0] and not truncated:
            continue
        if truncated and not bits:
            out.append(Annotation(s, length, "data", "?", error="truncated"))
            break
        data = bits[1:data_bits + 1]
        value = 0
        for i, b in enumerate(data):
            value |= b << i if lsb_first else b << (len(data) - 1 - i)
        if truncated:
            if len(data) < data_bits:
                value = None
            out.append(Annotation(s, length, "data", _text(value, data_bits), value=value, error="truncated"))
            break
        par = bits[data_bits + 1] if parity != "none" else None
        stop = bits[nbits + 1]
        end = max(s + 1, min(length, math.ceil(s - 0.5 + (1 + nbits + stop_bits) * p)))
        if not stop:
            # 停止ビットの中央が 0。線が休止に戻ってから次の開始ビットを探す（カーソルの次の変化点が戻り）
            back = edges[j] if j < n else length
            if value == 0 and not par:               # 開始からデータ・パリティ・停止までずっと 0: ブレーク
                out.append(Annotation(s, max(back, s + 1), "break", "BREAK"))
            else:
                out.append(Annotation(s, end, "data", _text(value, data_bits), value=value, error="frame",
                                      fields={"parity": par}))
            continue
        error = None
        if par is not None and par != _parity(value, parity):
            error = "parity"
        out.append(Annotation(s, end, "data", _text(value, data_bits), value=value, error=error,
                              fields={"parity": par}))
    return out


def _parity(value: int, parity: str) -> int:
    ones = bin(value).count("1") & 1
    return {"even": ones, "odd": ones ^ 1, "mark": 1, "space": 0}[parity]


def _text(value: int | None, data_bits: int) -> str:
    if value is None:
        return "?"
    text = f"0x{value:0{(data_bits + 3) // 4}X}"
    if data_bits <= 8 and 0x20 <= value < 0x7F:
        text += f" '{chr(value)}'"
    return text
