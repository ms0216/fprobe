"""UART の解読。仕様: docs/superpowers/specs/2026-10-17-f7-protocol-decode-design.md

ボーレートと枠の設定（データ・パリティ・ストップのビット、極性、ビットの順）は使う側が渡す（既定を持たない。CLAUDE.md の
「境界の値は使う側が明示」）。ボーレートは波形から推定しない（雑音とみなす割合・1 ビットとみなす幅・標準値に寄せる等の
経験則が要るため。CLAUDE.md の「本体にヒューリスティックを入れない」。1 ビットの長さは la.measure 等でパルスの幅を測れる）。"""

import math

from ..la import Transitions
from .core import Annotation, Channel, ChannelLevels, Decoding

__all__ = ["decode_uart"]

PARITIES = ("none", "even", "odd", "mark", "space")


def decode_uart(tr: Transitions, channel: Channel, *, baud: float, data_bits: int, parity: str, stop_bits: float,
                invert: bool, lsb_first: bool) -> Decoding:
    """1 本の UART の線を、渡したボーレートと枠の設定で解読する。枠の設定（data_bits・parity・stop_bits・invert
    （線の極性）・lsb_first）は既定を持たない（典型の 8N1 で補わない。違えば parity・frame の誤りか化けた値になる）。"""
    if not 5 <= data_bits <= 9:
        raise ValueError(f"データビットは 5〜9（{data_bits}）")
    if parity not in PARITIES:
        raise ValueError(f"パリティは {PARITIES} のどれか（{parity!r}）")
    if stop_bits not in (1, 1.5, 2):
        raise ValueError(f"停止ビットは 1 / 1.5 / 2（{stop_bits}）")
    if baud <= 0:
        raise ValueError(f"ボーレートが正でない（{baud}）")
    params = {"channel": channel, "baud": baud, "data_bits": data_bits,
              "parity": parity, "stop_bits": stop_bits, "invert": invert, "lsb_first": lsb_first}
    anns = _frames(tr, ChannelLevels(tr, channel), 1 / (baud * tr.sample_period_s),
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
