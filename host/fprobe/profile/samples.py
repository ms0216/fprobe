"""PC サンプルの取り込み: SWO（ITM の PC サンプルのパケット）と DWT_PCSR（DAP サンプラの値・記録の出来事）を共通の形へ。

仕様: docs/superpowers/specs/2026-10-17-f5-profile-design.md の 2 節。
"""

from collections.abc import Iterable
from dataclasses import dataclass

from ..itm import ExceptionTrace, Overflow, Packet, PcSample as ItmPcSample, TimedPacket
from ..recording import Event
from .dwt import DWT_PCSR, PCSR_INVALID
from .exceptions import ExceptionState

__all__ = ["PcSample", "PcSamples"]


@dataclass(frozen=True)
class PcSample:
    """1 つの PC サンプル。pc=None はコアが眠っていた（ITM の眠りのパケット）。
    exception は例外トレースから分かる、そのとき処理中の例外番号（0 = スレッド、None = 分からない）。"""
    pc: int | None
    t: int | None = None
    exception: int | None = None


@dataclass(frozen=True)
class PcSamples:
    samples: tuple[PcSample, ...]
    source: str                         # "itm" / "pcsr" / "list"
    unavailable: int = 0                # PCSR が 0xFFFFFFFF（止まっている・サンプルできない）で数から外した数
    overflows: int = 0                  # ITM のオーバーフロー（サンプルが欠けた）
    exception_trace: bool = False       # 例外トレースのパケットがあった

    def __len__(self) -> int:
        return len(self.samples)

    @classmethod
    def from_pcs(cls, pcs: Iterable[int | None]) -> "PcSamples":
        """番地の列（None = 眠り）から。試験・ほかの取り方の入口。"""
        return cls(tuple(PcSample(None if pc is None else pc & ~1) for pc in pcs), "list")

    @classmethod
    def from_itm(cls, packets: Iterable[Packet | TimedPacket]) -> "PcSamples":
        """ITM のパケット（fprobe.itm.ItmDecoder）か、時刻付きのパケット（Timestamper）から。
        例外トレースがあれば、各サンプルに処理中の例外を付ける。"""
        state = ExceptionState()
        out: list[PcSample] = []
        overflows = 0
        traced = False
        for item in packets:
            packet, t = (item.packet, item.local_time) if isinstance(item, TimedPacket) else (item, None)
            if isinstance(packet, ItmPcSample):
                out.append(PcSample(packet.pc, t, state.current))
                continue
            if isinstance(packet, Overflow):
                overflows += 1
            traced |= isinstance(packet, ExceptionTrace)
            state.feed(packet, t)
        return cls(tuple(out), "itm", 0, overflows, traced)

    @classmethod
    def from_pcsr(cls, values: Iterable[int | Event]) -> "PcSamples":
        """DWT_PCSR を読んだ値の列（整数）か、記録の出来事（stream "sample"、a = 0xE000101C のものだけ使う）から。
        0xFFFFFFFF は数から外し、unavailable に数える。"""
        out: list[PcSample] = []
        unavailable = 0
        for v in values:
            if isinstance(v, Event):
                if v.stream != "sample" or v.a != DWT_PCSR:
                    continue
                value, t = v.b, v.t
            else:
                value, t = v, None
            if value == PCSR_INVALID:
                unavailable += 1
                continue
            out.append(PcSample(value & ~1, t))
        return cls(tuple(out), "pcsr", unavailable)
