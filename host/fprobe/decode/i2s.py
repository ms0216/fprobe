"""I2S（と左詰め・右詰め）の解読。仕様: docs/superpowers/specs/2026-10-18-u20-decoders-design.md

SCK の立ち上がりで WS と SD（直前の値）を読む。WS が変わると次のスロット（チャンネル）が始まる:
- "i2s": WS が変わった次のクロックから MSB（1 クロック遅れ）。WS = 0 が左。
- "left_justified": WS が変わったクロックから MSB。WS = 1 が左。
- "right_justified": スロットの最後の word_bits ビットが値（LSB がスロットの最後）。WS = 1 が左。
捕捉の始まりの途中のスロット（始まりが見えない）は読まない。捕捉の終わりで WS が変わっていない最後のスロットは
終わりが見えていないので error = "truncated"（ビット数で完全かを推し量らない。読めたビット数は fields.bits）。
"""

from ..la import Transitions
from .core import Annotation, Channel, Decoding, project

__all__ = ["FORMATS", "decode_i2s"]

FORMATS = {"i2s": (1, 0), "left_justified": (0, 1), "right_justified": (0, 1)}     # (遅れ, 左の WS)


def decode_i2s(tr: Transitions, *, sck: Channel, ws: Channel, sd: Channel, format: str, word_bits: int | None,
               signed: bool) -> Decoding:
    """I2S を解読する。設定はすべて既定を持たない。word_bits = None はスロットの全ビットを値とする
    （right_justified では数が要る）。"""
    if format not in FORMATS:
        raise ValueError(f"形式は {sorted(FORMATS)} のどれか（{format!r}）")
    if word_bits is not None and not 1 <= word_bits <= 64:
        raise ValueError(f"語長は 1〜64（{word_bits}）")
    if format == "right_justified" and word_bits is None:
        raise ValueError("right_justified には word_bits が要る")
    delay, left_ws = FORMATS[format]
    starts, values = project(tr, [sck, ws, sd])
    params = {"sck": sck, "ws": ws, "sd": sd, "format": format, "word_bits": word_bits, "signed": signed}
    out: list[Annotation] = []
    history: list[int] = []                           # 直前の立ち上がりで読んだ WS（遅れの分だけ）
    slot_ws = None
    slot_start = 0
    bits: list[int] = []
    bits_started = False                              # 今のスロットの始まりが見えたか（最初のスロットは途中から）

    def emit(end: int, truncated: bool) -> None:
        n = len(bits)
        take = word_bits or n
        chosen = bits[-take:] if format == "right_justified" else bits[:take]
        raw = 0
        for b in chosen:
            raw = raw << 1 | b
        width = len(chosen)
        value = raw - (1 << width) if signed and width and raw >> (width - 1) else raw
        channel = "left" if slot_ws == left_ws else "right"
        error = "truncated" if truncated else ("short" if word_bits and n < word_bits else None)
        out.append(Annotation(slot_start, max(end, slot_start + 1), channel, f"{channel[0].upper()} {value}",
                              value=value, error=error, fields={"raw": raw, "bits": n}))

    prev = values[0] if len(values) else 0
    for i in range(1, len(values)):
        now, at = values[i], starts[i]
        if (prev ^ now) & 1 and now & 1:                # SCK の立ち上がり
            w, d = prev >> 1 & 1, prev >> 2 & 1
            history.append(w)
            if len(history) > delay + 1:
                history.pop(0)
            if len(history) == delay + 1:
                current = history[0]                    # このビットが属するスロットの WS
                if current != slot_ws:
                    if slot_ws is not None and bits_started:
                        emit(at, False)
                    bits_started = slot_ws is not None
                    slot_ws, slot_start, bits = current, at, []
                bits.append(d)
        prev = now
    if slot_ws is not None and bits and bits_started:
        emit(tr.length, True)                         # 終わり（WS の変化）が見えていない
    return Decoding("i2s", tuple(out), tr.sample_period_s, tr.trigger, tr.length, params)
