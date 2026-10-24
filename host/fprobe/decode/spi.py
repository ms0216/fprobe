"""SPI の解読。仕様: docs/superpowers/specs/2026-10-17-f7-protocol-decode-design.md"""

from ..la import Transitions
from .core import Annotation, Channel, Decoding, project

__all__ = ["decode_spi"]


class _Word:
    """組み立て中の 1 語（MOSI と MISO を同時に）。"""

    def __init__(self, start: int):
        self.start = start
        self.bits = 0
        self.mosi = 0
        self.miso = 0


def decode_spi(tr: Transitions, *, sck: Channel, mosi: Channel | None, miso: Channel | None, cs: Channel | None, mode: int,
               msb_first: bool, word_bits: int, cs_active_low: bool) -> Decoding:
    """SPI を解読する。mode = CPOL × 2 + CPHA。設定はすべて既定を持たない（つながない線は None を明示）。
    cs = None のときは最初のクロックの辺から word_bits ごとに区切る。"""
    if mode not in (0, 1, 2, 3):
        raise ValueError(f"モードは 0〜3（{mode}）")
    if mosi is None and miso is None:
        raise ValueError("MOSI か MISO の少なくとも一方のチャンネルが要る")
    if not 1 <= word_bits <= 64:
        raise ValueError(f"語長は 1〜64（{word_bits}）")
    cpol, cpha = mode >> 1 & 1, mode & 1
    sample_on_rise = (cpol ^ cpha) == 0
    lines = [ch for ch in (sck, mosi, miso, cs) if ch is not None]
    bit = {ch: 1 << k for k, ch in enumerate(lines)}
    b_sck = bit[sck]
    b_mosi = bit.get(mosi, 0) if mosi is not None else 0
    b_miso = bit.get(miso, 0) if miso is not None else 0
    b_cs = bit[cs] if cs is not None else 0
    active_cs = (0 if cs_active_low else b_cs) if cs is not None else 0
    starts, values = project(tr, lines)
    out: list[Annotation] = []
    params = {"sck": sck, "mosi": mosi, "miso": miso, "cs": cs, "mode": mode, "msb_first": msb_first,
              "word_bits": word_bits, "cs_active_low": cs_active_low}
    if not len(values):
        return Decoding("spi", (), tr.sample_period_s, tr.trigger, tr.length, params)

    word: _Word | None = None
    transfer_start: int | None = None
    transfer_error: str | None = None
    transfer_words: tuple[list[int], list[int]] = ([], [])

    def emit_word(end: int, error: str | None) -> None:
        nonlocal word
        n = word.bits
        fields = {"bits": n} if error else None
        for line, value, kind, sink in ((mosi, word.mosi, "mosi", transfer_words[0]),
                                        (miso, word.miso, "miso", transfer_words[1])):
            if line is None:
                continue
            if not msb_first:                         # LSB から来たビットを並べ直す
                value = int(f"{value:0{n}b}"[::-1], 2) if n else 0
            out.append(Annotation(word.start, max(end, word.start + 1), kind, f"0x{value:0{(n + 3) // 4}X}",
                                  value=value, error=error, fields=fields))
            if error is None:
                sink.append(value)
        word = None

    def open_transfer(at: int, error: str | None) -> None:
        nonlocal transfer_start, transfer_error, transfer_words
        transfer_start, transfer_error, transfer_words = at, error, ([], [])

    def close_transfer(at: int, error: str | None) -> None:
        nonlocal transfer_start
        if word is not None:
            emit_word(at, "incomplete")
        mo, mi = transfer_words
        parts = []
        if mosi is not None:
            parts.append("MOSI " + " ".join(f"{v:0{(word_bits + 3) // 4}X}" for v in mo))
        if miso is not None:
            parts.append("MISO " + " ".join(f"{v:0{(word_bits + 3) // 4}X}" for v in mi))
        out.append(Annotation(transfer_start, max(at, transfer_start + 1), "transfer", " / ".join(parts),
                              error=transfer_error or error, fields={"mosi": list(mo), "miso": list(mi)}))
        transfer_start = None

    def clock(prev: int, now: int, at: int) -> None:
        nonlocal word
        rising = bool(now & b_sck)
        if word is None:
            if rising == bool(cpol):                  # 語は前縁（休止の値から離れる辺）で始まる。語の後の後縁は数えない
                return
            word = _Word(at)                          # 語の範囲は最初のクロックの辺から
        if rising != sample_on_rise:
            return
        word.mosi = word.mosi << 1 | bool(prev & b_mosi)     # 辺の直前（セットアップ側）の値を読む
        word.miso = word.miso << 1 | bool(prev & b_miso)
        word.bits += 1
        if word.bits == word_bits:
            emit_word(at + 1, None)

    prev = values[0]
    selected = cs is None or (prev & b_cs) == active_cs
    if cs is not None and selected:
        open_transfer(0, "truncated")                 # 捕捉の先頭ですでに有効（語の境目が分からない）
    for i in range(1, len(values)):
        now, at = values[i], starts[i]
        changed = prev ^ now
        cs_now = cs is None or (now & b_cs) == active_cs
        if cs is not None and cs_now and not selected:        # 有効になる: 先に CS を、次にクロックを
            selected = True
            word = None
            open_transfer(at, None)
            prev = prev & ~b_cs | now & b_cs
            changed &= ~b_cs
        if selected and changed & b_sck:
            clock(prev, now, at)
        if cs is not None and not cs_now and selected:        # 無効になる: クロックを数えてから CS を
            selected = False
            close_transfer(at, None)
        prev = now
    if word is not None:
        emit_word(tr.length, "incomplete")
    if transfer_start is not None:
        close_transfer(tr.length, "truncated")
    return Decoding("spi", tuple(out), tr.sample_period_s, tr.trigger, tr.length, params)
