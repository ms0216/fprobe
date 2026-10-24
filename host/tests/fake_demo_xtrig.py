"""機器なしで画面を試すデモ（fake_daemon.py --demo）の一部: クロストリガーの窓（源 × 動作の経路）が読む源・行き先の偽物。

DemoProbe（fake_demo.py）に混ぜる mixin。値の形は本物の Probe（mcp_server.py の fpga_obs_trigger_* / sampler_trigger_*、
probe_xtrig_out.py の xtrig_output_*）と同じにする（画面はデモと本物を区別しない）。経路（xtrig.route・xtrig.status の
route）は fake_demo_search.SearchDemo の self._xtrig["route"] を共有し、本物と同じく FPGA の条件・サンプラの値の一致の
設定はその源のビットだけを変える。

- ケイパビリティ: クロストリガー・多段・割り込み書き込み・FPGA の条件・サンプラの値の一致・出力の端子 4 本（動作 2 つ）。
- 初めの状態（窓に見せる値）: 経路 sequence → ロジアナ、dap_match → 割り込み書き込み、サンプラの値の一致（DHCSR の S_HALT）
  → ロジアナ。出力 ch4 = 条件の間（sequence | sampler_match、有効）、ch7 = 脈 10 µs（host、無効）。
- サンプラの値の一致の比べた標本の数 seen は、設定してからの時間で増える（1 kHz で読んでいるとみなす）。
- ロジアナのトリガ（la.trigger_status）は最後の la.capture の条件をレジスタの読み戻しの形で返す。
"""

import time

__all__ = ["XtrigDemo"]

OUTPUT_PINS = ({"index": 0, "channel": 4, "pull_up": False}, {"index": 1, "channel": 5, "pull_up": False},
               {"index": 2, "channel": 6, "pull_up": False}, {"index": 3, "channel": 7, "pull_up": True})
OUTPUT_ACTIONS = 2
SAMPLE_HZ = 1000                                  # サンプラが番地を読む速さ（偽物）
TICK_US = 0.32                                    # 脈の長さの刻み（偽物: 返す範囲の幅）


class XtrigDemo:
    def _init_xtrig(self) -> None:
        """DemoProbe.__init__ から（_init_search の後: 経路は self._xtrig["route"]）。"""
        self._obs_trigger: dict | None = None
        self._obs_count = 0
        self._sampler_trigger: dict | None = None
        self._outputs: dict[int, dict] = {}       # channel → 設定（configured の端子だけ）
        self.xtrig_route(to_la=["sequence"], to_inject=["dap_match"])
        self.sampler_trigger_set(0xE000EDF0, 0x20000, 0x20000, halt=False, la=True)
        self._sampler_trigger.update(count=1, time=12_345_678)
        self.xtrig_output_set(4, mode="while", level="high", sources=["sequence", "sampler_match"], enable=True)
        self.xtrig_output_set(7, mode="pulse", level="low", sources=["host"], pulse_us=10)

    # --- ロジアナのトリガ（la.trigger_status。最後の捕捉の条件をレジスタの読み戻しの形で） ------------------

    def la_capture(self, rate_hz=None, pretrigger=1024, mask=0, value=0, rise=0, fall=0, timeout_s=5.0, vcd_path=None):
        out = super().la_capture(rate_hz, pretrigger, mask, value, rise, fall, timeout_s, vcd_path)
        self._la_trigger = {"mask": mask, "value": value, "rise": rise, "fall": fall,
                            "rate_hz": rate_hz or 25_000_000, "pretrigger": pretrigger}
        return out

    def la_trigger_status(self):
        t = getattr(self, "_la_trigger", None) or {"mask": 0, "value": 0, "rise": 0, "fall": 0, "rate_hz": 100e6,
                                                   "pretrigger": 0}
        return {**t, "external_only": (t["mask"], t["value"], t["rise"], t["fall"]) == (1, 1, 0, 1),
                "internal": {"capturing": False, "triggered": True, "done": True, "trigger_time": 0}}

    def probe_info(self):
        info = super().probe_info()
        info["capabilities"].update(xtrig=True, trig_seq=True, inject=True, obs_match=True, sampler_match=True,
                                    outputs=[dict(p) for p in OUTPUT_PINS], output_actions=OUTPUT_ACTIONS)
        return info

    def _route_bit(self, source: str, halt: bool, la: bool) -> None:
        route = self._xtrig["route"]
        for key, on in (("to_inject", halt), ("to_la", la)):
            route[key] = [s for s in route[key] if s != source] + ([source] if on else [])

    def _routed(self, source: str) -> dict:
        route = self._xtrig["route"]
        return {"halt": source in route["to_inject"], "la": source in route["to_la"]}

    # --- FPGA の条件（源 obs_match） -------------------------------------------------------------

    def fpga_obs_trigger_set(self, condition, halt=True, la=False, group=0):
        if not halt and not la:
            raise ValueError("halt か la の少なくとも一方が要る（どちらも無ければ成立しても何も起きない）")
        self._obs_trigger, self._obs_count = {"condition": condition, "halt": halt, "la": la}, 0
        self._route_bit("obs_match", halt, la)
        return {"mask": 0, "value": 0, "change": 0, **self._obs_trigger}

    def fpga_obs_trigger_status(self):
        if self._obs_trigger is None:
            return {"condition": None}
        return {**self._obs_trigger, **self._routed("obs_match"), "count": self._obs_count}

    def fpga_obs_trigger_clear(self):
        self._obs_trigger = None
        self._route_bit("obs_match", False, False)
        return {"condition": None}

    # --- サンプラの値の一致（源 sampler_match） ----------------------------------------------------

    def sampler_trigger_set(self, address, mask, value, halt, la):
        if not halt and not la:
            raise ValueError("halt か la の少なくとも一方が要る（どちらも無ければ成立しても何も起きない）")
        self._sampler_trigger = {"address": address, "mask": mask, "value": value, "since": time.monotonic(),
                                 "count": 0, "time": 0}
        self._route_bit("sampler_match", halt, la)
        return {"address": address, "mask": mask, "value": value, "halt": halt, "la": la}

    def sampler_trigger_status(self):
        t = self._sampler_trigger
        if t is None:
            return {"enabled": False, "address": 0, "mask": 0, "value": 0, **self._routed("sampler_match"),
                    "matched": False, "seen_any": False, "seen": 0, "count": 0, "time": 0, "prev_time": 0}
        seen = int((time.monotonic() - t["since"]) * SAMPLE_HZ)
        return {"enabled": True, "address": t["address"], "mask": t["mask"], "value": t["value"],
                **self._routed("sampler_match"), "matched": False, "seen_any": seen > 0, "seen": seen,
                "count": t["count"], "time": t["time"], "prev_time": max(0, t["time"] - 10_000) if t["count"] else 0}

    def sampler_trigger_clear(self):
        self._sampler_trigger = None
        self._route_bit("sampler_match", False, False)
        return {"enabled": False}

    # --- 出力の端子（xtrig.output_*） -------------------------------------------------------------

    def _pin(self, channel: int) -> dict:
        for p in OUTPUT_PINS:
            if p["channel"] == channel:
                return p
        raise ValueError(f"ch{channel} は出力に使えない（使える端子のロジアナの ch: "
                         f"{', '.join(str(p['channel']) for p in OUTPUT_PINS)}）")

    def _configured(self, channel: int) -> dict:
        self._pin(channel)
        if channel not in self._outputs:
            raise RuntimeError(f"ch{channel} の出力は設定されていない（xtrig_output_set を先に）")
        return self._outputs[channel]

    def xtrig_output_set(self, channel, mode="while", level="high", pattern=None, sources=None, pulse_us=None,
                         enable=False):
        pin = self._pin(channel)
        pattern = dict(pattern or {})
        mask = int(pattern.get("mask", 0))
        if not mask and not sources:
            raise ValueError("条件が無い: pattern の mask か sources を指定する")
        if mode == "pulse" and not pulse_us:
            raise ValueError("mode pulse には pulse_us（> 0）が要る")
        if channel not in self._outputs and len(self._outputs) >= OUTPUT_ACTIONS:
            raise RuntimeError("出力の動作が空いていない（xtrig_output_release で空ける）")
        cfg = {"mode": mode, "level": level, "pattern": {"mask": mask, "value": int(pattern.get("value", 0)) & mask,
                                                         "any": bool(pattern.get("any", False))},
               "sources": list(sources or []), "enabled": enable}
        if mode == "pulse":
            cfg["pulse_us"] = {"min": pulse_us - TICK_US, "max": pulse_us}
        self._outputs[channel] = cfg
        result = {"channel": channel, "mode": mode, "level": level, "pull_up": pin["pull_up"], "enabled": enable}
        if mode == "pulse":
            result["pulse_us"] = cfg["pulse_us"]
        return result

    def xtrig_output_enable(self, channel):
        self._configured(channel)["enabled"] = True
        return {"channel": channel, "enabled": True}

    def xtrig_output_disable(self, channel):
        self._configured(channel)["enabled"] = False
        return {"channel": channel, "enabled": False}

    def xtrig_output_release(self, channel):
        self._pin(channel)
        self._outputs.pop(channel, None)
        return {"channel": channel, "claim": False}

    def xtrig_output_status(self):
        result = []
        for p in OUTPUT_PINS:
            item = {"channel": p["channel"], "pull_up": p["pull_up"]}
            cfg = self._outputs.get(p["channel"])
            if cfg is None:
                item["configured"] = False
            else:
                item.update({"configured": True, "enabled": cfg["enabled"], "active": False, "claim": True,
                             "mode": cfg["mode"], "level": cfg["level"], "pattern": dict(cfg["pattern"]),
                             "sources": list(cfg["sources"])})
                if "pulse_us" in cfg:
                    item["pulse_us"] = dict(cfg["pulse_us"])
            result.append(item)
        return {"outputs": result, "actions": OUTPUT_ACTIONS, "free": OUTPUT_ACTIONS - len(self._outputs)}
