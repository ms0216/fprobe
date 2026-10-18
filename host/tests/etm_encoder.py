"""試験用: TPIU のフォーマッタのフレームと ETMv4 のパケットを組む（CoreSight Architecture Specification の
フォーマッタの章、Arm IHI 0064 ETMv4 のパケットの形）。解読器（fprobe.trace）とは別に、仕様の形を直接書く。"""

FSYNC = bytes.fromhex("ffffff7f")
HSYNC = bytes.fromhex("ff7f")


# --- TPIU のフォーマッタ -----------------------------------------------------------------

def frames(chunks: list[tuple[int, bytes]]) -> bytes:
    """(トレース ID, バイト列) の並びを 16 バイトのフレームにする。足りない分は ID 0（null）で埋める。

    偶数のバイト: ビット 0 = 1 なら ID の変更（ID = ビット 7:1）、0 ならデータ（ビット 0 は最後のバイトの補助ビット）。
    奇数のバイト: 常にデータ。ID を変えたい所が奇数の位置に来たら、直前の偶数の位置で「次のバイトは前の ID」
    （補助ビット 1）の ID の変更を使う。"""
    stream = [(i, b) for i, data in chunks for b in data]
    out = bytearray()
    current = None
    pos = 0
    while pos < len(stream) or not out:
        frame = bytearray(16)
        aux = 0
        slot = 0
        while slot < 15:
            if pos >= len(stream):
                if slot % 2 == 0:
                    frame[slot] = 0x01                      # ID 0（null）へ変える。以後のバイトは捨てられる
                    current = 0
                slot += 1
                continue
            ident, byte = stream[pos]
            if slot % 2 == 0:
                if ident != current:
                    frame[slot] = ident << 1 | 1
                    current = ident
                    slot += 1
                    continue
                nxt = stream[pos + 1] if pos + 1 < len(stream) else (0, None)     # 終わりの後は ID 0（null）
                if slot < 14 and nxt[0] != ident:
                    # このバイトの次（奇数の位置）が別の ID: 偶数の位置に「新しい ID、次の 1 バイトは前の ID」
                    frame[slot] = nxt[0] << 1 | 1
                    aux |= 1 << (slot // 2)
                    frame[slot + 1] = byte
                    current = nxt[0]
                    pos += 1
                    slot += 2
                    continue
                frame[slot] = byte & 0xFE
                aux |= (byte & 1) << (slot // 2)
                pos += 1
                slot += 1
            else:
                if ident != current:                         # 奇数の位置では ID を変えられない: 詰め物の ID 0
                    raise AssertionError("組み方の誤り")
                frame[slot] = byte
                pos += 1
                slot += 1
        frame[15] = aux
        out += frame
    return bytes(out)


# --- ETMv4 のパケット --------------------------------------------------------------------

def async_() -> bytes:
    return bytes(11) + b"\x80"


def trace_info(cc: bool = False, cc_threshold: int | None = None) -> bytes:
    """Trace Info（0x01）: PLCTL（INFO と CYCT の有無）、INFO（ビット 0 = サイクル計数）、CYCT（閾値）。"""
    plctl = 0x01 | (0x08 if cc_threshold is not None else 0)
    out = bytes([0x01, plctl, 0x01 if cc else 0x00])
    if cc_threshold is not None:
        out += _cont(cc_threshold)
    return out


def _cont(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def long_address_is1(address: int) -> bytes:
    """Long Address 32 ビット IS1（0x9B）: A[7:1]、A[15:8]、A[23:16]、A[31:24]。"""
    return bytes([0x9B, (address >> 1) & 0x7F, (address >> 8) & 0xFF, (address >> 16) & 0xFF, (address >> 24) & 0xFF])


def long_address_is0(address: int) -> bytes:
    """Long Address 32 ビット IS0（0x9A）: A[8:2]、A[15:9]、A[23:16]、A[31:24]。"""
    return bytes([0x9A, (address >> 2) & 0x7F, (address >> 9) & 0x7F, (address >> 16) & 0xFF, (address >> 24) & 0xFF])


def short_address_is1(address: int, two_bytes: bool = False) -> bytes:
    """Short Address IS1（0x95）: A[7:1]（続きの印 = ビット 7）、続けば A[15:8]。上の桁は直前のアドレス。"""
    if two_bytes:
        return bytes([0x95, 0x80 | (address >> 1) & 0x7F, (address >> 8) & 0xFF])
    return bytes([0x95, (address >> 1) & 0x7F])


def exact_match(index: int) -> bytes:
    """Exact Match Address（0x90〜0x92）: アドレスの履歴の index 番目と同じ。"""
    return bytes([0x90 | index])


def address_context_is1(address: int, context: int = 0x00) -> bytes:
    """Address with Context 32 ビット IS1（0x83）: Long Address の 4 バイト + Context の情報 1 バイト（VMID・CID 無し）。"""
    return bytes([0x83]) + long_address_is1(address)[1:] + bytes([context])


def atoms(pattern: str) -> bytes:
    """E / N の並び（古い順）を、形式 1〜3 のアトムのパケットに詰める（3 つずつ）。"""
    out = bytearray()
    rest = pattern
    while rest:
        part, rest = rest[:3], rest[3:]
        bits = sum(1 << i for i, a in enumerate(part) if a == "E")
        if len(part) == 1:
            out.append(0xF6 | bits)                          # 形式 1: 0b1111011A
        elif len(part) == 2:
            out.append(0xD8 | bits)                          # 形式 2: 0b110110AA
        else:
            out.append(0xF8 | bits)                          # 形式 3: 0b11111AAA
    return bytes(out)


def exception(number: int, e1e0: int = 0b10) -> bytes:
    """Exception（0x06）: 1 バイト目 = C(7) E1(6) TYPE[4:0](5:1) E0(0)、型が 5 ビットを超えれば 2 バイト目に TYPE[9:5]。
    続けて戻り先のアドレスのパケットを置く（呼ぶ側）。"""
    e1, e0 = e1e0 >> 1 & 1, e1e0 & 1
    if number < 32:
        return bytes([0x06, e1 << 6 | (number & 0x1F) << 1 | e0])
    return bytes([0x06, 0x80 | e1 << 6 | (number & 0x1F) << 1 | e0, (number >> 5) & 0x1F])


def exception_return() -> bytes:
    return b"\x07"


def trace_on() -> bytes:
    return b"\x04"


def overflow() -> bytes:
    return b"\x00\x05"


def timestamp(value: int, bits: int = 14, cycles: int | None = None) -> bytes:
    """Timestamp（0x02、サイクル数付きは 0x03）: 下位から 7 ビットずつ（続きの印 = ビット 7）。"""
    out = bytearray([0x03 if cycles is not None else 0x02])
    groups = max(1, (bits + 6) // 7)
    for n in range(groups):
        byte = (value >> (7 * n)) & 0x7F
        out.append(byte | (0x80 if n < groups - 1 else 0))
    if cycles is not None:
        out += _cont(cycles)
    return bytes(out)
