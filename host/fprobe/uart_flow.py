"""UART のハードウェアのフロー制御（RTS/CTS）と制御線の状態（U52。CSR uart.flow・uart.lines）。

仕様: docs/superpowers/specs/2026-10-19-u51-u52-swo-uart-design.md §4

端子はデバイスが申告する候補（ケイパビリティ UART_FLOW = ロジアナのチャンネルの並び。Primer は 4 = 予備・1 = TDI）から
チャンネルの番号で選ぶ。選んだ端子はその機能（TDI 等）の出力を止めて RTS を出す / CTS を読む。線はアクティブ Low。
CDC-ACM に「フロー制御を有効に」の要求は無いので、自動の RTS/CTS（auto）はこの CSR で有効にする。
"""

from dataclasses import dataclass

from . import generated as g

__all__ = ["UartFlow", "UartLines"]


def _field(word: int, spec: tuple[int, int]) -> int:
    shift, width = spec
    return (word >> shift) & ((1 << width) - 1)


def _put(spec: tuple[int, int], value: int) -> int:
    shift, width = spec
    return (value & ((1 << width) - 1)) << shift


@dataclass(frozen=True)
class UartLines:
    cts: bool            # CTS が出ている（端子が Low。CTS を取っていなければ True）
    rts: bool            # RTS を出している
    host_rts: bool       # COM ポートの RTS（CDC-ACM の SET_CONTROL_LINE_STATE）
    host_break: bool     # ブレークを送っている（CDC-ACM の SEND_BREAK）


class UartFlow:
    def __init__(self, dev):
        self.dev = dev
        self.pins: tuple[int, ...] = tuple(getattr(dev.capabilities(), "uart_flow", ()))

    def _index(self, channel: int | None, what: str) -> int | None:
        if channel is None:
            return None
        if not self.pins:
            raise RuntimeError("この構成（ゲートウェア）には UART の RTS/CTS の回路が無い（ケイパビリティ uart_flow なし）")
        if channel not in self.pins:
            raise ValueError(f"{what} に使える端子はロジアナの ch {list(self.pins)}: {channel}")
        return self.pins.index(channel)

    def configure(self, auto: bool, rts_channel: int | None, cts_channel: int | None) -> None:
        """auto: 自動の RTS/CTS。rts_channel / cts_channel: RTS を出す / CTS を読む端子（None で使わない）。"""
        rts, cts = self._index(rts_channel, "RTS"), self._index(cts_channel, "CTS")
        if rts is not None and rts == cts:
            raise ValueError("RTS と CTS に同じ端子は使えない")
        if auto and not self.pins:
            self._index(0, "RTS")                                  # 回路が無い: 断る
        word = (_put(g.UART_FLOW_AUTO, int(auto))
                | _put(g.UART_FLOW_RTS_CLAIM, int(rts is not None)) | _put(g.UART_FLOW_RTS_PIN, rts or 0)
                | _put(g.UART_FLOW_CTS_CLAIM, int(cts is not None)) | _put(g.UART_FLOW_CTS_PIN, cts or 0))
        self.dev.write32(g.UART_FLOW, word)

    def config(self) -> dict:
        word = self.dev.read32(g.UART_FLOW)

        def pin(claim, index):
            k = _field(word, index)
            return self.pins[k] if _field(word, claim) and k < len(self.pins) else None
        rts, cts = pin(g.UART_FLOW_RTS_CLAIM, g.UART_FLOW_RTS_PIN), pin(g.UART_FLOW_CTS_CLAIM, g.UART_FLOW_CTS_PIN)
        return {"auto": bool(_field(word, g.UART_FLOW_AUTO)), "rts_channel": rts, "cts_channel": cts}

    def lines(self) -> UartLines:
        word = self.dev.read32(g.UART_LINES)
        return UartLines(cts=bool(_field(word, g.UART_LINES_CTS)), rts=bool(_field(word, g.UART_LINES_RTS)),
                         host_rts=bool(_field(word, g.UART_LINES_HOST_RTS)),
                         host_break=bool(_field(word, g.UART_LINES_HOST_BREAK)))
