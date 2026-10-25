"""pin_select: ランダムな入力を、端子ごとの優先（gpio > 小さい番号の動作の claim > 機能）の別の書き方の模型と突き合わせる。
PINS = 8、ACTIONS = 3、PIN_CHOICES = (4, 5, 6, 7)（候補は 4 本、動作は 3 つ）。"""
import random

import cocotb
from cocotb.triggers import Timer

PINS, ACTIONS = 8, 3
CHOICES = (4, 5, 6, 7)


def model(gd, ge, fo, foe, xo, xoe, claim, xpin):
    o, oe, claimed = [0] * PINS, [0] * PINS, [0] * PINS
    for i in range(PINS):
        owner = None
        for k in range(ACTIONS):                       # 小さい番号の動作が優先
            sel = (xpin >> (2 * k)) & 3
            if (claim >> k) & 1 and sel < len(CHOICES) and CHOICES[sel] == i:
                claimed[i] = 1
                if owner is None:
                    owner = k
        if (ge >> i) & 1:
            o[i], oe[i] = (gd >> i) & 1, 1
        elif owner is not None:
            o[i], oe[i] = (xo >> owner) & 1, (xoe >> owner) & 1
        else:
            o[i], oe[i] = (fo >> i) & 1, (foe >> i) & 1
    pack = lambda v: sum(b << i for i, b in enumerate(v))
    return pack(o), pack(oe), pack(claimed)


@cocotb.test()
async def random_inputs_match_the_model(dut):
    rng = random.Random(5)
    for n in range(4000):
        v = dict(gd=rng.getrandbits(PINS), ge=rng.getrandbits(PINS) if rng.random() < 0.5 else 0,
                 fo=rng.getrandbits(PINS), foe=rng.getrandbits(PINS), xo=rng.getrandbits(ACTIONS),
                 xoe=rng.getrandbits(ACTIONS), claim=rng.getrandbits(ACTIONS), xpin=rng.getrandbits(2 * ACTIONS))
        dut.gpio_drive.value, dut.gpio_enable.value = v["gd"], v["ge"]
        dut.func_o.value, dut.func_oe.value = v["fo"], v["foe"]
        dut.xout_o.value, dut.xout_oe.value, dut.xout_claim.value = v["xo"], v["xoe"], v["claim"]
        dut.xout_pin.value = v["xpin"]
        await Timer(1, unit="ns")
        exp = model(**v)
        got = (int(dut.pin_o.value), int(dut.pin_oe.value), int(dut.claimed.value))
        assert got == exp, f"{n}: {v} got {got} exp {exp}"
