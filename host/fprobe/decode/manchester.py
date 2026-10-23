"""Manchester 符号の解読。仕様: docs/superpowers/specs/2026-10-18-u20-decoders-design.md

ビットの中央に必ず変化がある。変化と変化の間が半ビット（短）か 1 ビット（長）かで、中央の変化と境目の変化を見分ける:
長い間の両端は必ず中央。フレーム（1 ビット + tolerance より長く変化しない所で区切る）の最初の長い間から逆にたどって、
最初の変化が中央かどうかを決める。長い間が無いフレーム（同じビットだけ）は決められないので解読せず
error="phase_unknown" にする（中央とみなさない）。tolerance（1 ビットに対する許容のずれ）は使う側が明示する。
フレームには名目の長さからの最大のずれ（max_deviation、ビット単位）を事実として付ける。
convention: "ieee"（IEEE 802.3: 中央の立ち上がりが 1）/ "thomas"（G. E. Thomas: 立ち下がりが 1）。
"""

from ..la import Transitions
from .core import Annotation, ChannelLevels, Decoding

__all__ = ["decode_manchester"]

CONVENTIONS = ("ieee", "thomas")


def decode_manchester(tr: Transitions, channel: int, *, bitrate: float, tolerance: float | None = None,
                      convention: str = "ieee", word_bits: int = 8, msb_first: bool = True) -> Decoding:
    """Manchester を解読する。ビットを word_bits ごとの語にまとめる（フレームの最後の半端は error="incomplete"）。"""
    if bitrate <= 0:
        raise ValueError(f"ビットレートが正でない（{bitrate}）")
    if tolerance is None:
        raise ValueError("tolerance（1 ビットに対する許容のずれ。0 より大きく 0.25 以下）を渡す（既定の値は持たない）")
    if not 0 < tolerance <= 0.25:
        raise ValueError(f"tolerance は 0 より大きく 0.25 以下（短と長の窓が重ならない。{tolerance}）")
    if convention not in CONVENTIONS:
        raise ValueError(f"規約は {CONVENTIONS} のどれか（{convention!r}）")
    if not 1 <= word_bits <= 64:
        raise ValueError(f"語長は 1〜64（{word_bits}）")
    period = 1 / (bitrate * tr.sample_period_s)        # 1 ビットのサンプル数
    lv = ChannelLevels(tr, channel)
    edges = list(lv.edges)
    params = {"channel": channel, "bitrate": bitrate, "tolerance": tolerance, "convention": convention, "word_bits": word_bits,
              "msb_first": msb_first}
    out: list[Annotation] = []
    # フレームに分ける
    frames: list[list[int]] = []
    for i, e in enumerate(edges):
        if not frames or e - edges[i - 1] > (1 + tolerance) * period:
            frames.append([])
        frames[-1].append(i)
    one_rising = convention == "ieee"
    for frame in frames:
        _frame(frame, edges, lv.initial, period, tolerance, one_rising, word_bits, msb_first, out, tr.length)
    return Decoding("manchester", tuple(out), tr.sample_period_s, tr.trigger, tr.length, params)


def _classify(delta: float, period: float, tolerance: float) -> str | None:
    if abs(delta - period / 2) <= tolerance * period:
        return "short"
    if abs(delta - period) <= tolerance * period:
        return "long"
    return None


def _frame(frame: list[int], edges, initial: int, period: float, tolerance: float, one_rising: bool, word_bits: int,
           msb_first: bool, out: list[Annotation], length: int) -> None:
    deltas = [edges[frame[k + 1]] - edges[frame[k]] for k in range(len(frame) - 1)]
    kinds = [_classify(d, period, tolerance) for d in deltas]
    deviation = max((min(abs(d - period / 2), abs(d - period)) / period for d in deltas), default=0.0)
    first_long = next((k for k, c in enumerate(kinds) if c == "long"), None)
    if first_long is None:                               # 最初の変化が中央か境目か決められない
        e0, e1 = edges[frame[0]], edges[frame[-1]]
        out.append(Annotation(e0, max(e1, e0 + 1), "frame", "phase unknown", value=0, error="phase_unknown",
                              fields={"bits": None, "edges": len(frame), "max_deviation": deviation}))
        return
    mid = first_long % 2 == 0                            # 最初の変化が中央か
    half = period / 2
    bits: list[tuple[int, int, int]] = []                 # (値, 始まり, 終わり)
    breaks: list[int] = []                                # 誤りの位置（bits の添え字）
    errors: list[Annotation] = []
    for k, i in enumerate(frame):                        # mid: True = 中央、False = 境目、None = 分からない（誤りの後）
        e = edges[i]
        if mid:
            rising = initial ^ ((i + 1) & 1)                 # 変化の後の値
            value = int(bool(rising) == one_rising)
            s, t = max(0, round(e - half)), min(length, round(e + half))
            bits.append((value, s, max(t, s + 1)))
        if k == len(frame) - 1:
            break
        c = kinds[k]
        if mid is None:                                      # 長い間（両端が中央）が来るまで位相を決めない
            mid = True if c == "long" else None
        elif c is None or (not mid and c == "long"):
            errors.append(Annotation(e, max(edges[frame[k + 1]], e + 1), "error", "timing", error="timing"))
            mid = True if c == "long" else None              # 次の変化を中央と仮定しない
            breaks.append(len(bits))                         # 語は誤りをまたいでつながない
        else:
            mid = c == "long" if mid else True
    for value, s, t in bits:
        out.append(Annotation(s, t, "bit", str(value), value=value))
    out.extend(errors)
    starts = sorted({0, *breaks} - {len(bits)})
    chunks = [bits[a:b][w:w + word_bits] for a, b in zip(starts, starts[1:] + [len(bits)])
              for w in range(0, b - a, word_bits)]
    for chunk in chunks:
        value = 0
        for k, (b, _, _) in enumerate(chunk):
            value |= b << (len(chunk) - 1 - k) if msb_first else b << k
        out.append(Annotation(chunk[0][1], chunk[-1][2], "word", f"0x{value:0{(len(chunk) + 3) // 4}X}", value=value,
                              error=None if len(chunk) == word_bits else "incomplete",
                              fields={"bits": len(chunk)}))
    if bits:
        out.append(Annotation(bits[0][1], bits[-1][2], "frame", "".join(str(b) for b, _, _ in bits),
                              value=len(bits), error="timing" if errors else None,
                              fields={"bits": len(bits), "max_deviation": deviation}))
