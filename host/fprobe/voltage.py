"""ターゲットの電圧（補助マイコンの ADC）の事実: 読みの統計、使う人が渡した電圧以下の区間、デーモンが読んだ電圧の履歴。

基準の水準（中央値等）は推定しない。低下の区間は使う人が渡した below_v 以下の連続する点だけ（渡さなければ出さない）。
"""

import collections
import time

__all__ = ["VoltageHistory", "dips", "stats"]

HISTORY_LIMIT = 100_000                 # デーモンが読んだ電圧を残す数（超えた古い読みは dropped に数える）


def stats(volts: list[float]) -> dict:
    """値の数・最小・最大・平均（無ければ None）。"""
    return {"count": len(volts), "min": min(volts, default=None), "max": max(volts, default=None),
            "mean": sum(volts) / len(volts) if volts else None}


def dips(points: list[tuple[int, float]], below_v: float) -> list[dict]:
    """below_v 以下の連続する点の区間（時刻の順）。t = 下がった最初の点、t_end = 戻った最初の点（戻らなければ最後の
    点）、at・min = いちばん低い点、under = below_v − min。"""
    out, cur = [], None
    for t, v in points:
        if v <= below_v:
            if cur is None:
                cur = {"t": t, "t_end": t, "at": t, "min": v}
            cur["t_end"] = t
            if v < cur["min"]:
                cur.update(at=t, min=v)
        elif cur is not None:
            cur["t_end"] = t
            out.append(cur)
            cur = None
    if cur is not None:
        out.append(cur)
    return [{**d, "under": below_v - d["min"]} for d in out]


class VoltageHistory:
    """デーモンが読んだ電圧（だれの probe.voltage でも）を、読んだ時刻（UNIX 秒）とともに残す。"""

    def __init__(self, limit: int = HISTORY_LIMIT):
        self.readings: collections.deque = collections.deque(maxlen=limit)
        self.dropped = 0

    def add(self, reading: dict, now: float | None = None) -> None:
        if len(self.readings) == self.readings.maxlen:
            self.dropped += 1
        self.readings.append({"time": time.time() if now is None else now, "volts": reading.get("volts"),
                              "over_range": bool(reading.get("over_range")), "live": bool(reading.get("live"))})

    def read(self, since: float | None = None) -> dict:
        """since（UNIX 秒。省くと残っているすべて）より後の読みと、その統計（測れた読み = live で範囲の中のものだけ
        数える。over_range・not_live はそれぞれの読みの数）。"""
        rows = [r for r in self.readings if since is None or r["time"] > since]
        volts = [r["volts"] for r in rows if r["live"] and not r["over_range"] and isinstance(r["volts"], (int, float))]
        return {"readings": rows, "stats": {**stats(volts), "readings": len(rows),
                                            "over_range": sum(r["over_range"] for r in rows),
                                            "not_live": sum(not r["live"] for r in rows)},
                "dropped": self.dropped, "limit": self.readings.maxlen}
