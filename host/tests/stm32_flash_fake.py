"""試験用: STM32 の FLASH の周辺（オプションバイトの部分）の模型。F4（OPTCR）と L4 / G4 / G0（OPTR）の鍵・BSY・
OPTSTRT・OBL_LAUNCH と、RDP を 1 → 0 に戻したときの全消去。read32 / write32 と、pyOCD のターゲット風の read_memory /
write_memory を持つ。"""

from fprobe.stm32_option_bytes import KEY1, KEY2, OPTKEY1, OPTKEY2


class FakeStm32Flash:
    def __init__(self, style: str = "optcr", option: int = 0x0FFFAAED, busy_reads: int = 3, dev_id: int = 0x433):
        self.style = style
        self.base = 0x40023C00 if style == "optcr" else 0x40022000
        self.option = option if style == "optcr" else option
        self.loaded = option                         # optr: 反映済みの値（OBL で読み込む）
        self.committed = option                      # optcr: 最後に書き終えた値
        self.cr = 0xC0000000                         # optr: LOCK | OPTLOCK
        self.sr = 0
        self.busy_left = 0
        self.busy_reads = busy_reads
        self.key_state: list[int] = []
        self.opt_key_state: list[int] = []
        self.mass_erased = False
        self.launched = False
        self.programmed: list[int] = []
        self.dbgmcu = 0x10006000 | dev_id
        self.other: dict[int, int] = {}
        self.writes: list[tuple[int, int]] = []

    # 番地
    @property
    def opt_addr(self):
        return self.base + (0x14 if self.style == "optcr" else 0x20)

    @property
    def sr_addr(self):
        return self.base + (0x0C if self.style == "optcr" else 0x10)

    @property
    def cr_addr(self):
        return self.base + (0x10 if self.style == "optcr" else 0x14)

    def read32(self, address):
        if address == self.sr_addr:
            if self.busy_left:
                self.busy_left -= 1
                if not self.busy_left:
                    self._commit()
                return self.sr | (1 << 16)
            return self.sr
        if address == self.opt_addr:
            return self.option
        if address == self.cr_addr and self.style == "optr":
            return self.cr
        if address == 0xE0042000:
            return self.dbgmcu
        return self.other.get(address, 0)

    def write32(self, address, value):
        self.writes.append((address, value))
        if address == self.sr_addr:
            self.sr &= ~value
            return
        if self.style == "optcr":
            if address == self.base + 0x08:
                self.opt_key_state.append(value)
                if self.opt_key_state[-2:] == [OPTKEY1, OPTKEY2]:
                    self.option &= ~1
                return
            if address == self.opt_addr:
                if self.option & 1:
                    return                                          # 閉じている
                self.option = value
                if value & 2:
                    self.busy_left = self.busy_reads
                return
        else:
            if address == self.base + 0x08:
                self.key_state.append(value)
                if self.key_state[-2:] == [KEY1, KEY2]:
                    self.cr &= ~(1 << 31)
                return
            if address == self.base + 0x0C:
                self.opt_key_state.append(value)
                if self.opt_key_state[-2:] == [OPTKEY1, OPTKEY2] and not self.cr & (1 << 31):
                    self.cr &= ~(1 << 30)
                return
            if address == self.opt_addr:
                if not self.cr & (3 << 30):
                    self.option = value
                return
            if address == self.cr_addr:
                if value & (1 << 27):
                    self.launched = True
                    if (self.loaded & 0xFF) != 0xAA and (self.programmed[-1] & 0xFF) == 0xAA:
                        self.mass_erased = True
                    self.loaded = self.programmed[-1]
                    raise RuntimeError("リセットで応答が無い")
                if value & (1 << 17) and not self.cr & (3 << 30):
                    self.busy_left = self.busy_reads
                self.cr = (self.cr & ~(1 << 17)) | (value & ((1 << 31) | (1 << 30)))
                if value & (1 << 31):
                    self.cr |= 1 << 30
                return
        self.other[address] = value

    def _commit(self):
        if self.style == "optcr":
            if (self.committed >> 8) & 0xFF != 0xAA and (self.option >> 8) & 0xFF == 0xAA:
                self.mass_erased = True
            self.committed = self.option & ~3
            self.programmed.append(self.committed)
        else:
            self.programmed.append(self.option)

    # pyOCD のターゲット風
    def read_memory(self, address, transfer_size=32):
        return self.read32(address)

    def write_memory(self, address, value, transfer_size=32):
        self.write32(address, value)
