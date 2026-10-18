"""pyOCD に無いターゲットの定義（pyOCD をライブラリとして使うときに register() する）。

STM32F401xE: pyOCD の内蔵ターゲットにも、現在の CMSIS パック索引にも無い。Flash コントローラと
セクタ構成（16 KB × 4 + 64 KB + 128 KB × 3 = 512 KB）は STM32F412xE と同じなので、その書き込み
アルゴリズムを使い、RAM だけ F401xE の 96 KB にする（アルゴリズムは 0x20000000 から約 12 KB を使う）。
"""

from pyocd.core.memory_map import MemoryMap, RamRegion
from pyocd.target import TARGET
from pyocd.target.builtin.target_STM32F412xx import STM32F412xE

__all__ = ["STM32F401xE", "register"]


class STM32F401xE(STM32F412xE):
    VENDOR = "STMicroelectronics"
    MEMORY_MAP = MemoryMap(
        *[region for region in STM32F412xE.MEMORY_MAP.regions if not isinstance(region, RamRegion)],
        RamRegion(start=0x20000000, length=0x18000),
    )


def register() -> None:
    TARGET.setdefault("stm32f401xe", STM32F401xE)
