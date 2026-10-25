"""fprobe の連続試験（ソーク試験）の実行器とシナリオ。使い方は tools/soak/README.md。

構成（責務を分ける）:
  - シナリオ（scenario_*）: 機器の抽象（Rig）を呼び、「1 回分の動作 + 合否 + 測った数字」を Result にして返す。
    機器の実装を知らない。合否の基準は Params（理論値との差の許容など。既定値は明示し、引数で変えられる）。
  - Rig: 機器の抽象。RealRig が実機（PYNQ-Z2 の fprobe）、試験では模擬（test_soak.py の FakeRig）。
  - Runner: シナリオを重みどおりに順番に繰り返し、JSON Lines に追記、一定間隔で要約を出し、安全に止まる。
道具は判断しない。要約は事実（回数・失敗数・各数字の最初/最後/最小/最大）だけで、良し悪しは読む人が決める。
"""
from __future__ import annotations

import argparse
import bisect
import contextlib
import dataclasses
import json
import math
import os
import random
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# 設定（合否の基準 = 理論値との差の許容。既定値はここに明示する）

COUNTER_CLOCK_HZ = 27_000_000


@dataclass
class Params:
    # a. 観測の長時間
    obs_seconds: float = 60.0
    obs_count_tol: float = 0.02            # 件数の期待（秒 × 27 MHz / 4096）との相対差
    obs_expected_interval: int = 4096      # 変化の間隔（対象のサイクル）
    obs_counter_mask: int = 0xFFFF         # 組 0 は 16 ビットで 1 ずつ増える
    # b. 開き直し
    reopen_count: int = 20
    obs_id: int = 0x464F4253               # "FOBS"
    # c. SWD のメモリ読み
    idcode_address: int = 0xE0042000       # DBGMCU_IDCODE
    idcode_expect: int = -1                # 0 以上なら期待値との一致を求める。-1 なら「全部が最初の読みと同じ」だけ
    idcode_reads: int = 200
    ram_address: int = 0x20004000          # 書き込みアルゴリズムの領域（先頭 16 KB）を避けた SRAM（既存の試験と同じ）
    # d. サンプラ
    sampler_rate_hz: float = 10_000.0
    sampler_seconds: float = 5.0
    sampler_count_tol: float = 0.10
    sampler_address: int = 0xE0042000      # 値が変わらない番地（IDCODE）。全部が同じ値であることも確かめる
    # e. ロジアナ
    la_channel: int = 10
    la_tol: float = 0.02
    la_min_edges: int = 5                  # 周期を測るのに要る立ち上がりの数
    la_period_s: float = 2 ** 13 / COUNTER_CLOCK_HZ     # カウンタのビット 12 の周期
    # e2. ロジアナ（深いバッファ）の条件を変えた取り込み・観測との時刻の合い
    la_trigger_tol: int = 0                # トリガの位置と pretrigger の差の許容（サンプル）。0 = 一致
    la_deep_post_periods: int = 7          # RAW: トリガ後に取る周期の数（拍数 = 周期の数 × 周期 × rate / 8）
    la_deep_rle_post_beats: int = 200      # RLE: トリガ後の拍数（1 拍 = 4 レコード）
    align_seconds: float = 0.6             # 観測を走らせる長さ（途中でロジアナを取る。手本 test_o2 と同じ）
    align_max_ns: float = 200.0            # 観測の辺とロジアナの辺の時刻の差の最大絶対値の許容（ns）
    align_min_edges: int = 5
    # f. イベントログ
    eventlog_reads: int = 100
    # 観測の追加（条件の数え・組の切り替え・長い静止・条件からの停止）
    count_seconds: float = 3.0
    groups_seconds: float = 2.0            # 組を切り替える 1 区間の長さ
    quiet_seconds: float = 60.0
    quiet_group: int = 1                   # 変化が遅い組（43〜28 ビット。2^28 サイクルごとに 1 増える）
    quiet_interval: int = 1 << 28
    quiet_count_slack: int = 1             # 期待の件数との差の許容（個）
    quiet_time_tol: float = 0.001          # 時刻の進み（fprobe）と対象のサイクルの比の、申告との相対差
    halt_condition: str = "mark rose"
    halt_timeout_s: float = 2.0
    # g. UDP の往復
    ping_count: int = 200
    ping_pause_s: float = 0.0
    # h. 観測の状態遷移のランダムな操作列（chaos_obs）
    chaos_ops: int = 60                    # 1 ラウンドの操作数
    seed: int = -1                         # 操作列の乱数の種。-1 = ラウンドごとに新しく決めてログに残す。0 以上なら n 回目のラウンドは seed + n
    chaos_wait_max_s: float = 1.0          # 操作の間の待ち（wait）の最大
    chaos_settle_s: float = 0.2            # 止めた後、飛んでいる記録が尽きるのを待つ時間（obs_capture の stop の後と同じ）
    chaos_replay: str = ""                 # 失敗した操作列を再現する: 操作の JSON（配列）か、details.ops を持つログ（JSON Lines）のパス


@dataclass
class Result:
    ok: bool = True
    failures: list[str] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)
    details: dict = field(default_factory=dict)       # 数字でない事実（chaos_obs の種と操作列など）。ログの "details" に入る

    def check(self, condition: bool, message: str) -> bool:
        if not condition:
            self.ok = False
            self.failures.append(message)
        return condition


@dataclass
class ObsCapture:
    """観測の 1 回の取り込み。values = [(value, target_cycles, time_fprobe か None)]（kind が value のものだけ）。"""
    values: list[tuple[int, int, float | None]]
    gaps: int
    event_lost: int
    status: dict
    timebase_hz: float
    clock_hz: float


def percentile(sorted_values: list[float], q: float) -> float:
    """最近順位法（補間しない）。"""
    if not sorted_values:
        return math.nan
    return sorted_values[min(len(sorted_values) - 1, max(0, math.ceil(q * len(sorted_values)) - 1))]


# ---------------------------------------------------------------------------
# シナリオ。rig の操作は RealRig の各メソッドを参照

def scenario_obs_long(rig, p: Params) -> Result:
    r = Result()
    cap: ObsCapture = rig.obs_capture(p.obs_seconds)
    values = cap.values
    r.metrics["values"] = len(values)
    r.metrics["gaps"] = cap.gaps
    r.metrics["event_lost"] = cap.event_lost
    r.check(cap.gaps == 0, f"溢れの切れ目 gap が {cap.gaps} 件")
    r.check(cap.event_lost == 0, f"イベントの lost が {cap.event_lost}")
    r.check(not cap.status.get("overflow_seen"), "overflow_seen")
    r.check(not cap.status.get("stuck"), "stuck")
    r.metrics["stuck"] = int(bool(cap.status.get("stuck")))
    r.metrics["id_timeouts"] = int(bool(cap.status.get("id_timeout")))
    steps = [(b[0] - a[0]) & p.obs_counter_mask for a, b in zip(values, values[1:])]
    bad = sum(1 for s in steps if s != 1)
    r.metrics["bad_steps"] = bad
    r.check(bad == 0, f"値が 1 ずつ増えない箇所が {bad} 件")
    intervals = [b[1] - a[1] for a, b in zip(values[1:], values[2:])]
    off = sum(1 for d in intervals if d != p.obs_expected_interval)
    r.metrics["bad_intervals"] = off
    r.check(off == 0, f"変化の間隔が {p.obs_expected_interval} サイクルでない箇所が {off} 件")
    expected = p.obs_seconds * COUNTER_CLOCK_HZ / p.obs_expected_interval
    r.metrics["count_vs_expected"] = len(values) / expected
    r.check(abs(len(values) - expected) <= p.obs_count_tol * expected,
            f"件数 {len(values)} が期待 {expected:.0f} の ±{p.obs_count_tol:.0%} の外")
    r.check(len({v[0] for v in values}) > 1, "値が変わらない（stuck）")
    # 時刻の傾き（fprobe の刻み / 対象のサイクル）。鎖が変わって target_cycles が戻る所は飛ばす
    dt = dc = 0.0
    for a, b in zip(values, values[1:]):
        if a[2] is not None and b[2] is not None and b[1] > a[1]:
            dt += b[2] - a[2]
            dc += b[1] - a[1]
    if dc > 0:
        slope = dt / dc
        r.metrics["slope_ticks_per_cycle"] = slope
        if cap.clock_hz:
            r.metrics["slope_ppm_vs_declared"] = (slope / (cap.timebase_hz / cap.clock_hz) - 1) * 1e6
    else:
        r.check(False, "時刻の傾きを測れない（時刻つきの値が無い）")
    return r


def scenario_obs_reopen(rig, p: Params) -> Result:
    r = Result()
    failed = timeouts = 0
    for i in range(p.reopen_count):
        try:
            got = rig.obs_open_close_once()
        except Exception as e:               # noqa: BLE001  失敗の中身（stuck / id_timeout など）をそのまま残す
            failed += 1
            timeouts += getattr(e, "kind", "") == "id_timeout"
            r.check(False, f"{i + 1} 回目の open が失敗: {type(e).__name__}: {e}")
            continue
        r.check(got == p.obs_id, f"{i + 1} 回目の ID が {got:#010x}（期待 {p.obs_id:#010x}）")
    r.metrics["open_failures"] = failed
    r.metrics["id_timeouts"] = timeouts
    return r


def scenario_swd_mem(rig, p: Params) -> Result:
    r = Result()
    lat: list[float] = []
    first = None
    with rig.mcu() as m:
        for i in range(p.idcode_reads):
            t0 = time.perf_counter()
            v = m.read32(p.idcode_address)
            lat.append(time.perf_counter() - t0)
            if first is None:
                first = v
                if p.idcode_expect >= 0:
                    r.check(v == p.idcode_expect, f"ID {v:#010x}（期待 {p.idcode_expect:#010x}）")
            elif v != first:
                r.check(False, f"{i + 1} 回目の ID {v:#010x} が最初の {first:#010x} と違う")
        lat.sort()
        r.metrics["id_latency_max_ms"] = lat[-1] * 1e3
        r.metrics["id_latency_p99_ms"] = percentile(lat, 0.99) * 1e3
        r.metrics["id_latency_p50_ms"] = percentile(lat, 0.50) * 1e3
        original = m.read32(p.ram_address)
        try:
            for pattern in (0x5A5AC3C3, 0xA5A53C3C):
                m.write32(p.ram_address, pattern)
                m.flush()
                back = m.read32(p.ram_address)
                r.check(back == pattern, f"SRAM {p.ram_address:#x} に {pattern:#010x} を書いて {back:#010x} を読んだ")
        finally:
            m.write32(p.ram_address, original)
            m.flush()
        r.check(m.read32(p.ram_address) == original, "SRAM を元の値に戻せない")
    return r


def scenario_sampler(rig, p: Params) -> Result:
    r = Result()
    s = rig.sampler_run(p.sampler_rate_hz, p.sampler_seconds, p.sampler_address)
    expected = p.sampler_seconds * p.sampler_rate_hz
    r.metrics.update(written=s["written"], errors=s["errors"], overruns=s["overruns"],
                     written_vs_expected=s["written"] / expected)
    r.check(s["errors"] == 0, f"サンプラの errors が {s['errors']}")
    r.check(abs(s["written"] - expected) <= p.sampler_count_tol * expected,
            f"書き込まれた数 {s['written']} が期待 {expected:.0f} の ±{p.sampler_count_tol:.0%} の外")
    r.check(s["distinct_values"] == 1, f"変わらないはずの番地の値が {s['distinct_values']} 通り")
    return r


def scenario_logic_analyzer(rig, p: Params) -> Result:
    r = Result()
    cap = rig.la_capture(p.la_channel)
    edges = cap["rising_s"]
    r.metrics["rising_edges"] = len(edges)
    if not r.check(len(edges) >= p.la_min_edges, f"立ち上がりが {len(edges)} 個（{p.la_min_edges} 個要る）"):
        return r
    periods = [b - a for a, b in zip(edges, edges[1:])]
    mean = sum(periods) / len(periods)
    r.metrics["period_mean_us"] = mean * 1e6
    r.metrics["period_vs_theory"] = mean / p.la_period_s
    r.metrics["period_worst_vs_theory"] = max(periods, key=lambda x: abs(x - p.la_period_s)) / p.la_period_s
    r.check(abs(mean - p.la_period_s) <= p.la_tol * p.la_period_s,
            f"周期の平均 {mean * 1e6:.2f} us が理論 {p.la_period_s * 1e6:.2f} us の ±{p.la_tol:.0%} の外")
    return r


LA_DEEP_RATES_HZ = (1e6, 10e6, 100e6)          # 巡回する取り込みの速さ
LA_DEEP_PRETRIGGERS = (0, 1, 16, 4096)         # 0・1 は境界（R4: 0 番・1 番サンプルの扱い）、小、大


def scenario_la_deep(rig, p: Params) -> Result:
    """深いバッファを (rate, RLE の有無) を 1 通り（実行のたびに 3 rate × RAW/RLE の 6 通りを巡回）、pretrigger 4 通りで
    取り込む。辺の周期が理論値と ±la_tol で合い、トリガの位置が pretrigger と一致する。"""
    r = Result()
    step = getattr(rig, "la_deep_step", 0)
    rig.la_deep_step = step + 1
    rate = LA_DEEP_RATES_HZ[step % len(LA_DEEP_RATES_HZ)]
    rle = bool(step // len(LA_DEEP_RATES_HZ) % 2)
    r.metrics.update(rate_mhz=rate / 1e6, rle=int(rle))
    if rle:
        post = p.la_deep_rle_post_beats
    else:
        post = -(-int(p.la_deep_post_periods * p.la_period_s * rate) // 8) + 16
    worst_period = worst_trigger = 0.0
    for pre in LA_DEEP_PRETRIGGERS:
        tag = f"{rate / 1e6:g} MS/s {'RLE' if rle else 'RAW'} pretrigger={pre}"
        cap = rig.la_deep_capture(rate, rle, pre, post, p.la_channel)
        edges = cap["rising_s"]
        offset = abs(cap["trigger"] - pre)
        worst_trigger = max(worst_trigger, offset)
        r.check(offset <= p.la_trigger_tol, f"[{tag}] トリガの位置 {cap['trigger']}（pretrigger {pre}）")
        if not r.check(len(edges) >= p.la_min_edges, f"[{tag}] 立ち上がりが {len(edges)} 個（{p.la_min_edges} 個要る）"):
            continue
        periods = [b - a for a, b in zip(edges, edges[1:])]
        mean = sum(periods) / len(periods)
        worst_period = max(worst_period, abs(mean / p.la_period_s - 1))
        r.check(abs(mean - p.la_period_s) <= p.la_tol * p.la_period_s,
                f"[{tag}] 周期の平均 {mean * 1e6:.2f} us が理論 {p.la_period_s * 1e6:.2f} us の ±{p.la_tol:.0%} の外")
    r.metrics["period_worst_rel_err"] = worst_period
    r.metrics["trigger_worst_offset"] = worst_trigger
    return r


def scenario_la_obs_align(rig, p: Params) -> Result:
    """同じ信号の辺（カウンタのビット 12 = ロジアナ ch10 = 観測の組 0 の bit0）を、深いバッファと観測で同時に取り、
    観測の各辺の fprobe 時刻とロジアナの辺の時刻の差（ns）の分布を測る。"""
    r = Result()
    got = rig.la_obs_align(p.la_channel, p.align_seconds)
    hz = got["timebase_hz"]
    la, obs = sorted(got["la_edges"]), sorted(got["obs_edges"])
    r.metrics.update(la_edges=len(la), obs_edges=len(obs))
    ok = r.check(len(la) >= p.align_min_edges and len(obs) >= p.align_min_edges,
                 f"辺の数 ロジアナ {len(la)}・観測 {len(obs)}（どちらも {p.align_min_edges} 個以上要る）")
    if not ok:
        return r
    diffs = []
    for t in la:                                            # ロジアナの辺ごとに、最も近い観測の辺との差（観測 - ロジアナ）
        k = bisect.bisect_left(obs, t)
        near = min((obs[j] for j in (k - 1, k) if 0 <= j < len(obs)), key=lambda o: abs(o - t))
        diffs.append((near - t) / hz * 1e9)
    mean = sum(diffs) / len(diffs)
    r.metrics.update(align_mean_ns=mean, align_max_ns=max(diffs), align_min_ns=min(diffs),
                     align_std_ns=math.sqrt(sum((d - mean) ** 2 for d in diffs) / len(diffs)),
                     align_absmax_ns=max(abs(d) for d in diffs))
    r.check(max(abs(d) for d in diffs) <= p.align_max_ns,
            f"辺の時刻の差の最大絶対値 {max(abs(d) for d in diffs):.1f} ns が許容 {p.align_max_ns:g} ns の外")
    return r


def scenario_eventlog(rig, p: Params) -> Result:
    r = Result()
    e = rig.eventlog_run(p.eventlog_reads, p.idcode_address)
    r.metrics.update(written=e["written"], lost=e["lost"], read=e["read"], capacity=e["capacity"])
    r.check(e["written"] > 0, "イベントが 1 件も書かれない")
    r.check(e["lost"] == 0, f"lost が {e['lost']}")
    r.check(e["read"] == min(e["written"], e["capacity"]),
            f"読めた {e['read']} 件が min(written {e['written']}, capacity {e['capacity']}) と合わない")
    r.check(e["written_later"] == e["written"], f"stop の後に written が {e['written']} から {e['written_later']} へ増えた")
    return r


def scenario_link_ping(rig, p: Params) -> Result:
    r = Result()
    lat: list[float] = []
    failures = 0
    for i in range(p.ping_count):
        t0 = time.perf_counter()
        try:
            rig.ping()
            lat.append(time.perf_counter() - t0)
        except Exception as e:               # noqa: BLE001
            failures += 1
            if failures <= 3:
                r.failures.append(f"{i + 1} 回目の往復が失敗: {type(e).__name__}: {e}")
        if p.ping_pause_s:
            time.sleep(p.ping_pause_s)
    r.metrics["ping_failures"] = failures
    r.check(failures == 0, f"往復の失敗 {failures} / {p.ping_count}")
    lat.sort()
    if lat:
        r.metrics.update(ping_p50_ms=percentile(lat, 0.5) * 1e3, ping_p99_ms=percentile(lat, 0.99) * 1e3,
                         ping_max_ms=lat[-1] * 1e3)
    return r


def process_resources() -> dict[str, float]:
    """この実行器自身の RSS（MB）・ハンドル / fd の数・スレッド数。取れないものは含めない。"""
    out: dict[str, float] = {"threads": threading.active_count()}
    try:
        import psutil
        proc = psutil.Process()
        out["rss_mb"] = proc.memory_info().rss / 2 ** 20
        out["handles"] = proc.num_handles() if hasattr(proc, "num_handles") else proc.num_fds()
        out["os_threads"] = proc.num_threads()
    except ImportError:
        if os.path.isdir("/proc/self/fd"):
            out["handles"] = len(os.listdir("/proc/self/fd"))
    return out


# 信号の定義（手本 test_fpga_obs_scenarios_hw.py と同じ組 0 + 組 1）と、条件ごとの「その場で数える」述語（値の並びから直接）
SIGNALS = {"groups": {"0": [{"name": "mark", "lsb": 0, "width": 1}, {"name": "counter", "lsb": 1, "width": 15, "format": "dec"}],
                      "1": [{"name": "hi", "lsb": 0, "width": 16, "format": "dec"}]}}
CONDITION_PREDICATES = {
    "mark rose": lambda cur, prev: not prev & 1 and cur & 1,
    "mark fell": lambda cur, prev: prev & 1 == 1 and cur & 1 == 0,
    "counter changed": lambda cur, prev: cur >> 1 != prev >> 1,
}


def scenario_obs_count(rig, p: Params) -> Result:
    """count_condition（API の fpga_obs_count）の回数が、同じ記録の値をその場で数えた回数と一致する。"""
    r = Result()
    got = rig.obs_conditions(p.count_seconds, SIGNALS, list(CONDITION_PREDICATES))
    values = [it for it in got["items"] if it["kind"] == "value" and it["group"] == 0 and it["time_us"] is not None]
    r.metrics["values"] = len(values)
    r.check(len(values) > 0, "数える値が 1 件も無い")
    for cond, pred in CONDITION_PREDICATES.items():
        expect = sum(1 for a, b in zip(values, values[1:]) if pred(b["value"], a["value"]))
        api = got["api"].get(cond)
        r.metrics[f"count[{cond}]"] = api if isinstance(api, (int, float)) else math.nan
        r.check(api == expect, f"条件 '{cond}' の回数 API {api} とその場の数え {expect} が違う")
    return r


def scenario_obs_groups(rig, p: Params) -> Result:
    """選択（select）で組 0 と組 1 を切り替えながら、各組の値が読める。組ごとの連続した区間で 1 ずつ増える。"""
    r = Result()
    sequence = (0, 1, 0, 1)
    got = rig.obs_groups(sequence, p.groups_seconds)
    values = got["values"]                                  # [(group, value)]
    r.metrics.update(gaps=got["gaps"], event_lost=got["event_lost"], group_markers=got["markers"], values=len(values))
    r.check(got["gaps"] == 0, f"gap が {got['gaps']} 件")
    r.check({g for g, _ in values} == set(sequence), f"読めた組 {sorted({g for g, _ in values})}（期待 {sorted(set(sequence))}）")
    r.check(got["markers"] >= len(sequence) - 1, f"組の切り替えの印が {got['markers']} 個（{len(sequence) - 1} 個以上）")
    bad = 0
    for (ga, a), (gb, b) in zip(values, values[1:]):
        if ga == gb and (b - a) & 0xFFFF != 1:
            bad += 1
    r.metrics["bad_steps"] = bad
    r.check(bad == 0, f"同じ組の続きで 1 ずつ増えない箇所が {bad} 件")
    return r


def scenario_obs_quiet(rig, p: Params) -> Result:
    """変化が遅い組を長く見る。値の変化が無い間も刻み（tick）で時刻が進み続け、fprobe の時刻の進みが対象のサイクルと合う。"""
    r = Result()
    cap: ObsCapture = rig.obs_capture(p.quiet_seconds, group=p.quiet_group)
    v = cap.values
    r.metrics.update(values=len(v), gaps=cap.gaps, event_lost=cap.event_lost,
                     stuck=int(bool(cap.status.get("stuck"))), id_timeouts=int(bool(cap.status.get("id_timeout"))))
    r.check(cap.gaps == 0, f"gap が {cap.gaps} 件")
    r.check(cap.event_lost == 0, f"イベントの lost が {cap.event_lost}")
    expected = p.quiet_seconds * COUNTER_CLOCK_HZ / p.quiet_interval
    r.check(abs(len(v) - expected) <= p.quiet_count_slack + 1,
            f"件数 {len(v)} が期待 {expected:.2f} から ±{p.quiet_count_slack + 1} 個の外（最初の値を含む）")
    times = [x[2] for x in v]
    r.check(all(t is not None for t in times), "時刻の決まらない値がある")
    known = [t for t in times if t is not None]
    r.check(all(b >= a for a, b in zip(known, known[1:])), "fprobe の時刻が単調に進まない")
    intervals = [b[1] - a[1] for a, b in zip(v[1:], v[2:])]          # 最初の値は始めた時点の現在値で、変化の瞬間ではない（最初の間隔は半端）
    bad_int = sum(1 for d in intervals if d != p.quiet_interval)
    r.metrics["bad_intervals"] = bad_int
    r.check(bad_int == 0, f"変化の間隔が {p.quiet_interval} サイクルでない箇所が {bad_int} 件")
    worst = 0.0
    if cap.clock_hz:
        for a, b in zip(v, v[1:]):
            if a[2] is not None and b[2] is not None and b[1] > a[1]:
                ratio = (b[2] - a[2]) / cap.timebase_hz / ((b[1] - a[1]) / cap.clock_hz)
                worst = max(worst, abs(ratio - 1))
        r.metrics["time_vs_cycles_worst"] = worst
        r.check(worst <= p.quiet_time_tol, f"時刻の進みとサイクルの比のずれ {worst:.2e} が許容 {p.quiet_time_tol:g} の外")
    dt = dc = 0.0
    for a, b in zip(v, v[1:]):
        if a[2] is not None and b[2] is not None and b[1] > a[1]:
            dt += b[2] - a[2]
            dc += b[1] - a[1]
    if dc > 0:
        r.metrics["slope_ticks_per_cycle"] = dt / dc
    return r


def scenario_obs_halt(rig, p: Params) -> Result:
    """FPGA の条件 → MCU の停止（fpga_obs.trigger_set）を 1 回行い、停止までの遅延を測る。終わりに resume して戻す。"""
    r = Result()
    h = rig.halt_on_condition(SIGNALS, p.halt_condition, p.halt_timeout_s)
    r.check(h["halted"], f"条件 '{p.halt_condition}' で {p.halt_timeout_s} 秒以内に停止しない")
    if h["halted"]:
        r.metrics["halt_delay_ms"] = h["delay_s"] * 1e3
    r.metrics["trigger_count"] = h["count"]
    r.check(h["count"] >= 1 or not h["halted"], "停止したのにトリガの count が 0")
    r.check(h["resumed"], "resume して RUNNING に戻せなかった")
    return r


# ---------------------------------------------------------------------------
# chaos_obs: 状態を持つ観測の API に、有効なランダムな操作列を流して不変条件を検査する。
# 状態は自分で追う（closed / opened / running / stopped）。合否は事実の不変条件だけ。操作は ["名前", 引数...] の並びで、
# 種と一緒にログへ残し、--chaos-replay で同じ列を再現する。Rig の chaos_session() が返す口（ObsSession）の操作:
#   open(**上書き) -> 申告 dict / close() / start() / stop() / select(g) / register_read(a) / register_write(a, v) /
#   status() -> dict / trigger_set() / trigger_clear() / trigger_status() -> dict / wait(秒) /
#   drain() -> 新しく読んだ記録の件数 / take_samples() -> [(kind, group, value)] / lost / finish()

DOCUMENTED_ERRORS = (ValueError, RuntimeError, TimeoutError)
CHAOS_CONDITION = "mark rose"
CHAOS_WAITS = (0.0, 0.01, 0.05, 0.2, 0.5, 1.0)
CHAOS_GROUPS = (0, 1)
CHAOS_READABLE = (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 14, 15)
NEGATIVE_OPS = {"bad_open", "bad_register"}              # 文書化された ValueError が出るのが正しい操作
REG_CONTROL, REG_SELECT = 14, 15

# 状態ごとの操作の重み。closed は線を駆動していない。opened = 開いて一度も start していない、stopped = 開いて stop した後
CHAOS_WEIGHTS = {
    "closed": {"open": 6, "close": 1, "status": 2, "wait": 2, "trigger_set": 1, "trigger_clear": 1, "bad_open": 1},
    "opened": {"start": 5, "stop": 1, "select": 3, "reg_read": 3, "reg_write": 2, "reopen": 2, "close": 2, "status": 1,
               "wait": 2, "trigger_set": 1, "trigger_clear": 1, "bad_register": 1, "bad_open": 1},
    "running": {"stop": 5, "start": 1, "select": 4, "reg_read": 3, "reg_write": 2, "reopen": 2, "close": 2, "status": 1,
                "wait": 4, "trigger_set": 1, "trigger_clear": 1, "bad_register": 1, "bad_open": 1},
}
CHAOS_WEIGHTS["stopped"] = CHAOS_WEIGHTS["opened"]


def chaos_make_op(rng: random.Random, state: str, p: Params) -> list:
    table = CHAOS_WEIGHTS[state]
    name = rng.choices(list(table), weights=list(table.values()))[0]
    if name == "select":
        return [name, rng.choice(CHAOS_GROUPS)]
    if name == "reg_read":
        return [name, rng.choice(CHAOS_READABLE)]
    if name == "reg_write":                      # 14 = CONTROL（記録の開始・停止）、15 = SELECT（組 2 以上は観測の回路が無視する）
        return rng.choice([[name, REG_CONTROL, rng.choice((0, 1))], [name, REG_SELECT, rng.choice((0, 1, 2, 7))]])
    if name == "wait":
        return [name, rng.choice([w for w in CHAOS_WAITS if w <= p.chaos_wait_max_s] or [0.0])]
    return [name]


def _is_record_control(op: list, value_bit: int | None = None) -> bool:
    return op[0] == "reg_write" and op[1] == REG_CONTROL and (value_bit is None or op[2] & 1 == value_bit)


def chaos_state_after(state: str, op: list) -> str:
    """操作が成功した後の状態。"""
    name = op[0]
    if name in ("open", "reopen"):
        return "opened"
    if name == "close":
        return "closed"
    if name == "start" or _is_record_control(op, 1):
        return "running"
    if name == "stop" or _is_record_control(op, 0):
        return "stopped"
    return state


class ChaosRound:
    """操作列 1 回分の実行と不変条件の検査。最初の違反で止める（その後は状態が当てにならず、違反が連鎖するだけ）。"""

    def __init__(self, session, p: Params, r: Result):
        self.s, self.p, self.r = session, p, r
        self.state = "closed"
        self.sel = 0
        self.consts: dict[int, int] = {}
        self.groups = 2
        self.prev: dict[int, int] = {}
        self.lost_seen = 0
        self.events = self.gaps = self.opens = 0

    def fail(self, index: int, op: list, message: str) -> None:
        self.r.check(False, f"{index + 1} 番目の操作 {op}（状態 {self.state}）: {message}")

    # --- 検査 ---
    def check_status(self, index, op) -> None:
        """(a) status() が追っている状態と矛盾しない。"""
        st = self.s.status()
        if self.state == "closed":
            self.r.check(not st["running"] and not st["driving"],
                         f"{index + 1} 番目の操作 {op}: 閉じているのに running={st['running']} driving={st['driving']}")
            return
        ok = st["running"] and st["id_ok"] and not st["stuck"] and not st["id_timeout"] and st["id"] == self.p.obs_id
        self.r.check(ok, f"{index + 1} 番目の操作 {op}: 開いているのに status が矛盾（{st}）")
        if ok and self.state in ("opened", "stopped"):
            self.r.check(not st["target_running"], f"{index + 1} 番目の操作 {op}: 記録していない（{self.state}）のに target_running")

    def check_records_quiet(self, index, op) -> None:
        """(c) 記録していない状態（opened / stopped）では、値の記録が増えない（組の印は許す）。"""
        n = self.s.drain()
        self.events += n
        if n:
            values = [x for x in self.s.take_samples() if x[0] == "value"]
            self.r.check(not values, f"{index + 1} 番目の操作 {op}: 記録していない（{self.state}）のに値の記録が {len(values)} 件増えた")

    def flush(self, index, op) -> None:
        """(f) 走っていた区間の記録を読み、欠けず順序どおりか確かめて、区間を閉じる。"""
        self.events += self.s.drain()
        samples = self.s.take_samples()
        lost = self.s.lost
        judge = lost == self.lost_seen                # 読む前にリングが一周した区間は連続を判定できない（lost に数える）
        self.lost_seen = lost
        for kind, group, value in samples:
            if kind == "value":
                self.r.check(0 <= group < self.groups and 0 <= value <= 0xFFFF,
                             f"{index + 1} 番目の操作 {op}: 範囲外の記録 組 {group} 値 {value}")
                before = self.prev.get(group)
                if judge and before is not None and (value - before) & 0xFFFF != 1:
                    self.r.check(False, f"{index + 1} 番目の操作 {op}: 組 {group} の値が {before} の次に {value}"
                                        "（1 ずつ増えない。切れ目・印の無い区間）")
                self.prev[group] = value
            else:
                self.gaps += kind == "gap"
                self.prev.clear()                     # 切れ目・組の切り替えの前後は続きを判定しない
        self.prev.clear()

    def expect_register(self, index, op, addr: int, want: int) -> None:
        got = self.s.register_read(addr)
        self.r.check(got == want, f"{index + 1} 番目の操作 {op}: レジスタ {addr} が {got:#x}（期待 {want:#x}）")

    # --- 実行 ---
    def do(self, op: list):
        s, name = self.s, op[0]
        if name in ("open", "reopen"):
            return s.open()
        if name == "bad_open":
            return s.open(half_period=1)              # 範囲外 → ValueError
        if name == "bad_register":
            return s.register_read(16)                # 番地は 0〜15 → ValueError
        if name == "reg_read":
            return s.register_read(op[1])
        if name == "reg_write":
            return s.register_write(op[1], op[2])
        return getattr(s, name)(*op[1:])

    def step(self, index: int, op: list) -> bool:
        """1 操作を実行して検査する。違反があれば False。"""
        name, before = op[0], self.state
        ends_run = name in ("open", "reopen", "close", "start") or _is_record_control(op)
        if before == "running" and ends_run:
            self.flush(index, op)                     # open / close / 始め直しは記録を捨てる。その前に読む
        try:
            ret = self.do(op)
        except DOCUMENTED_ERRORS as e:
            if name in NEGATIVE_OPS and isinstance(e, ValueError):
                self.check_status(index, op)          # 失敗した操作が状態を変えない
                return self.r.ok
            self.fail(index, op, f"有効な操作が失敗 {type(e).__name__}: {e}")
            return False
        except Exception as e:                        # noqa: BLE001  (e) 文書化されていない型は不具合
            self.fail(index, op, f"文書化されていない例外 {type(e).__name__}: {e}")
            return False
        if name in NEGATIVE_OPS:
            self.fail(index, op, "不正な引数が通った（ValueError が出ない）")
            return False
        self.state = chaos_state_after(before, op)
        if name in ("open", "reopen"):
            self.opens += 1
            self.after_open(index, op, ret)
        elif name == "close":
            self.sel = 0
            self.s.drain()
            self.s.take_samples()
            self.prev.clear()
        elif name in ("start", "stop") or _is_record_control(op):
            if self.state == "stopped":
                self.after_stop(index, op)
            else:
                self.expect_register(index, op, REG_CONTROL, 1)
        elif name == "select" or (name == "reg_write" and op[1] == REG_SELECT):
            group = op[1] if name == "select" else op[2]
            if group < self.groups:
                self.sel = group
            self.expect_register(index, op, REG_SELECT, self.sel)
        elif name == "reg_read":
            self.after_reg_read(index, op, ret)
        elif name == "trigger_set":
            cond = self.s.trigger_status().get("condition")
            self.r.check(cond == CHAOS_CONDITION, f"{index + 1} 番目の操作 {op}: trigger_set の後の条件が {cond!r}")
        elif name == "trigger_clear":
            cond = self.s.trigger_status().get("condition")
            self.r.check(cond is None, f"{index + 1} 番目の操作 {op}: trigger_clear の後も条件 {cond!r} が残る")
        elif name == "wait" and self.state == "running":
            self.events += self.s.drain()
        if self.r.ok and self.state in ("opened", "stopped"):
            self.check_records_quiet(index, op)
        if self.r.ok:
            self.check_status(index, op)
        return self.r.ok

    def after_open(self, index, op, info: dict) -> None:
        """(b) open の後の ID。(d) 開き直した後に前のセッションの ID・応答・記録・設定が見えない。"""
        self.r.check(info.get("id") == self.p.obs_id,
                     f"{index + 1} 番目の操作 {op}: open の ID が {info.get('id')!r}（期待 {self.p.obs_id:#010x}）")
        self.sel = 0
        self.prev.clear()
        self.groups = info["groups"]
        clock = info["clock_hz"]
        self.consts = {0: self.p.obs_id & 0xFFFF, 1: self.p.obs_id >> 16, 2: info["version"], 3: info["groups"],
                       4: info["fifo_depth"], 5: clock & 0xFFFF, 6: clock >> 16, 7: round(info["sync_delay"] * 2),
                       8: int(info["sample_delay"]), 9: info["lost_bits"]}
        if self.s.drain():
            self.r.check(False, f"{index + 1} 番目の操作 {op}: open の直後に、前のセッションの記録が見える")
            self.s.take_samples()
        for addr in (0, 1):
            self.expect_register(index, op, addr, self.consts[addr])
        self.expect_register(index, op, REG_CONTROL, 0)           # 前のセッションの「記録中」「選んだ組」が持ち越されない
        self.expect_register(index, op, REG_SELECT, 0)

    def after_stop(self, index, op) -> None:
        """(c) 止めた後、飛んでいる記録が尽きたら新しい記録は増えない。"""
        self.s.wait(self.p.chaos_settle_s)
        self.flush(index, op)
        self.s.wait(self.p.chaos_settle_s)
        n = self.s.drain()
        self.events += n
        self.r.check(n == 0, f"{index + 1} 番目の操作 {op}: stop の後も記録が {n} 件増えた")
        self.s.take_samples()
        self.expect_register(index, op, REG_CONTROL, 0)

    def after_reg_read(self, index, op, value: int) -> None:
        addr = op[1]
        want = {REG_CONTROL: int(self.state == "running"), REG_SELECT: self.sel}.get(addr, self.consts.get(addr))
        self.r.check(want is None or value == want, f"{index + 1} 番目の操作 {op}: レジスタ {addr} が {value:#x}（期待 {want}）")


def load_chaos_replay(path: str) -> list[list]:
    """操作の JSON（配列）か、details.ops を持つログ（JSON Lines。失敗した記録を優先）から操作列を読む。"""
    text = Path(path).read_text(encoding="utf-8")
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass
    found = [rec for rec in map(json.loads, filter(None, text.splitlines())) if rec.get("details", {}).get("ops")]
    found.sort(key=lambda rec: rec.get("ok", True))              # 失敗した記録が先
    if not found:
        raise ValueError(f"{path} に操作列（details.ops）が無い")
    return found[0]["details"]["ops"]


def scenario_chaos_obs(rig, p: Params) -> Result:
    r = Result()
    n = getattr(p, "_chaos_n", 0)
    p._chaos_n = n + 1                                           # 種を決めたとき、n 回目のラウンドは seed + n
    seed = random.SystemRandom().getrandbits(32) if p.seed < 0 else p.seed + n
    rng = random.Random(seed)
    r.details["seed"] = seed
    before = process_resources()
    session = rig.chaos_session()
    rnd = ChaosRound(session, p, r)
    ops: list[list] = []
    try:
        if p.chaos_replay:
            for index, op in enumerate(load_chaos_replay(p.chaos_replay)):
                ops.append(op)
                if not rnd.step(index, op):
                    break
        else:
            for index in range(p.chaos_ops):
                ops.append(chaos_make_op(rng, rnd.state, p))
                if not rnd.step(index, ops[-1]):
                    break
        if r.ok and rnd.state == "running":
            rnd.flush(len(ops), ["end"])                          # ラウンドの終わりの区間も判定する
    except Exception as e:                       # noqa: BLE001  検査の途中で装置が壊れた: 失敗として操作列を残す
        r.check(False, f"検査の途中の例外 {type(e).__name__}: {e}")
    finally:
        with contextlib.suppress(Exception):
            session.finish()
    r.details["ops"] = ops
    after = process_resources()
    r.metrics.update(ops=len(ops), opens=rnd.opens, events=rnd.events, chaos_gaps=rnd.gaps, chaos_event_lost=session.lost)
    for key in ("rss_mb", "handles"):
        if key in before and key in after:
            r.metrics[f"{key}_delta"] = after[key] - before[key]
    return r


SCENARIOS = {
    "obs_long": scenario_obs_long,
    "obs_reopen": scenario_obs_reopen,
    "obs_quiet": scenario_obs_quiet,
    "obs_count": scenario_obs_count,
    "obs_groups": scenario_obs_groups,
    "obs_halt": scenario_obs_halt,
    "chaos_obs": scenario_chaos_obs,
    "swd_mem": scenario_swd_mem,
    "sampler": scenario_sampler,
    "la": scenario_logic_analyzer,
    "la_deep": scenario_la_deep,
    "la_obs_align": scenario_la_obs_align,
    "eventlog": scenario_eventlog,
    "ping": scenario_link_ping,
}

# 時間配分の重み（実行時間の割合。合計 100 = %）。FPGA 観測に寄せる: obs_long + obs_reopen = 50、観測全体 78
DEFAULT_WEIGHTS = {"obs_long": 30, "obs_reopen": 20, "obs_quiet": 5, "obs_count": 2, "obs_groups": 2, "obs_halt": 2,
                   "chaos_obs": 10, "swd_mem": 3, "sampler": 2, "la": 4, "la_deep": 8, "la_obs_align": 8, "eventlog": 2,
                   "ping": 2}


# ---------------------------------------------------------------------------
# 実行器

class Stats:
    """シナリオ別の事実の集計: 回数・失敗数・例外数・使った時間・各数字の最初 / 最後 / 最小 / 最大 / 標準偏差 / 合計。"""

    def __init__(self):
        self.runs = self.fails = self.exceptions = 0
        self.seconds = 0.0
        self.metrics: dict[str, dict[str, float]] = {}

    def add(self, ok: bool, exception: bool, metrics: dict[str, float], seconds: float = 0.0):
        self.runs += 1
        self.fails += not ok
        self.exceptions += exception
        self.seconds += seconds
        for k, v in metrics.items():
            m = self.metrics.get(k)
            if m is None:
                self.metrics[k] = {"first": v, "last": v, "min": v, "max": v, "n": 1, "mean": v, "m2": 0.0, "sum": v}
            else:
                m["last"] = v
                m["min"] = min(m["min"], v)
                m["max"] = max(m["max"], v)
                m["sum"] += v
                m["n"] += 1
                delta = v - m["mean"]                       # Welford の逐次の分散
                m["mean"] += delta / m["n"]
                m["m2"] += delta * (v - m["mean"])

    def total(self, name: str) -> float:
        return self.metrics[name]["sum"] if name in self.metrics else 0

    def std(self, name: str) -> float:
        m = self.metrics.get(name)
        return math.sqrt(m["m2"] / m["n"]) if m else math.nan


# 要約に必ず出す累計（観測の欠けと止まりの事実）。名前は各シナリオの metrics と同じ
TOTALS = (("gaps", "観測の欠け gap の累計"), ("event_lost", "イベント lost の累計"), ("stuck", "stuck の回数"),
          ("id_timeouts", "id_timeout の回数"))
SLOPE = "slope_ticks_per_cycle"


def format_summary(stats: dict[str, Stats], elapsed_s: float, rounds: int, final: bool = False) -> str:
    lines = [f"=== 要約{'（最終）' if final else ''}: 経過 {elapsed_s / 3600:.2f} 時間、{rounds} ラウンド ==="]
    scen = [st for n, st in stats.items() if n != "resources"]
    for key, label in TOTALS:
        lines.append(f"{label}: {sum(st.total(key) for st in scen):g}")
    slopes = [st.metrics[SLOPE] for st in scen if SLOPE in st.metrics]
    if slopes:
        n = sum(m["n"] for m in slopes)
        mean = sum(m["mean"] * m["n"] for m in slopes) / n
        var = sum(m["m2"] + m["n"] * (m["mean"] - mean) ** 2 for m in slopes) / n
        lines.append(f"時刻の傾き（fprobe 刻み/対象サイクル）: {n} 回 最小 {min(m['min'] for m in slopes):.9g} "
                     f"最大 {max(m['max'] for m in slopes):.9g} 標準偏差 {math.sqrt(var):.3g}")
    else:
        lines.append("時刻の傾き: 測れた回が無い")
    total_s = sum(st.seconds for st in scen) or 1.0
    for name, st in stats.items():
        share = "" if name == "resources" else f"、時間 {st.seconds:.0f} 秒（{st.seconds / total_s:.0%}）"
        lines.append(f"{name}: {st.runs} 回、失敗 {st.fails}、例外 {st.exceptions}{share}")
        for k, m in st.metrics.items():
            lines.append(f"    {k}: 最初 {m['first']:.6g} 最後 {m['last']:.6g} 最小 {m['min']:.6g} 最大 {m['max']:.6g} "
                         f"標準偏差 {math.sqrt(m['m2'] / m['n']):.3g}")
    return "\n".join(lines)


def next_scenario(names: list[str], weights: dict[str, float], seconds: dict[str, float]) -> str:
    """使った時間 / 重み が最も小さいシナリオ（同点は並び順）。長い目で、実行時間の割合が重みの比になる。重み 0 は選ばない。"""
    live = [n for n in names if weights.get(n, 1) > 0]
    return min(live, key=lambda n: seconds[n] / weights.get(n, 1))


def parse_until(text: str, now: datetime | None = None) -> float:
    """"HH:MM" → 次にその時刻になる UNIX 時刻（今より前なら翌日）。"""
    now = now or datetime.now()
    hh, mm = text.split(":")
    t = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    if t <= now:
        t += timedelta(days=1)
    return t.timestamp()


class Runner:
    def __init__(self, rig, params: Params, names: list[str], weights: dict[str, int] | None = None, *,
                 log_path: str | Path, until: float | None = None, stop_file: str | Path | None = None,
                 summary_interval_s: float = 600.0, max_consecutive_failures: int = 3,
                 backoff_s: float = 5.0, backoff_max_s: float = 300.0, out=print, scenarios=None,
                 clock=time.time, sleep=time.sleep):
        self.rig, self.p, self.names = rig, params, names
        self.weights = {**DEFAULT_WEIGHTS, **(weights or {})}
        self.log_path = Path(log_path)
        self.until, self.stop_file = until, Path(stop_file) if stop_file else None
        self.summary_interval_s = summary_interval_s
        self.max_consecutive, self.backoff_s, self.backoff_max_s = max_consecutive_failures, backoff_s, backoff_max_s
        self.out, self.scenarios, self.clock, self.sleep_fn = out, scenarios or SCENARIOS, clock, sleep
        self.stats = {n: Stats() for n in [*names, "resources"]}
        self.consecutive = 0
        self.backoffs = 0
        self.rounds = 0
        self._stop = threading.Event()
        self.t_start = clock()

    def request_stop(self):
        self._stop.set()

    def should_stop(self) -> bool:
        return (self._stop.is_set() or (self.stop_file is not None and self.stop_file.exists())
                or (self.until is not None and self.clock() >= self.until))

    def _sleep(self, seconds: float):
        end = self.clock() + seconds
        while not self.should_stop() and self.clock() < end:
            self.sleep_fn(min(0.2, max(0.0, end - self.clock())))

    def _write(self, record: dict):
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def run_one(self, name: str) -> dict:
        t0 = self.clock()
        rec = {"time": datetime.fromtimestamp(t0).isoformat(timespec="seconds"), "elapsed_s": round(t0 - self.t_start, 1),
               "round": self.rounds, "scenario": name}
        exception = False
        try:
            res = self.scenarios[name](self.rig, self.p)
            rec.update(ok=res.ok, failures=res.failures, metrics=res.metrics)
            if res.details:
                rec["details"] = res.details
        except Exception as e:               # noqa: BLE001  1 つの例外で夜の運転を止めない。ログに残して次へ
            exception = True
            rec.update(ok=False, failures=[], metrics={}, exception=f"{type(e).__name__}: {e}",
                       traceback=traceback.format_exc(limit=-6))
        rec["duration_s"] = round(self.clock() - t0, 3)
        lost = getattr(getattr(getattr(self.rig, "device", None), "_transport", None), "link", None)
        if hasattr(lost, "lost_frames"):                         # UDP の落ちたデータグラム数（この接続の累計。再接続で 0 に戻る）
            rec["lost_frames"] = lost.lost_frames
        rec["resources"] = process_resources()                  # 毎回の記録に（リークの検出。RSS・ハンドル・スレッド）
        self.stats[name].add(rec["ok"], exception, rec["metrics"], rec["duration_s"])
        self.stats["resources"].add(True, False, rec["resources"])
        self._write(rec)
        return rec

    def _event(self, **fields):
        self._write({"time": datetime.fromtimestamp(self.clock()).isoformat(timespec="seconds"), **fields})

    def _recover(self):
        self.backoffs += 1
        wait = min(self.backoff_s * 2 ** (self.backoffs - 1), self.backoff_max_s)
        self.out(f"連続 {self.consecutive} 回失敗。{wait:.0f} 秒待って再接続する")
        self._event(event="reconnect", wait_s=wait, consecutive_failures=self.consecutive)
        self._sleep(wait)
        if self.should_stop():
            return
        try:
            self.rig.reconnect()
            self._event(event="reconnected")
        except Exception as e:               # noqa: BLE001
            self._event(event="reconnect_failed", error=f"{type(e).__name__}: {e}")

    def summary(self, final=False) -> str:
        text = format_summary(self.stats, self.clock() - self.t_start, self.rounds, final)
        self.out(text)
        with open(self.log_path.with_name(self.log_path.name + ".summary.txt"), "w", encoding="utf-8") as f:
            f.write(text + "\n")
        return text

    def run(self) -> str:
        last_summary = self.clock()
        try:
            while not self.should_stop():
                name = next_scenario(self.names, self.weights, {n: self.stats[n].seconds for n in self.names})
                rec = self.run_one(name)
                self.rounds += 1                          # ラウンド = シナリオ 1 回
                if rec["ok"]:
                    self.consecutive = self.backoffs = 0
                else:
                    self.consecutive += 1
                    if self.consecutive >= self.max_consecutive:
                        self._recover()
                        self.consecutive = 0
                if self.clock() - last_summary >= self.summary_interval_s:
                    self.summary()
                    last_summary = self.clock()
        except KeyboardInterrupt:
            self.out("中断（Ctrl+C）")
        finally:
            with contextlib.suppress(Exception):
                self.rig.close()
        return self.summary(final=True)


# ---------------------------------------------------------------------------
# 実機（PYNQ-Z2 の fprobe を Ethernet で）

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
        self.probe = Probe(eth=self.host)
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


# ---------------------------------------------------------------------------

def _parse_weights(text: str) -> dict[str, float]:
    out = {}
    for item in filter(None, text.split(",")):
        name, _, w = item.partition("=")
        if name not in SCENARIOS:
            raise argparse.ArgumentTypeError(f"知らないシナリオ {name}（{', '.join(SCENARIOS)}）")
        out[name] = float(w)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="fprobe の連続試験（tools/soak/README.md）")
    ap.add_argument("--host", default=os.environ.get("FPROBE_PYNQ_HOST"), help="PYNQ の IP（既定: 環境変数 FPROBE_PYNQ_HOST）")
    ap.add_argument("--until", help="止める時刻 HH:MM（次にその時刻になるとき）")
    ap.add_argument("--hours", type=float, help="--until の代わりに、これだけの時間で止める")
    ap.add_argument("--log", default="soak.jsonl")
    ap.add_argument("--stop-file", default="soak.stop", help="このファイルができたら止まる")
    ap.add_argument("--summary-interval", type=float, default=600.0, help="要約を出す間隔（秒）")
    ap.add_argument("--scenarios", default=",".join(SCENARIOS), help="走らせるシナリオ（カンマ区切り）")
    ap.add_argument("--weights", type=_parse_weights, default={}, help=f"実行時間の配分の重み（相対）。例 obs_long=10,ping=1。既定 {DEFAULT_WEIGHTS}。0 で外す")
    ap.add_argument("--max-consecutive-failures", type=int, default=3)
    ap.add_argument("--backoff", type=float, default=5.0)
    ap.add_argument("--list", action="store_true")
    for f in dataclasses.fields(Params):          # 合否の基準・長さはすべて引数で変えられる
        kind = str if isinstance(f.default, str) else (lambda s: int(s, 0)) if isinstance(f.default, int) else float
        ap.add_argument("--" + f.name.replace("_", "-"), type=kind, default=f.default, help=f"既定 {f.default}")
    a = ap.parse_args(argv)
    if a.list:
        print("\n".join(SCENARIOS))
        return 0
    if not a.host:
        ap.error("--host か環境変数 FPROBE_PYNQ_HOST が要る")
    params = Params(**{f.name: getattr(a, f.name) for f in dataclasses.fields(Params)})
    names = [n for n in a.scenarios.split(",") if n]
    for n in names:
        if n not in SCENARIOS:
            ap.error(f"知らないシナリオ {n}")
    until = parse_until(a.until) if a.until else (time.time() + a.hours * 3600 if a.hours else None)
    stop = Path(a.stop_file)
    if stop.exists():
        stop.unlink()                              # 前回の止めるファイルが残っていて直ちに止まらないように
    rig = RealRig(a.host)
    runner = Runner(rig, params, names, a.weights, log_path=a.log, until=until, stop_file=stop,
                    summary_interval_s=a.summary_interval, max_consecutive_failures=a.max_consecutive_failures,
                    backoff_s=a.backoff)
    print(f"開始: {names}、止める時刻 {datetime.fromtimestamp(until) if until else 'なし'}、止めるファイル {stop}")
    runner.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
