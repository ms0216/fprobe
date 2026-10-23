"""ターゲットが出す値とイベント（U16）: ITM のポート・RTT のチャンネルを型（i8〜u32・f32・固定小数点 qM.N・構造体）で
解釈した値と、fprobe_user.h の名前付きイベント（mark / start / stop / value。名前は ELF の文字列）。入出力なし。

仕様: docs/superpowers/specs/2026-10-18-u10-u16-trace-hooks-user-events-design.md の 3 節。

    dec = UserDecoder([Channel("temp", "itm", 1, ValueType.parse("f32"))], elf_strings(symbols))
    records = dec.feed_itm(itm_packets(swo_bytes)) + dec.feed_rtt(channel, t, data)
    found = pair_intervals(records)                     # start / stop を名前ごとに後入れ先出しで対に
    value_stats(records)・interval_stats(found.intervals, hz)・record_rows(records, found.intervals)

記録の stream "user"（record_rows）: value（a = ポートかチャンネル、b = 整数の値）・mark / start / stop・interval（t_end 付き）。
`timing.measure({"stream": "user", "kind": "start", "body": {"name": "adc"}}, {... "kind": "stop" ...})` がそのまま使える。
"""

from __future__ import annotations

import re
import struct
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

from .itm import DecodeError, Instrumentation, Overflow, Packet, Sync
from .profile.timing import interval_report

__all__ = ["EVENT_PORT", "KINDS", "NAME_TYPES", "SOURCES", "STREAM", "VALUE_PORT", "Channel", "Interval", "Pairing",
           "UserDecoder", "UserRecord", "ValueType", "elf_strings", "interval_stats", "pair_intervals", "record_rows",
           "value_stats"]

STREAM = "user"
EVENT_PORT = 24                        # fprobe_user.h の FPROBE_USER_EVENT_PORT（名前の番地 | 種類）
VALUE_PORT = 25                        # FPROBE_USER_VALUE_PORT（value の値）
KINDS = ("mark", "start", "stop", "value")                          # 語の bit 1..0
NAME_TYPES = {"i": "i32", "u": "u32", "f": "f32", "q": "q16.16"}    # value の名前の先頭（"f:volt"）
SOURCES = ("itm", "rtt")
NAME_LIMIT = 128                       # ELF から読む名前の長さの上限

_SCALARS = {"i8": "<b", "u8": "<B", "i16": "<h", "u16": "<H", "i32": "<i", "u32": "<I", "f32": "<f"}
_FIXED = re.compile(r"q(\d+)\.(\d+)")


@dataclass(frozen=True)
class ValueType:
    """値の型。scalar は struct の書式（固定小数点は符号付きの整数を 2**frac で割る）、struct は欄の列（詰めた little endian）。"""
    name: str
    size: int
    fmt: str | None = None
    frac: int = 0
    fields: tuple[tuple[str, ValueType], ...] = ()

    @classmethod
    def parse(cls, spec: str | Mapping) -> ValueType:
        """"i32"・"f32"・"q16.16" 等、または {"type": "struct", "fields": [[名前, 型], …]}。"""
        if isinstance(spec, Mapping):
            if spec.get("type") != "struct":
                raise ValueError(f"型の辞書は type = struct だけ: {spec!r}")
            fields = tuple((str(n), cls.parse(t)) for n, t in spec.get("fields") or ())
            if not fields or any(f[1].fields for f in fields):
                raise ValueError("struct の fields は 1 つ以上のスカラーの [名前, 型]")
            return cls("struct", sum(f[1].size for f in fields), fields=fields)
        spec = str(spec).lower()
        if spec in _SCALARS:
            return cls(spec, struct.calcsize(_SCALARS[spec]), _SCALARS[spec])
        m = _FIXED.fullmatch(spec)
        if m:
            whole, frac = int(m[1]), int(m[2])
            bits = whole + frac
            if bits not in (8, 16, 32):
                raise ValueError(f"固定小数点 {spec}: 整数部 + 小数部は 8・16・32 ビット")
            return cls(spec, bits // 8, {8: "<b", 16: "<h", 32: "<i"}[bits], frac)
        raise ValueError(f"知らない型 {spec!r}（i8・u8・i16・u16・i32・u32・f32・qM.N・struct）")

    def decode(self, data: bytes) -> int | float | dict:
        if self.fields:
            out, offset = {}, 0
            for name, t in self.fields:
                out[name] = t.decode(data[offset:offset + t.size])
                offset += t.size
            return out
        (raw,) = struct.unpack(self.fmt, data)
        return raw / (1 << self.frac) if self.frac else raw

    def to_spec(self) -> str | dict:
        if self.fields:
            return {"type": "struct", "fields": [[n, t.name] for n, t in self.fields]}
        return self.name


@dataclass(frozen=True)
class Channel:
    """値の出どころ: ITM のポート（index）か RTT の上りのチャンネル（index）と、その型・名前・単位。"""
    name: str
    source: str
    index: int
    type: ValueType
    unit: str | None = None

    @classmethod
    def from_dict(cls, d: Mapping) -> Channel:
        source = d.get("source", "itm")
        if source not in SOURCES:
            raise ValueError(f"source は itm か rtt: {source!r}")
        index = int(d["index"])
        if source == "itm" and not 0 <= index < 32:
            raise ValueError(f"ITM のポートは 0〜31: {index}")
        return cls(str(d.get("name") or f"{source}{index}"), source, index, ValueType.parse(d.get("type", "i32")),
                   d.get("unit"))

    def to_dict(self) -> dict:
        out = {"name": self.name, "source": self.source, "index": self.index, "type": self.type.to_spec()}
        if self.unit:
            out["unit"] = self.unit
        return out


@dataclass(frozen=True)
class UserRecord:
    """解読した 1 つ: kind は value / mark / start / stop。index は ITM のポートか RTT のチャンネル。"""
    t: int
    kind: str
    name: str
    value: int | float | dict | None
    source: str
    index: int
    uncertainty: float | None = None

    def to_dict(self) -> dict:
        out = {"t": self.t, "kind": self.kind, "name": self.name, "source": self.source, "index": self.index}
        if self.value is not None:
            out["value"] = self.value
        if self.uncertainty is not None:
            out["uncertainty"] = self.uncertainty
        return out


def elf_strings(image) -> Callable[[int], str | None]:
    """ELF の像（segments を持つもの: fprobe.symbols.Symbols）から、番地の NUL で終わる文字列を読む関数。"""
    segments = list(getattr(image, "segments", ()) or ())
    cache: dict[int, str | None] = {}

    def read(address: int) -> str | None:
        if address not in cache:
            cache[address] = None
            for seg in segments:
                if seg.address <= address < seg.address + len(seg.data):
                    data = seg.data[address - seg.address:address - seg.address + NAME_LIMIT]
                    end = data.find(b"\x00")
                    if end > 0:
                        cache[address] = data[:end].decode("utf-8", errors="replace")
                    break
        return cache[address]
    return read


class UserDecoder:
    """ITM のパケット・RTT の受信を UserRecord に（状態を持つ: 構造体の半端・value の見出し・RTT の半端）。
    errors: size_mismatch（書き込みの大きさが型と違う）・orphan_value（見出しの無い値）・missing_value（値の来ない
    value）・unknown_name（ELF に無い名前）・lost（あふれ・解読の誤りで捨てた）。"""

    def __init__(self, channels: Iterable[Channel], names: Callable[[int], str | None], *,
                 event_port: int | None = EVENT_PORT, value_port: int = VALUE_PORT,
                 event_rtt_channel: int | None = None):
        self.channels = list(channels)
        self._itm = {c.index: c for c in self.channels if c.source == "itm"}
        self._rtt = {c.index: c for c in self.channels if c.source == "rtt"}
        self.names = names
        self.event_port, self.value_port, self.event_rtt_channel = event_port, value_port, event_rtt_channel
        self.errors: Counter = Counter()
        self._struct: dict[int, tuple[int, bytearray]] = {}            # ITM のポート → (始めの時刻, 貯めたバイト)
        self._pending: tuple[int, str, ValueType] | None = None       # ITM の value の見出し
        self._rtt_buffers: dict[int, bytearray] = {}

    # --- 名前 ---

    def _event(self, word: int) -> tuple[str, str, ValueType | None]:
        """見出しの語 → (種類, 名前, value の型)。"""
        kind = KINDS[word & 3]
        address = word & ~3 & 0xFFFF_FFFF
        text = self.names(address)
        if text is None:
            self.errors["unknown_name"] += 1
            text = f"0x{address:08X}"
        vtype = None
        if kind == "value":
            prefix, sep, rest = text.partition(":")
            if sep and prefix in NAME_TYPES:
                text, vtype = rest, ValueType.parse(NAME_TYPES[prefix])
            else:
                vtype = ValueType.parse("i32")
        return kind, text, vtype

    # --- ITM ---

    def feed_itm(self, packets: Iterable[tuple[int, Packet]]) -> list[UserRecord]:
        out: list[UserRecord] = []
        for t, p in packets:
            if isinstance(p, (Overflow, DecodeError)) or (isinstance(p, Sync) and p.skipped):
                self.errors["lost"] += 1
                self._struct.clear()
                self._pending = None
                continue
            if not isinstance(p, Instrumentation):
                continue
            if self.event_port is not None and p.port == self.event_port:
                if self._pending is not None:
                    self.errors["missing_value"] += 1
                    self._pending = None
                kind, name, vtype = self._event(p.value)
                if kind == "value":
                    self._pending = (t, name, vtype)
                else:
                    out.append(UserRecord(t, kind, name, None, "itm", p.port))
            elif self.event_port is not None and p.port == self.value_port:
                if self._pending is None:
                    self.errors["orphan_value"] += 1
                    continue
                t0, name, vtype = self._pending
                self._pending = None
                out.append(UserRecord(t0, "value", name, vtype.decode(p.data), "itm", self.event_port))
            elif p.port in self._itm:
                out += self._itm_value(self._itm[p.port], t, p)
        return out

    def _itm_value(self, ch: Channel, t: int, p: Instrumentation) -> list[UserRecord]:
        if not ch.type.fields:
            if p.size != ch.type.size:
                self.errors["size_mismatch"] += 1
                return []
            return [UserRecord(t, "value", ch.name, ch.type.decode(p.data), "itm", ch.index)]
        t0, buf = self._struct.get(ch.index) or (t, bytearray())
        buf += p.data
        out = []
        while len(buf) >= ch.type.size:
            out.append(UserRecord(t0, "value", ch.name, ch.type.decode(bytes(buf[:ch.type.size])), "itm", ch.index))
            del buf[:ch.type.size]
            t0 = t
        if buf:
            self._struct[ch.index] = (t0, buf)
        else:
            self._struct.pop(ch.index, None)
        return out

    # --- RTT ---

    def feed_rtt(self, channel: int, t: int, data: bytes, uncertainty: float | None = None) -> list[UserRecord]:
        """RTT の上りのチャンネルの受信（t はその受信の時刻）。値のチャンネルは型の大きさごと、イベントのチャンネルは
        ITM と同じ語（見出し 4 バイト、value は値の 4 バイトが続く）。記録はそれが揃った受信の時刻。"""
        if channel != self.event_rtt_channel and channel not in self._rtt:
            return []
        buf = self._rtt_buffers.setdefault(channel, bytearray())
        buf += data
        out = []
        if channel == self.event_rtt_channel:
            while len(buf) >= 4:
                (word,) = struct.unpack_from("<I", buf)
                need = 8 if word & 3 == KINDS.index("value") else 4
                if len(buf) < need:
                    break
                kind, name, vtype = self._event(word)
                value = vtype.decode(bytes(buf[4:8])) if vtype is not None else None
                out.append(UserRecord(t, kind, name, value, "rtt", channel, uncertainty))
                del buf[:need]
            return out
        ch = self._rtt[channel]
        while len(buf) >= ch.type.size:
            out.append(UserRecord(t, "value", ch.name, ch.type.decode(bytes(buf[:ch.type.size])), "rtt", channel,
                                  uncertainty))
            del buf[:ch.type.size]
        return out


# --- 区間 ---------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Interval:
    name: str
    t0: int
    t1: int
    depth: int                         # 同じ名前の入れ子の深さ（0 = 外側）
    source: str = "itm"

    @property
    def duration(self) -> int:
        return self.t1 - self.t0


@dataclass
class Pairing:
    intervals: list[Interval] = field(default_factory=list)
    unmatched_stops: dict[str, int] = field(default_factory=dict)     # 対の無い stop の数（並びの崩れ）
    open: dict[str, int] = field(default_factory=dict)                # 終わりの無い start の数（窓の終わりで切れた）
    started_before: dict[str, int] = field(default_factory=dict)      # 最初の start より前の stop（窓の始めで切れた）


def pair_intervals(records: Iterable[UserRecord]) -> Pairing:
    """start と stop を名前ごとに後入れ先出しで対にする（入れ子・割り込みの中の区間も正しい）。区間は閉じた順。"""
    stacks: dict[str, list[UserRecord]] = {}
    started: set[str] = set()
    out = Pairing()
    for r in sorted(records, key=lambda r: r.t):
        if r.kind == "start":
            stacks.setdefault(r.name, []).append(r)
            started.add(r.name)
        elif r.kind == "stop":
            stack = stacks.get(r.name)
            if not stack:
                counts = out.unmatched_stops if r.name in started else out.started_before
                counts[r.name] = counts.get(r.name, 0) + 1
                continue
            start = stack.pop()
            out.intervals.append(Interval(r.name, start.t, r.t, len(stack), start.source))
    out.open = {name: len(s) for name, s in stacks.items() if s}
    return out


def interval_stats(intervals: Iterable[Interval], timebase_hz: float | None = None, *, bins: int = 20,
                   longest: int = 10) -> dict[str, dict]:
    """名前ごとの区間の統計（timing.measure と同じ: 最小・最大・平均・σ・分位・分布・長い順の上位と開始の時刻）。"""
    per: dict[str, list[tuple[int, int]]] = {}
    for i in intervals:
        per.setdefault(i.name, []).append((i.t0, i.duration))
    return {name: interval_report(sorted(pairs), timebase_hz, bins=bins, longest=longest).as_dict()
            for name, pairs in per.items()}


def value_stats(records: Iterable[UserRecord]) -> dict[str, dict]:
    """名前ごと（構造体は "名前.欄" ごと）の数・最小・最大・平均・最後の値と時刻。"""
    per: dict[str, dict] = {}

    def add(name: str, value, r: UserRecord) -> None:
        row = per.get(name)
        if row is None:
            row = per[name] = {"count": 0, "min": value, "max": value, "sum": 0, "last": value, "last_t": r.t,
                               "source": r.source, "index": r.index}
        row["count"] += 1
        row["min"], row["max"] = min(row["min"], value), max(row["max"], value)
        row["sum"] += value
        row["last"], row["last_t"] = value, r.t
    for r in sorted(records, key=lambda r: r.t):
        if r.kind != "value":
            continue
        if isinstance(r.value, dict):
            for k, v in r.value.items():
                add(f"{r.name}.{k}", v, r)
        else:
            add(r.name, r.value, r)
    out = {}
    for name, row in per.items():
        total = row.pop("sum")
        out[name] = {"count": row["count"], "min": row["min"], "max": row["max"], "mean": total / row["count"],
                     "last": row["last"], "last_t": row["last_t"], "source": row["source"], "index": row["index"]}
    return out


def record_rows(records: Iterable[UserRecord], intervals: Iterable[Interval] = ()) -> list[tuple]:
    """記録の stream user の行（Recorder.add_many の形: stream, kind, t, a, b, body, t_end, 不確かさ）。"""
    rows = []
    for r in records:
        if r.kind == "value":
            b = r.value if isinstance(r.value, int) and -(1 << 63) <= r.value < 1 << 63 else 0
            rows.append((STREAM, "value", r.t, r.index, b,
                         {"name": r.name, "value": r.value, "source": r.source, "index": r.index}, None, r.uncertainty))
        else:
            rows.append((STREAM, r.kind, r.t, 0, 0, {"name": r.name, "source": r.source}, None, r.uncertainty))
    for i in intervals:
        rows.append((STREAM, "interval", i.t0, 0, 0, {"name": i.name, "duration": i.duration, "depth": i.depth}, i.t1,
                     None))
    return rows
