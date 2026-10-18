"""試験用: Flash を持つ偽の pyOCD ターゲット（fprobe.flash・fprobe.probe_flash の試験）。

メモリマップは pyOCD の本物（FlashRegion・RamRegion・MemoryMap）、書き込みの段取りも pyOCD の本物（FlashBuilder）。
偽物にするのは Flash の書き込みアルゴリズム（FakeFlash: 消すと 0xFF、書くと 1 → 0 だけ変わる本物の NOR と同じ規則）と、
ターゲットのメモリ・コアレジスタ（FakeChip）だけ。

配置（STM32F4 を小さくしたもの。セクタの大きさが混ざる）:
  Flash 0x08000000: 1 KiB × 4 + 4 KiB × 2 = 12 KiB、ページ 256 B
  RAM   0x20000000: 16 KiB（書き込みアルゴリズムは 0x20000000..0x20001000 を使う）
"""

from types import SimpleNamespace

from pyocd.core.memory_map import FlashRegion, MemoryMap, RamRegion
from pyocd.core.target import Target
from pyocd.flash.flash import Flash

FLASH, FLASH_SIZE = 0x08000000, 0x3000
RAM, RAM_SIZE = 0x20000000, 0x4000
PAGE = 0x100
SMALL, LARGE = 0x400, 0x1000
ALGO_RAM = (RAM, RAM + 0x1000)                       # アルゴリズムの命令・ページバッファ・スタック


class FakeFlash(Flash):
    """書き込みアルゴリズムの偽物。本物の NOR の規則（消すと 0xFF、書くと 1 → 0 だけ）を守り、呼ばれた操作を記録する。
    init のたびにアルゴリズムが RAM を使い（ALGO_RAM を 0xA5 で潰す）、r0..r3・pc を壊す（本物と同じ副作用）。"""

    def __init__(self, chip):
        super().__init__(chip, None)
        self.flash_algo = {"load_address": ALGO_RAM[0], "instructions": [0] * 0x100,
                           "page_buffers": [ALGO_RAM[0] + 0x800], "begin_stack": ALGO_RAM[1],
                           "static_base": ALGO_RAM[0] + 0x400, "analyzer_supported": False}
        self.chip = chip

    @property
    def is_erase_all_supported(self):
        return True

    def init(self, operation, address=None, clock=0, reset=False):
        self.chip.halt()
        self.chip.ram[ALGO_RAM[0] - RAM:ALGO_RAM[1] - RAM] = b"\xA5" * (ALGO_RAM[1] - ALGO_RAM[0])
        for name in ("r0", "r1", "r2", "r3"):
            self.chip.regs[name] = 0xDEAD0000
        self.chip.regs["pc"] = ALGO_RAM[0] + 0x21
        self.chip.ops.append(("init", operation.name))

    def uninit(self):
        pass

    def cleanup(self):
        self.chip.ops.append(("cleanup",))

    def erase_all(self):
        self.chip.flash[:] = b"\xFF" * FLASH_SIZE
        self.chip.ops.append(("erase_all",))

    def erase_sector(self, address):
        size = self.get_sector_info(address).size
        assert address % size == 0, hex(address)
        self.chip.flash[address - FLASH:address - FLASH + size] = b"\xFF" * size
        self.chip.ops.append(("erase", address))

    def program_page(self, address, data):
        assert address % PAGE == 0 and len(data) <= PAGE, (hex(address), len(data))
        offset = address - FLASH
        for i, b in enumerate(bytes(data)):
            self.chip.flash[offset + i] &= b & self.chip.stuck_mask   # NOR: 消していないと 1 → 0 しか変わらない
        self.chip.ops.append(("program", address, len(data)))


class FakeChip:
    """pyOCD のターゲット（CoreSightTarget）の偽物: メモリマップ、Flash と RAM の中身、コアレジスタ、状態。"""

    def __init__(self, running: bool = False, split: bool = False):
        """split: セクタの大きさごとに別の領域にする（pyOCD の内蔵の STM32F4 の定義と同じ形。既定は FLM と同じ
        親の領域 + 小領域）。"""
        self.flash = bytearray(b"\xFF" * FLASH_SIZE)
        self.ram = bytearray(RAM_SIZE)
        self.regs = {f"r{i}": i for i in range(13)}
        self.regs.update(sp=0x20003F00, msp=0x20003F00, psp=0, lr=0x08000123, pc=0x08000200, xpsr=0x01000000,
                         primask=0, basepri=0, faultmask=0, control=0)
        self.state = Target.State.RUNNING if running else Target.State.HALTED
        self.ops: list[tuple] = []
        self.reads = 0
        self.stuck_mask = 0xFF                                  # 0 のビットは書くと必ず 0 になる（壊れた Flash）
        self.session = SimpleNamespace(options={}, notify=lambda *a, **k: None,
                                       context_state=SimpleNamespace())
        ram = RamRegion(name="sram", start=RAM, length=RAM_SIZE, is_default=True)
        if split:
            small = FlashRegion(name="flash", start=FLASH, length=4 * SMALL, sector_size=SMALL, page_size=PAGE,
                                is_boot_memory=True, flash_class=FakeFlash)
            large = FlashRegion(name="flash_1", start=FLASH + 4 * SMALL, length=2 * LARGE, sector_size=LARGE,
                                page_size=PAGE, flash_class=FakeFlash)
            for region in (small, large):
                region.flash = FakeFlash(self)
                region.flash.region = region
            self.memory_map = MemoryMap(small, large, ram)
            return
        region = FlashRegion(name="flash", start=FLASH, length=FLASH_SIZE, sector_size=LARGE, page_size=PAGE,
                             is_boot_memory=True, flash_class=FakeFlash)
        region.submap.add_region(FlashRegion(name="flash_0x400", start=FLASH, length=4 * SMALL, sector_size=SMALL,
                                             page_size=PAGE))
        region.submap.add_region(FlashRegion(name="flash_0x1000", start=FLASH + 4 * SMALL, length=2 * LARGE,
                                             sector_size=LARGE, page_size=PAGE))
        region.flash = FakeFlash(self)
        region.flash.region = region
        self.memory_map = MemoryMap(region, ram)

    # メモリ
    def _store(self, address, length):
        if FLASH <= address and address + length <= FLASH + FLASH_SIZE:
            return self.flash, address - FLASH
        if RAM <= address and address + length <= RAM + RAM_SIZE:
            return self.ram, address - RAM
        raise AssertionError(f"範囲外 0x{address:08X}+{length}")

    def read_memory_block8(self, address, length):
        store, offset = self._store(address, length)
        self.reads += 1
        return list(store[offset:offset + length])

    def write_memory_block8(self, address, data):
        store, offset = self._store(address, len(data))
        assert store is self.ram, "Flash にはメモリの書き込みで書けない"
        store[offset:offset + len(data)] = bytes(data)

    def flush(self):
        pass

    # コア
    def get_state(self):
        return self.state

    def halt(self):
        self.state = Target.State.HALTED

    def resume(self):
        self.state = Target.State.RUNNING

    def reset(self, reset_type=None):
        self.state = Target.State.RUNNING
        self.ops.append(("reset",))

    def reset_and_halt(self, reset_type=None):
        self.state = Target.State.HALTED
        self.ops.append(("reset_and_halt",))

    def mass_erase(self):
        self.flash[:] = b"\xFF" * FLASH_SIZE
        self.ops.append(("mass_erase",))

    def read_core_register(self, name):
        return self.regs[name]

    def read_core_registers_raw(self, names):
        return [self.regs[n] for n in names]

    def write_core_registers_raw(self, names, values):
        for n, v in zip(names, values):
            self.regs[n] = v

    def write_core_register(self, name, value):
        self.regs[name] = value

    def step(self, disable_interrupts=True, start=0, end=0, hook_cb=None):
        """RAM の ldr pc, [pc, #0]（飛び先は次の語）だけを実行する（Flash の行の読み直し）。"""
        pc = self.regs["pc"]
        store, offset = self._store(pc, 8)
        assert store is self.ram and store[offset:offset + 4] == bytes.fromhex("dff800f0"), "ldr pc, [pc, #0] でない"
        target = int.from_bytes(store[offset + 4:offset + 8], "little")
        self.ops.append(("fetch", target & ~1))
        self.regs["pc"] = target & ~1
