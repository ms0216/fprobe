"""soak.py の自己試験（実機なし）。模擬の機器 FakeRig で、正常なら全シナリオが通り、わざと壊した機器では
その壊れ方に対応するシナリオが失敗として検出され、実行器が例外・連続失敗・止め方を正しく扱うことを確かめる。"""
import dataclasses
import json
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import soak  # noqa: E402
from soak import ObsCapture, ObsOpenFailure, Params, Runner  # noqa: E402

FAST = Params(obs_seconds=1.0, reopen_count=5, idcode_reads=20, sampler_seconds=0.5, ping_count=20,
              eventlog_reads=10, quiet_seconds=30.0, groups_seconds=0.1, count_seconds=1.0, chaos_ops=300, seed=1234)
ID = 0x10006433


class FakeMcu:
    def __init__(self, rig):
        self.rig, self.mem, self.n = rig, {0x20004000: 0x11111111}, 0

    def read32(self, address):
        self.n += 1
        if address == 0xE0042000:
            return ID ^ (1 if self.rig.fault == "idcode" and self.n == 7 else 0)
        return self.mem.get(address, 0)

    def write32(self, address, value):
        self.mem[address] = 0 if self.rig.fault == "ram" else value

    def flush(self):
        pass


class FakeObsSession:
    """chaos_obs の口（soak.RealObsSession と同じ操作）の模擬。正しい観測の回路のふるまいを写し、fault で壊す:
    carry = open が記録中・組を持ち越す / leak = stop の後も記録が増える / keyerror = レジスタ 5 を読むと素の KeyError /
    stale_id = 開き直しの open が古い ID を返す / stale_records = 開き直しの直後に前のセッションの記録が見える /
    status_open = close の後も status が running / skip = 記録の値が飛ぶ / wrong_exc = 範囲外の番地で IndexError /
    swallow = 範囲外の open の引数が通る / select_dead = stop の後の select で記録が増える。"""
    ID = 0x464F4253

    def __init__(self, fault=None):
        self.fault = fault
        self.opened = self.recording = self.leaking = False
        self.sel, self.counter, self.pending, self.buffer = 0, {0: 0, 1: 0}, [], []
        self.n_open, self.trigger, self.lost = 0, None, 0
        self.regs = {0: 0x4253, 1: 0x464F, 2: 2, 3: 2, 4: 64, 5: 27_000_000 & 0xFFFF, 6: 27_000_000 >> 16, 7: 4, 8: 3, 9: 8}

    def open(self, **override):
        if override.get("half_period", 10) < 2:
            if self.fault == "swallow":
                return {"id": self.ID}
            raise ValueError("half_period は 2〜65535")
        reopen = self.opened
        self.opened = True
        self.n_open += 1
        if self.fault != "carry":
            self.recording, self.sel = False, 0
        self.leaking = False
        self.counter = {0: 0, 1: 0}
        self.pending, self.buffer = [], []
        if self.fault == "stale_records" and reopen:
            self.pending = [("value", 0, 7)] * 3
        regs_id = 0x12345678 if self.fault == "stale_id" and reopen else self.ID
        return {"id": regs_id, "mode": "stream", "version": 2, "width": 16, "groups": 2, "fifo_depth": 64, "clock_hz": 27_000_000,
                "sync_delay": 2.0, "sample_delay": 3.0, "lost_bits": 8}

    def close(self):
        self.opened = self.recording = False

    def start(self):
        self.recording = True
        self.pending, self.buffer = [], []

    def stop(self):
        self.recording = False
        self.leaking = self.fault == "leak"

    def select(self, group):
        if self.fault == "select_dead" and self.opened and not self.recording:
            self.pending.append(("value", self.sel, 1))
        if group < 2:
            if self.recording and group != self.sel:
                self.pending.append(("group", group, None))
            self.sel = group

    def register_read(self, addr):
        if not 0 <= addr <= 15:
            raise IndexError(addr) if self.fault == "wrong_exc" else ValueError("番地は 0〜15")
        if addr == 14:
            return int(self.recording)
        if addr == 15:
            return self.sel
        if self.fault == "keyerror" and addr == 5:
            return {}[addr]
        return self.regs[addr]

    def register_write(self, addr, value):
        if addr == 14:
            self.start() if value & 1 else self.stop()
        elif addr == 15:
            self.select(value)

    def status(self):
        live = self.opened or self.fault == "status_open" and self.n_open > 0
        return {"running": live, "id_ok": self.opened, "stuck": False, "id_timeout": False, "driving": live,
                "target_running": self.recording, "id": self.ID}

    def trigger_set(self):
        self.trigger = "mark rose"

    def trigger_clear(self):
        self.trigger = None

    def trigger_status(self):
        return {"condition": self.trigger}

    def wait(self, seconds):
        if self.opened and (self.recording or self.leaking):
            for _ in range(max(1, int(seconds * 200))):
                step = 2 if self.fault == "skip" and self.counter[self.sel] == 50 else 1
                self.counter[self.sel] = (self.counter[self.sel] + step) & 0xFFFF
                self.pending.append(("value", self.sel, self.counter[self.sel]))

    def drain(self):
        n, self.buffer, self.pending = len(self.pending), self.buffer + self.pending, []
        return n

    def take_samples(self):
        out, self.buffer = self.buffer, []
        return out

    def finish(self):
        self.opened = self.recording = False


class FakeRig:
    def __init__(self, fault=None):
        self.fault, self.reconnects, self.closed = fault, 0, False

    def chaos_session(self):
        return FakeObsSession(self.fault[6:] if self.fault and self.fault.startswith("chaos_") else None)

    def obs_capture(self, seconds, group=0):
        if group:
            return self.quiet_capture(seconds)
        n = round(seconds * 27_000_000 / 4096)
        if self.fault == "count":
            n //= 2
        values = [((i & 0xFFFF) if not (self.fault == "skip" and i == 100) else (i + 5) & 0xFFFF, i * 4096,
                   i * 4096 * (1 + (50e-6 if self.fault == "drift" else 0))) for i in range(n)]
        return ObsCapture(values, 1 if self.fault == "gap" else 0, 0, {"overflow_seen": False, "stuck": False},
                          27_000_000.0, 27_000_000.0)

    def quiet_capture(self, seconds):
        n = round(seconds * 27_000_000 / 2 ** 28) + 1
        step = 2 ** 28
        values = [(i, i * step, (i * step + (3e6 if self.fault == "quiet_jump" and i == 3 else 0))
                   * (1 + (2e-3 if self.fault == "quiet_slow" else 0))) for i in range(n)]
        return ObsCapture(values, 0, 0, {"overflow_seen": False, "stuck": False}, 27_000_000.0, 27_000_000.0)

    def obs_conditions(self, seconds, signals, conditions):
        items = [{"kind": "value", "group": 0, "time_us": float(i), "value": i} for i in range(1000)]
        n_rose = 500
        api = {"mark rose": n_rose + (1 if self.fault == "count_api" else 0), "mark fell": 499, "counter changed": 499}
        return {"items": items, "api": api}

    def obs_groups(self, sequence, seconds_each):
        values = []
        for g in sequence:
            values += [(g, 100 + k) for k in range(5)]
        if self.fault == "groups_skip":
            values[2] = (values[2][0], values[2][1] + 3)
        return {"values": values, "markers": len(sequence) - 1, "gaps": 0, "event_lost": 0}

    def halt_on_condition(self, signals, condition, timeout_s):
        ok = self.fault != "no_halt"
        return {"halted": ok, "delay_s": 0.0004, "count": 1 if ok else 0, "resumed": self.fault != "no_resume"}

    def obs_open_close_once(self):
        if self.fault == "open_fails":
            raise ObsOpenFailure("id_timeout", "観測の回路が答えない（stuck）")
        return 0x464F4253

    @contextmanager
    def mcu(self):
        yield FakeMcu(self)

    def sampler_run(self, rate_hz, seconds, address):
        return {"errors": 3 if self.fault == "sampler" else 0, "overruns": 0, "written": round(rate_hz * seconds),
                "distinct_values": 1}

    def la_capture(self, channel):
        period = 2 ** 13 / 27e6 * (1.1 if self.fault == "la" else 1.0)
        return {"rising_s": [i * period for i in range(20)], "sample_period_s": 1e-8}

    def la_deep_capture(self, rate_hz, rle, pretrigger, post_beats, channel):
        period = 2 ** 13 / 27e6 * (1.1 if self.fault == "la_deep_period" else 1.0)
        return {"rising_s": [i * period for i in range(20)], "sample_period_s": 1 / rate_hz,
                "trigger": pretrigger + (1 if self.fault == "la_trigger" and pretrigger == 1 else 0)}

    def la_obs_align(self, channel, seconds):
        hz = 100e6
        step = 2 ** 13 / 27e6 * hz
        shift = 100 if self.fault == "align" else 0                 # ロジアナの辺を 1 us ずらす（刻み 10 ns）
        la = [i * step + shift for i in range(20)]
        obs = [i * step + 2 for i in range(20)]                      # 観測は 20 ns 遅れ
        return {"la_edges": la, "obs_edges": obs[:3] if self.fault == "align_few" else obs, "timebase_hz": hz}

    def eventlog_run(self, reads, address):
        return {"written": reads, "lost": 5 if self.fault == "eventlog" else 0, "read": reads, "capacity": 1000,
                "written_later": reads}

    def ping(self):
        if self.fault == "ping":
            raise TimeoutError("応答なし")

    def reconnect(self):
        self.reconnects += 1

    def close(self):
        self.closed = True


def run_once(rig, tmp_path, names=None, **kw):
    names = names or list(soak.SCENARIOS)
    runner = Runner(rig, dataclasses.replace(FAST), names, log_path=tmp_path / "s.jsonl", out=lambda *_: None, **kw)
    results = {n: runner.run_one(n) for n in names}
    return results, runner


def test_healthy_rig_passes_every_scenario(tmp_path):
    results, _ = run_once(FakeRig(), tmp_path)
    assert {n: r["ok"] for n, r in results.items()} == {n: True for n in soak.SCENARIOS}, \
        {n: r["failures"] for n, r in results.items() if not r["ok"]}
    assert abs(results["obs_long"]["metrics"]["slope_ppm_vs_declared"]) < 1e-6
    assert all("threads" in r["resources"] for r in results.values())
    lines = (tmp_path / "s.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(soak.SCENARIOS) and all(json.loads(x)["scenario"] for x in lines)


@pytest.mark.parametrize("fault, scenario", [
    ("count", "obs_long"), ("skip", "obs_long"), ("gap", "obs_long"), ("open_fails", "obs_reopen"),
    ("idcode", "swd_mem"), ("ram", "swd_mem"), ("sampler", "sampler"), ("la", "la"), ("eventlog", "eventlog"),
    ("ping", "ping"), ("quiet_jump", "obs_quiet"), ("quiet_slow", "obs_quiet"), ("count_api", "obs_count"),
    ("groups_skip", "obs_groups"), ("no_halt", "obs_halt"), ("no_resume", "obs_halt"),
    ("la_deep_period", "la_deep"), ("la_trigger", "la_deep"), ("align", "la_obs_align"),
    ("align_few", "la_obs_align"), ("chaos_carry", "chaos_obs"), ("chaos_leak", "chaos_obs"), ("chaos_keyerror", "chaos_obs"),
    ("chaos_stale_id", "chaos_obs"), ("chaos_stale_records", "chaos_obs"), ("chaos_status_open", "chaos_obs"),
    ("chaos_skip", "chaos_obs"), ("chaos_wrong_exc", "chaos_obs"), ("chaos_swallow", "chaos_obs"),
    ("chaos_select_dead", "chaos_obs")])
def test_a_broken_rig_is_detected(tmp_path, fault, scenario):
    results, _ = run_once(FakeRig(fault), tmp_path)
    assert not results[scenario]["ok"] and results[scenario]["failures"]
    assert all(r["ok"] for n, r in results.items() if n != scenario)


def chaos(fault=None, **params):
    return soak.scenario_chaos_obs(FakeRig(fault), dataclasses.replace(FAST, **params))


def test_chaos_healthy_passes_for_many_seeds_and_uses_every_operation():
    seen = set()
    for seed in range(20):
        r = chaos(seed=seed)
        assert r.ok, (seed, r.failures)
        assert r.details["seed"] == seed and len(r.details["ops"]) == 300
        seen |= {op[0] for op in r.details["ops"]}
    assert seen >= {"open", "reopen", "close", "start", "stop", "select", "reg_read", "reg_write", "status", "wait",
                    "trigger_set", "trigger_clear", "bad_open", "bad_register"}


def test_chaos_same_seed_gives_same_ops_and_seed_is_logged_when_random():
    assert chaos(seed=5).details["ops"] == chaos(seed=5).details["ops"] != chaos(seed=6).details["ops"]
    assert chaos(seed=-1).details["seed"] != chaos(seed=-1).details["seed"]


def test_chaos_failing_ops_are_replayable(tmp_path):
    first = chaos("chaos_leak")
    assert not first.ok and first.details["ops"]
    log = tmp_path / "s.jsonl"
    log.write_text(json.dumps({"ok": True, "details": {"ops": [["open"]]}}) + "\n"
                   + json.dumps({"ok": False, "details": first.details}) + "\n", encoding="utf-8")
    again = chaos("chaos_leak", chaos_replay=str(log), chaos_ops=1)
    assert again.details["ops"] == first.details["ops"] and again.failures == first.failures
    plain = tmp_path / "ops.json"
    plain.write_text(json.dumps(first.details["ops"]), encoding="utf-8")
    assert chaos("chaos_leak", chaos_replay=str(plain)).failures == first.failures
    assert chaos(chaos_replay=str(plain)).ok                                    # 正常な機器では同じ列が通る


def test_chaos_ops_option_and_log_record(tmp_path):
    results, _ = run_once(FakeRig(), tmp_path, ["chaos_obs"])
    rec = json.loads((tmp_path / "s.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert rec["details"]["seed"] == 1234 and len(rec["details"]["ops"]) == 300 and rec["metrics"]["ops"] == 300
    assert len(chaos(chaos_ops=17).details["ops"]) == 17


def test_drift_is_measured_not_judged(tmp_path):
    results, _ = run_once(FakeRig("drift"), tmp_path, ["obs_long"])
    assert results["obs_long"]["ok"]                                         # ドリフトは数字に出すだけ
    assert results["obs_long"]["metrics"]["slope_ppm_vs_declared"] == pytest.approx(50, rel=1e-3)


def test_an_exception_is_logged_and_the_next_scenario_runs(tmp_path):
    def boom(rig, p):
        raise ValueError("壊れた")
    scen = {**soak.SCENARIOS, "obs_long": boom}
    weights = {n: 0 for n in soak.SCENARIOS} | {"obs_long": 1, "ping": 1}
    runner = Runner(FakeRig(), FAST, ["obs_long", "ping"], weights, log_path=tmp_path / "s.jsonl", out=lambda *_: None,
                    scenarios=scen, until=time.time() + 0.3)
    text = runner.run()
    recs = [json.loads(x) for x in (tmp_path / "s.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(r.get("exception", "").startswith("ValueError") for r in recs)
    assert any(r.get("scenario") == "ping" and r["ok"] for r in recs)
    assert "obs_long: " in text and "例外" in text


def test_consecutive_failures_back_off_and_reconnect(tmp_path):
    rig = FakeRig("ping")
    runner = Runner(rig, FAST, ["ping"], log_path=tmp_path / "s.jsonl", out=lambda *_: None, until=time.time() + 0.6,
                    max_consecutive_failures=3, backoff_s=0.05)
    runner.run()
    events = [json.loads(x).get("event") for x in (tmp_path / "s.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rig.reconnects >= 1 and "reconnect" in events and "reconnected" in events and rig.closed


def test_time_share_follows_the_weights(tmp_path):
    clock = [0.0]

    def make(seconds):
        def scenario(rig, p):
            clock[0] += seconds
            return soak.Result()
        return scenario
    scen = {"a": make(60), "b": make(1), "c": make(5)}
    runner = Runner(None, FAST, ["a", "b", "c"], {"a": 50, "b": 25, "c": 25}, log_path=tmp_path / "s.jsonl",
                    out=lambda *_: None, scenarios=scen, clock=lambda: clock[0], sleep=lambda s: None,
                    until=3_000.0)
    runner.rig = FakeRig()
    runner.run()
    sec = {n: runner.stats[n].seconds for n in "abc"}
    total = sum(sec.values())
    assert sec["a"] / total == pytest.approx(0.5, abs=0.03) and sec["c"] / total == pytest.approx(0.25, abs=0.03)
    assert runner.stats["b"].runs > runner.stats["a"].runs                  # 短いものは回数が多い


def test_default_weights_put_half_the_time_on_obs_long_and_reopen():
    w = soak.DEFAULT_WEIGHTS
    assert sum(w.values()) == 100 and w["obs_long"] + w["obs_reopen"] == 50 and set(w) == set(soak.SCENARIOS)
    assert w["chaos_obs"] == 10


def test_summary_always_has_the_required_totals(tmp_path):
    stats = {n: soak.Stats() for n in ["obs_long", "resources"]}
    stats["obs_long"].add(True, False, {"gaps": 2, "event_lost": 3, "stuck": 1, "id_timeouts": 4, soak.SLOPE: 1.0}, 1.0)
    stats["obs_long"].add(True, False, {"gaps": 1, "event_lost": 0, "stuck": 0, "id_timeouts": 0, soak.SLOPE: 3.0}, 1.0)
    text = soak.format_summary(stats, 3600, 2)
    assert "gap の累計: 3" in text and "lost の累計: 3" in text and "stuck の回数: 1" in text and "id_timeout の回数: 4" in text
    assert "最小 1 最大 3 標準偏差 1" in text
    assert "時刻の傾き: 測れた回が無い" in soak.format_summary({"obs_long": soak.Stats()}, 1, 0)


def test_stop_file_stops_safely_and_prints_final_summary(tmp_path):
    stop = tmp_path / "stop"
    out = []
    runner = Runner(FakeRig(), FAST, ["ping", "resources"], log_path=tmp_path / "s.jsonl", stop_file=stop, out=out.append)
    threading.Timer(0.3, stop.touch).start()
    t0 = time.time()
    text = runner.run()
    assert time.time() - t0 < 5 and "最終" in text and (tmp_path / "s.jsonl.summary.txt").exists()


def test_deadline_stops_and_periodic_summary(tmp_path):
    out = []
    runner = Runner(FakeRig(), FAST, ["ping"], log_path=tmp_path / "s.jsonl", until=time.time() + 0.5,
                    summary_interval_s=0.1, out=out.append)
    runner.run()
    assert sum("要約" in x and "最終" not in x for x in out) >= 1 and "最終" in out[-1]


def test_parse_until():
    from datetime import datetime
    now = datetime(2026, 10, 25, 22, 0)
    assert datetime.fromtimestamp(soak.parse_until("06:30", now)) == datetime(2026, 10, 26, 6, 30)
    assert datetime.fromtimestamp(soak.parse_until("23:00", now)) == datetime(2026, 10, 25, 23, 0)


def test_resources_are_recorded():
    m = soak.process_resources()
    assert m["threads"] >= 1


def test_la_deep_cycles_through_rates_and_rle(tmp_path):
    rig = FakeRig()
    runner = Runner(rig, FAST, ["la_deep"], log_path=tmp_path / "s.jsonl", out=lambda *_: None)
    seen = {(runner.run_one("la_deep")["metrics"]["rate_mhz"], ) for _ in range(6)}
    assert seen == {(1.0,), (10.0,), (100.0,)}
    assert rig.la_deep_step == 6


def test_alignment_distribution_is_reported(tmp_path):
    results, _ = run_once(FakeRig(), tmp_path, ["la_obs_align"])
    m = results["la_obs_align"]["metrics"]
    assert m["align_mean_ns"] == pytest.approx(-0.0 + 20 - 0, abs=1e-6) or m["align_mean_ns"] == pytest.approx(20, abs=1e-6)
    assert m["align_absmax_ns"] == pytest.approx(20, abs=1e-6) and m["align_std_ns"] == pytest.approx(0, abs=1e-6)
