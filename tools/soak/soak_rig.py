"""fprobe の連続試験の実機側（PYNQ-Z2 の fprobe を Ethernet で）。soak.py のシナリオが呼ぶ Rig の実機の実装と、
観測の取り込みの型。soak.py からは読まれるだけで、soak.py を読まない（試験は模擬の Rig で soak.py だけを使う）。"""
from __future__ import annotations

import contextlib
import os
import tempfile
import time
from dataclasses import dataclass

# 信号の定義（手本 test_fpga_obs_scenarios_hw.py と同じ組 0 + 組 1）
SIGNALS = {"groups": {"0": [{"name": "mark", "lsb": 0, "width": 1}, {"name": "counter", "lsb": 1, "width": 15, "format": "dec"}],
                      "1": [{"name": "hi", "lsb": 0, "width": 16, "format": "dec"}]}}
# chaos_obs が設定する条件（トリガの条件。soak.py の検査と同じ文字列）
CHAOS_CONDITION = "mark rose"


@dataclass
class ObsCapture:
    """観測の 1 回の取り込み。values = [(value, target_cycles, time_fprobe か None)]（kind が value のものだけ）。"""
    values: list[tuple[int, int, float | None]]
    gaps: int
    event_lost: int
    status: dict
    timebase_hz: float
    clock_hz: float



OBS_OPEN = dict(chain={"ir_len": 4}, user_ir=2, half_period=5, words=64, sync_period=20)   # 手本 test_fpga_obs_hw.py と同じ


class ObsOpenFailure(RuntimeError):
    def __init__(self, kind: str, message: str):
        super().__init__(f"{kind}: {message}")
        self.kind = kind


class RealRig:
    """実機の Rig。手本は gateware/tests/hil/ の test_fpga_obs_hw.py・test_sampler_hw.py・test_pynq_deep_hw.py。
    各メソッドは終わるとき（例外でも）装置を元の状態に戻す。"""

    def __init__(self, host: str, mcu_target: str = "stm32f401xe", swd_hz: int = 4_000_000):
        self.host, self.mcu_target, self.swd_hz = host, mcu_target, swd_hz
        self.probe = self.session = None
        # Probe（API の実装）が条件・信号の定義を FPROBE_HOME に保存する。利用者の保存を触らないよう使い捨ての場所にする
        os.environ["FPROBE_HOME"] = tempfile.mkdtemp(prefix="soak_home_")
        self.connect()

    def connect(self):
        from fprobe.mcp_server import Probe
        self.probe = Probe(eth=self.host, link_up_wait_s=15.0)   # リンクの確立を待つ秒数（この道具の値）
        self.session = self.probe.session
        if not self.session.device.capabilities().fpga_obs:
            raise RuntimeError("この構成には FPGA の観測が無い")

    def close(self):
        if self.probe is not None:
            for stop in (self.probe.fpga_obs_close, self.probe.events_stop, self.probe.target_disconnect):
                with contextlib.suppress(Exception):
                    stop()
            with contextlib.suppress(Exception):
                self.probe.close()
            self.probe = self.session = None

    def reconnect(self):
        self.close()
        self.connect()

    @property
    def device(self):
        return self.session.device

    def obs_capture(self, seconds: float, group: int = 0) -> ObsCapture:
        from fprobe.events import EventLog
        from fprobe.fpga_obs import FpgaObs
        d = self.device
        obs, log = FpgaObs(d), EventLog(d)
        try:
            obs.open(**OBS_OPEN)
            obs.select(group)
            log.start()
            cursor = log.cursor()                           # 続きから読む。リングは 13 万件で、静かな信号でも毎秒約 1650 件の刻みが出るので、観測中に少しずつ読む
            obs.start()
            events = []
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                time.sleep(min(2.0, max(0.0, deadline - time.monotonic())))
                events += cursor.take()
            obs.stop()
            status = obs.status()
            time.sleep(0.2)
            events += cursor.take()
            log.stop()
            lost = cursor.lost
            samples = obs.samples(events)
        finally:
            with contextlib.suppress(Exception):
                log.stop()
            with contextlib.suppress(Exception):
                obs.close()
        caps = d.capabilities()
        return ObsCapture([(s.value, s.target_cycles, s.time_fprobe) for s in samples if s.kind == "value" and s.group == group],
                          sum(1 for s in samples if s.kind == "gap"), lost, status, caps.timebase_hz, obs.clock_hz)

    def obs_open_close_once(self) -> int:
        from fprobe.fpga_obs import FpgaObs
        obs = FpgaObs(self.device)
        try:
            return obs.open(**OBS_OPEN)["id"]
        except RuntimeError as e:            # open の失敗の理由の文言から分ける（FpgaObs は種類を持たない）: id_scans の上限か、答えない（stuck）か
            raise ObsOpenFailure("id_timeout" if "id_scans" in str(e) else "no_answer", str(e)) from e
        finally:
            with contextlib.suppress(Exception):
                obs.close()

    @contextlib.contextmanager
    def mcu(self):
        with self.session.debug(target_override=self.mcu_target, frequency=self.swd_hz, connect_mode="attach") as debug:
            yield debug.target

    def sampler_run(self, rate_hz: float, seconds: float, address: int) -> dict:
        from fprobe.events import KIND_SAMPLE, EventLog
        from fprobe.sampler import Sampler
        d = self.device
        with self.mcu():
            s, log = Sampler(d), EventLog(d)
            try:
                s.configure([(address, 4)], period_s=1 / rate_hz)
                log.start()
                written0 = log.counts()[0]
                s.start()
                time.sleep(seconds)
                s.stop()
                time.sleep(0.01)
                log.stop()
                status = s.status()
                written = log.counts()[0] - written0
                values = {e.data for e in log.read() if e.kind == KIND_SAMPLE}
            finally:
                with contextlib.suppress(Exception):
                    s.stop()
                with contextlib.suppress(Exception):
                    log.stop()
        return {"errors": status["errors"], "overruns": status["overruns"], "written": written,
                "distinct_values": len(values)}

    def la_capture(self, channel: int) -> dict:
        from fprobe.la import DeepAnalyzer
        la = DeepAnalyzer(self.device)
        la.start(rle=False, post_beats=200_000)
        la.wait(5)
        cap = la.read(before_beats=0)
        x = cap.channel(channel)
        return {"rising_s": [(i + 1) * cap.sample_period_s for i, (a, b) in enumerate(zip(x, x[1:])) if not a and b],
                "sample_period_s": cap.sample_period_s}

    def la_deep_capture(self, rate_hz: float, rle: bool, pretrigger: int, post_beats: int, channel: int) -> dict:
        from fprobe import generated as g
        from fprobe.la import DeepAnalyzer
        la = DeepAnalyzer(self.device)
        try:
            period = la.start(rate_hz=rate_hz, rle=rle, pretrigger=pretrigger, post_beats=post_beats)
            la.wait(10)
            cap = la.read()                                  # トリガの前に残っている全部から後まで
        finally:
            with contextlib.suppress(Exception):
                self.device.write32(g.LA_DEEP_CONTROL, 0)
        x = cap.channel(channel)
        return {"rising_s": [(i + 1) * cap.sample_period_s for i, (a, b) in enumerate(zip(x, x[1:])) if not a and b],
                "trigger": cap.trigger, "sample_period_s": cap.sample_period_s or period}

    def la_obs_align(self, channel: int, seconds: float) -> dict:
        """手本 test_fpga_obs_hw.py の test_o2: 観測を走らせ、途中でロジアナを取る。辺の時刻は fprobe のタイムベースの刻み。"""
        from fprobe.events import EventLog
        from fprobe.fpga_obs import FpgaObs
        from fprobe import generated as g
        from fprobe.la import DeepAnalyzer
        d = self.device
        obs, log = FpgaObs(d), EventLog(d)
        try:
            obs.open(**OBS_OPEN)
            obs.select(0)
            log.start()
            cursor = log.cursor()
            obs.start()
            time.sleep(seconds / 2)
            la = DeepAnalyzer(d)
            la.start(rle=False, post_beats=200_000)
            la.wait(5)
            cap = la.read(before_beats=0)
            time.sleep(seconds / 2)
            obs.stop()
            time.sleep(0.2)
            events = cursor.take()
            log.stop()
            samples = obs.samples(events)
        finally:
            with contextlib.suppress(Exception):
                self.device.write32(g.LA_DEEP_CONTROL, 0)
            with contextlib.suppress(Exception):
                log.stop()
            with contextlib.suppress(Exception):
                obs.close()
        hz = d.capabilities().timebase_hz
        x = cap.channel(channel)
        la_edges = [cap.trigger_time + (i + 1 - cap.trigger) * cap.sample_period_s * hz
                    for i, (a, b) in enumerate(zip(x, x[1:])) if a != b]
        values = [v for v in samples if v.kind == "value" and v.group == 0 and v.time_fprobe is not None]
        obs_edges = [b.time_fprobe for a, b in zip(values, values[1:]) if (a.value ^ b.value) & 1]
        return {"la_edges": la_edges, "obs_edges": obs_edges, "timebase_hz": hz}

    def eventlog_run(self, reads: int, address: int) -> dict:
        from fprobe.events import EventLog
        log = EventLog(self.device)
        try:
            with self.mcu() as m:
                log.start()
                for _ in range(reads):
                    m.read32(address)
                log.stop()
                written, lost = log.counts()
                time.sleep(0.05)
                later = log.counts()[0]
                n = len(log.read())
        finally:
            with contextlib.suppress(Exception):
                log.stop()
        return {"written": written, "lost": lost, "read": n, "capacity": log.capacity, "written_later": later}

    def obs_conditions(self, seconds: float, signals: dict, conditions: list[str]) -> dict:
        p = self.probe
        last = 200_000
        try:
            p.fpga_obs_signals_set(signals=signals)
            p.events_start()
            p.fpga_obs_open(**OBS_OPEN)
            p.fpga_obs_start()
            time.sleep(seconds)
            p.fpga_obs_stop()
            p.events_stop()
            items = p.fpga_obs_read(last=last)["items"]
            counted = p.fpga_obs_count(conditions, last=last)["conditions"]
        finally:
            for stop in (p.fpga_obs_close, p.events_stop):
                with contextlib.suppress(Exception):
                    stop()
        return {"items": items, "api": {asked: c.get("count") for asked, c in zip(conditions, counted)}}      # API は条件の文字を整形して返すので、頼んだ順で対応づける

    def obs_groups(self, sequence, seconds_each: float) -> dict:
        from fprobe.events import EventLog
        from fprobe.fpga_obs import FpgaObs
        d = self.device
        obs, log = FpgaObs(d), EventLog(d)
        try:
            obs.open(**OBS_OPEN)
            log.start()
            obs.start()
            for group in sequence:
                obs.select(group)
                time.sleep(seconds_each)
            obs.stop()
            log.stop()
            written, lost = log.counts()
            samples = obs.samples(log.read(written, end=written))
        finally:
            with contextlib.suppress(Exception):
                log.stop()
            with contextlib.suppress(Exception):
                obs.close()
        return {"values": [(s.group, s.value) for s in samples if s.kind == "value"],
                "markers": sum(1 for s in samples if s.kind == "group"), "gaps": sum(1 for s in samples if s.kind == "gap"),
                "event_lost": lost}

    def halt_on_condition(self, signals: dict, condition: str, timeout_s: float) -> dict:
        """FPGA の条件 → MCU の停止（手本 test_fpga_obs_scenarios_hw.py）。遅延 = trigger_set が返ってから、停止を
        1 ms ごとの問い合わせで見つけるまで（問い合わせの周期と往復を含む上限）。終わりに resume して接続を解く。"""
        p = self.probe
        halted, delay, count, resumed = False, 0.0, 0, False
        try:
            p.fpga_obs_signals_set(signals=signals)
            p.target_connect(target=self.mcu_target, frequency_hz=self.swd_hz, connect_mode="attach")
            if p.target_status()["state"] != "RUNNING":
                p.target_resume()
                time.sleep(0.2)
            p.events_start()
            p.fpga_obs_open(**OBS_OPEN)
            p.fpga_obs_start()
            p.fpga_obs_trigger_set(condition=condition, halt=True, la=False)
            t0 = time.perf_counter()
            while time.perf_counter() - t0 < timeout_s:
                if p.target_status()["state"] == "HALTED":
                    halted, delay = True, time.perf_counter() - t0
                    break
                time.sleep(0.001)
            count = p.fpga_obs_trigger_status()["count"]
        finally:
            with contextlib.suppress(Exception):
                p.fpga_obs_trigger_clear()
            with contextlib.suppress(Exception):
                p.target_resume()
                time.sleep(0.05)
                resumed = p.target_status()["state"] == "RUNNING"
            for stop in (p.fpga_obs_close, p.events_stop, p.target_disconnect):
                with contextlib.suppress(Exception):
                    stop()
        return {"halted": halted, "delay_s": delay, "count": count, "resumed": resumed}

    def chaos_session(self):
        return RealObsSession(self)

    def ping(self):
        from fprobe import generated as g
        self.device.read32(g.MEM_STATUS)


class RealObsSession:
    """chaos_obs の口の実機版。観測は FpgaObs（1 ラウンド中は同じ物を使い回し、開き直しの持ち越しも見る）、記録は
    ラウンドの間走らせる EventLog を open のたびに作る EventCursor で読む（前のセッションの記録を読まない）、
    条件のトリガは Probe（halt なし・la のみ。MCU に触れない）。"""

    def __init__(self, rig: "RealRig"):
        from fprobe.events import EventLog
        from fprobe.fpga_obs import FpgaObs
        self.rig, self.obs, self.log = rig, FpgaObs(rig.device), EventLog(rig.device)
        self.cursor = None
        self.events: list = []
        self._lost_before = 0
        self.log.start()
        rig.probe.fpga_obs_signals_set(signals=SIGNALS)

    @property
    def lost(self) -> int:
        return self._lost_before + (self.cursor.lost if self.cursor else 0)

    def open(self, **override) -> dict:
        info = self.obs.open(**{**OBS_OPEN, **override})
        self._lost_before = self.lost
        self.cursor, self.events = self.log.cursor(), []          # 開いた後に書かれる記録だけを読む
        return info

    def close(self):
        self.obs.close()

    def start(self):
        self.obs.start()

    def stop(self):
        self.obs.stop()

    def select(self, group: int):
        self.obs.select(group)

    def register_read(self, addr: int) -> int:
        return self.obs.register_read(addr)

    def register_write(self, addr: int, value: int):
        self.obs.register_write(addr, value)

    def status(self) -> dict:
        return self.obs.status()

    def trigger_set(self):
        self.rig.probe.fpga_obs_trigger_set(condition=CHAOS_CONDITION, halt=False, la=True)

    def trigger_clear(self):
        self.rig.probe.fpga_obs_trigger_clear()

    def trigger_status(self) -> dict:
        return self.rig.probe.fpga_obs_trigger_status()

    def drain(self) -> int:
        """リングを読み進め、**値の記録**（イベント種類 8 の記録型 0）の数を返す。エンジンの時刻合わせの印（種類 9）や、
        ID・レジスタの応答の記録（記録型 3）は、観測を開いている間は出続けるので数えない（不変条件が見たいのは値の記録）。"""
        if self.cursor is None:
            return 0
        total = 0
        while True:
            got = self.cursor.take()
            self.events += got
            total += sum(1 for e in got if e.kind == 8 and e.data >> 30 == 0)
            if len(got) < 1 << 16:
                return total

    def take_samples(self) -> list:
        events, self.events = self.events, []
        return [(x.kind, x.group, x.value) for x in self.obs.samples(events)] if events else []

    def wait(self, seconds: float):
        end = time.monotonic() + seconds
        while True:
            remain = end - time.monotonic()
            if remain <= 0:
                return
            time.sleep(min(0.5, remain))
            self.drain()                                          # 待つ間もリングを読む（リングは 13 万件）

    def finish(self):
        for stop in (self.rig.probe.fpga_obs_trigger_clear, self.obs.close, self.log.stop):
            with contextlib.suppress(Exception):
                stop()
