"""event_collector（BYTE_PACK = true）: UART・SWO のバイトは詰めた形（種類 13・14・15）で出る。
ランダムな入力（全部の源）で、(1) DAP・割り込みのイベントは今と同じ周期・同じ語、(2) 詰めた件をホストの解読で
バイトごとに戻すと、受け付けたバイトと時刻（古い形の時刻と同じ: UART 受信・SWO は源が付けた時刻、送信は 1 段レジスタで
受けた周期）がそのまま（欠けは数える）、
(3) 保存則（受け付けた数 = 出た数 + 数えた欠け）を確かめる。"""
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge

from fprobe.events import BYTE_KINDS, PACK_DELTA_BITS, PACKED_BYTE_KINDS, decode_events
from tests.tb.event_collector_tb import drive, rand_inputs

BYTE_SOURCES = (("u0", 3), ("u1", 4), ("u2", 5))


async def run(dut, seed, cycles, density, byte_density):
    rng = random.Random(seed)
    t = rng.getrandbits(40)
    for _ in range(3):
        i = rand_inputs(rng, 0.0, t, 0, 1)
        await FallingEdge(dut.clk)
        drive(dut, i)
        t += 1
    sent_bytes = {k: [] for _, k in BYTE_SOURCES}
    sent_other = 0
    records = []
    pending_bytes = []                           # 前の周期に fire した (種類, データ, 源の時刻)（1 段レジスタで受ける）
    # 受信の源の時刻: 受けた周期より一定の周期（開始ビットから受け渡しまで）+ 0〜50 周期（受け手の待ち）前で、受けた順に増える
    # （待ちが大きく変わる場合の切り方と欠けは event_byte_pack_tb が基準モデルで確かめる）
    src_time = {"u0": t - 5000, "u2": t - 5000}
    for n in range(cycles + 4 * (1 << PACK_DELTA_BITS)):
        idle = n >= cycles                       # 最後は入力を止めて出し切る
        i = rand_inputs(rng, 0.0 if idle else density, t, 1, 0)
        for name, _ in BYTE_SOURCES:
            i[f"{name}_fire"] = int(not idle and rng.random() < byte_density)
        for name in src_time:
            if i[f"{name}_fire"]:
                src_time[name] = max(src_time[name] + 1, t - 400 - rng.randint(0, 50))
            i[f"{name}_time"] = src_time[name] % (1 << 48)
        await FallingEdge(dut.clk)
        drive(dut, i)
        for kind, data, st in pending_bytes:
            sent_bytes[kind].append((t if st is None else st, data))   # 送信は受けた周期の次の時刻、受信は源の時刻
        pending_bytes = [(k, i[f"{name}_data"], i.get(f"{name}_time") if name != "u1" else None)
                         for name, k in BYTE_SOURCES if i[f"{name}_fire"]]
        sent_other += i["smp_valid"] + i["aux_valid"] + i["obs_valid"] + i["sync_valid"]
        immediate = None
        if i["ev_valid"]:
            immediate = (i["host_kind"] << 120) | (i["parity"] << 119) | (i["req"] << 115) | (i["ack"] << 112) \
                | (i["ev_data"] << 80) | (i["ev_addr"] << 48) | i["ev_time"]
        elif i["inj_written"]:
            immediate = (0x0B << 120) | (i["inj_transfers"] << 112) | (i["inj_value"] << 80) | (i["inj_addr"] << 48) \
                | i["inj_time"]
        await RisingEdge(dut.clk)
        await ReadOnly()
        if immediate is not None:                # DAP・割り込みは今と同じ（その周期に、その語）
            assert dut.event_valid.value == 1 and int(dut.event_data.value) == immediate, f"seed {seed} cycle {n}"
        elif dut.event_valid.value == 1:
            records.append(int(dut.event_data.value))
        t += 1
    return sent_bytes, sent_other, records, int(dut.byte_lost.value)


@cocotb.test()
async def bytes_are_packed_without_changing_any_byte_or_time(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    for seed, density, byte_density in ((1, 0.02, 0.05), (2, 0.1, 0.3), (3, 0.3, 0.6)):
        sent_bytes, sent_other, records, lost = await run(dut, seed, 6000, density, byte_density)
        kinds = [w >> 120 for w in records]
        assert not set(kinds) & set(BYTE_KINDS), "古い形の種類 3〜5 は出さない"
        packed = [w for w in records if w >> 120 in PACKED_BYTE_KINDS]
        events = decode_events(b"".join(w.to_bytes(16, "little") for w in packed))
        got = {k: [(e.time, e.data) for e in events if e.kind == k] for _, k in BYTE_SOURCES}
        others = len(records) - len(packed)
        total_in = sent_other + sum(len(v) for v in sent_bytes.values())
        total_out = others + len(events)
        assert total_in == total_out + lost, (seed, total_in, total_out, lost)
        for _, k in BYTE_SOURCES:
            if lost == 0:
                assert got[k] == sent_bytes[k], (seed, k)
            else:                                # 欠けた分を除いて、順も時刻も元のまま
                it = iter(sent_bytes[k])
                assert all(any(x == y for y in it) for x in got[k]), (seed, k)
        if seed == 1:
            assert lost == 0
            # 疎な入力でも、同じ源の続くバイトは 1 件にまとまる（バイトの数より件が少ない）
            assert len(packed) < len(events)
