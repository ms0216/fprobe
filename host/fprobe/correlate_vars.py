"""相関の変数: 記録の変数（MCU のメモリ・FPGA の信号・ピン）を名前で引き、値の並び（_Series）にする（fprobe.correlate が使う）。"""

import math
import re
import statistics
from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Any

from .correlate_cond import ConditionError
from .fpga_signals import FpgaSignals, Signal
from .la_wave import _np
from .obs_value import number

KINDS = ("mcu", "fpga", "pin")

@dataclass
class _Series:
    """変数の値の並び（時刻順。変わった所と最初の値だけ）。"""
    times: list[int]
    values: list[int]
    period: float | None = None          # 元の読みの間隔（タイムベース。MCU のメモリの読み・ピンのサンプル）
    enum: dict[str, int] | None = None
    gaps: list[dict] = field(default_factory=list)   # 値の無い区間・読めなかった語（_gaps が名前を付けて返す）
    # 値はあるが時刻の一部が分からない区間（ピンの ch のずれが分からない等）。値の欠けではないので結果から外さず
    # （_holes に入れない）、gaps に時刻を示すだけ
    unsure: list[dict] = field(default_factory=list)
    reads: dict | None = None            # MCU のメモリの読みの事実（回数・間隔・いちばん長い空白）

    def at(self, t: int) -> int | None:
        i = bisect_right(self.times, t) - 1
        return self.values[i] if i >= 0 else None

    def instants(self, op: str) -> list[int]:
        memo = self.__dict__.setdefault("_instants", {})
        if op not in memo:
            memo[op] = self._instants_of(op)
        return memo[op]

    def arrays(self) -> tuple:
        """numpy の (時刻, 値)。一度だけ作って使い回す（候補ごとに作り直さない）。"""
        memo = self.__dict__.setdefault("_arrays", None)
        if memo is None:
            memo = self.__dict__["_arrays"] = (_np.asarray(self.times, dtype=_np.int64),
                                               _np.asarray(self.values, dtype=_np.int64))
        return memo

    def _instants_of(self, op: str) -> list[int]:
        times, values = self.times, self.values
        if op == "changed":                               # 並びは変わった所だけなので、最初の値の後は全部
            return times[1:]
        if op == "rose":
            return [t for t, a, b in zip(times[1:], values, values[1:]) if not a and b]
        return [t for t, a, b in zip(times[1:], values, values[1:]) if a and not b]


class _Variables:
    """記録の変数を名前で引き、使うものだけ値の並びにする。"""

    def __init__(self, rec: Any, t0: int | None, t1: int | None, seeds: dict | None = None,
                 gap_s: float | None = None):
        self.rec, self.t0, self.t1 = rec, t0, t1
        self.gap_s = gap_s                    # 使う側が明示したときだけ、これより長い読みの空白を欠けにする
        self.seeds = seeds or {}              # 変数の正体 → 範囲の前の最後の (時刻, 値)（少しずつ調べるとき）
        meta = rec.meta
        self.dups: set[tuple[str, str]] = set()      # 同じ名前が別の番地・チャンネルを指す (種類, 名前)。使うと ConditionError
        self.mcu: dict[str, int] = {}
        for a, name in (meta.get("sample.names") or {}).items():
            if name in self.mcu and self.mcu[name] != int(a, 16):
                self.dups.add(("mcu", name))
            self.mcu[name] = int(a, 16)
        self.fpga: dict[str, tuple[int, Signal]] = {}
        signals = FpgaSignals.from_dict(meta.get("fpga.signals"))
        for g, sigs in signals.groups.items():
            for s in sigs:
                self.fpga[s.name] = (g, s)
        self.pins: dict[str, int] = {}
        for names in (meta.get("pins"), meta.get("channel_names")):
            for ch, n in enumerate(names or []):
                if str(n) in self.pins and self.pins[str(n)] != ch:
                    self.dups.add(("pin", str(n)))
                self.pins[str(n)] = ch
        self._cache: dict[tuple[str, Any], _Series] = {}
        self._groups: dict[int, list[tuple[int, int]]] = {}
        self.hidden = 0                       # 範囲の中の標本型の見えない変化の数
        self.fpga_lost: list[dict] = []       # 範囲の中の FPGA の観測の欠落（stream fpga の gap。全部の組で共通）

    def resolve(self, name: str) -> tuple[str, Any, str]:
        """名前 → (種類, 鍵, 表示の名前)。"""
        kind, _, bare = name.partition(".") if name.split(".", 1)[0] in KINDS else ("", "", name)
        found = []
        for k in ("mcu", "pin"):
            if kind in ("", k) and (k, bare) in self.dups:
                raise ConditionError(f"{k}.{bare} は複数の{'番地' if k == 'mcu' else 'チャンネル'}に付いている（記録の名前の重複）")
        if kind in ("", "mcu"):
            if bare in self.mcu:
                found.append(("mcu", self.mcu[bare]))
            elif re.fullmatch(r"0x[0-9a-fA-F]+", bare):
                found.append(("mcu", int(bare, 16)))
        if kind in ("", "fpga") and bare in self.fpga:
            found.append(("fpga", self.fpga[bare]))
        if kind in ("", "pin"):
            if bare in self.pins:
                found.append(("pin", self.pins[bare]))
            elif re.fullmatch(r"ch\d+", bare):
                found.append(("pin", int(bare[2:])))
        if not found:
            raise ConditionError(f"変数 {name!r} が記録に無い（MCU のメモリの名前・番地、FPGA の信号、ピンの名前・ch<N>）")
        if len(found) > 1:
            raise ConditionError(f"{name!r} は複数ある: " + " / ".join(f"{k}.{bare}" for k, _ in found) + " で区別する")
        return found[0][0], found[0][1], name

    def ident(self, name: str) -> tuple:
        """変数の正体（種類と番地・信号の名前・ch。同じ変数を別の書き方で指しても同じ）。"""
        kind, key, _ = self.resolve(name)
        return self._ident_of(kind, key)

    @staticmethod
    def _ident_of(kind: str, key: Any) -> tuple:
        return kind, key[1].name if kind == "fpga" else key

    def series(self, name: str) -> _Series:
        kind, key, _ = self.resolve(name)
        ident = self._ident_of(kind, key)
        cached = self._cache.get(ident)
        if cached is not None:
            return cached
        s = {"mcu": self._mcu, "fpga": self._fpga, "pin": self._pin}[kind](key)
        seed = self.seeds.get(ident)
        if seed is not None and (not s.times or s.times[0] > seed[0]):
            if s.values and s.values[0] == seed[1]:
                s.times[0] = seed[0]                               # 値が同じ: 変化ではない。持ち越しの時刻から続く 1 つの値にする
            else:
                s.times.insert(0, seed[0])                         # 前の範囲の最後の値: 範囲の最初の変化を落とさない
                s.values.insert(0, seed[1])
        self._cache[ident] = s
        return s

    def _mcu(self, address: int) -> _Series:
        times, values, raw, failed = [], [], [], []
        runs: list[list[int]] = []                   # 続けて読めなかった読みのまとまり [前の読めた時刻, 最初の失敗, 最後の失敗, 件数]
        run_open = False
        # DAP の時刻が線上の点の回路の語は本体に値を読んだ瞬間の範囲 {t_lo, t_hi}（time-accuracy S2）: 変化はその前の読みの
        # t_lo より後、変わった値を読んだ語の t_hi 以前。時刻（e.t）は読みを頼んだ点。古い回路の語は範囲が無い（値が返った周期）
        windows: list[tuple[int | None, int | None]] = []
        line_time = False
        prev_lo: int | None = None
        for e in self.rec.iter_events(self.t0, self.t1, ["sample"], a=address):
            if e.kind != "word":                     # 誤りの区間（kind error。a は語の番号）は値ではない
                continue
            if isinstance(e.body, dict) and e.body.get("ack", 1) != 1:
                failed.append(e.t)
                if run_open:
                    runs[-1][2:] = [e.t, runs[-1][3] + 1]
                else:
                    runs.append([raw[-1] if raw else e.t, e.t, e.t, 1])
                    run_open = True
                continue
            if run_open:
                runs[-1].append(e.t)                 # 次の読めた時刻（欠けの終わり）
                run_open = False
            raw.append(e.t)
            body = e.body if isinstance(e.body, dict) else {}
            line_time = line_time or "t_lo" in body
            if not values or values[-1] != e.b:
                if values:
                    windows.append((prev_lo, body.get("t_hi")) if "t_lo" in body else (None, None))
                times.append(e.t)
                values.append(e.b)
            prev_lo = body.get("t_lo")
        intervals = [b - a for a, b in zip(raw, raw[1:])]
        period = statistics.median(intervals) if intervals else None
        gaps = []
        if self.gap_s is not None:                   # 使う側が明示した gap_s より長い空白（範囲の端から最初・最後の読みまでも）
            hz = self.rec.meta["timebase_hz"]
            edges = [self.t0, *raw, self.t1] if self.t0 is not None and self.t1 is not None else raw
            gaps = [{"reason": "reads_missing", "t0": a, "t1": b} for a, b in zip(edges, edges[1:])
                    if b - a > self.gap_s * hz]
            s = _Series(times, values)
            for g in gaps:                           # 前後の値: 欠けの間に動いた量（回数は分からない）
                g["before"], g["after"] = s.at(g["t0"]), s.at(g["t1"])
        for before, first, last, count, *after in runs:     # 読めなかった語ごとのまとまり: 前の読めた時刻から次の読めた時刻まで
            gaps.append({"reason": "read_failed", "t0": before, "t1": after[0] if after else last, "count": count})
        longest = max(zip(raw, raw[1:]), key=lambda ab: ab[1] - ab[0]) if intervals else None
        reads = {"count": len(raw), "failed": len(failed), "intervals": intervals,
                 "longest": list(longest) if longest else None,
                 "time_point": "read_request" if line_time else "value_returned", "change_windows": windows}
        return _Series(times, values, period, gaps=gaps, reads=reads)

    def _group(self, group: int) -> list[tuple[int, int]]:
        """組の値の記録 (時刻, 組の値)。組ごとに 1 回だけ読む（信号ごとに読み直さない）。"""
        if group not in self._groups:
            rows, hidden, lost = [], 0, []
            for e in self.rec.iter_events(self.t0, self.t1, ["fpga"]):
                if e.kind == "hidden":
                    hidden += 1
                elif e.kind == "gap":
                    lost.append({"reason": "fpga_lost", "t0": e.t, "t1": e.t, "count": e.a})
                elif e.kind.endswith("_untimed"):              # 時刻の決まらない記録（t は受けた時 = 上限）: 欠けとして
                    lost.append({"reason": "fpga_untimed", "t0": e.t, "t1": e.t, "upper_bound": True,
                                 "count": e.a if e.kind == "gap_untimed" else 0, "kind": e.kind})
                elif e.kind == "value" and e.a == group:
                    rows.append((e.t, e.b))
            self._groups[group] = rows
            self.hidden = hidden
            self.fpga_lost = lost
        return self._groups[group]

    def _fpga(self, key: tuple[int, Signal]) -> _Series:
        group, sig = key
        times, values = [], []
        for t, raw in self._group(group):
            v = sig.extract(raw)
            if not values or values[-1] != v:
                times.append(t)
                values.append(v)
        return _Series(times, values, None, {name: v for v, name in sig.enum.items()})

    def _pin(self, channel: int) -> _Series:
        """かたまりごとの変化点の索引（Waveform。読み手が覚えておく = 時間軸の表示と共有）から、範囲の中の値と変化。"""
        times, values = [], []
        period = None
        lo, hi = self.t0, self.t1
        covered, unsure = [], []
        chunk_skew = getattr(self.rec, "chunk_skew", None)
        for chunk, present in self.rec.wave_chunks(lo, hi):
            if not present or channel >= chunk.channels:
                continue
            wave, tps = self.rec.waveform(chunk), chunk.ticks_per_sample
            covered.append((chunk.t0, chunk.t0 + round(chunk.length * tps)))
            skew = chunk_skew(chunk) if chunk_skew is not None else ()
            if channel < len(skew) and skew[channel] is None:          # ずれが分からない: ずらさずに置いた（fprobe.timing）
                unsure.append({"reason": "pin_skew_unknown", "t0": covered[-1][0], "t1": covered[-1][1]})
            period = tps
            s0 = max(0, math.floor((lo - chunk.t0) / tps))
            s1 = min(chunk.length, math.ceil((hi - chunk.t0) / tps))
            if s1 <= s0:
                continue
            level = wave.level_at(channel, s0)
            if not values or values[-1] != level:
                times.append(chunk.t0 + round(s0 * tps))
                values.append(level)
            edges = list(wave.edges_between(channel, s0, s1))
            if not edges:
                continue
            if _np is not None:                                # 数百万の変化（ピンのクロック等）もまとめて（丸めは同じ偶数丸め）
                times.extend((chunk.t0 + _np.rint(_np.asarray(edges, dtype=_np.float64) * tps)).astype(_np.int64).tolist())
            else:
                times.extend(chunk.t0 + round(s * tps) for s in edges)
            n = len(edges)
            values.extend(([level ^ 1, level] * ((n + 1) // 2))[:n])    # 変化のたびに反転
            level ^= n & 1
        gaps, at = [], lo                                      # 波形の無い区間（流す前・後・かたまりの間）
        for a, b in sorted(covered):
            if a - at > (period or 0):                         # 1 サンプルより短い隙間は端の丸め
                gaps.append({"reason": "pins_missing", "t0": at, "t1": a})
            at = max(at, b)
        if hi - at > (period or 0):
            gaps.append({"reason": "pins_missing", "t0": at, "t1": hi})
        return _Series(times, values, period, gaps=gaps, unsure=unsure)

def _target(series: _Series, text: str, name: str) -> int:
    if series.enum and text in series.enum:
        return series.enum[text]
    n = number(text)
    if n is None:
        raise ConditionError(f"{name} の値 {text!r} が数でも列挙の名前でもない")
    return n
