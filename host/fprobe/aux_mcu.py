"""補助マイコン（Raspberry Pi Pico）の計測値（CSR aux = ch 0 のターゲット電圧の最新値）。

仕様: docs/superpowers/specs/2026-10-17-m11-aux-pico-design.md。Pico のプログラムは host/firmware/aux_pico/main.py。
値はイベント記録（fprobe.events、種類 6 = "aux"）にも時刻付きで残る。
"""

import time
from dataclasses import dataclass

from . import generated as g
from .device import Device

__all__ = ["OVER_RANGE_MV", "Aux", "AuxReading"]

OVER_RANGE_MV = 0x3FFF        # 補助マイコンが ADC の振り切れを知らせる値（測れる上限を超えている）


@dataclass(frozen=True)
class AuxReading:
    target_millivolts: int           # ターゲット電圧の最新値（mV）
    samples: int                     # 受け取った計測値の数
    errors: int                      # 形の崩れた受信の数
    live: bool                       # 補助マイコンが計測値を送り続けている（読む間に samples が増えた）

    @property
    def over_range(self) -> bool:
        """ADC が上限に張り付いた（分圧の無い配線で 3.3V 系を測った等）。値は「上限以上」としか言えない。"""
        return self.target_millivolts == OVER_RANGE_MV

    @property
    def target_volts(self) -> float | None:
        """ターゲット電圧（V）。範囲外なら None。"""
        return None if self.over_range else self.target_millivolts / 1000


class Aux:
    def __init__(self, dev: Device):
        if not dev.capabilities().aux:
            raise RuntimeError("このデバイス（構成）には補助マイコンの受け口が無い")
        self.dev = dev

    def read(self, settle_s: float = 0.01) -> AuxReading:
        """最新値。settle_s（計測の間隔 1 ms より長く）待って samples が増えたかで、動いているかを見る。"""
        first = self.dev.read32(g.AUX_SAMPLES)
        time.sleep(settle_s)
        target, samples, errors = self.dev.read(g.AUX_TARGET, 3)
        return AuxReading(target & 0x3FFF, samples, errors, samples != first)
