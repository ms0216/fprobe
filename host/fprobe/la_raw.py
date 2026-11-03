"""ロジアナの生の保存ファイル（.fplaraw）: 取り込み（受け取った拍をそのまま追記）と書き出し（後から VCD へ）を分ける間の層。

受け取りのループ（LaStream.chunks）は RawCaptureWriter.write でバイト列をそのまま書くだけで、復号も VCD の整形もしない。
書き出し（raw_to_vcd）は後から、取り込みと無関係の速さで、今までと同じ部品（la_wave.stream_transitions → la_vcd.VcdWriter）を通す
ので、出力の VCD は取り込みと書き出しを分ける前とバイト単位で同じ。形式と理由は
docs/superpowers/specs/2026-11-03-la-raw-capture-design.md §3。

    file   := MAGIC(8)  META_LEN(u32)  META(JSON)  chunk*  [index]
    chunk  := CHUNK_HEAD(60 バイト)  DATA
    index  := OFFSETS(u64 × n)  N(u64)  INDEX_OFFSET(u64)  TAIL_MAGIC(8)

fprobe.la には依存しない（la が使う）。
"""

from __future__ import annotations

import json
import os
import struct
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from . import la_vcd, la_wave

__all__ = ["CHUNK_HEAD", "RawCapture", "RawCaptureWriter", "RawChunk", "RawFormatError", "RawGap", "RawMeta", "raw_to_vcd"]

VERSION = 1
MAGIC = b"FPLARAW" + bytes([VERSION])
TAIL_MAGIC = b"FPLAREND"
CHUNK_TAG = b"CHNK"
# 4s タグ / B flags / 3x / I nbytes / Q first / q count / I previous / 4x / Q gap_start / Q gap_samples / Q gap_beat
CHUNK_HEAD = "<4sB3xIQqI4xQQQ"
_HEAD = struct.Struct(CHUNK_HEAD)
_TAIL = struct.Struct("<QQ8s")                       # N, INDEX_OFFSET, TAIL_MAGIC
_HAS_PREVIOUS, _HAS_GAP = 1, 2
_BUFFER = 1 << 20


class RawFormatError(ValueError):
    """生の保存ファイルでない・版が違う・ヘッダが壊れている。"""


@dataclass(frozen=True)
class RawMeta:
    """流れの性質（書き出しがファイルだけで完結するための情報）。"""
    rle: bool
    pack: int
    channels: int
    first_channel: int
    sample_period_s: float
    names: dict[int, str]
    skew: tuple[float | None, ...]       # ch ごとの時刻のずれ（分からない ch は None。timebase_hz が無ければ空）
    timebase_hz: float | None

    def to_json(self) -> bytes:
        return json.dumps({"version": VERSION, "rle": self.rle, "pack": self.pack, "channels": self.channels,
                           "first_channel": self.first_channel, "sample_period_s": self.sample_period_s,
                           "names": {str(k): v for k, v in self.names.items()}, "skew": list(self.skew),
                           "timebase_hz": self.timebase_hz}).encode("utf-8")

    @classmethod
    def from_json(cls, blob: bytes) -> "RawMeta":
        try:
            d = json.loads(blob.decode("utf-8"))
            if d["version"] != VERSION:
                raise RawFormatError(f"生の保存ファイルの版 {d['version']} は読めない（この版は {VERSION}）")
            return cls(rle=bool(d["rle"]), pack=int(d["pack"]), channels=int(d["channels"]),
                       first_channel=int(d["first_channel"]), sample_period_s=float(d["sample_period_s"]),
                       names={int(k): v for k, v in d["names"].items()}, skew=tuple(d["skew"]),
                       timebase_hz=d["timebase_hz"])
        except RawFormatError:
            raise
        except (ValueError, KeyError, TypeError) as e:
            raise RawFormatError(f"ヘッダが壊れている: {e}") from e


@dataclass(frozen=True)
class RawGap:
    """欠けの区間（fprobe.la.StreamGap の、書き出しに要る分: 位置・長さ・拍）。"""
    start: int
    samples: int
    beat: int


@dataclass(frozen=True)
class RawChunk:
    """保存した 1 つのかたまり（fprobe.la.StreamChunk のロジアナの拍の部分）。"""
    data: bytes
    rle: bool
    pack: int
    first: int
    previous: int | None
    count: int                   # サンプル数（-1 = 取り込みで数えていない。length で数える）
    gap: RawGap | None

    @property
    def length(self) -> int:
        if self.count >= 0:
            return self.count
        return la_wave._extent(self.data, self.rle)[0] << self.pack if self.data else 0

    def changes(self):
        """変化点（値・開始）。StreamChunk.changes と同じ。"""
        return la_wave.stream_transitions(self.data, self.rle, self.pack, self.first, self.previous)


class RawCaptureWriter:
    """受け取ったかたまりをそのまま追記する。write は復号も整形もしない（受け取りのループから呼ぶ）。
    close（with を抜けたとき）で索引を書く。chunk は StreamChunk のように data・rle・pack・first・previous・count・gap を持つもの。"""

    def __init__(self, path: str | Path, meta: RawMeta):
        self.meta = meta
        self._offsets: list[int] = []
        self._file = open(path, "wb", buffering=_BUFFER)
        try:
            blob = meta.to_json()
            self._file.write(MAGIC + struct.pack("<I", len(blob)) + blob)
            self._end = self._file.tell()
        except BaseException:
            self._file.close()
            raise

    def write(self, chunk) -> None:
        if chunk.rle != self.meta.rle or chunk.pack != self.meta.pack:
            raise ValueError(f"かたまりの形（rle={chunk.rle}, pack={chunk.pack}）がファイルの流れ"
                             f"（rle={self.meta.rle}, pack={self.meta.pack}）と違う")
        gap = chunk.gap
        flags = (_HAS_PREVIOUS if chunk.previous is not None else 0) | (_HAS_GAP if gap is not None else 0)
        data = chunk.data
        head = _HEAD.pack(CHUNK_TAG, flags, len(data), chunk.first, chunk.count,
                          chunk.previous if chunk.previous is not None else 0,
                          gap.start if gap is not None else 0, gap.samples if gap is not None else 0,
                          gap.beat if gap is not None else 0)
        self._offsets.append(self._end)
        self._file.write(head)
        self._file.write(data)                                   # memoryview はコピーしない
        self._end += len(head) + len(data)

    def close(self) -> None:
        if self._file.closed:
            return
        try:
            index = struct.pack(f"<{len(self._offsets)}Q", *self._offsets)
            self._file.write(index + _TAIL.pack(len(self._offsets), self._end, TAIL_MAGIC))
        finally:
            self._file.close()

    def __enter__(self) -> "RawCaptureWriter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class RawCapture:
    """生の保存ファイルの読み。索引があればそれで（complete = True）、無ければ先頭から走査する（途中で落ちたファイル。
    最後のかたまりが途中で切れていれば捨て、complete = False。切れる前までは正しい）。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        with open(self.path, "rb") as f:
            head = f.read(len(MAGIC) + 4)
            if len(head) < len(MAGIC) + 4 or head[:len(MAGIC) - 1] != MAGIC[:-1]:
                raise RawFormatError(f"生の保存ファイルでない: {self.path}")
            if head[len(MAGIC) - 1] != VERSION:
                raise RawFormatError(f"生の保存ファイルの版 {head[len(MAGIC) - 1]} は読めない（この版は {VERSION}）")
            (meta_len,) = struct.unpack("<I", head[len(MAGIC):])
            blob = f.read(meta_len)
            if len(blob) != meta_len:
                raise RawFormatError("ヘッダが途中で切れている")
            self.meta = RawMeta.from_json(blob)
            self._start = len(head) + meta_len
            size = os.fstat(f.fileno()).st_size
            offsets = self._read_index(f, size)
            self.complete = offsets is not None
            self._offsets = offsets if offsets is not None else self._scan(f, size)
        self._samples: int | None = None

    def _read_index(self, f, size: int) -> list[int] | None:
        if size < self._start + _TAIL.size:
            return None
        f.seek(size - _TAIL.size)
        n, index_offset, tail = _TAIL.unpack(f.read(_TAIL.size))
        if tail != TAIL_MAGIC or index_offset + 8 * n + _TAIL.size != size or index_offset < self._start:
            return None
        f.seek(index_offset)
        blob = f.read(8 * n)
        return list(struct.unpack(f"<{n}Q", blob)) if len(blob) == 8 * n else None

    def _scan(self, f, size: int) -> list[int]:
        offsets, pos = [], self._start
        while pos + _HEAD.size <= size:
            f.seek(pos)
            tag, _, nbytes, *_ = _HEAD.unpack(f.read(_HEAD.size))
            if tag != CHUNK_TAG or pos + _HEAD.size + nbytes > size:
                break                                        # 途中で切れた（または索引の始まり）かたまり
            offsets.append(pos)
            pos += _HEAD.size + nbytes
        return offsets

    def __len__(self) -> int:
        return len(self._offsets)

    def chunks(self) -> Iterator[RawChunk]:
        """保存した順にかたまりを返す。"""
        with open(self.path, "rb") as f:
            for offset in self._offsets:
                f.seek(offset)
                tag, flags, nbytes, first, count, previous, g_start, g_samples, g_beat = _HEAD.unpack(f.read(_HEAD.size))
                data = f.read(nbytes)
                if tag != CHUNK_TAG or len(data) != nbytes:
                    raise RawFormatError(f"かたまりが壊れている（位置 {offset}）")
                yield RawChunk(data, self.meta.rle, self.meta.pack, first, previous if flags & _HAS_PREVIOUS else None, count,
                               RawGap(g_start, g_samples, g_beat) if flags & _HAS_GAP else None)

    @property
    def samples(self) -> int:
        """値のあるサンプルの数（欠けの区間は数えない）。"""
        if self._samples is None:
            self._samples = sum(c.length for c in self.chunks())
        return self._samples


def raw_to_vcd(raw: RawCapture, vcd_path: str | Path, names: dict[int, str] | None = None, workers: int | None = None) -> None:
    """生の保存ファイルを VCD に書き出す。取り込みと書き出しを分ける前の stream_to_vcd と同じバイト列。
    names はファイルに保存した名前に重ねる。"""
    m = raw.meta
    merged = {**m.names, **(names or {})}
    per = la_wave.Transitions((), (), 0, 0, 0, m.sample_period_s, m.channels, m.skew)
    with la_vcd.VcdWriter(vcd_path, m.channels, m.sample_period_s, merged,
                          shifts=per.skew_samples(m.sample_period_s * m.timebase_hz if m.timebase_hz else None),
                          unknown=per.skew_unknown(), workers=workers) as vcd:
        for chunk in raw.chunks():
            if chunk.gap is not None:
                vcd.gap(chunk.gap.start)                     # 欠けの区間: 全信号が x。次の値で書き直す
            vcd.write(*chunk.changes())
