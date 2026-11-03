"""event_collector: ランダムな入力を、別に書いた基準モデル（源ごとに 1 件保持・優先順・lost の数え方）と毎周期突き合わせる。
加えて、保存則（受け付けた数 = 出た数 + 捨てた数）を確かめる。
バイトの時刻: UART 受信・SWO は源が付けた時刻（uart_rx_time・swo_time。時刻の正しさの R3）、UART 送信は受けた周期 + 1。
DAP の転送・割り込み書き込みの時刻は源が運ぶ線上の点（ev_time・inj_time。受けた周期の time_count とは別の値を入れて、
そちらが出ることを確かめる）、サンプラの件（種類 7・17・18）は源が組んだ語をそのまま出す（time-accuracy R5・R6）。
CONFIG_EVENTS = true の版（tests/tb/event_collector_config_tb.py）は、PHY の設定（cfg_word）が変わった周期と記録を始めた
周期に種類 16 を出すことも同じモデルで確かめる（R7）。"""
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge

SOURCES = ("ev", "inj", "smp", "u0", "u1", "u2", "aux", "obs", "sync", "cfg")
ORDER = ("smp", "u0", "u1", "u2", "aux", "obs", "sync", "cfg")        # DAP・割り込みの次の優先順


def word(kind, b119_112, data, addr, t):
    return (kind << 120) | (b119_112 << 112) | (data << 80) | (addr << 48) | t


class Model:
    """周期ごとの入力から、その周期の出力（event_valid, event_data）と lost を返す。"""

    def __init__(self, config_events: bool = False):
        self.config_events = config_events
        self.held = {}                 # 源 -> 保持中の語（128 ビットの完成形）
        self.lost = 0
        self.prev_enable = 0
        self.prev_cfg = 0
        self.byte_fire = [0, 0, 0]     # 入力の 1 段レジスタ
        self.byte_in = [0, 0, 0]
        self.byte_src = [0, 0]         # UART 受信・SWO の源の時刻（1 段レジスタ）
        self.sent = {k: 0 for k in SOURCES}      # 保存則: 受け付けた数・出た数
        self.out = {k: 0 for k in SOURCES}

    def step(self, i):
        t = i["time"]
        en = i["en"]
        out = None
        taken = None
        if i["ev_valid"]:
            out = (i["host_kind"] << 120) | (i["parity"] << 119) | (i["req"] << 115) | (i["ack"] << 112) \
                | (i["ev_data"] << 80) | (i["ev_addr"] << 48) | i["ev_time"]
        elif i["inj_written"]:
            out = word(0x0B, i["inj_transfers"], i["inj_value"], i["inj_addr"], i["inj_time"])
        if out is None:
            for s in ORDER:
                if s in self.held:
                    out = self.held[s]
                    taken = s
                    self.out[s] += 1
                    break
        fires = {"smp": i["smp_valid"], "aux": i["aux_valid"], "obs": i["obs_valid"], "sync": i["sync_valid"],
                 "u0": self.byte_fire[0], "u1": self.byte_fire[1], "u2": self.byte_fire[2],
                 "cfg": self.config_events and (i["cfg"] != self.prev_cfg or not self.prev_enable)}
        words = {
            "smp": (i["smp_kind"] << 120) | i["smp_word"],
            "cfg": (0x10 << 120) | (i["cfg"] << 48) | t,
            "aux": word(6, 0, (i["aux_ch"] << 16) | i["aux_value"], 0, i["aux_time"]),   # 源の時刻（S3 の B2）
            "obs": word(9 if i["obs_sync"] else 8, 0, i["obs_data"], i["obs_addr"], i["obs_time"]),
            "sync": word(0x0A, 0, i["sync_parent"] >> 32, i["sync_parent"] & 0xFFFFFFFF, i["sync_local"]),
        }
        byte_times = (self.byte_src[0], t, self.byte_src[1])          # 送信だけは受けた周期 + 1（この周期の time）
        for k in range(3):
            words[f"u{k}"] = word(3 + k, 0, self.byte_in[k], 0, byte_times[k])
        lost_n = 0
        new = {}
        for s in ORDER:
            if fires[s] and en:
                self.sent[s] += 1
                if s in self.held and taken != s:
                    lost_n += 1
                else:
                    new[s] = words[s]
        if taken is not None:
            del self.held[taken]
        self.held.update(new)
        if not en or i["rst"]:
            self.held.clear()
        if i["rst"] or (en and not self.prev_enable):
            self.lost = 0
        else:
            self.lost += lost_n
        self.prev_enable = en
        self.prev_cfg = i["cfg"]
        self.byte_fire = [i["u0_fire"], i["u1_fire"], i["u2_fire"]]
        self.byte_in = [i["u0_data"], i["u1_data"], i["u2_data"]]
        self.byte_src = [i["u0_time"], i["u2_time"]]
        return out


def rand_inputs(rng, density, t, en, rst):
    def p():
        return int(rng.random() < density)
    i = {
        "time": t, "en": en, "rst": rst,
        "ev_valid": p(), "host_kind": rng.choice([1, 12]), "parity": rng.getrandbits(1), "req": rng.getrandbits(4),
        "ack": rng.getrandbits(3), "ev_data": rng.getrandbits(32), "ev_addr": rng.getrandbits(32),
        "inj_written": p(), "inj_transfers": rng.getrandbits(8), "inj_value": rng.getrandbits(32),
        "inj_addr": rng.getrandbits(32),
        "smp_valid": p(), "smp_addr": rng.getrandbits(32), "smp_data": rng.getrandbits(32),
        "smp_ack": rng.getrandbits(3),
        "u0_fire": p(), "u0_data": rng.getrandbits(8), "u1_fire": p(), "u1_data": rng.getrandbits(8),
        "u2_fire": p(), "u2_data": rng.getrandbits(8), "u0_time": rng.getrandbits(48), "u2_time": rng.getrandbits(48),
        "aux_valid": p(), "aux_ch": rng.randrange(4), "aux_value": rng.getrandbits(14),
        "obs_valid": p(), "obs_sync": rng.getrandbits(1), "obs_data": rng.getrandbits(32),
        "obs_addr": rng.getrandbits(32), "obs_time": rng.getrandbits(48),
        "sync_valid": p(), "sync_local": rng.getrandbits(48), "sync_parent": rng.getrandbits(64),
    }
    # 線上の点の時刻・サンプラの件の種類・PHY の設定は別の乱数から（rng の並びは変えない: 疎な入力で欠けが無いことを確かめる
    # 試験の入力を、この変更の前と同じにする）
    extra = random.Random(t * 7919 + 1)
    i["ev_time"] = extra.getrandbits(48)
    i["inj_time"] = extra.getrandbits(48)
    i["smp_kind"] = extra.choice([7, 17, 18])
    i["smp_word"] = (extra.getrandbits(5) << 115) | (i.pop("smp_ack") << 112) | (i.pop("smp_data") << 80) \
        | (i.pop("smp_addr") << 48) | extra.getrandbits(48)
    i["cfg"] = 0
    i["aux_time"] = extra.getrandbits(48)
    return i


def drive(dut, i):
    dut.rst.value = i["rst"]
    dut.time_count.value = i["time"]
    dut.events_enable.value = i["en"]
    dut.ev_valid.value = i["ev_valid"]
    dut.host_kind.value = i["host_kind"]
    dut.ev_parity.value = i["parity"]
    dut.ev_request.value = i["req"]
    dut.ev_ack.value = i["ack"]
    dut.ev_data.value = i["ev_data"]
    dut.ev_addr.value = i["ev_addr"]
    dut.ev_time.value = i["ev_time"]
    dut.inj_time.value = i["inj_time"]
    dut.cfg_word.value = i["cfg"]
    dut.inj_written.value = i["inj_written"]
    dut.inj_transfers.value = i["inj_transfers"]
    dut.inj_value.value = i["inj_value"]
    dut.inj_addr.value = i["inj_addr"]
    dut.smp_valid.value = i["smp_valid"]
    dut.smp_kind.value = i["smp_kind"]
    dut.smp_word.value = i["smp_word"]
    dut.uart_rx_fire.value = i["u0_fire"]
    dut.uart_rx_data.value = i["u0_data"]
    dut.uart_rx_time.value = i["u0_time"]
    dut.uart_tx_fire.value = i["u1_fire"]
    dut.uart_tx_data.value = i["u1_data"]
    dut.swo_fire.value = i["u2_fire"]
    dut.swo_data.value = i["u2_data"]
    dut.swo_time.value = i["u2_time"]
    dut.aux_valid.value = i["aux_valid"]
    dut.aux_ch.value = i["aux_ch"]
    dut.aux_value.value = i["aux_value"]
    dut.aux_src_time.value = i["aux_time"]
    dut.obs_valid.value = i["obs_valid"]
    dut.obs_sync.value = i["obs_sync"]
    dut.obs_data.value = i["obs_data"]
    dut.obs_addr.value = i["obs_addr"]
    dut.obs_time.value = i["obs_time"]
    dut.sync_valid.value = i["sync_valid"]
    dut.sync_local.value = i["sync_local"]
    dut.sync_parent.value = i["sync_parent"]


async def run(dut, model, rng, cycles, density, toggle_enable, tag, cfg_change=0.0):
    en = 1
    t = rng.getrandbits(40)
    prev_lost = model.lost
    prev_en = en
    for n in range(cycles):
        if toggle_enable and rng.random() < 0.02:
            en ^= 1
        rst = int(toggle_enable and rng.random() < 0.003)
        t += 1
        i = rand_inputs(rng, density, t, en, rst)
        if cfg_change:                                   # PHY の設定はまれに変わる（変わらない周期は前の値のまま）
            run.cfg = rng.getrandbits(72) if rng.random() < cfg_change else getattr(run, "cfg", 0)
            i["cfg"] = run.cfg
        await FallingEdge(dut.clk)
        drive(dut, i)
        exp = model.step(i)
        await RisingEdge(dut.clk)
        await ReadOnly()
        if dut.event_valid.value == 1:
            got = int(dut.event_data.value)
            assert exp is not None, f"{tag} cycle {n}: unexpected event {got:032x}"
            assert got == exp, f"{tag} cycle {n}: event {got:032x} != {exp:032x}"
        else:
            assert exp is None, f"{tag} cycle {n}: missing event {exp:032x}"
        # lost は 1 周期遅れて足される（同じ周期に落ちた数をいったんレジスタに置く。2026-10-29、sys を上げるため）:
        # この周期の lost = 前の周期までのモデルの数（リセットの周期は 0）
        want = 0 if rst or (en and not prev_en) else prev_lost       # リセット・有効にした周期は 0
        assert int(dut.byte_lost.value) == want, \
            f"{tag} cycle {n}: lost {int(dut.byte_lost.value)} != {want}"
        prev_lost = model.lost
        prev_en = en


async def reset(dut, model, rng):
    for n in range(3):
        i = rand_inputs(rng, 0.0, n, 0, 1)
        await FallingEdge(dut.clk)
        drive(dut, i)
        model.step(i)
        await RisingEdge(dut.clk)


@cocotb.test()
async def random_inputs_match_the_reference_model(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    for seed, density, toggle in ((1, 0.05, False), (2, 0.3, False), (3, 0.8, False), (4, 0.2, True), (5, 0.6, True)):
        rng = random.Random(seed)
        model = Model()
        await reset(dut, model, rng)
        await run(dut, model, rng, 3000, density, toggle, f"seed {seed}")


@cocotb.test()
async def every_accepted_item_is_output_or_counted_lost(dut):
    # 保存則: 有効のまま流し、入力を止めて保持を出し切る。受け付けた数 = 出た数 + lost
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    rng = random.Random(11)
    model = Model()
    await reset(dut, model, rng)
    await run(dut, model, rng, 4000, 0.4, False, "conservation")
    lost_before = model.lost
    for n in range(40):
        i = rand_inputs(rng, 0.0, 100000 + n, 1, 0)
        await FallingEdge(dut.clk)
        drive(dut, i)
        model.step(i)
        await RisingEdge(dut.clk)
    sent = sum(v for k, v in model.sent.items() if k not in ("ev", "inj"))
    out = sum(v for k, v in model.out.items() if k not in ("ev", "inj"))
    assert not model.held
    assert sent == out + model.lost, (sent, out, model.lost)
    assert int(dut.byte_lost.value) == model.lost >= lost_before
