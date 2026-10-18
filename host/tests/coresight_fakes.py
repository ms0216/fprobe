"""試験用: CoreSight の部品（CIDR・PIDR）と ROM テーブルを 32 ビット語の辞書（番地 → 値）に書く道具と、
STM32F401（Cortex-M4）らしい中身の例。"""

ARM = (4, 0x3B)
ST = (0, 0x20)


def add_component(words: dict[int, int], base: int, cls: int, designer: tuple[int, int], part: int,
                  devarch: int | None = None, revision: int = 0) -> None:
    cont, ident = designer
    cidr = [0x0D, (cls << 4) | 0x0, 0x05, 0xB1]
    pidr = [part & 0xFF, (part >> 8) & 0xF | (ident & 0xF) << 4, (ident >> 4) & 0x7 | 0x8 | revision << 4, 0]
    for i, v in enumerate(cidr):
        words[base + 0xFF0 + 4 * i] = v
    for i, v in enumerate(pidr):
        words[base + 0xFE0 + 4 * i] = v
    words[base + 0xFD0] = cont & 0xF
    if devarch is not None:
        words[base + 0xFBC] = devarch


def add_rom_table(words: dict[int, int], base: int, children: list[int], designer=ARM, part: int = 0x4C4,
                  cls: int = 1) -> None:
    add_component(words, base, cls, designer, part, devarch=0x47700AF7 if cls == 9 else None)
    for i, child in enumerate(children):
        offset = (child - base) & 0xFFFFFFFF
        words[base + 4 * i] = (offset & 0xFFFFF000) | (3 if cls == 9 else 3)
    words[base + 4 * len(children)] = 0


def stm32f401_words() -> dict[int, int]:
    """ROM テーブル（0xE00FF000、設計者 ST）→ SCS・DWT・FPB・ITM・TPIU・ETM と、CPUID・DBGMCU の IDCODE。"""
    words: dict[int, int] = {}
    parts = {0xE000E000: 0x00C, 0xE0001000: 0x002, 0xE0002000: 0x003, 0xE0000000: 0x001, 0xE0040000: 0x9A1,
             0xE0041000: 0x925}
    add_rom_table(words, 0xE00FF000, list(parts), designer=ST, part=0x413)
    for base, part in parts.items():
        add_component(words, base, 0xE if part == 0x00C else 9, ARM, part)
    words[0xE000ED00] = 0x410FC241                      # CPUID: Cortex-M4 r0p1
    words[0xE0042000] = 0x10006433                      # DBGMCU_IDCODE: DEV_ID 0x433（F401xD/E）
    words[0xE000EDF0] = 0x00030000                      # DHCSR: S_REGRDY | S_HALT
    words[0x40023C14] = 0x0FFFAAED                      # FLASH_OPTCR: 既定（RDP = 0xAA、OPTLOCK）
    return words
