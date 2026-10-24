"""プラグイン: STM32F2 / F4 / F7 の Flash の書き込みアルゴリズムを、走っていたプログラムのクロックのまま動かさない守り
（以前は本体の fprobe.flash にあった機器固有の処理。3 回目の点検 ?18 で外した）。

本体の汎用の口（flash.algorithm_guard.set = 書き込みアルゴリズムの間のレジスタの手順。fprobe.flash.RegisterGuard）に、
このチップの手順を渡すだけ。仕組みは fprobe.plugins の説明を参照。

    from fprobe.plugins import ProbeCaller
    from fprobe.plugins import stm32_flash_clock
    stm32_flash_clock.install(client, "f4")            # DaemonClient か ProbeCaller(probe)

なぜ要るか: 標準のアルゴリズム（Keil の FLM 由来）はリセット直後の HSI を前提に、Init で FLASH_ACR の待ちを 0 にする。
PLL（例 84 MHz）のまま動かすと Flash の読みが化けて書き込みが失敗し、戻ったプログラムも待ち 0 のまま走って LOCKUP する
（2026-10-18 に実機で）。アルゴリズムの間は SYSCLK を HSI にし、抜けるときに待ち → クロックの順に戻す。pyOCD は書く前に
リセットするので要らない（fprobe はプログラムを止めずに書くので要る）。抜けるときに ART のキャッシュも捨てる: 書き換えた
Flash の古い命令を実行し続ける（2026-10-18 に実機で、戻した BKPT で止まり続けた）。捨てるのはキャッシュを止めている間
だけできる（RM0368 3.5.2）。
"""

from typing import Any

from . import ApiCaller

__all__ = ["FAMILIES", "guard", "guard_for_part", "install"]

RCC_CR, RCC_CFGR, FLASH_ACR = 0x40023800, 0x40023808, 0x40023C00
HSION, HSIRDY = 1 << 0, 1 << 1
SW, SWS = 0x3, 0xC                                   # RCC_CFGR: SW = 0 / SWS = 0 が HSI
CACHES_ON = 3 << 9                                   # FLASH_ACR の ICEN・DCEN（F7 は ARTEN と予約）
CACHE_RESET = {"f2": 3 << 11, "f4": 3 << 11, "f7": 1 << 11}   # F2/F4: ICRST・DCRST、F7: ARTRST
FAMILIES = tuple(CACHE_RESET)
ALL = 0xFFFFFFFF


def guard(family: str) -> dict:
    """STM32 の系列（"f2" / "f4" / "f7"。参照マニュアルの RCC・FLASH の番地とビット）の守りの手順
    （flash.algorithm_guard.set の guard）。"""
    if family not in CACHE_RESET:
        raise ValueError(f"系列は {' / '.join(FAMILIES)}: {family!r}")
    reset = CACHE_RESET[family]
    return {
        "enter": [
            {"op": "write", "address": RCC_CR, "mask": HSION, "value": HSION},        # HSI を点けて
            {"op": "wait", "address": RCC_CR, "mask": HSIRDY, "value": HSIRDY},
            {"op": "write", "address": RCC_CFGR, "mask": SW, "value": 0},             # SYSCLK = HSI
            {"op": "wait", "address": RCC_CFGR, "mask": SWS, "value": 0},
        ],
        "exit": [
            {"op": "write", "address": FLASH_ACR, "mask": CACHES_ON, "value": 0},     # キャッシュを止めて捨てる
            {"op": "write", "address": FLASH_ACR, "mask": reset, "value": reset},
            {"op": "write", "address": FLASH_ACR, "mask": reset, "value": 0},
            {"op": "restore", "address": FLASH_ACR, "mask": ALL},                    # 先に待ちを戻す（速いクロックの前に）
            {"op": "restore", "address": RCC_CFGR, "mask": ALL},
            {"op": "wait_restored", "address": RCC_CFGR, "mask": SWS},
            {"op": "restore", "address": RCC_CR, "mask": HSION},                     # 点いていなかった HSI は消す
        ],
    }


def guard_for_part(part_number: str | None) -> dict:
    """品種名の前置き（STM32F2 / STM32F4 / STM32F7）から系列を選ぶ（以前の本体の applies と同じ）。当たらなければ
    guard = None。

    経験則: pyOCD のターゲットの part_number が STM32F2 / F4 / F7 で始まれば、その系列の RCC・FLASH の番地とビット。
    外れる場合: 名前は利用者・パックが付ける（別の名前の同じチップ、同じ前置きの違うレジスタの配置）。系列が分かって
    いるなら guard(family) を直接使う。"""
    part = str(part_number or "").upper()
    family = next((f for f in FAMILIES if part.startswith(f"STM32{f.upper()}")), None)
    return {"heuristic": "stm32_part_prefix", "assumption": "品種名が STM32F2 / F4 / F7 で始まればその系列のレジスタ",
            "family": family, "guard": guard(family) if family else None}


def install(caller: ApiCaller, family: str) -> Any:
    """その系列の守りを本体に渡す（flash.algorithm_guard.set）。外すのは call("flash.algorithm_guard.set", guard=None)。"""
    return caller.call("flash.algorithm_guard.set", guard=guard(family))
