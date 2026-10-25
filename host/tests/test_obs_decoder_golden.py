"""ObsDecoder の出力が変わらないことの試験（最適化・整理の安全網）。

乱数で作った操作列（記録・印・標本・欠け・開き直し・flush が混ざる）を ObsDecoder に与えた出力の全欄を、
最適化の前の実装で保存した `data/obs_decoder_golden.json` と比べる。保存し直すのは復号の意味を意図して
変えたときだけ: `python host/tests/test_obs_decoder_golden.py --regen`。
"""

import json
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_fpga_obs import group_record, mark, overflow, record, response, snapshot, sync, tick, value  # noqa: E402

from fprobe.obs_decoder import ObsDecoder  # noqa: E402

GOLDEN = Path(__file__).parent / "data" / "obs_decoder_golden.json"
SEEDS = range(60)


def operations(seed: int) -> list:
    """1 つの種から操作列（("feed", events) / ("lost",) / ("reset", ratio, sync_delay, sample_delay) / ("flush",)）を作る。"""
    rng = random.Random(seed)
    ops: list = []
    pending: list = []
    seq, fprobe = 0, rng.randint(0, 1 << 20)
    marks_waiting: list = []

    def emit(e):
        pending.append(e)

    emit(group_record(rng.randint(0, 3)))
    for _ in range(rng.randint(20, 400)):
        r = rng.random()
        received = fprobe = fprobe + rng.randint(0, 5000)
        if r < 0.45:
            emit(value(rng.choice([0, 0, 1, 7, 4096, 16383, rng.randint(0, 16383)]), rng.randint(0, 0xFFFF), time=received))
        elif r < 0.60:
            late = rng.choice([0, 3, 0x7F, rng.randint(0, 127)])
            emit(mark(rng.randint(0, 600), seq & 0xFF, late, time=received))
            marks_waiting.append(seq & 0xFF)
            seq += rng.choice([1, 1, 1, 2, 300])         # 番号の飛び・一周も
        elif r < 0.72 and marks_waiting:
            s = marks_waiting.pop(rng.randrange(len(marks_waiting)))
            emit(sync(s, max(0, fprobe + rng.choice([0, 0, 100, -(1 << 47), 1 << 47]))))
        elif r < 0.76:
            emit(tick(rng.randint(0, 16383), time=received))
        elif r < 0.80:
            emit(overflow(rng.randint(0, 0xFFFF), time=received))
        elif r < 0.84:
            emit(group_record(rng.randint(0, 255), time=received))
        elif r < 0.87:
            emit(response(rng.randint(0, 255), rng.randint(0, 0xFFFF), time=received))
        elif r < 0.91:
            emit(snapshot(rng.randint(0, 5), rng.randint(0, 0xFFFF), received))
        elif r < 0.93:
            emit(record(rng.randint(0, 3), rng.getrandbits(30), time=received))      # でたらめな語
        elif r < 0.95:
            ops.append(("feed", pending[:]))
            pending.clear()
            ops.append(("lost",))
        elif r < 0.96:
            ops.append(("feed", pending[:]))
            pending.clear()
            ops.append(("reset", rng.choice([None, 0.0, 1.0, 2.5]), rng.choice([0.0, 2.0]), rng.choice([0.0, 1.5])))
        elif r < 0.98:
            ops.append(("feed", pending[:]))
            pending.clear()
            ops.append(("flush",))
        elif len(pending) > 3:
            ops.append(("feed", pending[:rng.randint(1, len(pending))]))
            pending.clear()
    ops.append(("feed", pending[:]))
    ops.append(("flush",))
    return ops


def run(seed: int) -> list:
    rng = random.Random(seed * 7 + 1)
    d = ObsDecoder(sync_delay=rng.choice([0.0, 3.0]), sample_delay=rng.choice([0.0, 2.0]),
                   nominal_ratio=rng.choice([None, 1.0, 1.5]), recent=rng.choice([0, 50]))
    out = []
    for op in operations(seed):
        if op[0] == "feed":
            got = d.feed(op[1])
        elif op[0] == "lost":
            d.events_lost()
            continue
        elif op[0] == "reset":
            d.reset_clock(*op[1:])
            continue
        else:
            got = d.flush()
        out += got
    out.append(list(d.recent))
    return [[s.time_fprobe, s.target_cycles, s.value, s.lost, s.group, s.kind, s.received, s.time_basis]
            if not isinstance(s, list) else [[x.time_fprobe, x.target_cycles, x.value, x.kind] for x in s]
            for s in out]


@pytest.mark.parametrize("seed", SEEDS)
def test_decoder_output_matches_the_saved_golden(seed):
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert run(seed) == golden[str(seed)]


if __name__ == "__main__":
    if "--regen" in sys.argv:
        GOLDEN.write_text(json.dumps({str(s): run(s) for s in SEEDS}), encoding="utf-8")
        print("saved", GOLDEN)
