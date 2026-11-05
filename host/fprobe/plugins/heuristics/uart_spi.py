"""プラグイン heuristics: ロジアナの解読の見方（UART のボーの推定・SPI Flash のよく見る命令の表）。"""

import math
import tempfile

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .. import ApiCaller
from ._common import _tag

STANDARD_BAUDS = (300, 600, 1200, 2400, 4800, 9600, 14400, 19200, 28800, 38400, 57600, 76800, 115200, 128000,
                  230400, 250000, 256000, 460800, 500000, 576000, 921600, 1_000_000, 1_152_000, 1_500_000,
                  2_000_000, 2_500_000, 3_000_000, 3_500_000, 4_000_000, 6_000_000, 8_000_000, 10_000_000, 12_000_000)


def estimate_baud(transitions, channel: int, *, snap: float | None = 0.03, noise: float = 0.01,
                  tolerance: float = 0.2, max_bits: int = 10,
                  standard: Sequence[float] = STANDARD_BAUDS) -> dict:
    """同じ値が続いた長さ（fprobe.la.Transitions の 1 チャンネル）から 1 ビットの長さを推定する（73d5717 より前の
    decode/uart.py の estimate_baud）。

    経験則:
    - 短い順に並べた区間のうち、下位 noise（既定 1 %。少なくとも 2 個目）より短いものは雑音とみなす。
    - その長さの 0.5〜1.5 倍の区間を 1 ビットとみなして平均し、各区間を整数ビット（1〜max_bits）に丸めて ±tolerance
      （既定 0.2 ビット）に入るものだけをビット数の合計で割る（停止ビット 1.5・フレームの間の休止を除く）。
    - snap（既定 3 %）以内に標準のボーレートがあれば、その値に寄せる（None で寄せない）。
    外れる場合: 1 ビットの並びが 1 つも無いデータ（例: 2 ビットずつしか変わらない）では倍の長さを 1 ビットと見る。
    雑音が 1 % より多い・グリッチがある・別のボーの通信が混ざると外れる。標準値から 3 % 以内の非標準のボーは標準値に
    寄ってしまう。
    返す: baud（寄せた後。推定できなければ None）・raw_baud（寄せる前）・snapped。"""
    from ...decode.core import ChannelLevels
    edges = ChannelLevels(transitions, channel).edges
    runs = sorted(edges[i + 1] - edges[i] for i in range(len(edges) - 1))   # 両端の切れた区間は使わない
    assumption = (f"下位 {noise:.0%} の区間は雑音、最短の 0.5〜1.5 倍が 1 ビット、整数ビットから ±{tolerance} の区間"
                  f"だけを使う" + ("" if snap is None else f"、標準値から {snap:.0%} 以内なら標準値に寄せる"))
    none = _tag("uart_baud_estimate", assumption, channel=channel, baud=None, raw_baud=None, snapped=False)
    if len(runs) < 2:
        return none
    k = max(2, math.ceil(noise * len(runs)))
    if len(runs) < k:
        return none
    rough = runs[k - 1]
    ones = [r for r in runs if 0.5 * rough <= r <= 1.5 * rough]
    unit = sum(ones) / len(ones)
    total, bits = 0, 0
    for r in runs:
        n = round(r / unit)
        if 1 <= n <= max_bits and abs(r / unit - n) <= tolerance:
            total += r
            bits += n
    raw = bits / (total * transitions.sample_period_s)
    baud, snapped = raw, False
    if snap is not None:
        nearest = min(standard, key=lambda b: abs(b - raw))
        if abs(nearest - raw) <= snap * nearest:
            baud, snapped = nearest, True
    return {**none, "baud": baud, "raw_baud": raw, "snapped": snapped}


def decode_uart_with_estimated_baud(caller: ApiCaller, channel: int, *, snap: float | None = 0.03,
                                    work_dir: str | Path | None = None, **settings: Any) -> dict:
    """最後の捕捉（la.capture 等）の channel からボーを estimate_baud で推定し、その baud で la.decode（uart）を呼ぶ。

    最後の捕捉の波形は la.export（format sr）でファイルに書いて fprobe.sigrok.read_sr で読む（デーモンと同じ計算機の
    ファイルを読む。work_dir を省くと一時ディレクトリ）。settings は la.decode の uart の枠の設定（data_bits・parity・
    stop_bits・invert・lsb_first。既定を持たないので渡す。推定するのはボーだけ）と la.decode の引数（start・end・
    kinds・errors_only・offset・limit）。
    経験則・外れる場合は estimate_baud と同じ。推定できなければ la.decode を呼ばず decoding = None。"""
    from ...sigrok import read_sr
    decode_keys = {"start", "end", "kinds", "errors_only", "offset", "limit", "capture_id"}
    uart = {k: v for k, v in settings.items() if k not in decode_keys}
    extra = {k: v for k, v in settings.items() if k in decode_keys}
    with tempfile.TemporaryDirectory(dir=work_dir) as tmp:
        path = Path(tmp) / "capture.sr"
        caller.call("la.export", path=str(path), format="sr")
        transitions, _names = read_sr(path)
    estimate = estimate_baud(transitions, channel, snap=snap)
    out = dict(estimate)
    out["decoding"] = None
    if estimate["baud"] is not None:
        out["decoding"] = caller.call("la.decode", protocol="uart",
                                      settings={"channel": channel, "baud": estimate["baud"], **uart}, **extra)
    return out


def _flash_command(name: str, address_bytes: int | None, dummy: int, direction: str | None, write: bool = False) -> dict:
    return {"name": name, "address_bytes": address_bytes, "dummy": dummy, "direction": direction, "write": write}


# 2026-10-24 より前の本体の decode/spiflash.py の表のうち、JEDEC で値の決まっていない命令（Winbond W25Q 系等でよく見る値）
SPI_FLASH_CONVENTIONAL_COMMANDS = {
    "0x35": _flash_command("READ_STATUS2", 0, 0, "r"), "0x15": _flash_command("READ_STATUS3", 0, 0, "r"),
    "0x31": _flash_command("WRITE_STATUS2", 0, 0, "w", True), "0x11": _flash_command("WRITE_STATUS3", 0, 0, "w", True),
    "0x90": _flash_command("READ_MANUFACTURER_ID", 3, 0, "r"), "0xAB": _flash_command("RELEASE_POWER_DOWN", 0, 3, "r"),
    "0x4B": _flash_command("READ_UNIQUE_ID", 0, 4, "r"), "0xB9": _flash_command("POWER_DOWN", 0, 0, None),
    "0x20": _flash_command("SECTOR_ERASE_4K", None, 0, None, True), "0x21": _flash_command("SECTOR_ERASE_4K_4B", 4, 0, None, True),
    "0x52": _flash_command("BLOCK_ERASE_32K", None, 0, None, True), "0xD8": _flash_command("BLOCK_ERASE_64K", None, 0, None, True),
    "0xDC": _flash_command("BLOCK_ERASE_64K_4B", 4, 0, None, True), "0xC7": _flash_command("CHIP_ERASE", 0, 0, None, True),
    "0x60": _flash_command("CHIP_ERASE", 0, 0, None, True),
    "0x75": _flash_command("SUSPEND", 0, 0, None), "0x7A": _flash_command("RESUME", 0, 0, None),
    "0x3B": _flash_command("DUAL_OUTPUT_READ", None, 1, "multi"), "0x6B": _flash_command("QUAD_OUTPUT_READ", None, 1, "multi"),
    "0xBB": _flash_command("DUAL_IO_READ", None, 0, "multi"), "0xEB": _flash_command("QUAD_IO_READ", None, 0, "multi"),
    "0x32": _flash_command("QUAD_PAGE_PROGRAM", None, 0, "multi", True),
}


def spiflash_conventional_commands() -> dict:
    """SPI Flash の解読（la の spiflash）の extra_commands に渡す、よく見る命令の表（2026-10-24 より前の本体の既定の表から
    JEDEC で決まった命令を除いたもの）。

    仮定: 機器がこの値をこの意味で使う。外れる場合: 製造元で意味が違う（35h・15h・31h・11h は第 2 / 第 3 の状態の
    レジスタとは限らない、一時停止は B0h / 30h の機器もある、消去の命令と大きさは SFDP で機器が申告する）と、名前・
    アドレスとデータの切り分け・write_disabled の誤りが誤って付く。使う機器のデータシートか SFDP で確かめる。"""
    return _tag("spiflash_conventional_opcodes", "SPI Flash の機器がこれらの命令の値をこの意味で使う（W25Q 系等でよく見る値）",
                commands={k: dict(v) for k, v in SPI_FLASH_CONVENTIONAL_COMMANDS.items()})
