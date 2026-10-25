"""obs_record_rx: 基準モデルとランダム入力（脈は互いに重ならない。engine の使い方どおり）で突き合わせる。"""

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, RisingEdge

ID_LO, ID_HI = 0x4253, 0x464F
INPUTS = ("clear", "scan_start", "word_valid", "word", "part", "scan_index", "time_count", "cap_time",
          "scan_end", "sync_mark", "sync_num", "sync_time")


class Model:
    def __init__(self):
        self.reset()

    def reset(self):
        self.id_ok = self.bad = self.snap_mode = self.snap_seen = self.ovf = self.tgt_run = 0
        self.lo = self.hi = 0
        self.snap_last = 0
        self.rec_count = 0
        self.read_value = 0
        self.id = 0
        self.resp_seq = 0
        self.obs = None
        self.ev = None

    @property
    def id_known(self):
        return int(bool(self.id_ok or (self.lo and self.hi)))

    def step(self, i):
        self.obs = None
        self.ev = None
        if i["clear"]:
            self.id_ok = 0
            self.id = 0
            self.tgt_run = 0
            self.read_value = 0
            self.ovf = 0
            self.lo = self.hi = 0
            self.snap_mode = self.snap_seen = 0
        if i["scan_start"]:
            self.bad = 0
        if i["word_valid"]:
            w, part = i["word"], i["part"]
            if part == 0 and (w >> 24) == 0xA7 and (not self.id_ok or self.snap_mode):
                self.rec_count = 0
                self.tgt_run = self.snap_mode = self.id_ok = 1
                self.id = 0x534E4150
                last, self.snap_last, seen, self.snap_seen = self.snap_last, w, self.snap_seen, 1
                if not seen or (w & 0xFFFF) != (last & 0xFFFF) or ((w >> 16) & 0xFF) != 0:
                    self.ev = (0, w, i["scan_index"], i["cap_time"])
                    self.obs = w & 0xFFFF
            elif part == 0:
                self.rec_count = w & 0xFFFF
                self.ovf = (w >> 16) & 1
                self.tgt_run = (w >> 17) & 1
                if (w >> 24) != 0xA6:
                    self.bad = 1
            elif part - 1 < self.rec_count and not self.bad:
                if (w >> 26) == 0b110001:
                    self.resp_seq = (self.resp_seq + 1) & 0xFF
                    self.read_value = (self.resp_seq << 24) | (w & 0xFFFFF)
                    sel = (w >> 16) & 0xF
                    if sel == 0:
                        self.id = (self.id & 0xFFFF0000) | (w & 0xFFFF)
                        if (w & 0xFFFF) == ID_LO:
                            self.lo = 1
                        else:
                            self.bad = 1
                    elif sel == 1:
                        self.id = (self.id & 0xFFFF) | ((w & 0xFFFF) << 16)
                        if (w & 0xFFFF) == ID_HI:
                            self.hi = 1
                        else:
                            self.bad = 1
                if self.id_ok:
                    self.ev = (0, w, i["scan_index"], i["time_count"])
                    if (w >> 30) == 0:
                        self.obs = w & 0xFFFF
        if i["scan_end"]:
            if i["sync_mark"] and self.id_ok and not self.bad and not self.snap_mode:
                self.ev = (1, i["sync_num"], 0, i["sync_time"])
            if not self.id_ok and self.lo and self.hi:
                self.id_ok = 1


def rand_word(rng, part):
    kind = rng.random()
    if part == 0:
        mark = rng.choice([0xA6, 0xA6, 0xA6, 0xA7, 0xA7, 0x00, 0xFF])
        return (mark << 24) | (rng.getrandbits(8) << 16) | rng.randrange(0, 5)
    if kind < 0.4:
        sel = rng.choice([0, 1, 2])
        lo = (ID_LO if sel == 0 else ID_HI) if rng.random() < 0.85 else rng.choice([ID_LO, ID_HI, rng.getrandbits(16)])
        return (0b110001 << 26) | (sel << 16) | lo
    if kind < 0.8:
        return (rng.getrandbits(2) << 30) | rng.getrandbits(30)
    return rng.getrandbits(32)


@cocotb.test()
async def random_inputs_match_the_model(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    rng = random.Random(7)
    for n in INPUTS:
        getattr(dut, n).value = 0
    dut.rst.value = 1
    await RisingEdge(dut.clk)
    await RisingEdge(dut.clk)
    dut.rst.value = 0
    m = Model()
    seen = {"ev0": 0, "ev1": 0, "obs": 0, "id_ok": 0, "snap": 0, "bad": 0}
    for cycle in range(30000):
        await FallingEdge(dut.clk)
        assert int(dut.id_ok.value) == m.id_ok, f"id_ok @ {cycle}"
        assert int(dut.id_known.value) == m.id_known, f"id_known @ {cycle}"
        assert int(dut.bad.value) == m.bad, f"bad @ {cycle}"
        assert int(dut.snap_mode.value) == m.snap_mode, f"snap_mode @ {cycle}"
        assert int(dut.rec_count.value) == m.rec_count, f"rec_count @ {cycle}"
        assert int(dut.ovf.value) == m.ovf and int(dut.tgt_run.value) == m.tgt_run, f"flags @ {cycle}"
        assert int(dut.read_value.value) == m.read_value, f"read_value @ {cycle}"
        assert int(dut.id.value) == m.id, f"id @ {cycle}"
        assert int(dut.obs_valid.value) == int(m.obs is not None), f"obs_valid @ {cycle}"
        if m.obs is not None:
            assert int(dut.obs_v.value) == m.obs, f"obs_v @ {cycle}"
            seen["obs"] += 1
        assert int(dut.ev_valid.value) == int(m.ev is not None), f"ev_valid @ {cycle}"
        if m.ev is not None:
            s, d, a, t = m.ev
            assert (int(dut.ev_sync.value), int(dut.ev_data.value), int(dut.ev_addr.value),
                    int(dut.ev_time.value)) == (s, d, a, t), f"event @ {cycle}"
            seen["ev1" if s else "ev0"] += 1
        seen["id_ok"] += m.id_ok
        seen["snap"] += m.snap_mode
        seen["bad"] += m.bad
        pulse = rng.choices(["none", "clear", "scan_start", "word", "scan_end"], [3, 0.3, 1.5, 6, 1.5])[0]
        part = 0 if rng.random() < 0.3 else rng.randrange(1, 6)
        i = {"clear": int(pulse == "clear"), "scan_start": int(pulse == "scan_start"),
             "word_valid": int(pulse == "word"), "word": rand_word(rng, part), "part": part,
             "scan_index": rng.getrandbits(32), "time_count": rng.getrandbits(48), "cap_time": rng.getrandbits(48),
             "scan_end": int(pulse == "scan_end"), "sync_mark": int(rng.random() < 0.5),
             "sync_num": rng.getrandbits(8), "sync_time": rng.getrandbits(48)}
        rst = rng.random() < 0.0005
        for n, v in i.items():
            getattr(dut, n).value = v
        dut.rst.value = int(rst)
        await RisingEdge(dut.clk)
        if rst:
            m.reset()
        else:
            m.step(i)
    for k, v in seen.items():
        assert v > 20, f"coverage {k} = {v}"
