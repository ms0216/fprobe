"""ObsDecoder のランダム試験: 正しい答えが分かるように作った記録列を、与え方を変えても同じ答えが出るか・時刻が合うかで確かめる。"""

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_fpga_obs import group_record, mark, overflow, sync, value  # noqa: E402

from fprobe.obs_decoder import ObsDecoder  # noqa: E402


def stream(rng: random.Random, n: int, ratio: float, offset: int, with_overflow: bool):
    """n 個の値の変化と、その途中の印。返り値 = (イベント, [(値, 期待の fprobe の時刻)])"""
    events, expected = [group_record(0)], []
    cycles, seq, v = 0, 0, 0
    for i in range(n):
        delta = rng.randint(1, 3000) if i else 0
        if rng.random() < 0.15 and i:
            d = rng.randint(1, 500)
            cycles += d
            events.append(mark(d, seq & 0xFF))
            events.append(sync(seq & 0xFF, round(offset + ratio * cycles)))
            seq += 1
            delta = rng.randint(1, 3000)
        cycles += delta
        v = (v + rng.randint(1, 0xFFFF)) & 0xFFFF
        events.append(value(delta, v))
        expected.append((v, offset + ratio * cycles))
        if with_overflow and rng.random() < 0.03:
            events.append(overflow(rng.randint(1, 50)))
            return events, expected                      # 溢れの後は鎖が切れる: ここまでを答えにする
    return events, expected


def decode(events, cuts):
    d = ObsDecoder(nominal_ratio=None)
    out, last = [], 0
    for c in list(cuts) + [len(events)]:
        out += d.feed(events[last:c])
        last = c
    return out + d.flush()


@pytest.mark.parametrize("seed", range(40))
def test_the_answer_does_not_depend_on_how_the_events_are_fed(seed):
    rng = random.Random(seed)
    events, _ = stream(rng, rng.randint(5, 400), rng.uniform(0.5, 4.0), rng.randint(0, 10**9), rng.random() < 0.3)
    whole = decode(events, [])
    cuts = sorted(rng.sample(range(1, len(events)), min(len(events) - 1, rng.randint(1, 30))))
    assert decode(events, cuts) == whole
    assert decode(events, range(1, len(events))) == whole                    # 1 イベントずつ


@pytest.mark.parametrize("seed", range(40))
def test_times_follow_the_marks_and_values_keep_their_order(seed):
    rng = random.Random(1000 + seed)
    ratio, offset = rng.uniform(0.5, 4.0), rng.randint(0, 10**9)
    events, expected = stream(rng, rng.randint(30, 400), ratio, offset, False)
    values = [s for s in decode(events, []) if s.kind == "value"]
    assert [s.value for s in values] == [v for v, _ in expected]            # 値は欠けず順番どおり
    timed = [(s, t) for s, (_, t) in zip(values, expected) if s.time_fprobe is not None]
    assert len(timed) > len(values) // 2
    for s, t in timed:
        assert abs(s.time_fprobe - t) <= 2 + 0.002 * ratio * 3000, (s, t)    # 印の丸めと当てはめの誤差の範囲
