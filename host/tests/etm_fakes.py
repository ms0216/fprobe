"""試験用: ETMv4・ATB ファンネル・TPIU のレジスタを持つ偽のメモリ（CoreSight の標準のオフセット）。

TRCPRGCTLR を書くと TRCSTATR.IDLE が変わり、TPIU の FFCR.FOnMan は書いた直後に済む（読むと 0）。"""

from probe_fakes import DictMemory

ETM = 0xE004_1000
TPIU = 0xE004_0000
DEMCR = 0xE000_EDFC
TRCPRGCTLR, TRCSTATR, TRCCONFIGR = 0x004, 0x00C, 0x010
TRCEVENTCTL0R, TRCEVENTCTL1R, TRCSTALLCTLR, TRCTSCTLR = 0x020, 0x024, 0x02C, 0x030
TRCSYNCPR, TRCCCCTLR, TRCBBCTLR, TRCTRACEIDR = 0x034, 0x038, 0x03C, 0x040
TRCVICTLR, TRCVIIECTLR, TRCVISSCTLR = 0x080, 0x084, 0x088
TRCIDR0, TRCIDR1, TRCIDR2 = 0x1E0, 0x1E4, 0x1E8
CSPSR, FFSR, FFCR, FSCR = 0x004, 0x300, 0x304, 0x308


class CoreSightMemory(DictMemory):
    """ETMv4.2（COMMOPT = 1、CID・VMID 無し）と TPIU のレジスタ。etm_arch で TRCIDR1 の版を変えられる。"""

    def __init__(self, etm: int = ETM, tpiu: int = TPIU, etm_arch: tuple[int, int] = (4, 2)):
        self.etm, self.tpiu = etm, tpiu
        self.flushes = 0
        super().__init__({etm + TRCSTATR: 0x3,                                   # IDLE | PMSTABLE
                          etm + TRCIDR0: 1 << 29 | 1 << 7 | 0b11 << 1,
                          etm + TRCIDR1: 0x4100_F000 | etm_arch[0] << 8 | etm_arch[1] << 4,
                          etm + TRCIDR2: 0x0000_0004})

    def write_memory(self, address, data):
        super().write_memory(address, data)
        if address == self.etm + TRCPRGCTLR and len(data) == 4:
            enabled = int.from_bytes(data, "little") & 1
            super().write_memory(self.etm + TRCSTATR, (0x2 | (0 if enabled else 1)).to_bytes(4, "little"))
        if address == self.tpiu + FFCR and len(data) == 4 and int.from_bytes(data, "little") & 1 << 6:
            self.flushes += 1
            super().write_memory(address, (int.from_bytes(data, "little") & ~(1 << 6)).to_bytes(4, "little"))
