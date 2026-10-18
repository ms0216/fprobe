"""試験用: 拡張ヘッダの gpio（CSR の drive・enable・level・io と CONTROL の EXCHANGE）を持つ偽の機器と、その端子に
つなぐ偽の SPI の相手（汎用のシフトレジスタ・SPI NOR Flash）。

端子の値 = 出力にした端子は drive、それ以外は相手が駆動していればその値、誰も駆動しなければプル（プルアップの端子は 1、
プル無しは 0）。相手は端子が変わるたびに知らされ、SCK の辺で MOSI を取り込み・MISO を出す。
lag = 1 は「読みは書く前の端子」（FPGA の同期化の遅れのまね）、0 は「書いた後の端子」。どちらでも正しく動くこと。
"""

from types import SimpleNamespace

from fprobe import generated as g
from probe_fakes import FakeDevice

PULL_UP = 0b0110_1111                 # Primer の J12: 4・7 番だけプル無し


class SpiSlave:
    """汎用の SPI の相手（mode 0〜3、語長 bits、MSB / LSB 先頭、CS は Low で選択）。
    受け取った語ごとに received(word) を呼び、その戻り値を次の語として送り出す。"""

    def __init__(self, sck=0, mosi=1, miso=6, cs=3, mode=0, bits=8, lsb_first=False):
        self.sck, self.mosi, self.miso, self.cs = sck, mosi, miso, cs
        self.cpol, self.cpha = mode >> 1 & 1, mode & 1
        self.bits, self.lsb_first = bits, lsb_first
        self.selected = False
        self.last_sck = self.cpol
        self.out_word = 0
        self.out_bit = None                 # 駆動中の MISO（None = 高インピーダンス）
        self.in_word = 0
        self.in_count = 0
        self.out_count = 0
        self.words: list[list[int]] = []     # CS の選択ごとの受け取った語

    # 下位の型が上書きする
    def selected_begin(self) -> int:
        """選ばれた。最初に送る語を返す。"""
        return 0

    def received(self, word: int) -> int:
        """1 語受け取った。次に送る語を返す。"""
        return word

    def deselected(self) -> None:
        pass

    def _bit_of(self, word: int, i: int) -> int:
        return word >> (i if self.lsb_first else self.bits - 1 - i) & 1

    def _shift_out(self) -> None:
        self.out_bit = self._bit_of(self.out_word, self.out_count)
        self.out_count += 1

    def _sample(self, mosi: int) -> None:
        i = self.in_count
        self.in_word |= mosi << (i if self.lsb_first else self.bits - 1 - i)
        self.in_count += 1
        if self.in_count == self.bits:
            self.words[-1].append(self.in_word)
            self.out_word = self.received(self.in_word) & ((1 << self.bits) - 1)
            self.in_word, self.in_count, self.out_count = 0, 0, 0

    def pins(self, values: int) -> None:
        cs = values >> self.cs & 1
        sck = values >> self.sck & 1
        mosi = values >> self.mosi & 1 if self.mosi is not None else 1
        if not cs and not self.selected:
            self.selected = True
            self.words.append([])
            self.in_word, self.in_count, self.out_count = 0, 0, 0
            self.out_word = self.selected_begin() & ((1 << self.bits) - 1)
            self.out_bit = None
            if self.cpha == 0:
                self._shift_out()                       # CPHA 0: 選ばれたら最初のビットを出す
            self.last_sck = sck
            return
        if cs and self.selected:
            self.selected = False
            self.out_bit = None
            self.deselected()
            self.last_sck = sck
            return
        if self.selected and sck != self.last_sck:
            leading = sck != self.cpol
            if leading == (self.cpha == 0):
                self._sample(mosi)
            else:
                if self.out_count >= self.bits:
                    self.out_count = 0
                self._shift_out()
        self.last_sck = sck

    def drives(self) -> int | None:
        return self.out_bit if self.selected else None


class GpioDevice(FakeDevice):
    """gpio ブロックと EXCHANGE を持つ偽の機器。slaves の端子を毎回の書き込みで動かす。"""

    def __init__(self, slaves=(), lag=1, protocol_version=2, pull_up=PULL_UP):
        super().__init__()
        self.slaves = list(slaves)
        self.lag, self.protocol_version, self.pull_up = lag, protocol_version, pull_up
        self.drive = self.enable = 0
        self.exchanged_words = 0
        self.exchange_calls = 0

    def capabilities(self):
        caps = super().capabilities()
        return SimpleNamespace(**vars(caps), protocol_version=self.protocol_version)

    def level(self) -> int:
        value = 0
        for pin in range(8):
            if self.enable >> pin & 1:
                bit = self.drive >> pin & 1
            else:
                driven = [s.drives() for s in self.slaves if s.miso == pin and s.drives() is not None]
                bit = driven[0] if driven else self.pull_up >> pin & 1
            value |= bit << pin
        return value

    def _apply(self) -> None:
        values = self.level()
        for s in self.slaves:
            s.pins(values)

    def write32(self, address, value):
        if address in (g.GPIO_DRIVE, g.GPIO_IO):
            self.drive = value & 0xFF
            self._apply()
        elif address == g.GPIO_ENABLE:
            self.enable = value & 0xFF
            self._apply()
        else:
            super().write32(address, value)

    def read32(self, address):
        return {g.GPIO_DRIVE: self.drive, g.GPIO_ENABLE: self.enable, g.GPIO_LEVEL: self.level(),
                g.GPIO_IO: self.level()}.get(address, None) if address in (
                    g.GPIO_DRIVE, g.GPIO_ENABLE, g.GPIO_LEVEL, g.GPIO_IO) else super().read32(address)

    def read(self, address, count=1):
        return [self.read32(address + 4 * i) for i in range(count)]

    def exchange(self, address, words):
        assert self.protocol_version >= 2, "古い機器に EXCHANGE を送った"
        assert address == g.GPIO_IO
        self.exchange_calls += 1
        self.exchanged_words += len(words)
        out = []
        for w in words:
            before = self.level()
            self.write32(address, w)
            out.append(before if self.lag else self.level())
        return out


class SpiNorFlash(SpiSlave):
    """SPI NOR Flash（JEDEC の命令）。mode 0 / 3。保護（BP のビット）・WEL・WIP（状態を poll_busy 回読む間 1）・
    SFDP（JESD216B の基本表）・4 バイト番地のモード。"""

    def __init__(self, size=1 << 20, jedec=(0xEF, 0x40, 0x14), page=256, qer=0b101, sfdp=True, busy_polls=2,
                 mode=0, **pins):
        super().__init__(mode=mode, **pins)
        self.memory = bytearray(b"\xFF" * size)
        self.size, self.jedec, self.page, self.qer, self.has_sfdp = size, jedec, page, qer, sfdp
        self.busy_polls = busy_polls
        self.sr1, self.sr2 = 0, 0
        self.busy = 0
        self.four_byte = False
        self.commands: list[int] = []
        self._reset_command()

    # 状態
    def _reset_command(self):
        self.cmd = None
        self.addr = 0
        self.addr_bytes_left = 0
        self.dummy_left = 0
        self.data: list[int] = []
        self.reading = None             # 読み出す値の列を作る関数（番地 → 値）
        self.index = 0

    def selected_begin(self) -> int:
        self._reset_command()
        return 0xFF

    def status(self) -> int:
        return self.sr1 | (1 if self.busy else 0)

    def _sfdp_bytes(self) -> bytes:
        import struct
        dwords = [0] * 16
        dwords[0] = 0b01 | 0x20 << 8 | (0b01 if self.size > 1 << 24 else 0b00) << 17
        dwords[1] = self.size * 8 - 1
        dwords[7] = 12 | 0x20 << 8 | 15 << 16 | 0x52 << 24        # 種類 1: 4 KB 0x20、種類 2: 32 KB 0x52
        dwords[8] = 16 | 0xD8 << 8                                # 種類 3: 64 KB 0xD8
        dwords[10] = (self.page.bit_length() - 1) << 4
        dwords[14] = self.qer << 20
        table = struct.pack("<16I", *dwords)
        header = b"SFDP" + bytes([6, 1, 0, 0xFF])
        param = bytes([0x00, 6, 1, 16]) + (16).to_bytes(3, "little") + b"\xFF"
        blob = header + param + table
        return blob

    def _read_at(self, address: int) -> int:
        return self.memory[address % self.size]

    def received(self, byte: int) -> int:
        if self.cmd is None:
            self.cmd = byte
            self.commands.append(byte)
            return self._start(byte)
        if self.addr_bytes_left:
            self.addr = self.addr << 8 | byte
            self.addr_bytes_left -= 1
            if self.addr_bytes_left == 0 and not self.dummy_left:
                return self._after_address()
            return 0xFF
        if self.dummy_left:
            self.dummy_left -= 1
            if self.dummy_left == 0:
                return self._after_address()
            return 0xFF
        if self.reading is not None:
            self.index += 1
            return self.reading(self.index)
        self.data.append(byte)
        return 0xFF

    def _addressed(self, dummy=0) -> int:
        self.addr_bytes_left = 4 if self.four_byte else 3
        self.dummy_left = dummy
        return 0xFF

    def _start(self, cmd: int) -> int:
        if cmd == 0x9F:
            ident = list(self.jedec)
            self.reading = lambda i: ident[i] if i < len(ident) else 0xFF
            return ident[0]
        if cmd == 0x05:
            self.reading = lambda i: self._poll()
            return self._poll()
        if cmd == 0x35:
            self.reading = lambda i: self.sr2
            return self.sr2
        if cmd in (0x03, 0x02, 0x20, 0x52, 0xD8):
            return self._addressed()
        if cmd == 0x0B:
            return self._addressed(dummy=1)
        if cmd == 0x5A:
            if not self.has_sfdp:
                return 0xFF
            self.addr_bytes_left, self.dummy_left = 3, 1        # SFDP はいつも 3 バイト番地
            return 0xFF
        return 0xFF

    def _poll(self) -> int:
        value = self.status()
        if self.busy:
            self.busy -= 1
        return value

    def _after_address(self) -> int:
        if self.cmd in (0x03, 0x0B):
            base = self.addr
            self.reading = lambda i: self._read_at(base + i)
            return self._read_at(base)
        if self.cmd == 0x5A:
            blob = self._sfdp_bytes()
            base = self.addr
            self.reading = lambda i: blob[base + i] if base + i < len(blob) else 0xFF
            return blob[base] if base < len(blob) else 0xFF
        return 0xFF

    def _protected(self) -> bool:
        mask = 0x3C if self.qer == 0b010 else 0x7C          # Macronix は bit 6 が QE
        return bool(self.sr1 & mask)

    def deselected(self) -> None:
        cmd, data = self.cmd, self.data
        if cmd is None or self.busy:
            if cmd not in (0x05, None):
                self.ignored = getattr(self, "ignored", 0) + 1
            return
        wel = self.sr1 & 0x02
        if cmd == 0x06:
            self.sr1 |= 0x02
        elif cmd == 0x04:
            self.sr1 &= ~0x02
        elif cmd == 0xB7:
            self.four_byte = True
        elif cmd == 0xE9:
            self.four_byte = False
        elif cmd == 0x01 and wel:
            if data:
                self.sr1 = data[0] & 0xFC
                if len(data) > 1:
                    self.sr2 = data[1]
                elif self.qer == 0b001:
                    self.sr2 = 0                                 # 1 バイトの書き込みで SR2 が消える型
            self._done()
        elif cmd == 0x02 and wel and not self.addr_bytes_left:
            if not self._protected():
                start = self.addr - self.addr % self.page
                for i, b in enumerate(data):
                    at = start + (self.addr + i) % self.page
                    self.memory[at % self.size] &= b
            self._done()
        elif cmd in (0x20, 0x52, 0xD8) and wel and not self.addr_bytes_left:
            size = {0x20: 4096, 0x52: 32768, 0xD8: 65536}[cmd]
            if not self._protected():
                start = self.addr - self.addr % size
                self.memory[start:start + size] = b"\xFF" * size
            self._done()
        elif cmd in (0xC7, 0x60) and wel:
            if not self._protected():
                self.memory[:] = b"\xFF" * self.size
            self._done()

    def _done(self):
        self.sr1 &= ~0x02
        self.busy = self.busy_polls
