"""Probe の道具: クロストリガの動作「出力の端子を動かす」（U41 の一部）。

源（ロジアナの入力の端子の並び・ロジアナのトリガ・DAP の番地一致・ホストの指令・多段トリガ）で、J12 の決めた端子を
脈・保持・条件の間の 3 つの形で動かす。動いていない間は駆動しない（高インピーダンス。プルアップのある端子は弱く High）。
出力は端子のロジアナのチャンネル（channel）で指す。どの端子が使えるか（候補）と、同時に動かせる数（動作の数）は
ケイパビリティ outputs・output_actions が申告する。端子に動作を割り当てるのはこの層（使う側は動作の番号を知らない）。

Probe（fprobe.mcp_server）に混ぜて使う mixin。機器に触るのは必ず _lock の中。
仕様: docs/superpowers/specs/2026-10-18-u41-xtrig-pin-output-design.md
"""

from .xtrig import OutputConfig, OutputMode, PinOutputs, Source, pulse_range_s, pulse_ticks

__all__ = ["MODES", "XtrigOutputTools"]

MODES = {"pulse": OutputMode.PULSE, "hold": OutputMode.HOLD, "while": OutputMode.WHILE}
LEVELS = {"high": True, "low": False}
PATTERN_KEYS = {"mask", "value", "any"}


class XtrigOutputTools:
    def _init_xtrig_out(self) -> None:
        # 出力に使っている端子の設定（channel → xtrig_output_set の引数）。FPGA が立ち上がり直すと機器の設定は消え、
        # 端子が普段の機能に戻ってターゲットの線を駆動しうる（UART TX は High）ので、開き直したらすぐ取り戻す（B29）
        self._claimed_outputs: dict[int, dict] = {}

    def _restore_outputs(self) -> dict:
        """覚えている出力の端子を、無効（駆動しない）で設定し直す。{"restored": [ch], "failed": {ch: 誤り}}。"""
        restored, failed = [], {}
        for channel, args in list(self._claimed_outputs.items()):
            try:
                self.xtrig_output_set(channel, **args, enable=False)
                restored.append(channel)
            except Exception as e:              # 構成が違う等: 取り戻せない端子を知らせる（残りは続ける）
                failed[channel] = f"ch{channel}: {e}"
        return {"restored": restored, "failed": failed}

    def _pin_outputs(self) -> PinOutputs:
        outputs = PinOutputs(self.session.device)
        if not outputs.actions:
            raise RuntimeError("この構成にクロストリガの出力の端子が無い（ケイパビリティ outputs が 0 本）")
        return outputs

    @staticmethod
    def _pin_index(outputs: PinOutputs, channel: int) -> int:
        for pin in outputs.pins:
            if pin.channel == channel:
                return pin.index
        usable = ", ".join(str(p.channel) for p in outputs.pins)
        raise ValueError(f"ch{channel} は出力に使えない（使える端子のロジアナの ch: {usable}）")

    def _action_of(self, outputs: PinOutputs, channel: int) -> int:
        action = outputs.action_for(self._pin_index(outputs, channel), allocate=False)
        if action is None:
            raise RuntimeError(f"ch{channel} の出力は設定されていない（xtrig_output_set を先に）")
        return action

    def xtrig_output_set(self, channel: int, mode: str = "while", level: str = "high", pattern: dict | None = None,
                         sources: list[str] | None = None, pulse_us: float | None = None,
                         enable: bool = False) -> dict:
        """出力の端子（channel = その端子のロジアナの ch）の動作を設定する。条件 = pattern（端子の並び。ロジアナの
        ch 0〜7 のビット {"mask", "value", "any"}。any = true は見る ch のどれかが一致で成立）or sources（源の名前
        "la_trigger" / "dap_match" / "host" / "sequence"）。mode: "pulse"（条件の立ち上がりで pulse_us の脈）/
        "hold"（立ち上がりから無効にするまで）/ "while"（条件の間。遅れ 40 ns 程度）。level: 動いている間に出す値。
        設定するとその端子の普段の機能（UART・SWO）を止めて出力に使う（戻すのは xtrig_output_release）。
        enable = true で続けて有効にする。"""
        if mode not in MODES:
            raise ValueError(f"mode は {', '.join(MODES)} のどれか")
        if level not in LEVELS:
            raise ValueError("level は high か low")
        pattern = dict(pattern or {})
        if set(pattern) - PATTERN_KEYS:
            raise ValueError(f"pattern のキーは {', '.join(sorted(PATTERN_KEYS))}")
        flags = Source(0)
        for name in sources or []:
            try:
                flags |= Source[name.upper()]
            except KeyError:
                raise ValueError(f"源 {name!r} は無い（la_trigger / dap_match / host / sequence）") from None
        mask = int(pattern.get("mask", 0))
        if not mask and not flags:
            raise ValueError("条件が無い: pattern の mask か sources を指定する")
        with self._lock:
            outputs = self._pin_outputs()
            pin = self._pin_index(outputs, channel)
            clock_hz = self.session.device.capabilities().timebase_hz
            length, unit = 1, 0
            if MODES[mode] is OutputMode.PULSE:
                if pulse_us is None or pulse_us <= 0:
                    raise ValueError("mode pulse には pulse_us（> 0）が要る")
                length, unit = pulse_ticks(pulse_us * 1e-6, clock_hz)
            cfg = OutputConfig(mode=MODES[mode], mask=mask, value=int(pattern.get("value", 0)) & mask,
                               any=bool(pattern.get("any", False)), sources=flags, high=LEVELS[level],
                               length=length, unit=unit, claim=True)
            action = outputs.action_for(pin, allocate=True)
            outputs.configure(action, pin, cfg, enable=enable)
            self._claimed_outputs[channel] = {"mode": mode, "level": level, "pattern": pattern or None,
                                              "sources": list(sources or []) or None, "pulse_us": pulse_us}
        notes = []
        if outputs.pins[pin].pull_up and level == "high":
            notes.append(f"ch{channel} の端子にはプルアップがある: 動いていない間も弱く High に見える"
                         "（プルダウンの入力を Low にできない。level low か、プル無しの端子を使う）")
        if mask >> channel & 1:
            notes.append(f"条件の端子の並びに出力自身の ch{channel} を含む（自分の出力を見て動く）")
        result = {"channel": channel, "mode": mode, "level": level, "pull_up": outputs.pins[pin].pull_up,
                  "enabled": enable}
        if cfg.mode is OutputMode.PULSE:
            low, high = pulse_range_s(length, unit, clock_hz)
            result["pulse_us"] = {"min": low * 1e6, "max": high * 1e6}
        if notes:
            result["notes"] = notes
        return result

    def xtrig_output_enable(self, channel: int) -> dict:
        """出力の動作を有効にする（脈・保持は次の条件の立ち上がりから、条件の間はすぐ）。"""
        with self._lock:
            outputs = self._pin_outputs()
            outputs.enable(self._action_of(outputs, channel))
        return {"channel": channel, "enabled": True}

    def xtrig_output_disable(self, channel: int) -> dict:
        """出力の動作を無効にする（保持も戻る）。端子は出力に使ったまま駆動しない（高インピーダンス）。"""
        with self._lock:
            outputs = self._pin_outputs()
            outputs.disable(self._action_of(outputs, channel))
        return {"channel": channel, "enabled": False}

    def xtrig_output_release(self, channel: int) -> dict:
        """出力の動作を無効にし、設定を消して、端子を普段の機能（UART・SWO）に戻す（動作が空く）。"""
        with self._lock:
            outputs = self._pin_outputs()
            action = outputs.action_for(self._pin_index(outputs, channel), allocate=False)
            if action is not None:
                outputs.release(action)
            self._claimed_outputs.pop(channel, None)
        return {"channel": channel, "claim": False}

    def xtrig_output_status(self) -> dict:
        """出力の端子の候補ごとの状態: channel・プルアップ・設定済みか・有効・動いているか（駆動中）・claim・形・条件。
        actions = 同時に動かせる数、free = いま空いている数。"""
        with self._lock:
            outputs = PinOutputs(self.session.device)
            states = outputs.status()
            clock_hz = self.session.device.capabilities().timebase_hz
        mode_names = {v: k for k, v in MODES.items()}
        by_pin = {st.pin: st for st in states if st.in_use}
        result = []
        for pin in outputs.pins:
            item = {"channel": pin.channel, "pull_up": pin.pull_up}
            st = by_pin.get(pin.index)
            if st is None:
                item["configured"] = False
            else:
                cfg = st.config
                item.update({"configured": True, "enabled": st.enabled, "active": st.active, "claim": cfg.claim,
                             "mode": mode_names[cfg.mode], "level": "high" if cfg.high else "low",
                             "pattern": {"mask": cfg.mask, "value": cfg.value, "any": cfg.any},
                             "sources": [s.name.lower() for s in Source if s & cfg.sources]})
                if cfg.mode is OutputMode.PULSE:
                    low, high = pulse_range_s(cfg.length, cfg.unit, clock_hz)
                    item["pulse_us"] = {"min": low * 1e6, "max": high * 1e6}
            result.append(item)
        return {"outputs": result, "actions": outputs.actions, "free": sum(not st.in_use for st in states)}
