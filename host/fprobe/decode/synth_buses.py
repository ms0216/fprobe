"""試験・デモ用の波形の発生器: 線を 1 サイクルずつ動かすバス（SwdBus・OneWireBus・JtagBus）と 1-Wire の ROM 番号。

Wave（synth_wave.py）の上に載り、各バスは状態（時刻）を持つ。UART・SPI・I2C などの関数形の発生器は synth.py。
"""

from collections.abc import Iterable, Sequence

from .synth_wave import Wave, bits_lsb

__all__ = ["JtagBus", "OneWireBus", "SwdBus", "onewire_rom"]


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
