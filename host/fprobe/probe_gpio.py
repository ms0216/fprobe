"""Probe の道具: 拡張ヘッダの汎用入出力（gpio.*）と、その上のビットバングの SPI マスタ（spi.transfer）。U41・U42。

端子はロジアナの ch（Primer の J12: 0 SWCLK/TCK・1 TDI・2 nRESET・3 SWDIO/TMS・4 予備・5 UART RX・6 SWO/TDO・7 UART TX）。
出力にした端子は gpio が最優先で駆動する（デバッグの線より先）ので、端子を動かす道具はターゲットに接続中は断る。
出力をやめる（普段の機能に戻す）のは gpio.release。SPI の端子は transfer の後も休止の値で駆動したまま（CS を離した
状態）なので、使い終えたら gpio.release する。

機器固有の手順（SPI Flash の JEDEC の命令等）は本体に入れない: fprobe.plugins.spiflash がこの API の上に載る。
Probe（fprobe.probe。共通の部品の型は fprobe.probe_host.ProbeHost）に混ぜて使う mixin。機器に触るのは必ず _lock の中。
仕様: docs/superpowers/specs/2026-10-19-u42-spi-flash-design.md
"""

import time

from .gpio import ALL_PINS, Gpio, GpioState
from .spi import SpiConfig, SpiMaster, SpiPins

__all__ = ["GpioTools", "SEQUENCE_LIMIT", "decode_words", "encode_words"]

SEQUENCE_LIMIT = 1 << 20              # gpio.sequence の 1 回の状態の数の上限
TRANSFER_LIMIT = 1 << 20              # spi.transfer の 1 回の語の数の上限


def _word_bytes(bits: int) -> int:
    return (bits + 7) // 8


def decode_words(text: str, bits: int) -> list[int]:
    """16 進の文字列 → 語の列（1 語 = ceil(bits / 8) バイト、上位のバイトから）。"""
    text = text.replace(" ", "")
    n = _word_bytes(bits)
    try:
        raw = bytes.fromhex(text)
    except ValueError:
        raise ValueError("data は 16 進の文字列（例 \"9f0000\"）") from None
    if len(raw) % n:
        raise ValueError(f"data の長さが語（{bits} ビット = {n} バイト）の倍数でない（16 進で {2 * n} 桁ごと）")
    return [int.from_bytes(raw[i:i + n], "big") for i in range(0, len(raw), n)]


def encode_words(words: list[int], bits: int) -> str:
    n = _word_bytes(bits)
    return b"".join(w.to_bytes(n, "big") for w in words).hex()


def _state_dict(st: GpioState) -> dict:
    return {"drive": st.drive, "enable": st.enable, "level": st.level}


class GpioTools:
    def _gpio(self) -> Gpio:
        return Gpio(self.session.device)

    def gpio_read(self) -> dict:
        """拡張ヘッダの 8 本の状態: drive（出力の値）・enable（出力にしている端子）・level（端子の値）。ビット n = ch n。"""
        with self._lock:
            return _state_dict(self._gpio().state())

    def gpio_set(self, drive: int, enable: int, mask: int = ALL_PINS) -> dict:
        """mask の端子だけ、出力の値を drive、出力にするかを enable のビットにする（ほかの端子は変えない）。
        出力にした端子は普段の機能（デバッグの線・UART 等）より優先して駆動する。ターゲットに接続中は断る。"""
        with self._lock:
            self._require_disconnected("拡張ヘッダの端子を駆動する操作")
            return _state_dict(self._gpio().set(drive, enable, mask))

    def gpio_release(self, mask: int = ALL_PINS) -> dict:
        """mask の端子を出力にしない（普段の機能に戻す）。"""
        with self._lock:
            return _state_dict(self._gpio().release(mask))

    def gpio_sequence(self, states: list[int]) -> dict:
        """drive の値の列を順に書き、書くごとに端子を読む（USB の 1 往復で最大 126 状態）。出力にする端子は先に
        gpio_set で決める。levels[i] は states[i] を書いた頃の端子（同期化の分、書く前の値のことがある）。"""
        if len(states) > SEQUENCE_LIMIT:
            raise ValueError(f"states は {SEQUENCE_LIMIT} 個まで")
        with self._lock:
            self._require_disconnected("拡張ヘッダの端子を駆動する操作")
            return {"levels": self._gpio().sequence(states)}

    def spi_transfer(self, sck: int, mosi: int | None = None, miso: int | None = None, cs: int | None = None,
                     data: str = "", read_length: int = 0, mode: int = 0, bits: int = 8, lsb_first: bool = False,
                     cs_active_high: bool = False, keep_cs: bool = False, stretch: int = 0,
                     fill: int | None = None) -> dict:
        """拡張ヘッダの端子で SPI の 1 回の転送（ビットバング）: CS を選ぶ → data の語を送りながら受ける →
        続けて read_length 語を fill（既定 全 1）を送りながら受ける → CS を離す（keep_cs なら選んだまま）。
        received は受けた全部の語（len(data の語) + read_length）。語は 16 進で 1 語 = ceil(bits / 8) バイト。"""
        pins = SpiPins(sck=sck, mosi=mosi, miso=miso, cs=cs)
        config = SpiConfig(mode=mode, bits=bits, lsb_first=lsb_first, cs_active_high=cs_active_high,
                           stretch=stretch)
        words = decode_words(data, bits)
        if len(words) + read_length > TRANSFER_LIMIT:
            raise ValueError(f"1 回の転送は {TRANSFER_LIMIT} 語まで（分けて keep_cs でつなぐ）")
        with self._lock:
            self._require_disconnected("拡張ヘッダの端子を駆動する操作")
            spi = SpiMaster(self._gpio(), pins, config)
            started = time.perf_counter()
            received = spi.transfer(words, read_length, fill=fill, keep_cs=keep_cs)
            elapsed = time.perf_counter() - started
        return {"received": encode_words(received, bits), "words": len(received), "states": spi.last_states,
                "elapsed_s": elapsed}
