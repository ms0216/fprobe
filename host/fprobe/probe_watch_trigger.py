"""変数のライブ観測の、条件つきの取得（watch_trigger / watch_capture。プリトリガ・ポスト、イベント記録の続きの読み）の部品（U43）。

WatchService（fprobe.probe_watch）に mixin される。条件と取得の本体は fprobe.watch_trigger。
"""

import time
from dataclasses import dataclass, field

from .collector import unwrap_events
from .events import INJECT_KINDS, KIND_SAMPLE, EventCursor, EventLog
from .watch_trigger import Capture, Condition
from .watch_value import _Assembler

PREFILL_LIMIT = 1 << 16                # プリトリガのために arm の時点でリングから読むイベントの上限


@dataclass
class _Trigger:
    capture: Capture
    cursor: EventCursor | None
    assembler: _Assembler
    hz: float
    skip_until: int = -1                                  # プリトリガで補った最後の時刻（カーソルの重複を捨てる）
    started: float = field(default_factory=time.monotonic)


class WatchTriggerMixin:
    """条件つきの取得。"""

    def watch_trigger(self, start: dict | None = None, stop: dict | None = None, pre_s: float = 0.0,
                      post_s: float | None = None, max_samples: int = 100_000) -> dict:
        """観測している変数の取得を条件で始める・止める（watch_set の後）。start / stop: {"variable", "op"（> >= < <= ==
        !=）, "value", "edge"} か {"event": "xtrig"}（クロストリガの割り込み書き込み。xtrig_route の to_inject に
        ロジアナのトリガ・DAP の一致・多段の条件をつなぐ）。start を省くといまから。pre_s: 始まりより前の分（プリ
        トリガ）、post_s: 始まりから取る長さ、max_samples: 件数の上限。結果は watch_capture。"""
        if not self._watched:
            raise RuntimeError("観測していない（watch_set を先に）")
        names = {w.name for w in self._watched}
        start_c = Condition.parse(start, names) if start is not None else None
        stop_c = Condition.parse(stop, names) if stop is not None else None
        if pre_s < 0 or (post_s is not None and post_s <= 0) or max_samples < 1:
            raise ValueError("pre_s ≥ 0、post_s > 0、max_samples ≥ 1")
        with self._host._lock:
            device = self._host.session.device
            hz = device.capabilities().timebase_hz
            self._end_watch_jobs()
            capture = Capture(start_c, stop_c, round(pre_s * hz), None if post_s is None else round(post_s * hz),
                              max_samples)
            sampled = any(not w.chase for w in self._watched)
            needs_events = sampled or any(c is not None and c.event for c in (start_c, stop_c))
            cursor, prefill = None, []
            if needs_events:
                log = EventLog(device)
                log.ensure_started()
                cursor = log.cursor()
                if capture.pre_ticks:
                    prefill = unwrap_events(log.read(min(log.capacity, PREFILL_LIMIT)), device.time())
            trigger = _Trigger(capture, cursor, _Assembler(self._watched), hz)
            self._prefill(trigger, prefill)
            self._watch_trigger = trigger
            if needs_events:
                self._host._add_job("watch.drain", 0.0, self._drain_once)
            if self._chase_series:
                self._host._add_job("watch.chase", 1.0 / self._chase_rate, self._chase_once)
        return self._capture_dict(trigger, include_samples=False)

    def _prefill(self, trigger: _Trigger, events: list) -> None:
        """プリトリガ: arm の時点でリングに残っている語と、ホストが読んだ値の直近を、時刻順に流し込む。"""
        updates = []
        for e in events:
            if e.kind == KIND_SAMPLE and e.ok:
                updates += [(e.time, name, value) for name, value in trigger.assembler.feed(e.address, e.data)]
            trigger.skip_until = max(trigger.skip_until, e.time)
        if updates or self._chase_series:
            latest = max([u[0] for u in updates] + [s[-1][0] for s in self._chase_series.values() if s] or [0])
            for name, series in self._chase_series.items():
                updates += [(t, name, v) for t, v in series if t >= latest - trigger.capture.pre_ticks]
        for t, name, value in sorted(updates, key=lambda u: u[0]):
            trigger.capture.feed(t, name, value)

    def _drain_once(self) -> None:
        """背景の仕事（_lock の中で）: イベント記録の続きを読み、条件つきの取得に流す。"""
        trigger = self._watch_trigger
        if trigger is None or trigger.capture.done:
            self._host._remove_job("watch.drain")
            return
        latest = None
        device = self._host.session.device
        for e in unwrap_events(trigger.cursor.take(), device.time()):
            if e.time <= trigger.skip_until:
                continue
            latest = e.time
            if e.kind == KIND_SAMPLE and e.ok:
                for name, value in trigger.assembler.feed(e.address, e.data):
                    trigger.capture.feed(e.time, name, value)
            elif e.kind in INJECT_KINDS:
                trigger.capture.event(e.time, "xtrig")
        trigger.capture.tick(device.time() if latest is None else latest)

    def _capture_dict(self, trigger: _Trigger, include_samples: bool = True) -> dict:
        c, hz = trigger.capture, trigger.hz
        us = (lambda t: None if t is None else t * 1e6 / hz)
        out = {"state": c.state, "reason": c.reason, "start": None if c.start is None else c.start.describe(),
               "stop": None if c.stop is None else c.stop.describe(), "pre_s": c.pre_ticks / hz,
               "post_s": None if c.post_ticks is None else c.post_ticks / hz, "max_samples": c.max_samples,
               "t_start_us": us(c.t_start), "t_end_us": us(c.t_end), "count": len(c.samples), "dropped": c.dropped,
               "lost_events": trigger.cursor.lost if trigger.cursor is not None else 0}
        if include_samples:
            samples: dict[str, list] = {w.name: [] for w in self._watched}
            for t, name, value in sorted(c.samples, key=lambda s: s[0]):
                samples.setdefault(name, []).append({"time_us": t * 1e6 / hz, "value": value})
            out["samples"] = samples
        return out

    def watch_capture(self, wait_s: float = 0.0) -> dict:
        """条件つきの取得（watch_trigger）の状態と結果（機器には触れない）。終わるまで wait_s 待つ。state: armed（条件を
        待つ）/ capturing / done（reason: stop / post / max_samples / cancelled）。samples は変数ごとの値の列（時刻は
        watch_series と同じ軸の µs）。"""
        trigger = self._watch_trigger
        if trigger is None:
            raise RuntimeError("条件つきの取得を仕掛けていない（watch_trigger を先に）")
        deadline = time.monotonic() + wait_s
        while not trigger.capture.done and time.monotonic() < deadline:
            time.sleep(0.01)
        with self._host._lock:
            return self._capture_dict(trigger)
