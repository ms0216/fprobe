"""プラグイン heuristics: 接続・診断の見方（診断の判定・原因の候補・JTAG の IR 長・接続スクリプトの名前の候補）。"""

import difflib

from collections.abc import Mapping, Sequence

from ._common import _tag

def diagnose_hints(result: Mapping, min_volts: float = 1.0, low_volts: float = 1.65,
                   high_volts: float = 3.6) -> dict:
    """target.diagnose の結果の steps に、電圧の判定と原因の候補（typical）を足す（9331a9b・bb73f1c より前の
    diagnose.py の文）。

    経験則:
    - 電圧: min_volts（1.0 V）未満は電源が入っていない、low_volts（1.65 V）未満はレベル変換が要る（fprobe の入出力は
      3.3 V）、high_volts（3.6 V）超は fprobe を壊す。ただしターゲットが SWD・JTAG に応答したのに 1.0 V 未満なら、
      電源ではなく電圧の計測の配線（分圧・補助マイコン）を疑う（2026-10-18 の実機で、補助の Pico の計測の線が外れて
      いたのに「電源が入っていない」と誤って結論した）。
    - 原因の候補: SWD の無応答 → 配線・電源・GND・SWD が無効、パリティ誤り → 信号品質、WAIT → 低消費電力・クロック
      停止、低い速さでだけ応答 → 配線長・GND・信号品質、デバッグ電源の無応答 → 深い低消費電力・電源ドメイン・保護、
      ピンが Low → プルアップ・短絡・リセットの押さえ。
    外れる場合: どれもよくある原因の見込みで、測った事実ではない（例: 計測の線が外れていても 0 V になる）。"""
    steps = [dict(s) for s in result.get("steps", [])]
    responded = any(s["name"] in ("swd", "jtag") and s["status"] in ("ok", "warn") for s in steps)
    for s in steps:
        typical: list[str] = []
        data = s.get("data", {})
        msg = s.get("message", "")
        if s["name"] == "voltage":
            volts = data.get("volts")
            if data.get("over_range"):
                typical.append("分圧の配線を確かめる")
            elif s["status"] == "ok" and volts is not None:
                if volts < min_volts:
                    typical.append("電圧の計測は低いが、ターゲットは応答している: 電圧の計測の配線（分圧・補助マイコン）"
                                   "を確かめる" if responded else
                                   f"ターゲットの電源が入っていない（{min_volts} V 未満）")
                elif volts < low_volts:
                    typical.append(f"ターゲットの電圧が低い（{low_volts} V 未満）: fprobe の入出力は 3.3 V。レベル変換が要る")
                elif volts > high_volts:
                    typical.append(f"ターゲットの電圧が高い（{high_volts} V 超）: 3.3 V を超える入力は fprobe を壊す")
        elif s["name"] == "pins":
            if data.get("nreset") == 0:
                typical.append("nRESET が Low: リセットが掛かったまま。外部で押さえていないか・プルアップ")
            if data.get("swdio") == 0:
                typical.append("SWDIO が Low: ターゲットのプルアップが無い・短絡・電源が無い")
        elif s["name"] == "swd":
            if msg.startswith("SWD に応答が無い"):
                typical.append("配線・電源・GND・SWD が無効のターゲット")
            elif "パリティ誤り" in msg:
                typical.append("信号品質: 配線長・GND・速度")
            elif "WAIT を返し続ける" in msg:
                typical.append("ターゲットが低消費電力・クロック停止")
            elif "では応答せず" in msg:
                typical.append("配線長・GND・信号品質を疑う")
        elif s["name"] == "power" and s["status"] == "fail" and "CDBGPWRUPACK" in msg:
            typical.append("ターゲットが深い低消費電力・電源ドメインが落ちている・保護")
        if typical:
            s["typical"] = typical
    return _tag("diagnose_typical_causes",
                f"電圧 {min_volts} V 未満は電源なし・{low_volts} V 未満は低い・{high_volts} V 超は高い。"
                "各段の失敗によくある原因の見込み",
                min_volts=min_volts, low_volts=low_volts, high_volts=high_volts, reached=result.get("reached"),
                steps=steps)


_SOFT_FAILS = ("part", "ap_idr", "core", "protection")    # 読めなくても接続はできうる段（以前の本体の warn）


def _diagnose_severity(s: Mapping, first_hz: int | None) -> str:
    status, name, data = s.get("status"), s.get("name"), s.get("data", {})
    if status == "skip":
        return "skip"
    if status == "fail":
        return "warn" if name in _SOFT_FAILS else "fail"
    if name == "pins":
        return "warn" if data.get("nreset") == 0 or data.get("swdio") == 0 else "ok"
    if name == "voltage":
        return "warn" if data.get("over_range") else "ok"
    if name in ("swd", "jtag"):
        hz = data.get("frequency_hz")
        return "warn" if hz is not None and first_hz is not None and hz != first_hz else "ok"
    if name in ("dp_errors", "ap_disabled"):
        return "warn"
    if name == "power":
        return "ok" if data.get("system_power", True) else "warn"
    if name == "core":
        return "warn" if data.get("lockup") else "ok"
    if name == "protection":
        level = data.get("rdp_level")
        return "fail" if level == 2 else "warn" if level == 1 or data.get("approtect") else "ok"
    return "ok"


def diagnose_verdict(result: Mapping) -> dict:
    """target.diagnose の事実（段ごとの status = ok / fail / skip と data）に、以前の本体がしていた良し悪しの判定
    （各段の severity = ok / warn / fail）・結論の文（verdict）・接続できるか（ok）・target_connect の引数のおすすめ
    （suggested_connect）を足す。

    経験則（以前の本体の判定）:
    - warn: ピンの nRESET・SWDIO が Low、電圧が測れる上限を超えた、最初に試した速さで応答せず後の速さで応答した、
      DP に誤りの印が残っていた、システム電源の応答が無い、MEM-AP が無効、コアがロックアップ、STM32 の RDP Level 1・
      nRF の APPROTECT、部品・コア・AP の IDR・保護を読めなかった（接続はできうる）。fail: RDP Level 2、ほかの読めない段。
    - ok（接続できる）= DP に届き（reached）、fail の段が無い。
    - おすすめの接続: 部品の候補がちょうど 1 つならその名前、無ければ汎用の cortex_m。応答した速さ。JTAG ならチェーン。
      ロックアップなら connect_mode = under-reset（眠っている（WFI）は勧めない: 止めると壊れるターゲットがある）。
    外れる場合: Low のピン・遅い速さが正常な配線もある。候補が 1 つでも別の品種のことがある（DBGMCU の値の表）。
    ロックアップでもリセットしないほうがよい（状態を調べたい）こともある。"""
    first = (result.get("frequencies_hz") or [None])[0]
    steps = [{**s, "severity": _diagnose_severity(s, first)} for s in result.get("steps", [])]
    reached = bool(result.get("reached"))
    fails = [s for s in steps if s["severity"] == "fail"]
    ok = reached and not fails
    part, core = result.get("part"), result.get("core")
    if not ok:
        head = fails[0]["message"] if fails else "ターゲットのデバッグポートに届かなかった"
        volts = next((s.get("data", {}).get("volts") for s in steps if s["name"] == "voltage" and s["status"] == "ok"),
                     None)
        verdict = f"接続できない: {head}" + ("" if volts is None else f"（Vtarget = {volts:.2f} V）")
    else:
        what = part["name"] if part else core["core"] if core else "ターゲット"
        verdict = f"接続できる（{what}、{str(result.get('protocol')).upper()} {result.get('frequency_hz'):,} Hz）"
        warns = [s for s in steps if s["severity"] == "warn"]
        if warns:
            verdict += "。ただし: " + " / ".join(s["message"] for s in warns)
    connect = None
    if reached:
        connect = {"target": (part or {}).get("suggested_target") or "cortex_m", "frequency_hz": result.get("frequency_hz")}
        chain = (result.get("jtag") or {}).get("jtag_chain")
        if result.get("protocol") == "jtag" and chain:
            connect["jtag_chain"] = chain
        if core and core.get("lockup"):
            connect["connect_mode"] = "under-reset"
    return _tag("diagnose_verdict",
                "以前の本体の判定: Low のピン・遅い速さ・ロックアップ・保護等を warn、DP に届いて fail が無ければ接続できる。"
                "おすすめの接続は部品の候補が 1 つならその名前、ロックアップなら under-reset",
                ok=ok, verdict=verdict, suggested_connect=connect, steps=steps)


_ACK_CAUSES = {"WAIT": ["低消費電力", "クロック停止", "バスが塞がっている"],
               "FAULT": ["前の誤りの印が残っている", "アクセスが拒まれた"],
               "NO_ACK": ["配線", "電源", "速度", "線のプロトコルの選択"],
               "PROTOCOL": ["信号品質: 速度を下げる・GND・配線長"]}


_TDO_CAUSES = {"stuck_low": ["TDO の線", "ターゲットの電源", "TRST"],
               "stuck_high": ["TDO が浮いてプルアップ", "TCK / TMS の配線", "JTAG が無効のターゲット"]}


def link_cause_hints(ack: str | None = None, tdo: str | None = None) -> dict:
    """SWD/JTAG の ACK（WAIT / FAULT / NO_ACK / PROTOCOL）や jtag.scan の tdo（stuck_low / stuck_high）に、よくある原因を
    並べる（27cdb7e より前の本体の文）。順に確かめる候補で、どれかである保証は無い。"""
    causes = _ACK_CAUSES.get(ack or "", []) + _TDO_CAUSES.get(tdo or "", [])
    return _tag("link_causes", "よくある原因の一覧。ほかの原因もありうる", ack=ack, tdo=tdo, causes=causes)


def jtag_guess_ir(scan: Mapping) -> dict:
    """jtag.scan の Capture-IR（ir_capture・total_ir_length）を、境目が一つに決まらない装置では最短の候補を取って
    装置に分け直し、jtag_chain を作る（27cdb7e より前の本体の推測）。表の IR 長（ir_length_source="table"）は使う。
    guessed = 最短の候補を取った装置。外れる場合: IR の Capture 値に境目と同じ形（…01）が並ぶ装置。データシートの値が
    あればそちらを jtag_chain に与える。"""
    total, capture = scan.get("total_ir_length"), scan.get("ir_capture")
    chain, guessed = [], []
    if total is not None and capture is not None:
        starts = sorted(k for k in range(total) if (capture >> k) & 1 and not (capture >> (k + 1)) & 1)
        devices = scan["devices"]
        known = [d["ir_length"] if d.get("ir_length_source") == "table" else None for d in devices]
        pos = 0
        for i, n in enumerate(known):
            rest = known[i + 1:]
            if n is None:
                if not rest or None not in rest:
                    n = total - pos - sum(rest)
                else:
                    candidates = [k for k in starts if k >= pos + 2]
                    if not candidates:
                        break
                    n = candidates[0] - pos
                    if len(candidates) > len(rest):
                        guessed.append(devices[i]["index"])
            chain.append(n)
            pos += n
        if len(chain) != len(devices) or pos != total or min(chain, default=2) < 2:
            chain = None
    else:
        chain = None
    return _tag("ir_shortest", "Capture-IR の境目の候補のうち、最短が本当の IR 長",
                jtag_chain=chain, guessed=guessed)


def connect_script_name_hints(check: Mapping, cutoff: float = 0.8,
                              prefixes: Sequence[str] = ("will_", "did_")) -> dict:
    """target.script.check の結果の functions（pyOCD の手順の名前でない関数 = pyOCD は呼ばない）から、手順の名前の
    書き誤りかもしれないものを挙げる（以前は本体の check_script が problems に入れていたもの）。

    経験則:
    - 名前の似ている度合い（difflib の比）が cutoff（既定 0.8）以上の手順の名前があれば、そのつもりだったとみなし
      close に入れる。
    - prefixes（既定 will_ / did_。pyOCD の手順の名前の付け方）で始まる関数は、似た名前が無くても手順のつもりとみなす。
    外れる場合: 手順と似た名前の補助の関数（will_connect_helper 等）を誤りに挙げる。似ていない書き誤り（短い名前の
    大きな誤り）は挙がらない。"""
    from ...connect_scripts import HOOKS
    hints = []
    for f in check.get("functions", []):
        close = difflib.get_close_matches(f["name"], HOOKS, n=1, cutoff=cutoff)
        if close or f["name"].startswith(tuple(prefixes)):
            hints.append({"name": f["name"], "line": f.get("line"), "close": close[0] if close else None})
    return _tag("hook_name_similarity",
                f"名前の似ている度合い {cutoff} 以上か、接頭辞 {', '.join(prefixes) or '（なし）'} の関数は手順のつもり",
                cutoff=cutoff, prefixes=list(prefixes), hints=hints)
