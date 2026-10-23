"""perf の出力の読み取り（事実のまま。perf の計算した比 perf_metric は perf の値として分けて返す）。

- `perf stat -x,` の行: 値,単位,イベント,走った時間,走った割合,perf の比,その単位（数えられなければ値は <not counted> 等）。
- `perf script -F comm,pid,tid,cpu,time,period,event,ip,sym,dso` の行（時刻は µs。--ns は --clockid と組むと書式が崩れた）。
"""
from __future__ import annotations

import re

__all__ = ["parse_perf_sample", "parse_perf_stat"]

_SAMPLE = re.compile(r"^\s*(?P<comm>.*?)\s+(?P<pid>-?\d+)/(?P<tid>-?\d+)\s+\[(?P<cpu>\d+)\]\s+(?P<sec>\d+)\.(?P<us>\d{6}):"
                     r"\s+(?P<period>\d+)\s+(?P<event>\S+):\s+(?P<ip>[0-9a-f]+)\s+(?P<sym>.*?)\s+\((?P<dso>.*)\)\s*$")   # dso は "(deleted)" 等の括弧を含みうる


def parse_perf_sample(line: str) -> dict | None:
    m = _SAMPLE.match(line)
    if m is None:
        return None
    return {"comm": m["comm"], "pid": int(m["pid"]), "tid": int(m["tid"]), "cpu": int(m["cpu"]),
            "t_ns": int(m["sec"]) * 1_000_000_000 + int(m["us"]) * 1000, "period": int(m["period"]),
            "event": m["event"], "ip": int(m["ip"], 16), "sym": m["sym"], "dso": m["dso"]}


def _num(text: str) -> int | float | None:
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return None


def parse_perf_stat(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        f = line.split(",")
        if len(f) < 5 or not f[2]:
            continue
        value = _num(f[0])
        row = {"event": f[2], "value": value, "unit": f[1], "run_time": _num(f[3]), "run_pct": _num(f[4])}
        if value is None:
            row["counted"] = f[0]                            # <not counted> / <not supported>
        if len(f) >= 7 and f[5]:
            row["perf_metric"] = {"value": _num(f[5]), "unit": f[6]}
        rows.append(row)
    return rows
