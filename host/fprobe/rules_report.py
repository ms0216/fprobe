"""規則の検査の結果の見せ方（U25）: 状態遷移図（dot）と、要件と対応づけた証跡（Markdown / JSON）。

検査そのものは fprobe.rules（check）。fprobe.rules が再公開する。
"""

import hashlib
import json
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any

from .recording import Recording
from .rules_eval import _Context, _state_series, recording_info
from .rules_model import _condition, worst


# --- 状態遷移図 ---------------------------------------------------------------------------------

def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def state_machine(recording: Recording, event: dict, field_name: str, labels: dict | None = None,
                  t0: int | None = None, t1: int | None = None) -> dict:
    """出来事の field の値を状態として、状態ごとの入った回数・滞在時間（µs。最後の状態は終わりが分からないので除く）、
    遷移ごとの回数と最初の時刻、Graphviz の dot。"""
    _condition(event, "event")
    ctx = _Context(recording, t0, t1)
    series = _state_series(ctx, event, field_name, labels or {})
    states: dict[str, dict] = {}
    dwell: dict[str, list[float]] = {}
    for t, name in series:
        states.setdefault(name, {"entries": 0, "first_t": t})["entries"] += 1
        dwell.setdefault(name, [])
    for (t, name), (t_next, _) in zip(series, series[1:]):
        dwell[name].append(ctx.us(t_next - t))
    for name, values in dwell.items():
        states[name]["dwell_us"] = ({"min": min(values), "max": max(values), "mean": round(statistics.fmean(values), 6),
                                     "total": round(sum(values), 6)} if values else None)
    transitions: dict[tuple[str, str], dict] = {}
    for (_, a), (t, b) in zip(series, series[1:]):
        transitions.setdefault((a, b), {"from": a, "to": b, "count": 0, "first_t": t})["count"] += 1
    lines = ["digraph states {", "  rankdir=LR;"]
    for name, info in states.items():
        d = info["dwell_us"]
        label = f"{name}\n{info['entries']} 回" + (f"・平均 {d['mean']:g} µs" if d else "")
        lines.append(f"  {_quote(name)} [label={_quote(label)}];")
    for x in transitions.values():
        lines.append(f"  {_quote(x['from'])} -> {_quote(x['to'])} [label=\"{x['count']}\"];")
    lines.append("}")
    return {"states": states, "transitions": list(transitions.values()), "changes": len(series),
            "initial": series[0][1] if series else None, "dot": "\n".join(lines) + "\n"}


# --- 証跡 ---------------------------------------------------------------------------------------

def _sha256_file(path: str | None) -> str | None:
    if not path:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    return hashlib.sha256(p.read_bytes()).hexdigest()


def evidence(rules: dict | None = None, inject: dict | None = None, recording: Recording | None = None) -> dict:
    """証跡の中身（render_report の JSON と同じ）。要件 → 規則・故障注入の試行 → 判定の対応表を作る。"""
    rec = recording_info(recording) if recording is not None else (rules or {}).get("recording") or \
        (inject or {}).get("recording")
    elf = (rec or {}).get("elf")
    requirements: dict[str, dict] = {}
    texts = dict((rules or {}).get("ruleset", {}).get("requirements", {}))
    texts.update((inject or {}).get("requirements", {}))
    for kind, items in (("rule", (rules or {}).get("rules", [])), ("inject", (inject or {}).get("trials", []))):
        for item in items:
            for req in item.get("requirement", []):
                entry = requirements.setdefault(req, {"text": texts.get(req), "items": []})
                entry["items"].append({"type": kind, "id": item["id"], "verdict": item["verdict"]})
    for req, text in texts.items():
        requirements.setdefault(req, {"text": text, "items": []})
    for entry in requirements.values():
        entry["verdict"] = worst(i["verdict"] for i in entry["items"]) if entry["items"] else "not_covered"
    verdicts = [x["verdict"] for x in (rules, inject) if x]
    return {"generated": datetime.now().isoformat(timespec="seconds"), "verdict": worst(verdicts),
            "recording": rec, "elf": {"path": elf, "sha256": _sha256_file(elf)} if elf else None,
            "requirements": requirements, "rules": rules, "inject": inject}


def _md_value(v: Any) -> str:
    if v is None:
        return ""
    text = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    return text.replace("|", "\\|").replace("\n", " ")


def render_report(format: str, rules: dict | None = None, inject: dict | None = None,
                  recording: Recording | None = None) -> str:
    """証跡の文書: format markdown / json。rules は check の結果、inject は故障注入（fprobe.inject）の結果。"""
    if rules is None and inject is None:
        raise ValueError("規則の検査（rules_check）も故障注入（inject_run）もまだ無い")
    data = evidence(rules, inject, recording)
    if format == "json":
        return json.dumps(data, ensure_ascii=False, indent=2)
    if format != "markdown":
        raise ValueError("format は markdown か json")
    out = ["# 試験の証跡", "", f"- 作成: {data['generated']}", f"- 総合判定: **{data['verdict']}**"]
    rec = data["recording"]
    if rec:
        out.append(f"- 記録: `{rec['name']}`（{_md_value(rec.get('title'))}。基板 {rec.get('board')}・設計 "
                   f"{rec.get('design')}・タイムベース {rec['timebase_hz']} Hz・範囲 {rec.get('span')}）")
    if data["elf"]:
        out.append(f"- ELF: `{data['elf']['path']}`（SHA-256 {data['elf']['sha256']}）")
    if rules:
        rs = rules["ruleset"]
        out.append(f"- 規則集: {rs['name']}（{rs.get('source') or '直接'}、SHA-256 {rs['sha256']}）")
    if inject:
        out.append(f"- 故障注入の台本: {inject['name']}（SHA-256 {inject.get('sha256')}）")
    out += ["", "## 要件ごとの判定", "", "| 要件 | 内容 | 判定 | 規則・試行 |", "|---|---|---|---|"]
    for req, entry in data["requirements"].items():
        items = "、".join(f"{i['type']}:{i['id']}={i['verdict']}" for i in entry["items"])
        out.append(f"| {req} | {_md_value(entry['text'])} | {entry['verdict']} | {items} |")
    if rules:
        out += ["", "## 規則ごとの期待値と実測", "", "| 規則 | 種類 | 判定 | 期待 | 実測 | 違反 |", "|---|---|---|---|---|---|"]
        for r in rules["rules"]:
            out.append(f"| {r['id']} | {r['kind']} | {r['verdict']} | {_md_value(r['expected'])} | "
                       f"{_md_value(r['measured'])} | {r['violations_total']} |")
        for r in rules["rules"]:
            if r["violations"]:
                out += ["", f"### 違反: {r['id']}", "", "| 時刻（記録の最初から µs） | t | 種類 | 内容 |", "|---|---|---|---|"]
                out += [f"| {v['at_us']} | {v['t']} | {v['kind']} | {_md_value(v['message'])} |" for v in r["violations"]]
                if r["violations_total"] > len(r["violations"]):
                    out.append(f"\nほか {r['violations_total'] - len(r['violations'])} 件")
            if "baseline" in r:
                out += ["", f"基準との差（{r['id']}）: {_md_value(r['baseline']['delta'])}"]
    if inject:
        out += ["", "## 故障注入", "", "| 試行 | 判定 | 注入の時刻 t | 番地 | 値 | 判定の内訳 |", "|---|---|---|---|---|---|"]
        for tr in inject["trials"]:
            checks = "、".join(f"{c['check']}={'ok' if c['ok'] else 'NG'}" for c in tr.get("checks", []))
            addr = tr.get("address")
            out.append(f"| {tr['id']} | {tr['verdict']} | {_md_value(tr.get('t'))} | "
                       f"{'' if addr is None else f'0x{addr:08X}'} | {_md_value(tr.get('value'))} | {checks} |")
    return "\n".join(out) + "\n"
