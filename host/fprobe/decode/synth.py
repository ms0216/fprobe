"""試験・デモ用の波形の発生器: 時刻（秒）で積んだ変化から Transitions を作る。各プロトコルの発生器もここに置く。

各プロトコルの発生器は Wave と開始時刻を受け、終わりの時刻を返す（組み合わせられる）。Wave は synth_wave.py、
線を 1 サイクルずつ動かすバス（SwdBus・OneWireBus・JtagBus）は synth_buses.py にあり、ここから再公開する。
"""

from collections.abc import Iterable, Sequence

from .synth_buses import JtagBus, OneWireBus, SwdBus, onewire_rom
from .synth_wave import Wave, bits_lsb, bits_msb, parity_bit

__all__ = ["JtagBus", "OneWireBus", "SwdBus", "Wave", "can", "i2c", "i2s", "manchester", "onewire_rom", "spi", "uart",
           "uart_break"]



# ---- UART ----

def uart(w: Wave, channel: int, data: Iterable[int], t: float, *, baud: float, data_bits: int = 8,
         parity: str = "none", stop_bits: float = 1, invert: bool = False, lsb_first: bool = True,
         gap_bits: float = 0.0, bad_parity: Iterable[int] = (), bad_stop: Iterable[int] = ()) -> float:
    """data を 1 フレームずつ送る。bad_parity / bad_stop は誤りを入れるフレームの番号。終わりの時刻を返す。
    線の休止の値（invert なら 0）は Wave の idle で与えておくこと。"""
    bit_s, bad_parity, bad_stop = 1 / baud, set(bad_parity), set(bad_stop)
    idle = 0 if invert else 1
    for n, value in enumerate(data):
        value &= (1 << data_bits) - 1
        bits = [0] + (bits_lsb(value, data_bits) if lsb_first else bits_msb(value, data_bits))
        p = parity_bit(value, parity)
        if p is not None:
            bits.append(p ^ (n in bad_parity))
        stop = 0 if n in bad_stop else 1
        t = w.bits(channel, [b ^ invert for b in bits], t, bit_s)
        w.set(channel, stop ^ invert, t)
        t += stop_bits * bit_s
        if stop == 0:
            w.set(channel, idle, t)                     # 誤った停止ビットの後は休止に戻す
        t += gap_bits * bit_s
    return t


def uart_break(w: Wave, channel: int, t: float, duration_s: float, invert: bool = False) -> float:
    """線を duration_s のあいだ休止と逆の値に保つ（ブレーク）。終わりの時刻を返す。"""
    w.set(channel, 1 if invert else 0, t)
    w.set(channel, 0 if invert else 1, t + duration_s)
    return t + duration_s


# ---- SPI ----

def spi(w: Wave, t: float, *, sck: int, clock_hz: float, mosi: int | None = None, miso: int | None = None,
        cs: int | None = None, mosi_words: Sequence[int] = (), miso_words: Sequence[int] = (), mode: int = 0,
        word_bits: int = 8, msb_first: bool = True, cs_active_low: bool = True, select: bool = True,
        release: bool = True) -> float:
    """1 回の転送（CS を有効にして語を送り、無効に戻す）。SCK の休止の値（CPOL）と CS の無効の値は Wave の idle で与える。
    CPHA = 0 は語の各ビットを前の後縁（最初は CS の後）で出し前縁で読む。CPHA = 1 は前縁で出し後縁で読む。終わりの時刻を返す。"""
    cpol, cpha = mode >> 1 & 1, mode & 1
    h = 1 / (2 * clock_hz)
    n = max(len(mosi_words), len(miso_words))
    if cs is not None and select:
        w.set(cs, 0 if cs_active_low else 1, t)
    t += h
    for k in range(n):
        bits_out = {}
        for line, words in ((mosi, mosi_words), (miso, miso_words)):
            if line is not None and k < len(words):
                bits_out[line] = (bits_msb if msb_first else bits_lsb)(words[k], word_bits)
        for b in range(word_bits):
            if cpha == 0:
                for line, bits in bits_out.items():
                    w.set(line, bits[b], t)
                w.set(sck, cpol ^ 1, t + h)                 # 前縁（読む）
                w.set(sck, cpol, t + 2 * h)                 # 後縁
            else:
                w.set(sck, cpol ^ 1, t)                     # 前縁（出す）
                for line, bits in bits_out.items():
                    w.set(line, bits[b], t)
                w.set(sck, cpol, t + h)                     # 後縁（読む）
            t += 2 * h
    t += h
    if cs is not None and release:
        w.set(cs, 1 if cs_active_low else 0, t)
    return t


# ---- I2C ----

def i2c(w: Wave, t: float, *, scl: int, sda: int, clock_hz: float, address: int, read: bool = False,
        data: Sequence[int] = (), ten_bit: bool = False, restart: bool = False, stop: bool = True,
        address_ack: bool = True, acks: Sequence[bool] | None = None, stretch_s: float = 0.0) -> float:
    """1 回の取引（START か Sr、アドレス、データ、STOP）。SCL・SDA の休止の値 1 は Wave の idle で与える。
    10 ビットのアドレスの読みは見出しの 1 バイトだけを送る（同じ取引で直前に書いたアドレスを使う決まり）。
    acks は各データバイトの ACK（省略時は書きはすべて ACK、読みは最後だけ NACK）。stretch_s は ACK の後に
    ターゲットが SCL を 0 に保つ時間（クロックの引き伸ばし）。restart なら SCL が 0 の状態（前の取引を STOP なしで
    終えた後）から始める。終わりの時刻を返す。"""
    h = 1 / (2 * clock_hz)
    if restart:                                         # Sr: SCL が 0 のうちに SDA を 1 に、SCL を 1 に、その後 START
        w.set(sda, 1, t + h / 2)
        w.set(scl, 1, t + h)
        t += 2 * h
    w.set(sda, 0, t)                                    # START: SCL が 1 の間に SDA を 0 に
    w.set(scl, 0, t + h)
    t += h

    def byte(t: float, value: int, ack: bool) -> float:
        for b in bits_msb(value, 8) + [0 if ack else 1]:
            w.set(sda, b, t + h / 2)
            w.set(scl, 1, t + h)
            w.set(scl, 0, t + 2 * h)
            t += 2 * h
        return t + stretch_s                            # SCL を 0 に保ったまま（次の立ち上がりが遅れる）

    if ten_bit:
        t = byte(t, 0b11110000 | (address >> 8 & 3) << 1 | int(read), address_ack)
        if not read:
            t = byte(t, address & 0xFF, address_ack)
    else:
        t = byte(t, (address & 0x7F) << 1 | int(read), address_ack)
    if acks is None:
        acks = [not (read and i == len(data) - 1) for i in range(len(data))]
    for value, ack in zip(data, acks):
        t = byte(t, value, ack)
    if stop:                                            # STOP: SCL が 1 の間に SDA を 1 に
        w.set(sda, 0, t + h / 2)
        w.set(scl, 1, t + h)
        w.set(sda, 1, t + 2 * h)
        t += 2 * h
    return t



# ---- CAN / CAN FD ----

_CAN_DLC = (0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64)
_CAN_GRAY = (0, 1, 3, 2, 6, 7, 5, 4)


def _crc(bits: Iterable[int], width: int, poly: int, init: int) -> int:
    reg = init
    for b in bits:
        top = reg >> (width - 1) & 1
        reg = reg << 1 & ((1 << width) - 1)
        if top ^ b:
            reg ^= poly
    return reg


def _stuff(bits: Sequence[int]) -> tuple[list[int], list[int | None]]:
    """ダイナミックスタッフを入れた列と、各ビットの元の位置（スタッフは None）。最後の 5 個の後にも入れる。"""
    out, src, run_val, run_len = [], [], None, 0
    for i, b in enumerate(bits):
        out.append(b)
        src.append(i)
        run_len = run_len + 1 if b == run_val else 1
        run_val = b
        if run_len == 5:
            out.append(b ^ 1)
            src.append(None)
            run_val, run_len = b ^ 1, 1
    return out, src


def can(w: Wave, channel: int, t: float, *, bitrate: float, can_id: int, data: bytes | Sequence[int] = b"",
        extended: bool = False, rtr: bool = False, fd: bool = False, brs: bool = False, esi: bool = False,
        data_bitrate: float | None = None, sample_point: float = 0.75, data_sample_point: float | None = None,
        dlc: int | None = None, ack: bool = True, bad_crc: bool = False, error_at: int | None = None,
        invert: bool = False) -> float:
    """1 つの CAN / CAN FD のフレーム（SOF から EOF とフレーム間の 3 ビットまで）。線の休止（劣勢 1、invert なら 0）は
    Wave の idle で与える。error_at はスタッフ後の何ビット目の後に誤りのフラグ（優勢 6 + 劣勢 8 + 3）を出すか。
    終わりの時刻を返す。"""
    data = list(data)
    nb = 1 / bitrate
    db = 1 / data_bitrate if brs else nb
    dsp = sample_point if data_sample_point is None else data_sample_point
    if fd:
        dlc = _CAN_DLC.index(len(data)) if dlc is None else dlc
    else:
        dlc = (0 if rtr else len(data)) if dlc is None else dlc
    head = [0] + bits_msb(can_id >> 18 if extended else can_id, 11)
    if extended:
        head += [1, 1] + bits_msb(can_id & 0x3FFFF, 18) + [0 if fd else int(rtr)]
    else:
        head += [0 if fd else int(rtr), 0]
    head += [1, 0, int(brs), int(esi)] if fd else ([0, 0] if extended else [0])
    brs_index = len(head) - 2 if fd and brs else None
    payload = head + bits_msb(dlc, 4) + [b for byte in data for b in bits_msb(byte, 8)]
    if fd:
        stuffed, src = _stuff(payload)
        count = src.count(None)
        gray = _CAN_GRAY[count % 8]
        sc = bits_msb(gray, 3) + [bin(gray).count("1") & 1]
        width, poly, init = (17, 0x1685B, 1 << 16) if len(data) <= 16 else (21, 0x102899, 1 << 20)
        crc = _crc(stuffed + sc, width, poly, init) ^ int(bad_crc)
        field, prev = [], stuffed[-1]
        for j, b in enumerate(sc + bits_msb(crc, width)):
            if j % 4 == 0:
                prev ^= 1
                field.append(prev)
            field.append(b)
            prev = b
        seq = stuffed + field
        phases = ["n"] * len(seq)
        if brs_index is not None:
            k = src.index(brs_index)
            phases = ["n"] * k + ["brs"] + ["d"] * (len(seq) - k - 1)
    else:
        crc = _crc(payload, 15, 0x4599, 0) ^ int(bad_crc)
        seq, _ = _stuff(payload + bits_msb(crc, 15))
        phases = ["n"] * len(seq)
    durations = {"n": nb, "d": db, "brs": sample_point * nb + (1 - dsp) * db}
    for n, (b, phase) in enumerate(zip(seq, phases)):
        if error_at is not None and n == error_at:
            t = w.bits(channel, [0 ^ invert] * 6, t, nb)
            return w.bits(channel, [1 ^ invert] * 11, t, nb)
        w.set(channel, b ^ invert, t)
        t += durations[phase]
    w.set(channel, 1 ^ invert, t)                               # CRC 区切り
    t += dsp * db + (1 - sample_point) * nb if brs else nb
    w.set(channel, (0 if ack else 1) ^ invert, t)               # ACK スロット（受け手が優勢にする）
    t += nb
    return w.bits(channel, [1 ^ invert] * 11, t, nb)            # ACK 区切り・EOF・フレーム間



# ---- I2S ----

def i2s(w: Wave, t: float, *, sck: int, ws: int, sd: int, clock_hz: float, frames: Sequence[tuple[int, int]],
        word_bits: int = 16, slot_bits: int | None = None, format: str = "i2s") -> float:
    """(左, 右) の標本の列を送る。SCK の立ち下がりで WS・SD を変える。SCK の休止 0 は Wave の idle で与える。
    i2s は WS が MSB の 1 クロック前に変わり WS = 0 が左、left_justified / right_justified は WS = 1 が左。"""
    slot = slot_bits or word_bits
    left_ws = 0 if format == "i2s" else 1
    clocks: list[tuple[int, int]] = []                       # (WS, SD)（左詰めの並び）
    for left, right in frames:
        for value, level in ((left, left_ws), (right, left_ws ^ 1)):
            word = bits_msb(value & ((1 << word_bits) - 1), word_bits)
            pad = [0] * (slot - word_bits)
            clocks += [(level, b) for b in (pad + word if format == "right_justified" else word + pad)]
    if format == "i2s":                                      # WS を 1 クロック早める
        clocks = [(clocks[k + 1][0] if k + 1 < len(clocks) else clocks[k][0], clocks[k][1]) for k in range(len(clocks))]
    h = 1 / (2 * clock_hz)
    for level, bit in clocks:
        w.set(ws, level, t)
        w.set(sd, bit, t)
        w.set(sck, 1, t + h)
        w.set(sck, 0, t + 2 * h)
        t += 2 * h
    return t


# ---- Manchester ----

def manchester(w: Wave, channel: int, bits: Iterable[int], t: float, *, bitrate: float, convention: str = "ieee",
               idle: int = 0) -> float:
    """ビットの列を Manchester で送り、線を idle に戻す。ieee は 1 = 前半 0・後半 1、thomas はその逆。"""
    half = 1 / (2 * bitrate)
    for b in bits:
        first = (b ^ 1) if convention == "ieee" else b
        w.set(channel, first, t)
        w.set(channel, first ^ 1, t + half)
        t += 2 * half
    w.set(channel, idle, t)
    return t
