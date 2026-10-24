"""プラグイン: ZMK（Zephyr）のキーボードを止めずに診断する。

本体の汎用の API（debug.evaluate の live・rtos.tasks の live）だけを使う。仕組みは fprobe.plugins の説明を参照。
ELF を読み込んで（debug.elf.load）、接続した（attach）後に使う。

    from fprobe.plugins import ProbeCaller
    from fprobe.plugins.zmk_health import ZmkHealth
    report = ZmkHealth(client).report()      # DaemonClient か ProbeCaller(probe)

読むもの（ZMK の版・構成で無い変数は unavailable に入れ、ほかは続ける）:
- BLE の接続（Zephyr の acl_conns[]）: 状態・役割・接続の間隔・peripheral latency・切断までの待ち・送信の PHY。
  split の右手なら、キーの状態が変わってから相手に届くまでに 0〜接続の間隔の待ちが加わる。
- チャタリング除けと走査（kscan の行列の設定 kscan_matrix_config_0）。ZMK は積算式で、しきい値に達した次の走査で切り替える。
- 電池の残量（last_state_of_charge）・起動からの時間（curr_tick / ticks_per_sec）・スタックの最大の使用量（rtos.tasks）。
所見（findings）: スタックの最大が 80% を超えたスレッド、電池 0%、接続していない BLE。
2026-10-20 に XIAO nRF52840（hhkb-split の右手）で手作業でした解析（docs/reports/2026-10-20-xiao-pynq-analysis.md）をまとめたもの。
"""

from typing import Any

from . import ApiCaller

__all__ = ["ZmkHealth"]

STACK_WARN_PERCENT = 80
_ROLES = {0: "central", 1: "peripheral"}
_PHYS = {1: "1M", 2: "2M", 4: "Coded"}
_MAX_CONNS = 8


class ZmkHealth:
    def __init__(self, api: ApiCaller, ticks_per_sec: int = 32768):
        self.api = api
        self.ticks_per_sec = ticks_per_sec            # CONFIG_SYS_CLOCK_TICKS_PER_SEC（nRF の既定 32768）
        self.unavailable: list[str] = []

    def _value(self, expression: str) -> Any:
        """列挙は列挙子の名前（列挙子に無い値は None）、数・真偽はその値。ほかの種類（読めない等）は誤り。"""
        value = self.api.call("debug.evaluate", expression=expression, live=True)["value"]
        if value["kind"] == "enum":
            return value["name"]
        if value["kind"] in ("int", "float", "bool"):
            return value["value"]
        raise ValueError(f"{expression} は数でない（{value.get('reason') or value['kind']}）")

    def _try(self, what: str, expression: str) -> Any:
        try:
            return self._value(expression)
        except Exception as e:                          # 版・構成で無い変数（API は理由つきの例外で返す）
            self.unavailable.append(f"{what}: {expression}（{str(e)[:80]}）")
            return None

    def ble(self) -> list[dict]:
        conns = []
        for i in range(_MAX_CONNS):
            base = f"acl_conns[{i}]"
            try:
                state = self._value(f"{base}.state")
            except Exception as e:
                if i == 0:
                    self.unavailable.append(f"BLE: {base}（{str(e)[:80]}）")
                break                                   # 配列の終わり
            conns.append({
                "index": i, "state": state,
                "role": _ROLES.get(self._value(f"{base}.role"), "?"),
                "interval_ms": self._value(f"{base}.le.interval") * 1.25,
                "latency": self._value(f"{base}.le.latency"),
                "timeout_ms": self._value(f"{base}.le.timeout") * 10,
                "tx_phy": _PHYS.get(self._value(f"{base}.le.phy.tx_phy"), "?"),
            })
        return conns

    def debounce(self) -> dict:
        cfg = "kscan_matrix_config_0"
        return {"press_ms": self._try("debounce", f"{cfg}.debounce_config.debounce_press_ms"),
                "release_ms": self._try("debounce", f"{cfg}.debounce_config.debounce_release_ms"),
                "scan_period_ms": self._try("debounce", f"{cfg}.debounce_scan_period_ms"),
                "poll_period_ms": self._try("debounce", f"{cfg}.poll_period_ms")}

    def stacks(self) -> list[dict]:
        out = []
        for t in self.api.call("rtos.tasks", live=True).get("tasks", []):
            st = t.get("stack") or {}
            if st.get("size") and st.get("peak_used") is not None:
                out.append({"name": t["name"], "size": st["size"], "peak": st["peak_used"],
                            "percent": round(100 * st["peak_used"] / st["size"], 1)})
        return out

    def report(self) -> dict:
        self.unavailable = []
        ble = self.ble()
        debounce = self.debounce()
        battery = self._try("電池", "last_state_of_charge")
        tick = self._try("起動からの時間", "curr_tick")
        stacks = self.stacks()
        findings = [f"スタック {s['name']}: 最大 {s['percent']}%（{s['peak']}/{s['size']} バイト）"
                    for s in stacks if s["percent"] > STACK_WARN_PERCENT]
        if battery == 0:
            findings.append("電池の残量 0% を送っている（電池が無い・切れている、または測り方の設定を確かめる）")
        findings += [f"BLE の接続 {c['index']} が {c['state']}" for c in ble if c["state"] != "BT_CONN_CONNECTED"]
        return {"ble": ble, "debounce": debounce, "battery_percent": battery,
                "uptime_s": None if tick is None else tick / self.ticks_per_sec, "stacks": stacks,
                "findings": findings, "unavailable": list(self.unavailable)}
