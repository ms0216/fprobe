"""システム制御空間（SCB・NVIC・SysTick・FPU の設定）の番地と、スナップショットからの書き戻し（fprobe.probe_snapshot が使う）。"""

from . import fault
from .dwarf.access import AccessError
from .snapshot import SnapshotAccess

# システム制御空間（読んでも状態が変わらない所）
ICTR, SYST_CSR, SYST_RVR, SYST_CVR = 0xE000E004, 0xE000E010, 0xE000E014, 0xE000E018
NVIC_ISER, NVIC_ICER, NVIC_ISPR, NVIC_ICPR, NVIC_IABR, NVIC_IPR = (0xE000E100, 0xE000E180, 0xE000E200, 0xE000E280,
                                                                    0xE000E300, 0xE000E400)
SCB, VTOR, AIRCR, SCR, CCR, SHPR1, SHPR2, SHPR3, SHCSR = (0xE000ED00, 0xE000ED08, 0xE000ED0C, 0xE000ED10, 0xE000ED14,
                                                         0xE000ED18, 0xE000ED1C, 0xE000ED20, 0xE000ED24)
MPU_TYPE, DEMCR, FPCCR, FPDSCR = 0xE000ED90, 0xE000EDFC, 0xE000EF34, 0xE000EF3C
AIRCR_VECTKEY, AIRCR_PRIGROUP = 0x05FA0000, 0x700
SHCSR_ENABLES = 0x000F0000                         # MEMFAULTENA・BUSFAULTENA・USGFAULTENA・SECUREFAULTENA
V6M_PARTNOS = {0xC20, 0xC60, 0xD20}


def restore_system(target, access: SnapshotAccess) -> list[str]:
    """システム制御空間を書き戻す（取ってあるものだけ）。割り込みは全部消してから許可と保留を書き、SysTick は
    再読み込み値と現在値を書いてから有効にする。MPU の領域は窓（RNR）の 1 つしか取っていないので戻さない。"""
    written: list[str] = []

    def saved(address: int) -> int | None:
        try:
            return int.from_bytes(access.read_memory(address, 4), "little")
        except AccessError:
            return None

    def put(address: int, value: int, name: str) -> None:
        target.write_memory(address, value & 0xFFFFFFFF)
        written.append(name)

    for address, name in ((VTOR, "VTOR"), (SCR, "SCR"), (CCR, "CCR"), (SHPR1, "SHPR1"), (SHPR2, "SHPR2"),
                          (SHPR3, "SHPR3"), (fault.CPACR, "CPACR"), (FPCCR, "FPCCR"), (FPDSCR, "FPDSCR")):
        if (value := saved(address)) is not None:
            put(address, value, name)
    if (value := saved(AIRCR)) is not None:
        put(AIRCR, AIRCR_VECTKEY | value & AIRCR_PRIGROUP, "AIRCR.PRIGROUP")
    if (value := saved(SHCSR)) is not None:
        current = target.read32(SHCSR)
        put(SHCSR, current & ~SHCSR_ENABLES | value & SHCSR_ENABLES, "SHCSR")
    for i in range(0, 496, 4):
        if (value := saved(NVIC_IPR + i)) is None:
            break
        put(NVIC_IPR + i, value, f"NVIC.IPR{i // 4}")
    for n in range(16):
        enabled, pending = saved(NVIC_ISER + 4 * n), saved(NVIC_ISPR + 4 * n)
        if enabled is None and pending is None:
            break
        if enabled is not None:
            put(NVIC_ICER + 4 * n, 0xFFFFFFFF, f"NVIC.ICER{n}")
            put(NVIC_ISER + 4 * n, enabled, f"NVIC.ISER{n}")
        if pending is not None:
            put(NVIC_ICPR + 4 * n, 0xFFFFFFFF, f"NVIC.ICPR{n}")
            put(NVIC_ISPR + 4 * n, pending, f"NVIC.ISPR{n}")
    control = saved(SYST_CSR)
    if (value := saved(SYST_RVR)) is not None:
        put(SYST_RVR, value, "SYST_RVR")
        put(SYST_CVR, 0, "SYST_CVR")                              # 現在値は書けない（書くと 0 になる）
    if control is not None:
        put(SYST_CSR, control & 0x7, "SYST_CSR")
    target.flush()
    return written
