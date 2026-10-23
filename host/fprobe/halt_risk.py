"""止めると壊れうるターゲットの手がかり（ELF のシンボルだけで。機器には触らない）。返すのは事実（ELF に、無線・USB・
ウォッチドッグ・RTOS の決まった名前と合うシンボルがあるか）だけで、「このターゲットは壊れる」とは判定しない（CLAUDE.md）。

コアを止める操作（中断・ブレークポイント・ステップ・関数の呼び出し）の間も、ターゲットの外の相手は待たない:
無線の相手（BLE の接続は監視時間で切れ、コントローラの時刻がずれて致命的エラーになる）、USB のホスト（応答しない
機器を切る）、ウォッチドッグ（止まっている間に満了しうる）、RTOS（ティック・タイムアウトがずれ、通信の相手と
食い違う）。どれも ELF に手がかりが残る: 割り込みの処理の名前（RADIO_IRQHandler 等）、スタックの関数の接頭辞
（Zephyr の BLE コントローラ ll_ / lll_ 等）、カーネルの大域変数（pxCurrentTCB・_kernel 等）。手がかりは
RTOS・スタック・ツールチェーンの共通の名前で、特定の機器の名前ではない。

止めずにデバッグする方式（モニタ方式。target.debug_mode）なら、DebugMon より優先度の高い割り込み（無線・USB）は
止まっている間も動く。ELF にモニタ（host/firmware/monitor の fprobe_monitor_init）があるかも返す。

    assess(Symbols("app.elf").names())
    # {"matched": True, "matches": [{"kind": "radio", "count": 12, "symbols": ["RADIO_IRQHandler", ...]}, ...],
    #  "monitor_in_elf": False}
"""

import re
from collections.abc import Iterable

from .symbols import Symbol

__all__ = ["EVIDENCE", "KINDS", "assess"]

EVIDENCE = 5                                   # 理由ごとに返す手がかりのシンボルの数

# 種類 → 手がかりの名前の形
_IRQ = r"_IRQHandler$"
KINDS: dict[str, list[re.Pattern]] = {
    # 無線: 無線の割り込み、BLE / 802.15.4 / Wi-Fi のスタックの関数
    "radio": [re.compile(rf"^(RADIO|RF\w*|BLE\w*|BT\w*|WIFI\w*|WLAN\w*|IEEE802154\w*|LORA\w*|SUBGHZ\w*){_IRQ}", re.I),
              re.compile(r"^(ll|lll|ull|radio|bt_ctlr|hci_driver|sdc|mpsl|btdm|nimble_port|ble_ll|ble_hs|sd_ble|"
                         r"nrf_802154|esp_wifi|wlan|lora)_\w")],
    # USB の機器（ホストが応答の無い機器を切る）
    "usb": [re.compile(rf"^(USB\w*|OTG\w*){_IRQ}", re.I), re.compile(r"^(tud|usbd|USBD|usb_dc|udc)_\w")],
    # ウォッチドッグ（止まっている間に満了しうる）
    "watchdog": [re.compile(rf"^(WDT|IWDG|WWDG|WDOG)\w*{_IRQ}", re.I),
                 re.compile(r"(^|_)(wdt|iwdg|wwdg|wdog|watchdog)(_|$)", re.I)],
}
# RTOS のカーネルの大域変数（FreeRTOS・Zephyr・ThreadX・RTX5・µC/OS・embOS・NuttX）
RTOS_GLOBALS = {"pxCurrentTCB": "FreeRTOS", "_kernel": "Zephyr", "_tx_thread_current_ptr": "ThreadX",
                "osRtxInfo": "RTX5", "OSTCBCurPtr": "uC/OS", "OS_Global": "embOS", "g_readytorun": "NuttX"}
ORDER = ("radio", "usb", "watchdog", "rtos")                    # 返す順（決まった順。重さの順ではない）
MONITOR = "fprobe_monitor_init"


def assess(symbols: Iterable[Symbol]) -> dict:
    """シンボルの並び → {"matched"（どれかの名前が合った）, "matches": [{"kind", "count", "symbols"（合ったシンボルの
    先頭）, "rtos"?}], "monitor_in_elf"}。matches は決まった順（radio → usb → watchdog → rtos）。"""
    found: dict[str, list[str]] = {k: [] for k in ORDER}
    rtos: str | None = None
    monitor = False
    for s in symbols:
        name = s.name
        if name == MONITOR:
            monitor = True
        if s.kind == "object" and name in RTOS_GLOBALS:
            found["rtos"].append(name)
            rtos = rtos or RTOS_GLOBALS[name]
            continue
        if s.kind != "function":
            continue
        for kind, patterns in KINDS.items():
            if any(p.search(name) for p in patterns):
                found[kind].append(name)
                break
    matches = []
    for kind in ORDER:
        names = sorted(set(found[kind]), key=lambda n: (not n.endswith("IRQHandler"), n))
        if names:
            match = {"kind": kind, "count": len(names), "symbols": names[:EVIDENCE]}
            if kind == "rtos":
                match["rtos"] = rtos
            matches.append(match)
    return {"matched": bool(matches), "matches": matches, "monitor_in_elf": monitor}
