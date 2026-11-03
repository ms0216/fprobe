"""形式 1 しか知らない古い読み手（記録の形式の版 3 より前。0506d568 の fprobe/recording.py の _transitions・_transitions_np・chunk_skew を
そのまま写したもの。かたまりのファイルの path を受ける形にしただけ）。古いソフトは直せないので、新しい記録のかたまりを渡して、
黙って違う波形を返さず、必ず例外で止まることを確かめるのに使う。"""

import json
from array import array
from pathlib import Path

from fprobe.la import Transitions


def old_transitions(path: Path) -> Transitions:
    data = Path(path).read_bytes()
    newline = data.index(b"\n")
    head = json.loads(data[:newline])
    n = head["count"]
    values = array("H")
    values.frombytes(data[newline + 1:newline + 1 + 2 * n])
    starts = array("Q")
    starts.frombytes(data[newline + 1 + 2 * n:newline + 1 + 10 * n])
    return Transitions(values, starts, head["length"], head["trigger"], 0, head["period_s"], head["channels"],
                       tuple(head.get("channel_skew", ())))


def old_transitions_np(path: Path) -> Transitions:
    import numpy as np
    data = Path(path).read_bytes()
    newline = data.index(b"\n")
    head = json.loads(data[:newline])
    n = head["count"]
    values = np.frombuffer(data, dtype=np.uint16, count=n, offset=newline + 1)
    starts = np.frombuffer(data, dtype=np.uint64, count=n, offset=newline + 1 + 2 * n)
    return Transitions(values, starts, head["length"], head["trigger"], 0, head["period_s"], head["channels"],
                       tuple(head.get("channel_skew", ())))


def old_chunk_skew(path: Path) -> tuple:
    with Path(path).open("rb") as f:
        return tuple(json.loads(f.readline()).get("channel_skew", ()))
