"""試験・デモ用の波形の発生器: 時刻（秒）で積んだ変化から Transitions を作る。各プロトコルの発生器もここに置く。

各プロトコルの発生器は Wave と開始時刻を受け、終わりの時刻を返す（組み合わせられる）。
"""

import math
import random
from array import array
from collections.abc import Iterable, Mapping, Sequence

from ..la import Transitions

__all__ = ["JtagBus", "OneWireBus", "SwdBus", "Wave", "can", "i2c", "i2s", "manchester", "onewire_rom", "spi", "uart",
           "uart_break"]


class Wave:
    """チャンネルごとの変化（時刻・値）を積み、サンプルの格子に載せて Transitions にする。
    変化はその時刻以降の最初のサンプルに置く。同じサンプルに落ちる変化は時刻の遅いものが勝つ。"""

    def __init__(self, channels: int, sample_rate_hz: float, idle: Mapping[int, int] | None = None,
                 jitter: float = 0.0, seed: int = 0):
        self.channels = channels
        self.sample_rate_hz = sample_rate_hz
        self.idle = dict(idle or {})
        self.jitter = jitter                               # 変化の時刻に加える一様な揺れ（秒、±jitter）
        self._rng = random.Random(seed)
        self._changes: list[tuple[float, int, int, int]] = []     # (時刻, 積んだ順, ch, 値)

    def set(self, channel: int, level: int, t: float) -> None:
        if not 0 <= channel < self.channels:
            raise ValueError(f"チャンネル {channel} は無い")
        if self.jitter:
            t += self._rng.uniform(-self.jitter, self.jitter)
        self._changes.append((t, len(self._changes), channel, level & 1))

    def bits(self, channel: int, levels: Iterable[int], t: float, bit_s: float) -> float:
        """levels を 1 ビット bit_s 秒ずつ順に出す。終わりの時刻を返す。"""
        for level in levels:
            self.set(channel, level, t)
            t += bit_s
        return t

    def _sample(self, t: float) -> int:
        return math.ceil(t * self.sample_rate_hz - 1e-6)     # 浮動小数の誤差で 1 サンプルずれない

    def build(self, *, start_s: float = 0.0, end_s: float | None = None, trigger_s: float = 0.0) -> Transitions:
        """[start_s, end_s) を捕捉した Transitions。trigger_s は捕捉の先頭からの秒ではなく絶対の時刻。"""
        changes = sorted(self._changes)
        state = 0
        for ch, level in self.idle.items():
            state |= (level & 1) << ch
        first = self._sample(start_s)
        if end_s is None:
            end_s = (changes[-1][0] if changes else start_s) + 1 / self.sample_rate_hz
        length = self._sample(end_s) - first
        if length <= 0:
            raise ValueError("捕捉の区間が空")
        values, starts = array("H"), array("Q")
        rate, n = self.sample_rate_hz, len(changes)
        samples = [math.ceil(c[0] * rate - 1e-6) for c in changes]      # _sample と同じ（ループの外で 1 回）
        i = 0
        while i < n and samples[i] <= first:                           # 先頭までの変化は初期値に畳む
            state = _apply(state, changes[i])
            i += 1
        values.append(state)
        starts.append(0)
        while i < n:
            s = samples[i]
            if s - first >= length:
                break
            while i < n and samples[i] == s:
                _, _, ch, level = changes[i]
                state = state & ~(1 << ch) | level << ch
                i += 1
            if state != values[-1]:
                values.append(state)
                starts.append(s - first)
        trigger = min(max(self._sample(trigger_s) - first, 0), length - 1)
        return Transitions(values, starts, length, trigger, 0, 1 / self.sample_rate_hz, self.channels)


def _apply(state: int, change: tuple[float, int, int, int]) -> int:
    _, _, ch, level = change
    return state & ~(1 << ch) | level << ch


def bits_lsb(value: int, bits: int) -> list[int]:
    return [value >> i & 1 for i in range(bits)]


def bits_msb(value: int, bits: int) -> list[int]:
    return [value >> i & 1 for i in reversed(range(bits))]


def parity_bit(value: int, kind: str) -> int | None:
    """kind のパリティのビット（"none" なら None）。"""
    ones = bin(value).count("1") & 1
    return {"none": None, "even": ones, "odd": ones ^ 1, "mark": 1, "space": 0}[kind]



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


# ---- SWD ----

class SwdBus:
    """SWD の線を 1 サイクルずつ動かす。1 サイクル = SWCLK の立ち下がり（t）と立ち上がり（t + 半周期）。
    ホストは立ち下がりで SWDIO を変え、ターゲットは前のサイクルの立ち上がりの少し後で変える。ターンアラウンドでは
    誰も駆動せず、プルアップで 1 になる。drive（プローブが SWDIO を駆動中、Primer では ch8）も動かす。
    SWCLK の休止の値 1 は Wave の idle で与える。"""

    OK, WAIT, FAULT, NONE = 0b001, 0b010, 0b100, 0b111

    def __init__(self, w: Wave, *, swclk: int, swdio: int, clock_hz: float, drive: int | None = None,
                 t: float = 0.0, turnaround: int = 1, orundetect: bool = False):
        self.w, self.swclk, self.swdio, self.drive = w, swclk, swdio, drive
        self.h = 1 / (2 * clock_hz)
        self.t = t
        self.turnaround = turnaround
        self.orundetect = orundetect

    def _cycle(self, bit: int, who: str) -> None:
        w, t, h = self.w, self.t, self.h
        w.set(self.swclk, 0, t)
        if who == "host":
            w.set(self.swdio, bit, t)
        elif who == "target":
            w.set(self.swdio, bit, t - 0.6 * h)          # 前の立ち上がりの後
        else:                                             # 誰も駆動しない: プルアップ
            w.set(self.swdio, 1, t - 0.6 * h)
        if self.drive is not None:
            w.set(self.drive, 1 if who == "host" else 0, t - 0.6 * h if who != "host" else t)
        w.set(self.swclk, 1, t + h)
        self.t = t + 2 * h

    def host(self, bits: Iterable[int]) -> None:
        for b in bits:
            self._cycle(b, "host")

    def target(self, bits: Iterable[int]) -> None:
        for b in bits:
            self._cycle(b, "target")

    def undriven(self, n: int) -> None:
        for _ in range(n):
            self._cycle(1, "none")

    def pause(self, seconds: float) -> None:
        """クロックを止める（SWCLK は 1 のまま）。"""
        self.t += seconds

    def idle(self, n: int = 2) -> None:
        self.host([0] * n)

    def line_reset(self, n: int = 51) -> None:
        self.host([1] * n)

    def sequence16(self, value: int) -> None:
        self.host(bits_lsb(value, 16))

    def jtag_to_swd(self) -> None:
        self.line_reset()
        self.sequence16(0xE79E)
        self.line_reset()
        self.idle()

    def dormant_to_swd(self, code: int = 0x1A) -> None:
        from .swd import DORMANT_ALERT
        self.host([1] * 8)
        self.host(bits_lsb(DORMANT_ALERT, 128))
        self.host([0] * 4)
        self.host(bits_lsb(code, 8))
        self.line_reset()
        self.idle()

    def transfer(self, ap: bool, rnw: bool, addr: int, data: int | None = None, *, ack: int = OK,
                 bad_request_parity: bool = False, bad_data_parity: bool = False) -> None:
        """1 つのパケット。読みで data を省くと 0。ACK が OK 以外ならデータの相は ORUNDETECT のときだけ。
        DP の 0xC への書き込みは TARGETSEL（ACK の相は誰も駆動しない）。"""
        a2, a3 = addr >> 2 & 1, addr >> 3 & 1
        parity = (ap ^ rnw ^ a2 ^ a3) ^ bad_request_parity
        self.host([1, int(ap), int(rnw), a2, a3, int(parity), 0, 1])
        data = 0 if data is None else data & 0xFFFFFFFF
        dpar = (bin(data).count("1") & 1) ^ bad_data_parity
        payload = bits_lsb(data, 32) + [dpar]
        if not ap and not rnw and addr == 0xC:            # TARGETSEL
            self.undriven(self.turnaround + 3 + self.turnaround)
            self.host(payload)
            return
        self.undriven(self.turnaround)
        if ack == self.NONE:
            self.undriven(3 + self.turnaround)
            return
        self.target(bits_lsb(ack, 3))
        if ack != self.OK and not self.orundetect:
            self.undriven(self.turnaround)
            return
        if rnw:
            self.target(payload)
            self.undriven(self.turnaround)
        else:
            self.undriven(self.turnaround)
            self.host(payload)
        # turnaround・orundetect は呼び手が DLCR・CTRL/STAT を書いた後に設定する（バス自身は追跡しない）

    def read(self, ap: bool, addr: int, data: int = 0, **kw) -> None:
        self.transfer(ap, True, addr, data, **kw)

    def write(self, ap: bool, addr: int, data: int, **kw) -> None:
        self.transfer(ap, False, addr, data, **kw)


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


# ---- 1-Wire ----

class OneWireBus:
    """1-Wire の線を主とスレーブの両方の側から動かす。線の休止 1 は Wave の idle で与える。時間は µs（標準 / OD）。"""

    STANDARD = {"slot": 70, "write1": 6, "write0": 60, "read": 3, "read0": 30, "reset": 480, "presence_wait": 70,
                "presence": 120, "recovery": 410}
    OVERDRIVE = {"slot": 10, "write1": 1, "write0": 7.5, "read": 0.8, "read0": 3, "reset": 70, "presence_wait": 8.5,
                 "presence": 8, "recovery": 40}

    def __init__(self, w: Wave, channel: int, t: float = 0.0, overdrive: bool = False):
        self.w, self.channel, self.t, self.overdrive = w, channel, t, overdrive

    @property
    def _us(self) -> dict:
        return self.OVERDRIVE if self.overdrive else self.STANDARD

    def _low(self, low_us: float, slot_us: float) -> None:
        self.w.set(self.channel, 0, self.t)
        self.w.set(self.channel, 1, self.t + low_us * 1e-6)
        self.t += slot_us * 1e-6

    def reset(self, presence: bool = True, *, overdrive: bool | None = None) -> None:
        """リセット（overdrive=False なら標準のリセットで標準の速度に戻す）とプレゼンス。"""
        if overdrive is not None:
            self.overdrive = overdrive
        us = self._us
        self._low(us["reset"], us["reset"])
        if presence:
            self.w.set(self.channel, 0, self.t + us["presence_wait"] * 1e-6)
            self.w.set(self.channel, 1, self.t + (us["presence_wait"] + us["presence"]) * 1e-6)
        self.t += (us["presence_wait"] + us["presence"] + us["recovery"]) * 1e-6

    def write_bit(self, bit: int) -> None:
        us = self._us
        self._low(us["write1"] if bit else us["write0"], us["slot"])

    def read_bit(self, bit: int) -> None:
        """主の読みのスロットに、スレーブが bit を返す（0 なら 0 を延ばす）。"""
        us = self._us
        self._low(us["read"] if bit else us["read0"], us["slot"])

    def write_byte(self, value: int) -> None:
        for b in bits_lsb(value, 8):
            self.write_bit(b)

    def read_byte(self, value: int) -> None:
        for b in bits_lsb(value, 8):
            self.read_bit(b)

    def rom_command(self, command: int) -> None:
        self.write_byte(command)
        if command in (0x3C, 0x69):
            self.overdrive = True

    def search(self, rom: int, others: Sequence[int] = ()) -> None:
        """SEARCH ROM の 64 の三つ組（ビット・その補数・主が選ぶ向き）。他のスレーブ（others）も応えて線は AND。"""
        alive = [rom, *others]
        for i in range(64):
            ones = [r >> i & 1 for r in alive]
            chosen = rom >> i & 1
            self.read_bit(int(all(ones)))
            self.read_bit(int(not any(ones)))
            self.write_bit(chosen)
            alive = [r for r in alive if r >> i & 1 == chosen]


def onewire_rom(family: int, serial: int) -> int:
    """CRC を付けた 64 ビットの ROM。"""
    data = bytes([family]) + serial.to_bytes(6, "little")
    crc = 0
    for byte in data:
        for _ in range(8):
            mix = (crc ^ byte) & 1
            crc >>= 1
            if mix:
                crc ^= 0x8C
            byte >>= 1
    return int.from_bytes(data + bytes([crc]), "little")


# ---- JTAG ----

class JtagBus:
    """JTAG の線を 1 サイクルずつ動かす（TCK の立ち下がりで TMS・TDI・TDO を変え、立ち上がりで読まれる）。
    TCK の休止 0 は Wave の idle で与える。状態は Run-Test/Idle から始め、シフトは Run-Test/Idle に戻して終える。"""

    def __init__(self, w: Wave, *, tck: int, tms: int, tdi: int, tdo: int | None = None, clock_hz: float,
                 t: float = 0.0):
        self.w, self.tck, self.tms, self.tdi, self.tdo = w, tck, tms, tdi, tdo
        self.h = 1 / (2 * clock_hz)
        self.t = t

    def clock(self, tms: int, tdi: int = 0, tdo: int = 0) -> None:
        w, t = self.w, self.t
        w.set(self.tms, tms, t)
        w.set(self.tdi, tdi, t)
        if self.tdo is not None:
            w.set(self.tdo, tdo, t)
        w.set(self.tck, 1, t + self.h)
        w.set(self.tck, 0, t + 2 * self.h)
        self.t = t + 2 * self.h

    def reset(self) -> None:
        """5 回の TMS = 1 で Test-Logic-Reset、続けて Run-Test/Idle へ。"""
        for _ in range(5):
            self.clock(1)
        self.clock(0)

    def idle(self, n: int = 1) -> None:
        for _ in range(n):
            self.clock(0)

    def _shift(self, select: list[int], value: int, bits: int, tdo_value: int, pause_after: int | None) -> None:
        for m in select + [0, 0]:                       # Select → Capture → Shift
            self.clock(m)
        for i in range(bits):
            last = i == bits - 1
            pause = pause_after is not None and i == pause_after - 1 and not last
            self.clock(1 if last or pause else 0, value >> i & 1, tdo_value >> i & 1)
            if pause:                                   # Exit1 → Pause → Pause → Exit2 → Shift
                for m in (0, 0, 1, 0):
                    self.clock(m)
        self.clock(1)                                   # Exit1 → Update
        self.clock(0)                                   # Update → Run-Test/Idle

    def shift_ir(self, value: int, bits: int, tdo_value: int = 0b0001, pause_after: int | None = None) -> None:
        self._shift([1, 1], value, bits, tdo_value, pause_after)

    def shift_dr(self, value: int, bits: int, tdo_value: int = 0, pause_after: int | None = None) -> None:
        self._shift([1], value, bits, tdo_value, pause_after)


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
