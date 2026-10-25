"""故障注入の台本（U25、WR-03）: クロストリガの割り込み書き込みで「いつ・何を壊すか・どうなるべきか」を台本にし、
試行して判定する。

仕様: docs/superpowers/specs/2026-10-18-u25-u44-rules-hooks-design.md の 2 節。
ここは台本の検証と試行の進め方だけ。機器の操作は InjectHost（Probe の RulesTools が実装。ロックは向こう）。
"""

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .dwarf.values import scalar
from .recording import Recording
from .rules import RANK, RuleError, RuleSet, check, load_yaml, worst
from .xtrig import InjectStatus

__all__ = ["INJECT_STREAM", "InjectHost", "InjectScript", "ScriptError", "Trial", "run_script"]

INJECT_STREAM = "inject"
WHEN_KINDS = {"pattern", "sequence", "dap_match", "host"}
POLL_S = 0.002                                   # 成立を待つ間の読みの間隔
COMPARE = ("equals", "not_equals", "min", "max")


class ScriptError(ValueError):
    """台本の書き方の誤り。"""


class InjectHost(Protocol):
    """試行に要る機器の操作（Probe の実装は各操作でロックを取る）。"""

    def resolve(self, address: int | str) -> int: ...                  # 変数名 → 番地
    def read_memory(self, address: int, size: int) -> int: ...
    def write_memory(self, address: int, value: int, size: int) -> None: ...
    def set_inject(self, address: int, value: int, ap: int) -> None: ...
    def arm(self, when: dict) -> None: ...                            # 条件を仕掛け、その源を割り込み書き込みへつなぐ
    def fire(self) -> None: ...
    def disarm(self) -> None: ...                                     # 割り込み書き込みへのつなぎを切る
    def restore_default(self) -> None: ...                            # 割り込み書き込みの中身を既定（停止）へ
    def inject_status(self) -> InjectStatus: ...
    def timebase_hz(self) -> float: ...                               # 割り込み書き込みの時刻の単位
    def state(self) -> str: ...                                       # "running" / "halted" / ...
    def evaluate(self, expression: str) -> dict: ...                  # fprobe.dwarf.values の形
    def reset(self, kind: str) -> None: ...
    def resume(self) -> None: ...
    def recording(self) -> Recording | None: ...                      # 読む記録（書いた分まで反映済み）
    def note(self, stream: str, kind: str, t: int, t_end: int | None, body: dict) -> None: ...
    def sleep(self, seconds: float) -> None: ...
    def monotonic(self) -> float: ...


@dataclass(frozen=True)
class Trial:
    id: str
    when: dict                                   # {kind: params} の 1 つ
    write: dict                                  # address・value か flip・ap
    expect: tuple[dict, ...] = ()
    restore: dict = field(default_factory=dict)
    requirement: tuple[str, ...] = ()
    description: str = ""
    timeout_s: float = 0.0                       # 条件の成立を待つ秒（not_triggered の判定。台本で必ず書く）
    observe_s: float = 0.0                       # 注入の後に見る秒（pass / fail の判定。台本で必ず書く）
    repeat: int = 1


@dataclass(frozen=True)
class InjectScript:
    name: str
    trials: tuple[Trial, ...]
    requirements: dict = field(default_factory=dict)
    sha256: str = ""

    @classmethod
    def parse(cls, data: dict) -> "InjectScript":
        if not isinstance(data, dict) or not isinstance(data.get("trials"), list) or not data["trials"]:
            raise ScriptError("台本は {name, requirements, trials: [...]} の形（trials は 1 つ以上）")
        unknown = set(data) - {"name", "requirements", "trials", "description"}
        if unknown:
            raise ScriptError(f"台本の未知の欄: {sorted(unknown)}")
        trials, seen = [], set()
        for n, raw in enumerate(data["trials"]):
            trial = _parse_trial(raw, f"trials[{n}]")
            if trial.id in seen:
                raise ScriptError(f"試行の id {trial.id!r} が重複")
            seen.add(trial.id)
            trials.append(trial)
        canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return cls(str(data.get("name") or "inject"), tuple(trials),
                   {str(k): str(v) for k, v in (data.get("requirements") or {}).items()},
                   hashlib.sha256(canonical.encode()).hexdigest())

    @classmethod
    def parse_text(cls, text: str) -> "InjectScript":
        try:
            data = json.loads(text)
        except ValueError:
            try:
                data = load_yaml(text)
            except RuleError as e:
                raise ScriptError(str(e)) from None
        return cls.parse(data)

    @classmethod
    def load(cls, path: str | Path) -> "InjectScript":
        return cls.parse_text(Path(path).read_text(encoding="utf-8"))


def _address(value: Any, where: str) -> None:
    if not (isinstance(value, str) and value) and not (isinstance(value, int) and not isinstance(value, bool)
                                                         and 0 <= value < 1 << 32):
        raise ScriptError(f"{where} は番地（32 ビット）か ELF の変数名")


def _word(value: Any, where: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 1 << 32:
        raise ScriptError(f"{where} は 32 ビットの値")


def _compare(raw: dict, where: str) -> None:
    ops = set(raw) & set(COMPARE)
    if not ops:
        raise ScriptError(f"{where}: equals / not_equals / min / max のどれかが要る")


def _parse_expect(raw: Any, where: str) -> dict:
    if not isinstance(raw, dict):
        raise ScriptError(f"{where} は辞書")
    if "state" in raw:
        if raw["state"] not in ("running", "halted") or len(raw) != 1:
            raise ScriptError(f"{where}: state は running か halted だけ")
    elif "expression" in raw:
        if not isinstance(raw["expression"], str) or set(raw) - {"expression", *COMPARE}:
            raise ScriptError(f"{where}: {{expression: C の式, equals|not_equals|min|max}}")
        _compare(raw, where)
    elif "memory" in raw:
        mem = raw["memory"]
        if not isinstance(mem, dict) or set(mem) - {"address", "size"} or "address" not in mem \
                or mem.get("size", 4) not in (1, 2, 4) or set(raw) - {"memory", *COMPARE}:
            raise ScriptError(f"{where}: {{memory: {{address, size: 1|2|4}}, equals|not_equals|min|max}}")
        _address(mem["address"], f"{where}.memory.address")
        _compare(raw, where)
    elif "rules" in raw:
        if set(raw) != {"rules"}:
            raise ScriptError(f"{where}: {{rules: [規則, ...]}}")
        try:
            RuleSet.parse({"rules": raw["rules"]})
        except RuleError as e:
            raise ScriptError(f"{where}: 規則の誤り: {e}") from None
    else:
        raise ScriptError(f"{where}: expect は state / expression / memory / rules のどれか")
    return raw


PATTERN_KEYS = {"mask", "value", "rise", "fall"}
MATCH_KEYS = {"address", "address_mask", "data", "data_mask", "on_write", "on_read"}
STAGE_KEYS = {"when", "count", "within_us"}
STAGE_EVENTS = {"pattern_a", "pattern_b", "dap_match", "host"}


def _pattern(p: Any, where: str) -> None:
    if not isinstance(p, dict) or set(p) - PATTERN_KEYS:
        raise ScriptError(f"{where} は {{mask, value, rise, fall}}（ロジアナのチャンネルのビット）")
    for k, v in p.items():
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v < 1 << 16:
            raise ScriptError(f"{where}.{k} は 16 ビットの値")


def _parse_when(when: dict, where: str) -> None:
    """when の中身（xtrig の設定と同じ形）。"""
    kind, p = next(iter(when.items()))
    if kind == "host":
        if set(p) - {"delay_s"} or not isinstance(p.get("delay_s", 0), (int, float)) or p.get("delay_s", 0) < 0:
            raise ScriptError(f"{where}.host は {{delay_s: 0 以上の秒}}")
    elif kind == "pattern":
        _pattern(p, f"{where}.pattern")
    elif kind == "dap_match":
        if set(p) - MATCH_KEYS or "address" not in p:
            raise ScriptError(f"{where}.dap_match は {{address, address_mask, data, data_mask, on_write, on_read}}")
        for k in ("address", "address_mask", "data", "data_mask"):
            if k in p:
                _word(p[k], f"{where}.dap_match.{k}")
        for k in ("on_write", "on_read"):
            if k in p and not isinstance(p[k], bool):
                raise ScriptError(f"{where}.dap_match.{k} は true / false")
    else:
        if set(p) - {"stages", "pattern_a", "pattern_b"} or not isinstance(p.get("stages"), list) \
                or not 1 <= len(p["stages"]) <= 4:
            raise ScriptError(f"{where}.sequence は {{stages: 1〜4 段, pattern_a, pattern_b}}")
        for n, st in enumerate(p["stages"]):
            if not isinstance(st, dict) or set(st) - STAGE_KEYS or not isinstance(st.get("when"), list) \
                    or not st["when"] or set(st["when"]) - STAGE_EVENTS:
                raise ScriptError(f"{where}.sequence.stages[{n}] は {{when: {sorted(STAGE_EVENTS)} の列, count, "
                                  "within_us}")
            count, within = st.get("count", 1), st.get("within_us")
            if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 0xFFFF:
                raise ScriptError(f"{where}.sequence.stages[{n}].count は 1〜65535 の整数")
            if within is not None and (isinstance(within, bool) or not isinstance(within, (int, float))
                                       or not within > 0):
                raise ScriptError(f"{where}.sequence.stages[{n}].within_us は正の数")
        for k in ("pattern_a", "pattern_b"):
            if k in p:
                _pattern(p[k], f"{where}.sequence.{k}")


def _parse_trial(raw: Any, where: str) -> Trial:
    if not isinstance(raw, dict):
        raise ScriptError(f"{where} は辞書")
    tid = raw.get("id")
    if not isinstance(tid, str) or not tid:
        raise ScriptError(f"{where}: id（文字列）が要る")
    where = f"試行 {tid!r}"
    unknown = set(raw) - {"id", "when", "write", "expect", "restore", "requirement", "description", "timeout_s",
                          "observe_s", "repeat"}
    if unknown:
        raise ScriptError(f"{where}: 未知の欄 {sorted(unknown)}")
    when = raw.get("when")
    if not isinstance(when, dict) or len(when) != 1 or next(iter(when)) not in WHEN_KINDS \
            or not isinstance(next(iter(when.values())), dict):
        raise ScriptError(f"{where}: when は {sorted(WHEN_KINDS)} のどれか 1 つ（{{種類: {{...}}}}）")
    _parse_when(when, f"{where}.when")
    write = raw.get("write")
    if not isinstance(write, dict) or "address" not in write or ("value" in write) == ("flip" in write) \
            or set(write) - {"address", "value", "flip", "ap"}:
        raise ScriptError(f"{where}: write は {{address, value か flip, ap}}")
    _address(write["address"], f"{where}.write.address")
    _word(write.get("value", write.get("flip")), f"{where}.write.{'value' if 'value' in write else 'flip'}")
    if not isinstance(write.get("ap", 0), int) or not 0 <= write.get("ap", 0) < 256:
        raise ScriptError(f"{where}.write.ap は 0〜255")
    expect = raw.get("expect") or []
    if not isinstance(expect, list):
        raise ScriptError(f"{where}: expect は判定の列")
    expect = tuple(_parse_expect(e, f"{where}.expect[{n}]") for n, e in enumerate(expect))
    restore = raw.get("restore") or {}
    if not isinstance(restore, dict) or set(restore) - {"write", "reset", "resume"}:
        raise ScriptError(f"{where}: restore は {{write: [{{address, value, size}}], reset, resume}}")
    for n, w in enumerate(restore.get("write") or []):
        if not isinstance(w, dict) or set(w) - {"address", "value", "size"} or "address" not in w or "value" not in w \
                or w.get("size", 4) not in (1, 2, 4):
            raise ScriptError(f"{where}.restore.write[{n}] は {{address, value, size: 1|2|4}}")
        _address(w["address"], f"{where}.restore.write[{n}].address")
    for key in ("timeout_s", "observe_s"):       # 判定を決める待ちなので暗黙の既定にしない（使う側が書く）
        if key not in raw:
            raise ScriptError(f"{where}.{key} が要る（判定を決める待ちの秒。既定は無い）")
        v = raw[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            raise ScriptError(f"{where}.{key} は 0 以上の秒")
    repeat = raw.get("repeat", 1)
    if isinstance(repeat, bool) or not isinstance(repeat, int) or not 1 <= repeat <= 10_000:
        raise ScriptError(f"{where}.repeat は 1〜10000")
    requirement = raw.get("requirement") or ()
    requirement = (requirement,) if isinstance(requirement, str) else tuple(str(r) for r in requirement)
    return Trial(tid, when, write, expect, restore, requirement, str(raw.get("description", "")),
                 float(raw["timeout_s"]), float(raw["observe_s"]), repeat)


# --- 試行 -------------------------------------------------------------------------------------

def _compare_ok(value: Any, spec: dict) -> bool:
    if "equals" in spec and value != spec["equals"]:
        return False
    if "not_equals" in spec and value == spec["not_equals"]:
        return False
    for op, ok in (("min", lambda v, ref: v >= ref), ("max", lambda v, ref: v <= ref)):
        if op in spec:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not ok(value, spec[op]):
                return False
    return True


def _judge(host: InjectHost, expect: dict, t_inject: int, observe_s: float, name: str) -> dict:
    """1 つの判定。誤り（読めない等）は ok = False と error。"""
    try:
        if "state" in expect:
            value = host.state()
            return {"check": "state", "ok": value == expect["state"], "value": value, "expected": expect["state"]}
        if "expression" in expect:
            value = host.evaluate(expect["expression"])           # fprobe.dwarf.values の形: 数（scalar）で比べる
            return {"check": f"expression {expect['expression']}", "ok": _compare_ok(scalar(value), expect),
                    "value": value,
                    "expected": {k: expect[k] for k in COMPARE if k in expect}}
        if "memory" in expect:
            mem = expect["memory"]
            address = host.resolve(mem["address"])
            value = host.read_memory(address, mem.get("size", 4))
            return {"check": f"memory 0x{address:08X}", "ok": _compare_ok(value, expect), "value": value,
                    "expected": {k: expect[k] for k in COMPARE if k in expect}}
        recording = host.recording()
        if recording is None:
            raise RuntimeError("規則の判定には記録が要る（recording_start を先に）")
        hz = recording.meta["timebase_hz"]
        ruleset = RuleSet.parse({"name": name, "rules": expect["rules"]})
        result = check(recording, ruleset, t0=t_inject, t1=t_inject + round(observe_s * hz) + 1)
        return {"check": "rules", "ok": RANK[result["verdict"]] <= RANK["warn"], "value": result}
    except Exception as e:
        return {"check": next(iter(expect)), "ok": False, "error": f"{type(e).__name__}: {e}"}


def _run_trial(host: InjectHost, script: InjectScript, trial: Trial, repeat: int) -> dict:
    out: dict[str, Any] = {"id": trial.id, "repeat": repeat, "requirement": list(trial.requirement),
                           "description": trial.description, "when": trial.when, "injected": False, "t": None,
                           "timeout_s": trial.timeout_s, "observe_s": trial.observe_s}
    kind, params = next(iter(trial.when.items()))
    armed = False                                            # 仕掛けた後なら、途中で失敗しても注入が起きたかもしれない
    try:
        address = host.resolve(trial.write["address"])
        if address % 4:
            raise ValueError(f"割り込み書き込みは 32 ビットの語: 番地 0x{address:08X} は 4 の倍数でない")
        if "flip" in trial.write:
            previous = host.read_memory(address, 4)
            value = previous ^ trial.write["flip"]
            out["previous"] = previous
            out["note"] = "flip は仕掛けた時点の値から計算した（その後に変わった値には当たらない）"
        else:
            value = trial.write["value"]
        out.update(address=address, value=value)
        host.set_inject(address, value, trial.write.get("ap", 0))
        before = host.inject_status().count
        armed = True
        host.arm(trial.when)
        try:
            if kind == "host":
                host.sleep(float(params.get("delay_s", 0.0)))
                host.fire()
            deadline = host.monotonic() + trial.timeout_s
            status = host.inject_status()
            while status.count == before and host.monotonic() < deadline:
                host.sleep(POLL_S)
                status = host.inject_status()
        finally:
            host.disarm()
    except Exception as e:
        out.update(verdict="error", error=f"{type(e).__name__}: {e}")
        if armed:                                            # 注入が起きたかもしれないので、戻す手順は必ず試す
            _restore(host, trial, out)
        return out
    if status.count == before:
        out["verdict"] = "not_triggered"
    elif status.result != "ok":
        out.update(injected=True, t=status.time, verdict="inject_error",
                   error=f"割り込み書き込みの結果 {status.result}（ACK {status.ack}）")
    else:
        out.update(injected=True, t=status.time)
        host.sleep(trial.observe_s)
        checks = [_judge(host, e, status.time, trial.observe_s, f"{script.name}:{trial.id}") for e in trial.expect]
        out["checks"] = checks
        out["verdict"] = "pass" if all(c["ok"] for c in checks) else "fail"
    if out["injected"]:
        t_end = status.time + round(trial.observe_s * host.timebase_hz())
        host.note(INJECT_STREAM, "trial", status.time, t_end,
                  {"script": script.name, "trial": trial.id, "repeat": repeat, "address": out["address"],
                   "value": out["value"], "verdict": out["verdict"], "requirement": list(trial.requirement)})
    _restore(host, trial, out)
    return out


def _restore(host: InjectHost, trial: Trial, out: dict) -> None:
    try:
        for w in trial.restore.get("write") or []:
            host.write_memory(host.resolve(w["address"]), w["value"], w.get("size", 4))
        if trial.restore.get("reset"):
            host.reset(trial.restore["reset"])
        if trial.restore.get("resume"):
            host.resume()
    except Exception as e:
        out["restore_error"] = f"{type(e).__name__}: {e}"
        out["verdict"] = worst([out["verdict"], "error"])


def run_script(host: InjectHost, script: InjectScript) -> dict:
    """台本の試行を順に行い、試行ごとの結果と全体の判定を返す。最後に割り込み書き込みの中身を既定へ戻す。"""
    trials = []
    try:
        for trial in script.trials:
            for k in range(trial.repeat):
                trials.append(_run_trial(host, script, trial, k))
    finally:
        host.restore_default()
    verdicts = [t["verdict"] for t in trials]
    counts = {v: verdicts.count(v) for v in ("pass", "fail", "not_triggered", "inject_error", "error")}
    return {"name": script.name, "sha256": script.sha256, "requirements": dict(script.requirements),
            "verdict": worst(verdicts), "counts": counts, "trials": trials}
