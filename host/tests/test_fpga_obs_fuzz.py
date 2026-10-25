"""FpgaObs の公開メソッドにでたらめな引数を与えて、内部の誤り（KeyError・AttributeError・ZeroDivisionError 等）が
漏れず、利用者に分かる例外（ValueError・TypeError・RuntimeError・TimeoutError）で断ることを確かめる。"""

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_fpga_obs import FakeDevice  # noqa: E402

from fprobe.fpga_obs import FpgaObs  # noqa: E402

ALLOWED = (ValueError, TypeError, RuntimeError, TimeoutError)
JUNK = [0, 1, -1, 2, 3, 15, 16, 31, 32, 33, 255, 256, 65535, 65536, 1 << 31, 1 << 32, -(1 << 31), 0.0, 0.5, 1e9,
        float("nan"), float("inf"), "", "0", "x", "0x10", None, True, False, [], {}, [1], {"a": 1}, b"\x00"]
CHAIN_KEYS = ["ir_len", "ir_before", "ir_after", "dr_before", "dr_after", "bogus"]


def junk(rng):
    if rng.random() < 0.2:
        return {rng.choice(CHAIN_KEYS): rng.choice(JUNK) for _ in range(rng.randint(0, 5))}
    return rng.choice(JUNK)


@pytest.mark.parametrize("seed", range(12))
def test_junk_arguments_are_refused_with_a_clear_error(seed):
    rng = random.Random(seed)
    for _ in range(200):
        obs = FpgaObs(FakeDevice())
        calls = [
            lambda: obs.open(chain=junk(rng), user_ir=junk(rng), half_period=junk(rng), words=junk(rng),
                             sync_period=junk(rng), id_scans=junk(rng), timeout_s=0.05),
            lambda: obs.register_read(junk(rng), timeout_s=0.05),
            lambda: obs.register_write(junk(rng), junk(rng), timeout_s=0.05),
            lambda: obs.select(junk(rng)),
            lambda: obs.samples([], junk(rng)),
        ]
        call = rng.choice(calls)
        try:
            call()
        except ALLOWED:
            pass                                                            # 分かる例外で断った
        except Exception as e:                                              # 内部の誤りが漏れた
            pytest.fail(f"{type(e).__name__}: {e}（{call.__code__.co_firstlineno} 行の呼び出し、seed {seed}）")


SIGNALS = {"groups": {"0": [{"name": "state", "lsb": 0, "width": 2, "enum": {"0": "IDLE", "1": "BUSY"}},
                            {"name": "restart", "lsb": 3, "width": 1}, {"name": "count", "lsb": 4, "width": 8}]}}
WORDS = ["state", "restart", "count", "fpga.state", "fpga.count", "nosuch", "=", "==", "changed", "rose", "fell", "and",
         "or", "not", "BUSY", "IDLE", "0", "1", "0x10", "-1", "99999999999999999999", "1.5", "", " ", "(", ")", "あ",
         "state=BUSY", "restart changed", "count>=3", "x=", "=y", "state=1=2"]


def condition(rng):
    return " ".join(rng.choice(WORDS) for _ in range(rng.randint(0, 6)))


@pytest.mark.parametrize("seed", range(8))
def test_junk_conditions_are_refused_with_a_clear_error(seed):
    from fprobe.fpga_obs import compile_match
    from fprobe.fpga_signals import FpgaSignals
    from fprobe.obs_condition import count_condition
    rng = random.Random(seed)
    signals = FpgaSignals.from_dict(SIGNALS)
    items = [{"kind": rng.choice(["value", "gap", "hidden", "group", "x"]), "group": rng.choice([0, 1, None]),
              "fields": rng.choice([None, {}, {"state": rng.choice(["IDLE", "BUSY", 1, "1", None]), "restart": rng.choice([0, 1, "0"]),
                                              "count": rng.randrange(256)}]),
              "time_us": rng.choice([None, 0, 1.5, 10**9]), "lost": rng.choice([None, 0, 3])} for _ in range(30)]
    for _ in range(300):
        text = condition(rng)
        for call in (lambda: compile_match(text, signals), lambda: count_condition(items, text)):
            try:
                call()
            except ALLOWED:
                pass
            except Exception as e:
                pytest.fail(f"{type(e).__name__}: {e}（条件 {text!r}）")
