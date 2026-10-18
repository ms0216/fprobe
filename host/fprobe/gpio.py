"""拡張ヘッダ（Primer 20K は J12）の汎用入出力 8 本（CSR の gpio ブロック）。

ビット n = 端子 n = ロジアナの ch n。出力にした端子（enable）は gpio が最優先で駆動する（デバッグの線・クロストリガの
出力・UART より先。fprobe_core / pin_select）。出力にしていない端子は普段の機能に戻る。

`sequence` は CONTROL の EXCHANGE（プロトコル 2）で、drive の値の列を 1 つの要求の中で順に書き、書くごとに端子を
読む（USB の 1 往復で最大 126 状態。ビットバングの SPI 等の汎用の部品。fprobe.spi）。読んだ値は同期化（2 周期）の
分だけ書く前の端子のことがある（同じ値を 2 回書けば 2 回目には必ず見える）。
仕様: docs/superpowers/specs/2026-10-19-u42-spi-flash-design.md
"""

from collections.abc import Sequence
from dataclasses import dataclass

from . import generated as g

__all__ = ["ALL_PINS", "EXCHANGE_PROTOCOL", "ExchangeUnsupported", "Gpio", "GpioState"]

ALL_PINS = 0xFF
EXCHANGE_PROTOCOL = 2                  # CONTROL の EXCHANGE を持つプロトコルの版


class ExchangeUnsupported(RuntimeError):
    """機器のビットストリームが古い（CONTROL プロトコル 2 の EXCHANGE が無い）。"""


@dataclass(frozen=True)
class GpioState:
    drive: int          # 出力の値
    enable: int         # 出力にしている端子
    level: int          # 端子の値（同期化した入力）


class Gpio:
    def __init__(self, dev):
        self.dev = dev
        self._exchange_ok: bool | None = None

    def state(self) -> GpioState:
        drive, enable, level = self.dev.read(g.GPIO_DRIVE, 3)
        return GpioState(drive & ALL_PINS, enable & ALL_PINS, level & ALL_PINS)

    def set(self, drive: int, enable: int, mask: int = ALL_PINS) -> GpioState:
        """mask の端子だけ、出力の値を drive、出力にするかを enable のビットにする（ほかの端子は変えない）。
        drive を先に書いてから enable を書く（出力にした瞬間に前の値を出さない）。"""
        cur = self.state()
        mask &= ALL_PINS
        new_drive = cur.drive & ~mask | drive & mask
        new_enable = cur.enable & ~mask | enable & mask
        self.dev.write(g.GPIO_DRIVE, [new_drive, new_enable])           # drive・enable は続きの番地
        return self.state()

    def release(self, mask: int = ALL_PINS) -> GpioState:
        """mask の端子を出力にしない（普段の機能に戻す）。"""
        cur = self.state()
        self.dev.write32(g.GPIO_ENABLE, cur.enable & ~mask & ALL_PINS)
        return self.state()

    def supports_sequence(self) -> bool:
        if self._exchange_ok is None:
            version = getattr(self.dev, "protocol_version", None)          # Device は開いたときに読んでいる
            if version is None:
                version = getattr(self.dev.capabilities(), "protocol_version", None)
            self._exchange_ok = (version or 0) >= EXCHANGE_PROTOCOL
        return self._exchange_ok

    def sequence(self, states: Sequence[int]) -> list[int]:
        """drive の値の列を順に書き、書くごとに端子を読んだ値の列を返す（出力にする端子は先に set で決める）。"""
        if not self.supports_sequence():
            raise ExchangeUnsupported("機器のビットストリームが古い（CONTROL プロトコル 2 の EXCHANGE が要る）: "
                                      "ゲートウェアを書き直す")
        return [v & ALL_PINS for v in self.dev.exchange(g.GPIO_IO, [s & ALL_PINS for s in states])]
