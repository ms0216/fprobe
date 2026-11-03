"""util_pkg の剰余の差・比べ（2^n で折り返す総数どうし）と飽和の和（tests/rtl/util_mod_harness.vhd）。

総数（書いた拍・読んだ拍）は 32 ビットで折り返す。比べは「差が 2^(n-1) 未満なら a は b 以前」（RFC 1982 の通し番号の比べと
同じ考え）。差が 2^(n-1) ちょうどは、どちらが先とも決まらないので「以前でない」とする。"""

import random

import cocotb
from cocotb.triggers import Timer

M32 = 1 << 32


def mod_le(a, b, bits=32):
    return (b - a) % (1 << bits) < (1 << (bits - 1))


async def settle(dut, **values):
    for name, v in values.items():
        getattr(dut, name).value = v
    await Timer(1, unit="ns")


@cocotb.test()
async def the_difference_wraps_and_the_order_survives_the_wrap(dut):
    dut.a8.value = dut.b8.value = dut.sat_a.value = dut.sat_b.value = 0
    cases = [(0, 0), (5, 3), (3, 5), (M32 - 2, 3), (3, M32 - 2), (M32 - 1, 0), (0, M32 - 1),
             (0, 1 << 31), (1 << 31, 0), (0, (1 << 31) - 1), ((1 << 31) - 1, 0)]
    rnd = random.Random(7)
    cases += [(rnd.randrange(M32), rnd.randrange(M32)) for _ in range(200)]
    base = M32 - 1000                                         # 2^32 の手前から数え始めた総数の並び
    cases += [((base + i) % M32, (base + j) % M32) for i in range(0, 2000, 97) for j in range(0, 2000, 89)]
    for a, b in cases:
        await settle(dut, a=a, b=b)
        assert int(dut.diff.value) == (a - b) % M32, (a, b)
        assert int(dut.le.value) == int(mod_le(a, b)), (a, b)
        assert int(dut.lt.value) == int(mod_le(a, b) and a != b), (a, b)


@cocotb.test()
async def the_same_rule_holds_for_narrow_counters(dut):
    dut.a.value = dut.b.value = dut.sat_a.value = dut.sat_b.value = 0
    for a in range(256):
        for b in range(0, 256, 7):
            await settle(dut, a8=a, b8=b)
            assert int(dut.le8.value) == int(mod_le(a, b, 8)), (a, b)


@cocotb.test()
async def the_saturating_sum_stops_at_the_top(dut):
    dut.a.value = dut.b.value = dut.a8.value = dut.b8.value = 0
    for x, y in [(0, 0), (1, 2), (M32 - 1, 0), (M32 - 1, 1), (M32 - 2, 1), (1 << 31, 1 << 31), (M32 - 1, M32 - 1),
                 (123456, M32 - 123456), (123456, M32 - 123457)]:
        await settle(dut, sat_a=x, sat_b=y)
        assert int(dut.sat_sum.value) == min(x + y, M32 - 1), (x, y)
