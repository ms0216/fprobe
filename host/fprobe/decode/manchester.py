"""Manchester 符号の解読。仕様: docs/superpowers/specs/2026-10-18-u20-decoders-design.md

ビットの中央に必ず変化がある。変化と変化の間が半ビット（短）か 1 ビット（長）かで、中央の変化と境目の変化を見分ける:
長い間の両端は必ず中央。フレーム（1.5 ビットより長く変化しない所で区切る）の最初の長い間から逆にたどって、
最初の変化が中央かどうかを決める（長い間が無いフレームは最初の変化を中央とみなす）。
convention: "ieee"（IEEE 802.3: 中央の立ち上がりが 1）/ "thomas"（G. E. Thomas: 立ち下がりが 1）。
"""

from ..la import Transitions
from .core import Annotation, ChannelLevels, Decoding

__all__ = ["decode_manchester"]

CONVENTIONS = ("ieee", "thomas")


def decode_manchester(tr: Transitions, channel: int, *, bitrate: float, convention: str = "ieee",
                      word_bits: int = 8, msb_first: bool = True) -> Decoding:
    """Manchester を解読する。ビットを word_bits ごとの語にまとめる（フレームの最後の半端は error="incomplete"）。"""
    if bitrate <= 0:
        raise ValueError(f"ビットレートが正でない（{bitrate}）")
    if convention not in CONVENTIONS:
        raise ValueError(f"規約は {CONVENTIONS} のどれか（{convention!r}）")
    if not 1 <= word_bits <= 64:
        raise ValueError(f"語長は 1〜64（{word_bits}）")
    period = 1 / (bitrate * tr.sample_period_s)        # 1 ビットのサンプル数
    lv = ChannelLevels(tr, channel)
    edges = list(lv.edges)
    params = {"channel": channel, "bitrate": bitrate, "convention": convention, "word_bits": word_bits,
              "msb_first": msb_first}
    out: list[Annotation] = []
    # フレームに分ける
    frames: list[list[int]] = []
    for i, e in enumerate(edges):
        if not frames or e - edges[i - 1] > 1.5 * period:
            frames.append([])
        frames[-1].append(i)
    one_rising = convention == "ieee"
    for frame in frames:
        _frame(frame, edges, lv.initial, period, one_rising, word_bits, msb_first, out, tr.length)
    return Decoding("manchester", tuple(out), tr.sample_period_s, tr.trigger, tr.length, params)


def _classify(delta: float, period: float) -> str | None:
    if 0.25 * period < delta < 0.75 * period:
        return "short"
    if 0.75 * period <= delta <= 1.25 * period:
        return "long"
    return None


def _frame(frame: list[int], edges, initial: int, period: float, one_rising: bool, word_bits: int, msb_first: bool,
           out: list[Annotation], length: int) -> None:
    kinds = [_classify(edges[frame[k + 1]] - edges[frame[k]], period) for k in range(len(frame) - 1)]
    first_long = next((k for k, c in enumerate(kinds) if c == "long"), None)
    mid = first_long is None or first_long % 2 == 0      # 最初の変化が中央か
    half = period / 2
    bits: list[tuple[int, int, int]] = []                 # (値, 始まり, 終わり)
    errors: list[Annotation] = []
    for k, i in enumerate(frame):
        e = edges[i]
        if mid:
            rising = initial ^ ((i + 1) & 1)                 # 変化の後の値
            value = int(bool(rising) == one_rising)
            s, t = max(0, round(e - half)), min(length, round(e + half))
            bits.append((value, s, max(t, s + 1)))
        if k == len(frame) - 1:
            break
        c = kinds[k]
        if c is None or (not mid and c == "long"):
            errors.append(Annotation(e, max(edges[frame[k + 1]], e + 1), "error", "timing", error="timing"))
            mid = True                                       # 次の変化を中央として合わせ直す
        else:
            mid = c == "long" if mid else True
    for value, s, t in bits:
        out.append(Annotation(s, t, "bit", str(value), value=value))
    out.extend(errors)
    for w in range(0, len(bits), word_bits):
        chunk = bits[w:w + word_bits]
        value = 0
        for k, (b, _, _) in enumerate(chunk):
            value |= b << (len(chunk) - 1 - k) if msb_first else b << k
        out.append(Annotation(chunk[0][1], chunk[-1][2], "word", f"0x{value:0{(len(chunk) + 3) // 4}X}", value=value,
                              error=None if len(chunk) == word_bits else "incomplete",
                              fields={"bits": len(chunk)}))
    if bits:
        out.append(Annotation(bits[0][1], bits[-1][2], "frame", "".join(str(b) for b, _, _ in bits),
                              value=len(bits), error="timing" if errors else None, fields={"bits": len(bits)}))
