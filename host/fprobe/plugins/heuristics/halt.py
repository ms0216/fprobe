"""プラグイン heuristics: 止まり・故障の見方（スタックの警告・故障の典型の原因・止めて壊れる相手の名前・致命的エラーの関数）。"""

import re

from collections.abc import Mapping

from .. import ApiCaller
from ._common import _tag

GUARD_BYTES = 16                       # 底のこのバイト数が塗りのままかを見る（FreeRTOS の方法 2 と同じ幅）


def stack_warning(usage: Mapping, threshold: float = 0.9, guard: int = GUARD_BYTES) -> dict:
    """target.stack.usage の結果（size・used・untouched・fraction・overflow 等）に、使用率 threshold（既定 90 %）
    以上の警告と、底から guard（既定 16）バイト未満しか塗りが残っていなければ「あふれの疑い」を足す（d933ff7 より前の
    stackusage.py の measure_stack）。

    経験則: 90 % は余裕の目安。底の 16 バイトまで使ったなら、下端より下まで書いた（あふれた）かもしれないとみなす
    （FreeRTOS の方法 2 と同じ幅）。外れる場合: 塗りの値と同じ値を書いた所は使っていないと数える（使用量は過小）。
    16 バイト未満でもちょうど下端で止まっていればあふれていない。逆に塗りが残っていても、塗りを飛び越えて
    下の領域だけを書いた（大きな配列を確保して一部だけ書く）ならあふれていても見えない。"""
    untouched = usage["untouched"]
    suspected = untouched < guard or bool(usage.get("overflow"))
    fraction = usage.get("fraction", 0.0)
    notes = []
    if suspected:
        notes.append(f"底（下端）から {guard} バイト未満まで使った: あふれの疑い（下端より下の変数・ヒープが壊れて"
                     "いるかもしれない）")
    return _tag("stack_threshold_and_guard",
                f"使用率 {threshold:.0%} 以上は警告、底の {guard} バイトまで使ったらあふれの疑い",
                threshold=threshold, guard=guard, fraction=fraction, untouched=untouched,
                overflow_suspected=suspected, warning=fraction >= threshold or suspected, notes=notes)


# 9c7eccf より前の fault.py の hint（経験的な原因の候補を含む）
_FAULT_TYPICAL = {
    "IACCVIOL": "実行禁止（XN）の領域・MPU で許していない番地へ分岐した",
    "DACCVIOL": "MMFAR の番地（MMARVALID のとき）。NULL ポインタの参照が多い",
    "MUNSTKERR": "スタックの壊れ",
    "MSTKERR": "スタックのあふれ（スタックの下限を越えた）",
    "MLSPERR": "スタックのあふれ",
    "IBUSERR": "存在しない番地・電源や時計の止まった領域へ分岐した",
    "PRECISERR": "BFAR の番地（BFARVALID のとき）。積まれた PC が故障した命令",
    "IMPRECISERR": "書き込みバッファのため、積まれた PC は故障の後の命令。時計の入っていない周辺への書き込みが多い",
    "UNSTKERR": "スタックの壊れ",
    "STKERR": "スタックのあふれ（SRAM の外へ）",
    "LSPERR": "スタックのあふれ",
    "UNDEFINSTR": "壊れたコード・データ領域への分岐・別の CPU 向けのビルド",
    "INVSTATE": "番地の bit0 が 0 の関数ポインタへ分岐した（関数ポインタの壊れ・ベクタ表の bit0）",
    "INVPC": "LR の壊れ・スタックの壊れ",
    "NOCP": "CPACR の CP10・CP11 を有効にしていない（起動のコードで FPU を有効にする）",
    "STKOF": "スタックのあふれ",
    "UNALIGNED": "構造体のパック・ポインタの型の取り違え",
    "VECTTBL": "VTOR の値・ベクタ表の場所",
    "FORCED": "その故障が無効か、優先度が足りない。原因は CFSR を見る",
    "DEBUGEVT": "デバッガが無いのに BKPT を実行した等",
    "HardFault": "積まれた PC の命令と、その時のレジスタから推測する",
}


def fault_hints(report: Mapping) -> dict:
    """target.fault の結果の causes の各ビットに、典型の原因（typical）を足す（9c7eccf より前の fault.py の hint）。
    本体の hint（アーキテクチャの定義から言えること）はそのまま残す。

    経験則: 「NULL 参照が多い」「スタックのあふれ」等は、よくある原因の見込みで、そのビットの定義ではない。外れる
    場合: 同じビットは別の原因でも立つ（例 STKERR はスタックのあふれのほか、SP を壊した・MSP の初期値の誤り）。
    原因は積まれたフレーム（frame）・故障の番地で確かめる。"""
    causes = []
    for c in report.get("causes", []):
        c = dict(c)
        typical = _FAULT_TYPICAL.get(c.get("bit"))
        if typical:
            c["typical"] = typical
        causes.append(c)
    return _tag("fault_typical_causes", "故障のビットごとの、よくある原因の見込み（そのビットの定義ではない）",
                causes=causes, exception=report.get("exception"), faulted=report.get("faulted"))


_IRQ = r"_IRQHandler$"


# 種類 → 関数の名前の形（本体の halt_risk.py の KINDS だったもの。関数だけに当てる）
HALT_RISK_PATTERNS: dict[str, list[str]] = {
    # 無線: 無線の割り込み、BLE / 802.15.4 / Wi-Fi のスタックの関数
    "radio": [rf"(?i)^(RADIO|RF\w*|BLE\w*|BT\w*|WIFI\w*|WLAN\w*|IEEE802154\w*|LORA\w*|SUBGHZ\w*){_IRQ}",
              r"^(ll|lll|ull|radio|bt_ctlr|hci_driver|sdc|mpsl|btdm|nimble_port|ble_ll|ble_hs|sd_ble|"
              r"nrf_802154|esp_wifi|wlan|lora)_\w"],
    # USB の機器（ホストが応答の無い機器を切る）
    "usb": [rf"(?i)^(USB\w*|OTG\w*){_IRQ}", r"^(tud|usbd|USBD|usb_dc|udc)_\w"],
    # ウォッチドッグ（止まっている間に満了しうる）
    "watchdog": [rf"(?i)^(WDT|IWDG|WWDG|WDOG)\w*{_IRQ}", r"(?i)(^|_)(wdt|iwdg|wwdg|wdog|watchdog)(_|$)"],
}


# RTOS のカーネルの大域変数（変数だけに当てる）
RTOS_GLOBALS = {"pxCurrentTCB": "FreeRTOS", "_kernel": "Zephyr", "_tx_thread_current_ptr": "ThreadX",
                "osRtxInfo": "RTX5", "OSTCBCurPtr": "uC/OS", "OS_Global": "embOS", "g_readytorun": "NuttX"}


_HALT_ORDER = ("radio", "usb", "watchdog", "rtos")


def halt_risk_kinds(result: Mapping) -> dict:
    """debug.halt_risk の結果（HALT_RISK_PATTERNS・RTOS_GLOBALS を条件にしたもの）を、種類（radio / usb / watchdog /
    rtos）ごとに分ける（2026-10-23 より前の本体の halt_risk.assess）。関数の形は関数のシンボルだけ、RTOS の名前は
    変数だけに当て、1 つのシンボルは先の種類（radio → usb → watchdog）に入れる。

    経験則: 関数名の接頭辞・部分一致（ll_ = BLE のコントローラ、名前に wdt を含む = ウォッチドッグ 等）は RTOS・スタックの
    名付けの慣習で、その名前の関数がそれをする保証は無い。外れる場合: 利用者の関数が同じ接頭辞を使う（ll_ = linked
    list 等）、名前を変えたスタック、慣習に無いスタック。止めて壊れるかは、そのシンボルの中身（割り込みを有効にして
    いるか・相手がいるか）で確かめる。"""
    found: dict[str, set[str]] = {k: set() for k in _HALT_ORDER}
    rtos: str | None = None
    kind_of_pattern = {p: k for k, ps in HALT_RISK_PATTERNS.items() for p in ps}
    claimed: dict[str, str] = {}
    for m in result.get("matches", []):
        for s in m["symbols"]:
            if m["by"] == "name" and m["query"] in RTOS_GLOBALS and s["kind"] == "object":
                found["rtos"].add(s["name"])
                rtos = rtos or RTOS_GLOBALS[m["query"]]
            elif m["by"] == "pattern" and m["query"] in kind_of_pattern and s["kind"] == "function":
                kind = kind_of_pattern[m["query"]]
                if _HALT_ORDER.index(kind) < _HALT_ORDER.index(claimed.get(s["name"], "rtos")):
                    claimed[s["name"]] = kind
    for name, kind in claimed.items():
        found[kind].add(name)
    reasons = []
    for kind in _HALT_ORDER:
        names = sorted(found[kind], key=lambda n: (not n.endswith("IRQHandler"), n))
        if names:
            reason = {"kind": kind, "count": len(names), "symbols": names}
            if kind == "rtos":
                reason["rtos"] = rtos
            reasons.append(reason)
    return _tag("halt_risk_name_conventions", "関数名の接頭辞・部分一致と RTOS の大域変数の名前で、止めている間も"
                "待たない相手（無線・USB のホスト・ウォッチドッグ・RTOS）の処理が ELF にあると見る",
                risky=bool(reasons), reasons=reasons, monitor_in_elf=result.get("monitor_in_elf"))


def halt_risk_conventions(caller: ApiCaller) -> dict:
    """debug.halt_risk.set に HALT_RISK_PATTERNS と RTOS_GLOBALS を渡し（デーモンの条件を置き換える。画面の警告も
    この条件で出る）、debug.halt_risk の結果を halt_risk_kinds で分ける。"""
    caller.call("debug.halt_risk.set", names=list(RTOS_GLOBALS),
                patterns=[p for ps in HALT_RISK_PATTERNS.values() for p in ps])
    return halt_risk_kinds(caller.call("debug.halt_risk"))


# 止まって回り続ける関数（本体の liveness.py の FATAL_FUNCTIONS だったもの）
FATAL_FUNCTIONS = frozenset({
    "arch_system_halt", "k_fatal_halt", "z_fatal_halt",                                   # Zephyr
    "vAssertCalled", "vApplicationStackOverflowHook", "vApplicationMallocFailedHook",     # FreeRTOS の慣習のフック
    "abort", "_exit", "__assert_func", "__assert_fail", "__stack_chk_fail",              # C のランタイム
    "rust_begin_unwind", "panic", "__rust_start_panic",                                   # Rust
    "Error_Handler", "osRtxErrorNotify", "_tx_thread_system_error",                       # CMSIS の生成コード・RTX・ThreadX
})


# 故障の処理（CMSIS の名前・cortex-m-rt・既定の処理。本体の liveness.py の FAULT_HANDLERS だったもの）
FAULT_HANDLER_PATTERN = r"(?i)^(HardFault|MemManage|BusFault|UsageFault|SecureFault)(_Handler)?$|^Default_?Handler_?$"


_ASSUME_FATAL = ("Error_Handler・vAssertCalled・panic・HardFault_Handler 等の名前の関数は、致命的エラーの後に止まって"
                 "回り続ける処理（RTOS・ランタイム・CubeMX の生成コードの慣習）")


def liveness_conventions(caller: ApiCaller, pc_samples: int = 4) -> dict:
    """target.liveness.set に FATAL_FUNCTIONS と FAULT_HANDLER_PATTERN を渡す（デーモンの見張りの条件を置き換える。
    以後 PC の見本がその中にあれば target.status の fatal が kind = pc_in になる。fatal_kind で種類に分ける）。
    pc_samples の 4 は 2026-10-23 より前の本体の固定値。

    経験則: 中身を利用者が書く関数（Error_Handler・vAssertCalled・panic 等）も含む。外れる場合: その関数が戻る・
    ログを出して続ける実装、名前を変えた処理（その名前の関数の中にいても致命的とは限らず、別の名前で止まって
    回っていれば見つけない）。"""
    settings = caller.call("target.liveness.set", functions=sorted(FATAL_FUNCTIONS), patterns=[FAULT_HANDLER_PATTERN],
                           pc_samples=pc_samples)
    return _tag("fatal_function_names", _ASSUME_FATAL, settings=settings)


def fatal_kind(fatal: Mapping) -> dict:
    """target.status の fatal（liveness_conventions の条件で見たもの）の kind = pc_in を、関数の名前で fault_loop
    （故障の処理）か fatal_halt（致命的エラーの処理）に分ける（2026-10-23 より前の本体の kind）。ほかの kind はそのまま。"""
    kind = fatal.get("kind")
    if kind == "pc_in":
        names = [n.strip() for n in str(fatal.get("function", "")).split(",")]
        kind = "fault_loop" if any(re.search(FAULT_HANDLER_PATTERN, n) for n in names) else "fatal_halt"
    return _tag("fatal_function_names", _ASSUME_FATAL, kind=kind, fatal=dict(fatal))
