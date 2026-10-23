"""プラグイン heuristics: 本体から外した経験則を、本体の API の結果の上に載せる（期待値は外す前の本体の試験から）。"""

import random
from pathlib import Path

import pytest

from fprobe.decode.synth import Wave, uart
from fprobe.heap import AllocationTracker
from fprobe.memview import ElfImage
from fprobe.plugins import heuristics as h
from fprobe.sigrok import write_sr
from fprobe.stackusage import measure_stack

SRAM_DEMO = Path(__file__).resolve().parents[1] / "firmware" / "sram_demo" / "sram_demo.elf"


class FakeApi:
    """call(method, **params) を持つ偽の呼び先。handlers[method](**params) を返す。"""

    def __init__(self, **handlers):
        self.handlers, self.calls = handlers, []

    def call(self, method, /, **params):
        self.calls.append((method, params))
        return self.handlers[method.replace(".", "_")](**params)


def tagged(out):
    assert out["heuristic"] and out["assumption"]
    return out


# --- 1. correlate ---------------------------------------------------------------------------------

def test_rank_causes_puts_most_preceding_first_then_by_lift():
    # 元の試験: 先行率 0.01・lift 2.9 が、先行率 1.0・lift 2.57 の本当の原因より上に来ないように
    rare = {"condition": "rare", "preceded": 0.01, "coverage": 0.01 / 2.9}
    real = {"condition": "real", "preceded": 1.0, "coverage": 1.0 / 2.57}
    half = {"condition": "half", "preceded": 0.5, "coverage": 0.5 / 1.2}
    none = {"condition": "none", "preceded": None, "coverage": 0.3}
    result = {"effect": {"condition": "errors changed"}, "window_s": 0.01, "causes": [rare, half, none, real]}
    out = tagged(h.rank_causes(result))
    assert [c["condition"] for c in out["causes"]] == ["real", "half", "rare", "none"]
    assert out["causes"][0]["lift"] == pytest.approx(2.57) and out["causes"][-1]["lift"] is None
    assert "lift" not in real                                              # 元の結果は変えない
    loose = h.rank_causes(result, min_preceded=0.0)                        # 閾値は引数で変えられる
    assert [c["condition"] for c in loose["causes"]][0] == "rare"


def test_rank_causes_with_zero_coverage_has_no_lift():
    out = h.rank_causes({"causes": [{"condition": "a", "preceded": 1.0, "coverage": 0.0}]})
    assert out["causes"][0]["lift"] is None


# --- 2. 外れ値 ------------------------------------------------------------------------------------

def test_outliers_iqr_and_zscore():
    values = [100] * 20 + [101] * 20 + [99] * 20 + [500]
    out = tagged(h.outliers(values))
    assert [r["index"] for r in out["iqr"]] == [60] and out["iqr"][0]["score"] > 1 and out["iqr"][0]["value"] == 500
    many = [100 + (i % 7) for i in range(200)] + [1000]
    assert [r["index"] for r in h.outliers(many)["zscore"]] == [200]
    assert h.outliers([])["iqr"] == [] and h.outliers([5])["zscore"] == []
    assert h.outliers([1, 1, 1, 1])["zscore"] == []                       # σ = 0


def test_outliers_of_intervals_adds_the_start_time():
    pairs = [[1000 * i, 100] for i in range(60)] + [[99_000, 500]]
    out = h.outliers_of_intervals({"intervals": pairs, "count": 61})
    assert [(r["t"], r["value"]) for r in out["iqr"]] == [(99_000, 500)] and out["truncated"] is False
    assert h.outliers_of_intervals({"intervals": pairs, "count": 5000})["truncated"] is True


# --- 3. UART のボー --------------------------------------------------------------------------------

def uart_capture(baud, rate, data, real_baud=None, gap_bits=1.0):
    w = Wave(channels=10, sample_rate_hz=rate, idle={5: 1})
    end = uart(w, 5, data, 7 / baud, baud=real_baud or baud, gap_bits=gap_bits)
    return w.build(end_s=end)


@pytest.mark.parametrize("baud", [9600, 115200, 1_000_000, 3_000_000])
def test_baud_is_estimated_and_snapped(baud):
    rng = random.Random(baud)
    data = [rng.randrange(256) for _ in range(50)]
    t = uart_capture(baud, 48e6 if baud > 115200 else baud * 23.3, data, real_baud=baud * 1.01, gap_bits=1.5)
    out = tagged(h.estimate_baud(t, 5))                                    # 1 % ずれていても標準値に寄せる
    assert out["baud"] == baud and out["snapped"] and out["raw_baud"] == pytest.approx(baud * 1.01, rel=0.005)
    raw = h.estimate_baud(t, 5, snap=None)
    assert not raw["snapped"] and raw["baud"] == raw["raw_baud"]


def test_nonstandard_baud_is_estimated_closely():
    t = uart_capture(83333, 10e6, bytes(range(0x20, 0x80)))
    assert h.estimate_baud(t, 5)["baud"] == pytest.approx(83333, rel=0.005)


def test_estimation_on_a_quiet_line_gives_nothing():
    w = Wave(channels=10, sample_rate_hz=115200 * 16, idle={5: 1})
    w.set(0, 1, 1e-6)
    out = h.estimate_baud(w.build(end_s=10e-6), 5)
    assert out["baud"] is None and out["heuristic"]


def test_decode_uart_with_estimated_baud_exports_estimates_and_decodes(tmp_path):
    t = uart_capture(115200, 115200 * 16, b"hello")
    api = FakeApi(la_export=lambda path, format: write_sr(t, path),
                  la_decode=lambda protocol, settings, **kw: {"protocol": protocol, "settings": settings, **kw})
    out = tagged(h.decode_uart_with_estimated_baud(api, 5, parity="none", limit=10, work_dir=tmp_path))
    assert out["baud"] == 115200
    assert out["decoding"] == {"protocol": "uart", "settings": {"channel": 5, "baud": 115200, "parity": "none"},
                               "limit": 10}
    assert [m for m, _ in api.calls] == ["la.export", "la.decode"] and api.calls[0][1]["format"] == "sr"


# --- 4. 典型のコアクロック ---------------------------------------------------------------------------

def test_typical_core_clock_by_part_name():
    # 元の試験（host/ui/tests/target.test.ts）と同じ値
    for part, mhz in (("nrf52840", 64), ("nrf52832", 64), ("stm32f401xe", 84), ("stm32f411ce", 100),
                      ("rp2040", 125), ("rp2350", 150)):
        assert tagged(h.typical_core_clock(part))["hz"] == mhz * 1e6
    assert h.typical_core_clock("cortex_m") is None and h.typical_core_clock(None) is None


# --- 5. RTOS のジョブ ------------------------------------------------------------------------------

H, M, L, IDLE = 1, 2, 3, 9
NAMES = {H: "high", M: "mid", L: "low", IDLE: "IDLE"}
PRIORITIES = {H: 3, M: 2, L: 1, IDLE: 0}


def runs(spec, exec_=None):
    exec_ = exec_ or {}
    return [{"task": NAMES[x], "handle": x, "t0": a, "t1": b, **({"exec": exec_[a]} if a in exec_ else {})}
            for x, a, b in spec]


def test_jobs_from_priorities_split_preemption_from_blocking():
    rs = runs([(L, 0, 10), (H, 10, 20), (L, 20, 30), (IDLE, 30, 50), (H, 50, 60), (IDLE, 60, 100)],
              exec_={10: 8})                                              # 割り込み (12, 14) を除いた自分の時間
    out = tagged(h.infer_jobs(rs, PRIORITIES, idle={IDLE}))
    by = {}
    for j in out["jobs"]:
        by.setdefault(j["task"], []).append(j)
    (low,) = by["low"]
    assert (low["start"], low["end"], low["exec"], low["preemptions"], low["end_reason"]) == (0, 30, 20, 1, "blocked")
    assert low["wall"] == 30
    assert [(j["start"], j["end"], j["exec"]) for j in by["high"]] == [(10, 20, 8), (50, 60, 10)]
    assert "IDLE" not in by


def test_jobs_with_equal_priorities_end_uncertain_and_gaps_truncate():
    pri = {"high": 1, "mid": 1, "low": 1}                                 # タスク名でも引ける
    rs = runs([(H, 0, 10), (M, 10, 20), (L, 20, 30), (L, 40, 50)])
    jobs = {(j["task"], j["start"]): j for j in h.infer_jobs(rs, pri, gaps=[{"t0": 30, "t1": 40}])["jobs"]}
    assert jobs[("high", 0)]["end_reason"] == "uncertain"
    assert jobs[("low", 20)]["end_reason"] == "truncated" and jobs[("low", 40)]["end_reason"] == "truncated"


def test_jobs_with_lower_is_more_urgent():
    rs = runs([(L, 0, 10), (H, 10, 20), (L, 20, 30)])
    pri = {H: 1, L: 5}                                                     # 小さい方が高い
    jobs = h.infer_jobs(rs, pri, order=-1)["jobs"]
    assert [(j["task"], j["end_reason"]) for j in jobs] == [("low", "truncated"), ("high", "blocked")]
    assert jobs[0]["preemptions"] == 1


def test_infer_jobs_from_api_collects_pages_priorities_and_order():
    all_runs = runs([(L, 0, 10), (H, 10, 20), (L, 20, 30), (IDLE, 30, 50)])

    def get(offset, limit):
        return {"runs": all_runs[offset:offset + limit], "runs_total": len(all_runs), "gaps": []}

    tasks = {"tasks": [{"name": NAMES[x], "handle": x, "priority": p} for x, p in
                       ((H, 1), (L, 5), (IDLE, 31))]}
    tasks["tasks"][1]["base_priority"] = 6                                 # 継承中: 元の優先度を使う
    api = FakeApi(rtos_timeline_get=get, rtos_tasks=lambda live: tasks,
                  rtos_objects=lambda live: {"priority_order": "lower_is_more_urgent"})
    out = tagged(h.infer_jobs_from_api(api, idle={"IDLE"}, page=3))
    assert out["order"] == -1 and out["priorities"]["low"] == 6
    low = next(j for j in out["jobs"] if j["task"] == "low")
    assert (low["start"], low["end"], low["end_reason"]) == (0, 30, "blocked")
    assert [m for m, _ in api.calls].count("rtos.timeline.get") == 2


# --- 6. トレースフックの CPU の負荷 ------------------------------------------------------------------

def test_trace_cpu_share_counts_instructions_and_the_fifo_wait():
    words = {"task": 10, "kernel": 4, "payload": 8}
    total = sum(words.values())
    over = {"words_by_port": words, "seconds": 1.0, "swo_share": 5 * total * 10 / 4e6}
    out = tagged(h.trace_cpu_share(over, core_clock_hz=16e6, baud=4e6))
    tx = 50 / 4e6 * 16e6                                                   # 1 語を送る間のコアのクロック数 = 200
    assert out["instruction_cycles"] == 10 * 14 + 4 * 36 + 8 * 7
    assert out["fifo_wait_cycles"] == pytest.approx(8 * (tx - h.PAYLOAD_CYCLES))
    assert out["cpu_share"] == pytest.approx(out["cycles"] / 16e6)
    assert "6.2" in out["assumption"] and "6.2 %" in h.trace_cpu_share.__doc__
    derived = h.trace_cpu_share(over, core_clock_hz=16e6)                  # baud を swo_share から逆算
    assert derived["baud"] == pytest.approx(4e6) and derived["cycles"] == pytest.approx(out["cycles"])
    assert out["notes"] == []
    busy = h.trace_cpu_share(dict(over, swo_share=0.9), core_clock_hz=16e6, baud=4e6)
    assert busy["notes"] and "80%" in busy["notes"][0]


def test_trace_cpu_share_without_baud_or_seconds():
    over = {"words_by_port": {"task": 1, "kernel": 0, "payload": 0}, "seconds": 0, "swo_share": None}
    out = h.trace_cpu_share(over, core_clock_hz=16e6)
    assert out["fifo_wait_cycles"] is None and out["cpu_share"] is None and out["cycles"] == 14


# --- 7. ヒープ・スタック -------------------------------------------------------------------------

def test_heap_growing_from_the_tracker_report():
    # 元の試験: leaky（確保だけ）が growing で先頭、steady・once は min_age_s で入らない
    t = AllocationTracker()
    now = 0.0
    for i in range(40):
        now += 1
        t.alloc(now, 0x1000 + 16 * i, 16, caller="leaky")
        t.alloc(now + 0.1, 0x8000, 32, caller="steady")
        t.free(now + 0.5, 0x8000, caller="steady")
    t.alloc(now, 0x9000, 100, caller="once")
    out = tagged(h.heap_growing(t.report(now=now + 1, min_age_s=5)))
    leaks = out["leak_candidates"]
    assert leaks[0]["caller"] == "leaky" and leaks[0]["count"] >= 35 and leaks[0]["growing"]


def test_heap_growing_puts_growing_before_bigger():
    report = {"live_by_caller": [
        {"caller": "big", "bytes": 1000, "live_counts": [[0, 5], [1, 5], [2, 5], [3, 5], [4, 5]]},
        {"caller": "grows", "bytes": 10, "live_counts": [[0, 0], [1, 1], [2, 1], [3, 2], [4, 3]]},
        {"caller": "dips", "bytes": 500, "live_counts": [[0, 0], [1, 2], [2, 1], [3, 3], [4, 4]]},
    ]}
    out = h.heap_growing(report)
    assert [(c["caller"], c["growing"]) for c in out["leak_candidates"]] == [
        ("grows", True), ("big", False), ("dips", False)]
    api = FakeApi(heap_trace_report=lambda min_age_s, top: report)
    assert h.heap_growing_from_api(api)["min_age_s"] == 1.0 and api.calls[0][1] == {"min_age_s": 1.0, "top": 20}


def test_stack_warning_at_90_percent_and_the_16_byte_guard():
    ram = bytearray(b"\xcd" * 0x1000)
    read = lambda a, n: bytes(ram[a - 0x2000_0000:a - 0x2000_0000 + n])
    ram[0xC00:] = b"\x01" * 0x400                                         # 25 %
    calm = tagged(h.stack_warning(measure_stack(read, 0x2000_0000, 0x2000_1000, 0xCD)))
    assert not calm["warning"] and not calm["overflow_suspected"]
    ram[0x100:] = b"\x01" * 0xF00                                         # 約 94 %
    assert h.stack_warning(measure_stack(read, 0x2000_0000, 0x2000_1000, 0xCD))["warning"]
    ram[8:] = b"\x01" * (0x1000 - 8)                                      # 底から 8 バイトしか残っていない
    near = h.stack_warning(measure_stack(read, 0x2000_0000, 0x2000_1000, 0xCD))
    assert near["overflow_suspected"] and near["warning"] and near["notes"]
    assert not h.stack_warning(measure_stack(read, 0x2000_0000, 0x2000_1000, 0xCD), guard=4)["overflow_suspected"]


# --- 8. 故障・接続 ---------------------------------------------------------------------------------

def test_fault_hints_add_the_typical_causes_and_keep_the_architecture_hint():
    from fprobe.fault import decode_cfsr, decode_hfsr
    report = {"causes": decode_hfsr(1 << 30) + decode_cfsr((1 << 1) | (1 << 12) | (1 << 25)), "faulted": True}
    out = tagged(h.fault_hints(report))
    by = {c["bit"]: c for c in out["causes"]}
    assert "NULL" in by["DACCVIOL"]["typical"] and "MMFAR" in by["DACCVIOL"]["hint"]
    assert "あふれ" in by["STKERR"]["typical"] and "CFSR" in by["FORCED"]["typical"]
    assert "typical" not in by["DIVBYZERO"]                               # 典型の助言が無いビット
    v6m = {"causes": [{"bit": "HardFault", "meaning": "...", "hint": "..."}]}
    assert "推測" in h.fault_hints(v6m)["causes"][0]["typical"]


def step(name, status, message, **data):
    return {"name": name, "status": status, "message": message, **({"data": data} if data else {})}


def typical_of(out, name):
    return next(s for s in out["steps"] if s["name"] == name).get("typical", [])


def test_diagnose_hints_for_voltage_pins_and_swd():
    result = {"ok": False, "verdict": "接続できない: SWD に応答が無い", "steps": [
        step("voltage", "ok", "Vtarget = 0.02 V（fprobe の入出力は 3.3 V）", volts=0.02, live=True),
        step("pins", "warn", "nRESET = Low / SWDIO = Low", swclk=1, swdio=0, nreset=0),
        step("swd", "fail", "SWD に応答が無い（どの試し方・速さでも ACK が来ない）", attempts=[]),
        step("jtag", "fail", "JTAG のチェーンに何も無い"),
    ]}
    out = tagged(h.diagnose_hints(result))
    assert "電源が入っていない" in typical_of(out, "voltage")[0]
    assert len(typical_of(out, "pins")) == 2 and "プルアップ" in typical_of(out, "pins")[1]
    assert "GND" in typical_of(out, "swd")[0]
    assert "typical" not in result["steps"][0]                             # 元の結果は変えない


def test_diagnose_hints_blame_the_measurement_when_the_target_responds():
    # 2026-10-18 の実機: 計測の線が外れて 0 V でも、ターゲットは SWD に応答していた
    result = {"ok": True, "steps": [
        step("voltage", "ok", "Vtarget = 0.08 V", volts=0.08, live=True),
        step("swd", "warn", "DPIDR = 0x2BA01477。4,000,000 Hz では応答せず 400,000 Hz で応答"),
        step("power", "ok", "デバッグ電源が上がった"),
    ]}
    out = h.diagnose_hints(result)
    assert "計測の配線" in typical_of(out, "voltage")[0]
    assert "信号品質" in typical_of(out, "swd")[0] and typical_of(out, "power") == []


@pytest.mark.parametrize("volts,word", [(1.2, "レベル変換"), (5.0, "壊す"), (3.3, None)])
def test_diagnose_hints_voltage_thresholds(volts, word):
    out = h.diagnose_hints({"steps": [step("voltage", "ok", "Vtarget", volts=volts, live=True)]})
    typical = typical_of(out, "voltage")
    assert (word in typical[0]) if word else typical == []


def test_diagnose_hints_for_other_swd_failures_and_power():
    for message, word in (("SWD の応答にパリティ誤りがある", "信号品質"), ("SWD が WAIT を返し続ける", "低消費電力")):
        assert word in typical_of(h.diagnose_hints({"steps": [step("swd", "fail", message)]}), "swd")[0]
    power = step("power", "fail", "デバッグ電源の要求に応答しない（CDBGPWRUPACK = 0）")
    assert "保護" in typical_of(h.diagnose_hints({"steps": [power]}), "power")[0]
    over = step("voltage", "warn", "ターゲットの電圧が測れる上限を超えている", over_range=True, live=True)
    assert "分圧" in typical_of(h.diagnose_hints({"steps": [over]}), "voltage")[0]


# --- 9. disasm ------------------------------------------------------------------------------------

def memory_api(image):
    return FakeApi(target_memory_read=lambda address, length: image.read_memory(address, length).hex())


def test_disasm_before_pc_aligns_by_the_thumb_heuristic():
    # 元の試験（シンボル無しの around）と同じ並び
    out = tagged(h.disasm_before_pc(memory_api(ElfImage(SRAM_DEMO)), 0x20000098, before=4, after=1))
    assert out["sync"] == "heuristic" and out["pc_index"] == 4
    assert [i["address"] for i in out["instructions"]] == [0x2000008E, 0x20000090, 0x20000094, 0x20000096,
                                                           0x20000098, 0x2000009C]


def test_disasm_before_pc_falls_back_to_pc_when_nothing_lands():
    from fprobe.memview import BytesMemory
    code = bytes.fromhex("ffff" * 40)                                      # どこから読んでも解読できない
    mem = BytesMemory(0x1000, code)
    out = h.disasm_before_pc(memory_api(mem), 0x1028, before=2, after=1)
    assert out["sync"] == "start" and out["pc_index"] == 0 and out["instructions"][0]["address"] == 0x1028


# --- 2 回目に外した判断（2026-10-22 以降） --------------------------------------------------------------

def test_aliasing_suspects_old_rules():
    result = {"by": "exception", "total": 1000, "rows": [{"exception": 15, "name": "SysTick", "samples": 8},
                                      {"exception": 16, "name": "IRQ0", "samples": 50}],
              "exceptions": [{"exception": 15, "name": "SysTick", "load": 0.001},     # 8 回 vs 期待 1 回: 外れ
                             {"exception": 16, "name": "IRQ0", "load": 0.05},        # 50 回 vs 50 回: 合う
                             {"exception": 17, "name": "IRQ1", "load": 0.01}]}       # 0 回 vs 期待 10 回 ≥ 4.6
    out = tagged(h.aliasing_suspects(result))
    assert [s["exception"] for s in out["suspects"]] == [15, 17]
    assert out["suspects"][1]["reason"] == "zero_samples"
    with pytest.raises(ValueError):                                         # by="exception" の全件でないと 0 回を作る
        h.aliasing_suspects({**result, "by": "function"})
    few = h.aliasing_suspects({**result, "exceptions": [{"exception": 17, "name": "IRQ1", "load": 0.004}]})
    assert few["suspects"] == []                                            # 期待 4 回 < 4.6 は言わない


def test_heap_overflow_suspects_name_previous_used():
    scan = {"issues": [{"kind": "bad_size", "address": 0x2000_0100, "previous_used": 0x2000_00C8},
                       {"kind": "bad_end", "address": 0x2000_0F00, "previous_used": None},
                       {"kind": "free_not_listed", "address": 0x2000_0200}]}
    out = tagged(h.heap_overflow_suspects(scan))
    assert out["suspects"] == [{"kind": "bad_size", "address": 0x2000_0100, "suspect": 0x2000_00C8}]


def test_sampler_falling_behind_ratio():
    slow = {"sampling": {"requested_hz": 10_000, "achieved_hz": 8_500}}
    assert tagged(h.sampler_falling_behind(slow))["falling_behind"] is True
    assert h.sampler_falling_behind({"sampling": {"requested_hz": 10_000, "achieved_hz": 9_500}})["falling_behind"] is False
    assert h.sampler_falling_behind({"sampling": {"requested_hz": 10_000, "achieved_hz": None}})["falling_behind"] is None


def test_counter_overflow_risk_half_bandwidth():
    # 2 個 × 16 MHz / 256 × 2 バイト = 250 kB/s、SWO 2 Mbaud = 200 kB/s → 125 %
    out = tagged(h.counter_overflow_risk(2, 16e6, 2_000_000))
    assert out["share"] == pytest.approx(1.25) and out["risky"] is True
    assert h.counter_overflow_risk(1, 16e6, 6_000_000)["risky"] is False    # 125 kB/s / 600 kB/s ≈ 21 %
    assert h.SWO_HEADROOM == 0.75


def test_jtag_guess_ir_takes_shortest_candidate():
    from fprobe.jtag_chain import ChainDevice, ChainScan, _split_ir
    # 表に無い 2 台・合計 12 ビット・境目の候補 0, 4, 8: 本体は装置 0 を ambiguous（候補 4, 8）で止め、装置 1 は unknown
    devices = [ChainDevice(0, 0x1, None, "unknown"), ChainDevice(1, 0x3, None, "unknown")]
    capture = 0b0001_0001_0001
    _split_ir(devices, 12, capture, [])
    scan = ChainScan(devices, 12, capture).as_dict()
    assert scan["devices"][0]["ir_length_source"] == "ambiguous" and scan["jtag_chain"] is None
    out = tagged(h.jtag_guess_ir(scan))
    assert out["jtag_chain"] == [4, 8] and out["guessed"] == [0]
    known = ChainScan([ChainDevice(0, 0x1, 4, "table"), ChainDevice(1, 0x3, None, "unknown")], 12, capture).as_dict()
    assert h.jtag_guess_ir(known) == {**h.jtag_guess_ir(known), "jtag_chain": [4, 8], "guessed": []}
    assert h.jtag_guess_ir({**scan, "ir_capture": None})["jtag_chain"] is None


def test_link_cause_hints():
    assert "低消費電力" in " ".join(tagged(h.link_cause_hints(ack="WAIT"))["causes"])
    assert "プルアップ" in " ".join(h.link_cause_hints(tdo="stuck_high")["causes"])
    assert h.link_cause_hints(tdo="ok")["causes"] == []


def test_wait_failed_as_timeout():
    stats = {"count": 3, "failed": 1}
    out = tagged(h.wait_failed_as_timeout(stats))
    assert out["timeouts"] == 1


def test_aliasing_suspects_cut_rows_are_unknown_not_zero():
    result = {"by": "exception", "total": 1000, "rows": [{"exception": 16, "name": "IRQ0", "samples": 50}],
              "exceptions": [{"exception": 16, "name": "IRQ0", "load": 0.05},
                             {"exception": 17, "name": "IRQ1", "load": 0.01}]}
    out = h.aliasing_suspects(result, top=1)                                # rows が 1 件で切られた: IRQ1 の回数は不明
    assert out["suspects"] == [] and out["unknown"] == [17]
    api = FakeApi(profile_result=lambda by, top: {**result, "top_seen": top})
    assert h.aliasing_suspects_from_api(api)["unknown"] == [] and api.calls == [("profile.result", {"by": "exception", "top": 1000})]
