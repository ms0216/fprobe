"""Probe の道具: 規則の検査・状態遷移図・証跡・故障注入の台本（U25）。

Probe（fprobe.mcp_server）に混ぜて使う mixin。記録（_reading・_recordings_dir・_recorder）、ELF（_symbols）、
ターゲット（target・target_status・evaluate・target_reset・target_resume）を使う。規則と証跡の計算は fprobe.rules、
試行の進め方は fprobe.inject。機器に触るのは _InjectHost の各操作で、必ず _lock の中。

仕様: docs/superpowers/specs/2026-10-18-u25-u44-rules-hooks-design.md の 1・2 節。
"""

import time
from pathlib import Path
from typing import Any

from . import generated as g
from .inject import InjectScript, run_script
from .recording import Recording
from .rules import RuleError, RuleSet, check, evidence, render_report, state_machine
from .xtrig import CrossTrigger, Event, InjectStatus, Pattern, Source, Stage, TriggerSequencer

__all__ = ["RulesTools"]

_WHEN_SOURCE = {"pattern": Source.SEQUENCE, "sequence": Source.SEQUENCE, "dap_match": Source.DAP_MATCH,
                "host": Source.HOST}
_STAGE_EVENTS = {"pattern_a": Event.PATTERN_A, "pattern_b": Event.PATTERN_B, "dap_match": Event.DAP_MATCH,
                 "host": Event.HOST}
_INJECT_MASK = ((1 << g.XTRIG_ROUTE_TO_INJECT[1]) - 1) << g.XTRIG_ROUTE_TO_INJECT[0]


def _one_of(value: Any, path: str | None, what: str) -> None:
    if (value is None) == (path is None):
        raise ValueError(f"{what} か path のどちらか一方を渡す")


class _InjectHost:
    """fprobe.inject.InjectHost の Probe での実装（各操作で Probe の _lock を取る）。"""

    def __init__(self, probe):
        self.p = probe

    @property
    def _dev(self):
        return self.p.session.device

    def resolve(self, address: int | str) -> int:
        if isinstance(address, int):
            return address
        text = address.strip()
        if text.lower().startswith("0x"):
            return int(text, 16)
        if self.p._symbols is None:
            raise ValueError(f"変数名 {text!r} の番地が分からない（ELF を読んでいない: debug_elf_load）")
        return self.p._symbols.variable(text).address

    def read_memory(self, address: int, size: int) -> int:
        with self.p._lock:
            return self.p.target.read_memory(address, transfer_size=8 * size)

    def write_memory(self, address: int, value: int, size: int) -> None:
        with self.p._lock:
            self.p.target.write_memory(address, value, transfer_size=8 * size)

    def set_inject(self, address: int, value: int, ap: int) -> None:
        with self.p._lock:
            CrossTrigger(self._dev).set_inject(address, value, ap)

    def _route_inject(self, source: Source) -> None:
        """割り込み書き込みへのつなぎだけを変える（ロジアナへのつなぎは保つ）。"""
        dev = self._dev
        route = dev.read32(g.XTRIG_ROUTE) & ~_INJECT_MASK
        dev.write32(g.XTRIG_ROUTE, route | int(source) << g.XTRIG_ROUTE_TO_INJECT[0])

    def arm(self, when: dict) -> None:
        kind, params = next(iter(when.items()))
        with self.p._lock:
            dev = self._dev
            if kind == "pattern":
                seq = TriggerSequencer(dev)
                seq.configure([Stage(Event.PATTERN_A)], Pattern(**params))
                seq.arm()
            elif kind == "sequence":
                stages = []
                for st in params.get("stages") or []:
                    mask = Event(0)
                    for name in st["when"]:
                        mask |= _STAGE_EVENTS[name]
                    within = st.get("within_us")
                    stages.append(Stage(mask, int(st.get("count", 1)), None if within is None else within * 1e-6))
                seq = TriggerSequencer(dev)
                seq.configure(stages, Pattern(**(params.get("pattern_a") or {})), Pattern(**(params.get("pattern_b") or {})))
                seq.arm()
            elif kind == "dap_match":
                CrossTrigger(dev).set_match(**params)
            self._route_inject(_WHEN_SOURCE[kind])

    def fire(self) -> None:
        with self.p._lock:
            CrossTrigger(self._dev).fire()

    def disarm(self) -> None:
        with self.p._lock:
            self._route_inject(Source(0))
            TriggerSequencer(self._dev).disarm()

    def restore_default(self) -> None:
        with self.p._lock:
            CrossTrigger(self._dev).set_inject(*self.p._halt_inject(), 0)   # 方式に合わせた停止（U37）

    def inject_status(self) -> InjectStatus:
        with self.p._lock:
            return CrossTrigger(self._dev).inject_status()

    def timebase_hz(self) -> float:
        with self.p._lock:
            return self._dev.capabilities().timebase_hz

    def state(self) -> str:
        return self.p.target_status()["state"].lower()

    def evaluate(self, expression: str) -> Any:
        return self.p.evaluate(expression)["value"]

    def reset(self, kind: str) -> None:
        self.p.target_reset(halt=False, kind=kind)

    def resume(self) -> None:
        self.p.target_resume()

    def recording(self) -> Recording | None:
        return self.p._reading() if self.p._view is not None else None

    def note(self, stream: str, kind: str, t: int, t_end: int | None, body: dict) -> None:
        rec = self.p._recorder
        if rec is not None:
            rec.add(stream, kind, t, body=body, t_end=t_end)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def monotonic(self) -> float:
        return time.monotonic()


class RulesTools:
    """規則の検査・故障注入（Probe の mixin）。"""

    _last_rules: dict | None = None
    _last_inject: dict | None = None

    def _inject_host(self) -> _InjectHost:
        return _InjectHost(self)

    def _named_recording(self, name: str) -> Recording:
        directory = self._recordings_dir() / name
        if directory.parent != self._recordings_dir() or not (directory / "meta.json").is_file():
            raise KeyError(f"記録 {name!r} が無い（recording_list の name）")
        return Recording.open(directory)

    def rules_check(self, rules: dict | None = None, path: str | None = None, t0: int | None = None,
                    t1: int | None = None, baseline: str | None = None, mark: bool = True, limit: int = 100) -> dict:
        """規則集（rules = 辞書、または path = JSON / YAML のファイル）を読む記録の範囲 [t0, t1) に当て、規則ごとの判定・
        期待値と実測・違反（時刻）を返す。mark なら違反を記録の stream "rule" に派生イベントとして書く（同じ規則集の
        前の印は置き換え）。baseline（recording_list の name）で基準の記録の実測と比べる。"""
        _one_of(rules, path, "rules")
        try:
            ruleset = RuleSet.parse(rules) if rules is not None else RuleSet.load(path)
        except RuleError as e:
            raise ValueError(str(e)) from None
        base = self._named_recording(baseline) if baseline is not None else None
        try:
            result = check(self._reading(), ruleset, t0=t0, t1=t1, baseline=base, mark=mark, limit=limit)
        finally:
            if base is not None:
                base.close()
        self._last_rules = result
        return result

    def rules_states(self, event: dict, field: str, labels: dict | None = None, t0: int | None = None,
                     t1: int | None = None, dot_path: str | None = None) -> dict:
        """記録の出来事（event は recording_find と同じ条件）の field（a / b / kind / body.<名前>）の値を状態として、
        状態ごとの回数・滞在時間、遷移ごとの回数、Graphviz の dot（dot_path にも書く）。"""
        try:
            sm = state_machine(self._reading(), event, field, labels, t0, t1)
        except RuleError as e:
            raise ValueError(str(e)) from None
        if dot_path:
            Path(dot_path).write_text(sm["dot"], encoding="utf-8")
        return sm

    def rules_report(self, path: str, format: str = "markdown") -> dict:
        """最後の rules_check と inject_run の結果を、要件と対応づけた証跡（markdown / json）として path に書く。"""
        recording = self._view
        text = render_report(format, rules=self._last_rules, inject=self._last_inject, recording=recording)
        Path(path).write_text(text, encoding="utf-8")
        data = evidence(self._last_rules, self._last_inject, recording)
        return {"path": path, "verdict": data["verdict"],
                "requirements": {k: v["verdict"] for k, v in data["requirements"].items()}}

    def inject_run(self, script: dict | None = None, path: str | None = None) -> dict:
        """故障注入の台本（script = 辞書、または path = JSON / YAML）を試行する: 試行ごとに、割り込み書き込みの中身を
        設定し、条件（when: pattern / sequence / dap_match / host）を割り込み書き込みへつなぎ、成立を待ち、observe_s 後に
        判定（expect）し、restore する。記録中なら stream "inject" に注入の時刻（ハードウェアの時刻）で入る。
        最後に割り込み書き込みを既定（ターゲット停止）へ戻す。"""
        _one_of(script, path, "script")
        try:
            parsed = InjectScript.parse(script) if script is not None else InjectScript.load(path)
        except ValueError as e:
            raise ValueError(str(e)) from None
        result = run_script(self._inject_host(), parsed)
        self._last_inject = result
        return result
