"""I2C の解読の上の 24 系の EEPROM（24Cxx / 24LCxx / AT24 等）。仕様: docs/superpowers/specs/2026-10-18-u20-decoders-design.md

- 書き: [デバイス W] [語アドレス（address_bytes）] [データ…]。データが無ければアドレスを合わせるだけ（次の読みの準備）。
- 読み: [デバイス R] [データ…] は内部のアドレスの指す所から。直前にアドレスだけの書きがあればまとめて「ランダム読み」。
- 書いた後の書き込みサイクルの間はデバイスがアドレスに NACK を返す（ACK ポーリング）: kind="poll"。
- page_size を与えると、ページの境目をまたぐ書き（ページの先頭に回り込んで上書きする、よくある不具合）を
  error="page_wrap" で示す。block_bits は 24C04〜24C16 のようにデバイスアドレスの下位ビットがメモリのアドレスの上位になる数。
"""

from .core import Annotation, Decoding

__all__ = ["decode_eeprom24"]


def decode_eeprom24(lower: Decoding, *, device: int = 0x50, address_bytes: int = 1, block_bits: int = 0,
                    page_size: int | None = None) -> Decoding:
    """I2C の解読の上で 24 系の EEPROM の読み書きを読む。device はデバイスアドレス（block_bits の分を除いた基準）。"""
    if lower.protocol != "i2c":
        raise ValueError("i2c の解読の上に重ねる")
    if address_bytes not in (1, 2):
        raise ValueError(f"語アドレスは 1 か 2 バイト（{address_bytes}）")
    if not 0 <= block_bits <= 3:
        raise ValueError(f"block_bits は 0〜3（{block_bits}）")
    if page_size is not None and page_size <= 0:
        raise ValueError(f"page_size が正でない（{page_size}）")
    block_mask = (1 << block_bits) - 1
    out: list[Annotation] = []
    pointer: int | None = None                       # 内部のアドレス（分からなければ None）
    pending: Annotation | None = None                # アドレスだけの書き（次の読みとまとめる）
    for t in lower.of("transfer"):
        f = t.fields
        dev = f["address"]
        if dev is None or f["ten_bit"] or dev & ~block_mask != device:
            continue
        block = (dev & block_mask) << (8 * address_bytes)
        data = list(f["data"])
        if f["ack"] is False:
            out.append(Annotation(t.start, t.end, "poll", "busy (NACK)", fields={"device": dev, "ack": False}))
            pending = None
            continue
        if f["rw"] == "W":
            if len(data) < address_bytes:
                out.append(Annotation(t.start, t.end, "write", "?", error=t.error or "short",
                                      fields={"device": dev, "address": None, "data": data}))
                pending = None
                continue
            word = 0
            for b in data[:address_bytes]:
                word = word << 8 | b
            address = block | word
            payload = data[address_bytes:]
            if not payload:
                pointer, pending = address, t
                continue
            error = t.error
            wrapped = page_size is not None and address % page_size + len(payload) > page_size
            if wrapped and error is None:
                error = "page_wrap"
            out.append(Annotation(t.start, t.end, "write", _text("W", address, payload, address_bytes, block_bits),
                                  value=address, error=error,
                                  fields={"device": dev, "address": address, "data": payload, "page_wrap": wrapped}))
            pending = None
            if page_size is not None:
                page = address - address % page_size
                pointer = page + (address % page_size + len(payload)) % page_size
            else:
                pointer = address + len(payload)
            continue
        start = pending.start if pending is not None else t.start
        address = pointer
        out.append(Annotation(start, t.end, "read", _text("R", address, data, address_bytes, block_bits),
                              value=address, error=t.error,
                              fields={"device": dev, "address": address, "data": data, "random": pending is not None}))
        pending = None
        if pointer is not None:
            pointer += len(data)
    return lower.derive("eeprom24", out, {"device": device, "address_bytes": address_bytes, "block_bits": block_bits,
                                          "page_size": page_size})


def _text(rw: str, address: int | None, data: list[int], address_bytes: int, block_bits: int) -> str:
    digits = 2 * address_bytes + (1 if block_bits else 0)
    where = "?" if address is None else f"0x{address:0{digits}X}"
    return f"{rw} {where} {len(data)} B: " + " ".join(f"{b:02X}" for b in data[:16]) + (" …" if len(data) > 16 else "")
