"""機器なしで画面を試すデモ（fake_daemon.py --demo）の一部: Data Sampling（watch.*）・Dashboard・Power（電圧）のデモの値（G03・G10）。

DemoProbe（fake_demo.py）に混ぜる mixin。DemoProbe の偽の RAM（self._mem）・本物の ELF（self._elf）・デモの記録
（self._elf._view。tests/demo_recording.py）・実行の模擬（self.counter・self.state）を使う。値の形は本物の Probe の
同じ名前のメソッドと同じにする（画面はデモと本物を区別しない）。

- 走行中のターゲットの模擬（_sim_advance）: 走っている間、偽の RAM の変数を時間で動かす。counter は 1 ms に 4 増え
  （デモの記録の sample.word の COUNTER と同じ形）、sensors[0].value は gain に比例する振幅の正弦波 + 雑音（記録の
  SENSOR と同じ 2048 ± 1500。gain を書くと振幅が変わる）、sensors[1].value はのこぎり波、sensors[2].value は雑音。
  enabled（ビットフィールド）を 0 にしたセンサの値は止まる。止まっている間は RAM の値のまま（Watch・Memory と同じ）。
- 観測（watch_*）: 本物のサンプラの代わりに背景の糸が周期的に RAM を読む（決まった番地の式は番地と型を一度だけ
  引き、ポインタをたどる式は毎回評価し直す = method host）。時刻はタイムベース 10 MHz（デモの記録と同じ）の
  デモの開始からのカウント。速すぎる周期は飛ばした周期（overruns）に数える。条件つきの取得は本物の Capture。
- 電圧（target_voltage）: 3.28〜3.31 V をゆっくり揺れ、20 秒ごとに 0.6 秒ほど 3.0 V 近くまで下がる。
"""

from __future__ import annotations

import collections
import math
import random
import struct
import threading
import time

from fprobe.dwarf.cexpr import evaluate
from fprobe.dwarf.info import FrameView
from fprobe.dwarf.location import NotAvailable
from fprobe.dwarf.typesys import type_name
from fprobe.dwarf.values import encode
from fprobe.probe_watch import _RAW, ValueStats, WatchTools, _read_value, measure, pair_latest, rates, xy_result
from fprobe.sampler import plan_slots
from fprobe.voltage import VoltageHistory
from fprobe.watch_trigger import Capture, Condition

__all__ = ["SamplingDemo"]

HZ = 10_000_000                       # タイムベース（デモの記録と同じ 100 ns）
SERIES_LIMIT = 4096                   # 式ごとに残す値（本物の READ_LIMIT と同じ）
POLL_HZ = 200                         # 背景の糸が読む速さの上限（実際の周期はこれと rate_hz の遅い方）
SAMPLER_WORDS_HZ = 400_000            # 模擬のサンプラが 1 秒に読める語の数（これを超える周期は飛ばす）
SENSOR_SIZE = 32


class _NeedsMemory(Exception):
    pass


def _no_memory(address: int, length: int) -> bytes:
    raise _NeedsMemory()


def _no_register(number: int) -> int:
    raise NotAvailable("走行中の観測ではレジスタ（ローカル変数）は使えない（大域・静的の式にする）")


class _Watch:
    """観測している式 1 つ。chase なら value は毎回式を評価する。"""

    def __init__(self, name: str, address: int | None, size: int, value=None, raw: bool = False):
        self.name, self.address, self.size, self.value, self.raw = name, address, size, value, raw
        self.series: collections.deque = collections.deque(maxlen=SERIES_LIMIT)
        self.stats = ValueStats()                                      # 本物と同じく watch_set からのすべての値

    @property
    def chase(self) -> bool:
        return self.address is None

    def describe(self) -> dict:
        return {"name": self.name, "address": None if self.address is None else f"0x{self.address:08X}",
                "size": self.size, "type": None if self.raw else type_name(self.value.type),
                "method": "host" if self.chase else "sampler"}


class SamplingDemo:
    def _init_sampling(self) -> None:
        """DemoProbe.__init__ から（ELF を読み、接続・停止した後）。"""
        self._sim_lock = threading.RLock()
        self._sim_last = time.monotonic()
        self._sim_carry = 0.0
        self._sim_rng = random.Random(20261018)
        self._sensors = self._values.info.lookup("sensors").address
        self._watches: list[_Watch] = []
        self._watch_rejected: list[dict] = []
        self._watch_rate = 0.0
        self._chase_rate = 0.0
        self._watch_started = 0.0
        self._watch_client = None                          # 最後に watch_set した呼び出し元（本物と同じ）
        self._watch_thread: threading.Thread | None = None
        self._watch_stop_flag = threading.Event()
        self._capture: Capture | None = None
        self._volt_started = time.monotonic()
        self._voltage_history = VoltageHistory()

    # --- ターゲットの模擬 ---------------------------------------------------------------------------

    def _ticks(self) -> int:
        return int((time.monotonic() - self.started) * HZ)

    def _sim_advance(self) -> None:
        """走っている間の時間の分だけ、偽の RAM の変数を進める。"""
        with self._sim_lock:
            now = time.monotonic()
            dt, self._sim_last = now - self._sim_last, now
            if self.state != "RUNNING" or not self.connected:
                return
            self._sim_carry += dt * 4000                               # counter: 1 ms に 4
            step = int(self._sim_carry)
            self._sim_carry -= step
            self.counter = (self.counter + step) & 0xFFFF_FFFF
            t = now - self.started
            for i in range(3):
                at = self._sensors + i * SENSOR_SIZE
                if not self._mem.word(at + 28) & 1:                    # enabled = 0: 値は止まる
                    continue
                gain = int.from_bytes(self._mem.read(at + 2, 2), "little")
                if i == 0:
                    value = 2048 + 15 * gain * math.sin(t * 1000 / 70) + self._sim_rng.gauss(0, 60)
                elif i == 1:
                    value = (t % 2.0) * 50                             # 0〜100 を 2 秒で
                else:
                    value = 25 + self._sim_rng.gauss(0, 0.3)
                self._mem.write(at + 4, struct.pack("<f", value))

    # --- 観測 ---------------------------------------------------------------------------------------

    def _watch_target(self, expression: str) -> _Watch:
        raw = _RAW.fullmatch(expression.strip())
        if raw:
            return _Watch(expression, int(raw[1], 16), int(raw[2] or 4), raw=True)
        try:
            v = evaluate(expression, self._values.info, FrameView(None, _no_memory, _no_register))
            chase = False
        except _NeedsMemory:
            v = evaluate(expression, self._values.info, FrameView(None, self._mem.read, _no_register))
            chase = True
        if v.address is None or v.type.size is None:
            raise ValueError(f"{expression!r} はメモリ上の値ではない（レジスタ・定数・不完全型）")
        return _Watch(expression, None if chase else v.address, v.type.size, v)

    def _read_watch(self, w: _Watch) -> dict:
        if w.raw:                                  # 番地の式: 符号なし整数（本物の probe_watch と同じ int の形）
            return {"kind": "int", "value": int.from_bytes(self._mem.read(w.address, w.size), "little")}
        v = w.value
        if w.chase:
            v = evaluate(w.name, self._values.info, FrameView(None, self._mem.read, _no_register))
        return _read_value(v, self._mem.read)

    def watch_set(self, expressions, rate_hz=1000.0, ap=0, chase_rate_hz=100.0, jitter=0, caller=None):
        if not 0 < chase_rate_hz <= 10_000:
            raise ValueError("chase_rate_hz は 0 より大きく 10000 以下")
        if not 0 <= jitter <= 15:
            raise ValueError("jitter は 0〜15")
        watches, rejected = WatchTools._watch_targets(self, expressions)     # 誤った式は外して残りを取る（本物と同じ）
        sampled = [w for w in watches if not w.chase]
        slots = plan_slots([(w.address, w.size) for w in sampled]) if sampled else []
        if sampled and not 1 <= round(100e6 / rate_hz) < 1 << 32:
            raise ValueError(f"周期 {1 / rate_hz} 秒は設定できない")
        self._end_watch()
        with self._sim_lock:
            self._watches, self._watch_rate, self._chase_rate = watches, float(rate_hz), float(chase_rate_hz)
            self._watch_rejected = rejected
            self._watch_started = time.monotonic()
            self._watch_client = dict(caller) if caller is not None else None
            self._capture = None
        if watches:
            self._watch_stop_flag = threading.Event()
            self._watch_thread = threading.Thread(target=self._watch_loop, args=(self._watch_stop_flag,), daemon=True)
            self._watch_thread.start()
        return {"watches": [w.describe() for w in watches], "rate_hz": rate_hz, "chase_rate_hz": chase_rate_hz,
                "slots": [{"address": f"0x{a:08X}", "words": n} for a, n in slots], "rejected": rejected}

    def _watch_loop(self, stop: threading.Event) -> None:
        rate = min(self._watch_rate or POLL_HZ, POLL_HZ)
        while not stop.wait(1.0 / rate):
            self._sample_once()

    def _sample_once(self) -> None:
        self._sim_advance()
        with self._sim_lock:
            t = self._ticks()
            for w in self._watches:
                try:
                    value = self._read_watch(w)
                except Exception:                                       # NULL を指した等: その周期は値なし
                    continue
                w.series.append((t, value))
                w.stats.add(value)
                if self._capture is not None:
                    self._capture.feed(t, w.name, value)
            if self._capture is not None:
                self._capture.tick(t)

    def _end_watch(self) -> None:
        self._watch_stop_flag.set()
        if self._watch_thread is not None:
            self._watch_thread.join(timeout=1.0)
            self._watch_thread = None
        with self._sim_lock:
            if self._capture is not None:
                self._capture.cancel(self._ticks())

    def _sampler_status(self, busy: bool) -> dict:
        """本物の watch_status のサンプラの形（watch_set からの巡回の数・誤り・飛ばした周期と、FPGA の起動からの数
        device、最後の ACK・動いているか）。"""
        words = sum(len(range(w.address // 4, (w.address + w.size + 3) // 4)) for w in self._watches if not w.chase)
        elapsed = time.monotonic() - self._watch_started
        able = min(self._watch_rate, SAMPLER_WORDS_HZ / max(words, 1))
        counts = {"rounds": int(elapsed * able), "errors": 0, "overruns": int(elapsed * (self._watch_rate - able))}
        boot = int((self._watch_started - self.started) * 1000)               # 起動からの数（デモの開始から 1 kHz）
        return {**counts, "device": {k: v + (boot if k == "rounds" else 0) for k, v in counts.items()},
                "last_ack": 1, "busy": busy}

    def sampler_status(self):
        """本物の sampler_status の形（デモでは観測だけがサンプラを使う）。"""
        with self._sim_lock:
            sampled = any(not w.chase for w in self._watches)
            return {"user": "watch" if sampled else None, "release": "watch.stop" if sampled else None,
                    "since": time.time() - (time.monotonic() - self._watch_started) if sampled else None,
                    "client": self._watch_client if sampled else None}

    def watch_stop(self):
        sampled = any(not w.chase for w in self._watches)
        status = self._sampler_status(False) if sampled else None
        self._end_watch()
        with self._sim_lock:
            self._watches = []
        return status if status is not None else {"stopped": True}

    def watch_status(self):
        with self._sim_lock:
            sampled = any(not w.chase for w in self._watches)
            c = self._capture
            out = {"watches": [w.describe() for w in self._watches], "rejected": list(self._watch_rejected),
                   "stats": {w.name: w.stats.as_dict() for w in self._watches},
                   "stats_lost_events": 0 if sampled else None,
                    "since_start_s": time.monotonic() - self._watch_started if self._watches else None,
                    "rate_hz": self._watch_rate, "chase_rate_hz": self._chase_rate, "chase_errors": {},
                    "client": self._watch_client,
                    "capture": None if c is None else {"state": c.state, "reason": c.reason},
                    **(self._sampler_status(True) if sampled else {})}
            return {**out, **rates(out)}

    def _need_watch(self) -> None:
        if not self._watches:
            raise RuntimeError("観測していない（watch_set を先に）")

    def watch_values(self):
        self._need_watch()
        with self._sim_lock:
            return {"values": [{"name": w.name, "time_us": w.series[-1][0] * 1e6 / HZ, "value": w.series[-1][1]}
                               if w.series else {"name": w.name, "time_us": None, "value": None}
                               for w in self._watches]}

    def watch_series(self, name, last=1000):
        self._need_watch()
        with self._sim_lock:
            w = next((w for w in self._watches if w.name == name), None)
            if w is None:
                raise KeyError(f"{name!r} は観測していない")
            kept = list(w.series)
            samples = kept[-min(last, SERIES_LIMIT):]
        older = len(kept) > len(samples)                     # 読まなかった古い値（本物の from_us と同じ意味）
        return {"name": name, "samples": [{"time_us": t * 1e6 / HZ, "value": v} for t, v in samples],
                "from_us": samples[0][0] * 1e6 / HZ if older and samples else None}

    def watch_xy(self, x, y, last=1000, max_dt_us=None):
        self._need_watch()
        with self._sim_lock:
            series = {w.name: list(w.series)[-min(last, SERIES_LIMIT):] for w in self._watches}
        for name in (x, y):
            if name not in series:
                raise KeyError(f"{name!r} は観測していない")
        us = 1e6 / HZ
        pairs, unpaired, excluded = pair_latest(series[x], series[y], None if max_dt_us is None else max_dt_us / us)
        return xy_result(x, y, pairs, unpaired, excluded, max_dt_us, us, {x: None, y: None})

    def watch_measure(self, a_us, b_us, names=None, source="live"):
        self._need_watch()
        with self._sim_lock:
            if source == "live":
                points = {w.name: [(t * 1e6 / HZ, v) for t, v in w.series] for w in self._watches}
            elif source == "capture":
                if self._capture is None:
                    raise RuntimeError("条件つきの取得が無い（watch_trigger を先に）")
                points = {w.name: [] for w in self._watches}
                for t, name, value in sorted(self._capture.samples, key=lambda x: x[0]):
                    points.setdefault(name, []).append((t * 1e6 / HZ, value))
            else:
                raise ValueError("source は live か capture")
        for n in names or []:
            if n not in points:
                raise KeyError(f"{n!r} は観測していない")
        return measure({n: points[n] for n in (names or points)}, a_us, b_us)

    def watch_trigger(self, start=None, stop=None, pre_s=0.0, post_s=None, max_samples=100_000):
        self._need_watch()
        names = {w.name for w in self._watches}
        start_c = Condition.parse(start, names) if start is not None else None
        stop_c = Condition.parse(stop, names) if stop is not None else None
        if pre_s < 0 or (post_s is not None and post_s <= 0) or max_samples < 1:
            raise ValueError("pre_s ≥ 0、post_s > 0、max_samples ≥ 1")
        with self._sim_lock:
            if self._capture is not None:
                self._capture.cancel(self._ticks())
            capture = Capture(start_c, stop_c, round(pre_s * HZ), None if post_s is None else round(post_s * HZ),
                              max_samples)
            latest = max((w.series[-1][0] for w in self._watches if w.series), default=0)
            for t, name, value in sorted(((t, w.name, v) for w in self._watches for t, v in w.series
                                          if t >= latest - capture.pre_ticks), key=lambda s: s[0]):
                capture.feed(t, name, value)                            # プリトリガ: 直近の値から
            self._capture = capture
            return self._capture_dict(capture, include_samples=False)

    def _capture_dict(self, c: Capture, include_samples: bool = True) -> dict:
        us = (lambda t: None if t is None else t * 1e6 / HZ)
        out = {"state": c.state, "reason": c.reason, "start": None if c.start is None else c.start.describe(),
               "stop": None if c.stop is None else c.stop.describe(), "pre_s": c.pre_ticks / HZ,
               "post_s": None if c.post_ticks is None else c.post_ticks / HZ, "max_samples": c.max_samples,
               "t_start_us": us(c.t_start), "t_end_us": us(c.t_end), "count": len(c.samples), "dropped": c.dropped,
               "lost_events": 0}
        if include_samples:
            samples: dict[str, list] = {w.name: [] for w in self._watches}
            for t, name, value in sorted(c.samples, key=lambda s: s[0]):
                samples.setdefault(name, []).append({"time_us": t * 1e6 / HZ, "value": value})
            out["samples"] = samples
        return out

    def watch_capture(self, wait_s=0.0):
        capture = self._capture
        if capture is None:
            raise RuntimeError("条件つきの取得を仕掛けていない（watch_trigger を先に）")
        deadline = time.monotonic() + wait_s
        while not capture.done and time.monotonic() < deadline:
            time.sleep(0.01)
        with self._sim_lock:
            return self._capture_dict(capture)

    def watch_write(self, expression, value):
        self._need()
        self._sim_advance()
        raw = _RAW.fullmatch(expression.strip())
        with self._sim_lock:
            if raw:
                address, size = int(raw[1], 16), int(raw[2] or 4)
                if isinstance(value, (str, float)) or size not in (1, 2, 4, 8):
                    raise ValueError("番地には 1 / 2 / 4 / 8 バイトの整数を書く")
                self._mem.write(address, (int(value) & ((1 << 8 * size) - 1)).to_bytes(size, "little"))
                return {"expression": expression, "address": f"0x{address:08X}", "type": None,
                        "value": {"kind": "int", "value": int.from_bytes(self._mem.read(address, size), "little")},
                        "atomic": True}
            v = evaluate(expression, self._values.info, FrameView(None, self._mem.read, _no_register))
            if v.address is None:
                raise ValueError(f"{expression!r} はメモリ上に無い（レジスタ・定数には書かない）")
            atomic = v.bit_size is None
            if atomic:
                self._mem.write(v.address, encode(v.type, value))
            else:
                self._values.write(expression, value, None)
            return {"expression": expression, "address": f"0x{v.address:08X}", "type": type_name(v.type),
                    "value": _read_value(v, self._mem.read), "atomic": atomic}

    # --- 電圧・記録 -----------------------------------------------------------------------------------

    def target_voltage(self):
        reading = self._demo_voltage()
        self._voltage_history.add(reading)                             # 本物と同じくデーモンが読んだ電圧を残す
        return reading

    def probe_voltage_history(self, since=None):
        return self._voltage_history.read(since)

    def _demo_voltage(self):
        """3.28〜3.31 V をゆっくり揺れ、20 秒ごとに 0.6 秒ほど下がる（負荷の入り。最低 3.0 V 近く）。"""
        t = time.monotonic() - self._volt_started
        phase = t % 20.0
        dip = 0.28 * math.exp(-((phase - 12.0) / 0.25) ** 2)
        volts = 3.295 + 0.012 * math.sin(t / 7.0) + self._sim_rng.gauss(0, 0.002) - dip
        samples = int(t * 100) + 1
        return {"volts": round(volts, 4), "over_range": False, "live": True, "samples": samples + 1,
                "samples_before": samples, "window_s": 0.01, "errors": 0}

    # 記録の読み（recording_find）は fake_demo_search.py（SearchDemo）
