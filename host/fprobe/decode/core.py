"""解読の共通部分: 注釈（Annotation）と結果（Decoding）、変化点の表現からのチャンネルの取り出し。

仕様: docs/superpowers/specs/2026-10-17-f7-protocol-decode-design.md
"""

import json
from array import array
from bisect import bisect_left, bisect_right
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass

from ..la import Transitions

__all__ = ["Annotation", "ChannelLevels", "Decoding", "project"]


@dataclass(frozen=True, slots=True)
class Annotation:
    """解読の 1 件。サンプル番号の区間 [start, end) に、種類・値・文字列・誤りを付ける。"""
    start: int
    end: int
    kind: str
    text: str
    value: int | None = None
    error: str | None = None                       # 誤りの識別子（"parity"、"frame"、"nack" …）
    fields: Mapping[str, object] | None = None     # 構造化した中身（JSON にできる値だけ）

    def __post_init__(self):
        if self.end <= self.start:
            raise ValueError(f"注釈の区間が空（start={self.start}, end={self.end}, kind={self.kind}）")


@dataclass(frozen=True)
class Decoding:
    """1 つの解読の結果。注釈は start の昇順（同じ start なら外側＝長い注釈が先）。"""
    protocol: str
    annotations: tuple[Annotation, ...]
    sample_period_s: float
    trigger: int
    length: int
    params: Mapping[str, object]

    def __post_init__(self):
        ordered = tuple(sorted(self.annotations, key=lambda a: (a.start, -a.end)))
        object.__setattr__(self, "annotations", ordered)
        object.__setattr__(self, "_starts", array("Q", (a.start for a in ordered)))
        # 区間の検索用: 先頭から i 番目までの注釈の end の最大（start の昇順なので、これで重なりの左端を二分探索できる）
        reach, longest = array("Q"), 0
        for a in ordered:
            longest = max(longest, a.end)
            reach.append(longest)
        object.__setattr__(self, "_reach", reach)

    def __len__(self) -> int:
        return len(self.annotations)

    def __iter__(self) -> Iterator[Annotation]:
        return iter(self.annotations)

    def derive(self, protocol: str, annotations, params: Mapping[str, object]) -> "Decoding":
        """同じ捕捉（時間軸）の上の別の解読の結果（積み重ねる解読器が上の層の結果を作るときに使う）。"""
        return Decoding(protocol, tuple(annotations), self.sample_period_s, self.trigger, self.length, dict(params))

    def seconds(self, sample: int) -> float:
        """捕捉の先頭を 0 とする秒。"""
        return sample * self.sample_period_s

    def since_trigger(self, sample: int) -> float:
        """トリガを 0 とする秒。"""
        return (sample - self.trigger) * self.sample_period_s

    def of(self, *kinds: str) -> list[Annotation]:
        return [a for a in self.annotations if a.kind in kinds]

    def errors(self) -> list[Annotation]:
        return [a for a in self.annotations if a.error is not None]

    def between(self, start: int, end: int) -> list[Annotation]:
        """サンプルの区間 [start, end) に重なる注釈（画面の表示区間の取り出し用。二分探索）。"""
        first = bisect_right(self._reach, start)          # これより前の注釈はすべて start までに終わっている
        last = bisect_left(self._starts, end)
        return [a for a in self.annotations[first:last] if a.end > start]

    def annotation_dict(self, a: Annotation) -> dict:
        return {"start": a.start, "end": a.end,
                "start_s": self.seconds(a.start), "end_s": self.seconds(a.end),
                "start_rel_s": self.since_trigger(a.start), "end_rel_s": self.since_trigger(a.end),
                "kind": a.kind, "text": a.text, "value": a.value, "error": a.error,
                "fields": dict(a.fields) if a.fields is not None else None}

    def to_dict(self) -> dict:
        return {"protocol": self.protocol, "params": dict(self.params), "sample_period_s": self.sample_period_s,
                "trigger": self.trigger, "length": self.length,
                "annotations": [self.annotation_dict(a) for a in self.annotations]}

    def to_json(self, **kwargs) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, **kwargs)


def project(tr: Transitions, channels: Sequence[int]) -> tuple["array[int]", "array[int]"]:
    """指定したチャンネルだけの変化点の列 (starts, values)。values のビット k が channels[k]。
    指定外のチャンネルだけが変わった点は落とす。1 回の走査（毎サンプルに展開しない）。"""
    for ch in channels:
        if not 0 <= ch < tr.channels:
            raise ValueError(f"チャンネル {ch} は捕捉に無い（0..{tr.channels - 1}）")
    mask = 0
    for ch in channels:
        mask |= 1 << ch
    starts, values, previous = array("Q"), array("H"), None
    packed: dict[int, int] = {}                       # 取り出した値 → 詰めた値（種類は高々 2^len(channels)）
    add_start, add_value = starts.append, values.append
    for v, t in zip(tr.values, tr.starts):
        m = v & mask
        if m == previous:
            continue
        previous = m
        p = packed.get(m)
        if p is None:
            p = packed[m] = sum((m >> ch & 1) << k for k, ch in enumerate(channels))
        add_start(t)
        add_value(p)
    return starts, values


class ChannelLevels:
    """1 チャンネルの初期値と変化点の表。値の参照は二分探索。"""

    def __init__(self, tr: Transitions, channel: int):
        starts, values = project(tr, [channel])
        self.initial = values[0] if len(values) else 0
        self.edges = starts[1:] if len(starts) and starts[0] == 0 else starts
        self.length = tr.length

    def level(self, sample: int) -> int:
        """サンプル sample の値。"""
        return self.initial ^ (bisect_right(self.edges, sample) & 1)

    def next_edge(self, sample: int, level: int) -> int | None:
        """sample 以降（sample 自身を含む）で値が level になる最初の変化点。無ければ None。"""
        i = bisect_left(self.edges, sample)
        if i < len(self.edges) and (self.initial ^ ((i + 1) & 1)) != level:
            i += 1                                        # 変化点ごとに値が交互に変わる
        return self.edges[i] if i < len(self.edges) else None
