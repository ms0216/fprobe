"""ロジックアナライザ: 設定・捕捉・読み出しと VCD 出力。

仕様: docs/superpowers/specs/2026-10-16-m7-logic-analyzer-design.md
      docs/superpowers/specs/2026-10-16-m8c-la-streaming-design.md（連続ストリーミング）
"""

import math
import time
from array import array
from bisect import bisect_left, bisect_right
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import generated as g
from .caps import LaChannel, legacy_la_layout
from .device import Device
from .events import DapEvent, decode_events

__all__ = ["Capture", "DeepAnalyzer", "Front", "channel_layout", "channel_names", "check_trigger_channels",
           "external_channels", "LaStream", "LogicAnalyzer", "Pulse", "SegmentSplitter", "StreamChunk",
           "StreamFormatError", "StreamSegment", "StreamStats", "Transitions", "VcdWriter", "Waveform", "decode_beats",
           "decode_transitions", "split_segments", "write_vcd"]

# 古い並び（ケイパビリティ LA_LAYOUT の前）の内部の ch の名前。今の並びは channel_names（申告から）
DEFAULT_NAMES = {c.channel: c.name for c in legacy_la_layout(10) if c.kind == "internal"}


def channel_layout(caps) -> tuple[LaChannel, ...]:
    """デバイスが申告したロジアナの ch の並び（U22。申告が無い古いデバイスは ch 0〜7 = ピン、8・9 = 内部）。"""
    return tuple(getattr(caps, "la_layout", ()) or legacy_la_layout(getattr(caps, "la_channels", 0)))


def channel_names(caps) -> dict[int, str]:
    """ch → 既定の名前（拡張ヘッダのピン = ch<n>、内部 = probe_drives_swdio・dap_busy、専用の入力 = in<k>）。
    何もつながっていない ch は含めない。"""
    return {c.channel: c.name for c in channel_layout(caps) if c.kind != "none"}


def check_trigger_channels(caps, **masks: int) -> None:
    """トリガの条件（mask・rise・fall 等のビット）が、デバイスが申告した ch（la_layout で何かがつながっている ch）だけを
    指すか。何もつながっていない ch（常に 0）を指していたら ValueError（専用の入力が要るなら構成 fprobe_la）。
    申告（la_layout）を持たない呼び出し元（試験の偽物など）は調べない。"""
    if not getattr(caps, "la_layout", None):
        return
    present = sum(1 << c.channel for c in caps.la_layout if c.kind != "none")
    for name, bits in masks.items():
        absent = bits & ~present
        if absent:
            channels = [ch for ch in range(absent.bit_length()) if absent >> ch & 1]
            raise ValueError(f"{name} の ch {channels} はこの構成（{getattr(caps, 'design', '?')}）に無い。"
                             f"専用の入力（ch 10〜15）は構成 {g.DESIGN_FOR_LA_INPUTS}（probe_design_switch）にある")


def external_channels(caps) -> list[int]:
    """外の信号が入る ch（拡張ヘッダのピンと専用の入力）。グリッチフィルタをかけられる ch。"""
    return [c.channel for c in channel_layout(caps) if c.kind in ("pin", "input")]


@dataclass(frozen=True)
class Capture:
    samples: Sequence[int]       # 時間順（深いバッファでは array）
    trigger: int                 # トリガのサンプルの位置
    trigger_time: int            # トリガ時のタイムベース（カウント）
    sample_period_s: float
    channels: int

    def channel(self, index: int) -> list[int]:
        return [s >> index & 1 for s in self.samples]


@dataclass(frozen=True)
class Transitions:
    """変化点の表現（長い記録を毎サンプルに展開しない）。starts[i] から次の starts まで値 values[i] が続く。"""
    values: Sequence[int]
    starts: Sequence[int]        # サンプル番号（昇順、starts[0] = 0）
    length: int                  # 全サンプル数
    trigger: int                 # トリガのサンプル番号
    trigger_time: int
    sample_period_s: float
    channels: int

    @classmethod
    def from_capture(cls, cap: "Capture") -> "Transitions":
        values, starts, previous = array("H"), array("Q"), None
        for i, v in enumerate(cap.samples):
            if v != previous:
                values.append(v)
                starts.append(i)
                previous = v
        return cls(values, starts, len(cap.samples), cap.trigger, cap.trigger_time, cap.sample_period_s,
                   cap.channels)

    def edges(self, channel: int) -> list[int]:
        """チャンネルが変化したサンプル番号。"""
        out, previous = [], None
        for v, t in zip(self.values, self.starts):
            bit = v >> channel & 1
            if previous is not None and bit != previous:
                out.append(t)
            previous = bit
        return out

    def level_at(self, channel: int, index: int) -> int:
        return self.values[bisect_right(self.starts, index) - 1] >> channel & 1


class Waveform:
    """画面に返す縮めた波形: 見ている区間を width 画素に分け、画素ごと・チャンネルごとに '0'（ずっと 0）/ '1'（ずっと 1）/
    'x'（区間内で変化）。チャンネルごとの変化点の表を一度だけ作り、表示のたびには二分探索で引く（全サンプルを送らない）。"""

    def __init__(self, tr: Transitions):
        self.tr = tr
        first = tr.values[0] if len(tr.values) else 0
        self._initial = [first >> ch & 1 for ch in range(tr.channels)]
        self._edges = [array("Q") for _ in range(tr.channels)]          # チャンネルが変わったサンプル番号
        # ponytail: 変化点ごとの Python のループ（100 万変化点で 1 秒ほど、捕捉ごとに 1 回）。遅ければ numpy に
        previous = first
        for v, t in zip(tr.values, tr.starts):
            changed = v ^ previous
            while changed:
                bit = changed & -changed
                self._edges[bit.bit_length() - 1].append(t)
                changed ^= bit
            previous = v

    @property
    def channels(self) -> int:
        return len(self._edges)

    def level_at(self, ch: int, s: int) -> int:
        """チャンネル ch のサンプル s の値（s 自身の変化を含む）。"""
        return self._initial[ch] ^ (bisect_right(self._edges[ch], s) & 1)

    def edges_between(self, ch: int, s0: int, s1: int) -> array:
        """チャンネル ch の s0 < t < s1 の変化点（サンプル番号の昇順。共通タイムラインのタイル用）。"""
        edges = self._edges[ch]
        return edges[bisect_right(edges, s0):bisect_left(edges, s1)]

    def count_between(self, ch: int, s0: int, s1: int) -> int:
        """edges_between の数（取り出さずに数える）。"""
        edges = self._edges[ch]
        return max(0, bisect_left(edges, s1) - bisect_right(edges, s0))

    def view(self, start: int, end: int, width: int) -> list[str]:
        """サンプル [start, end) を width 画素に縮めた、チャンネルごとの文字列。区間は捕捉の中に切り詰める。"""
        start, end = max(0, int(start)), min(self.tr.length, int(end))
        if end <= start or width < 1:
            raise ValueError(f"表示の区間が空（start={start}, end={end}, width={width}）")
        span = end - start
        bounds = []
        for i in range(width):                                          # 整数で割る（浮動小数の誤差で区間が延びない）
            s = start + i * span // width
            bounds.append((s, max(s + 1, start - (-(i + 1) * span // width))))
        out = []
        for initial, edges in zip(self._initial, self._edges):
            chars = []
            for s, e in bounds:
                before = bisect_right(edges, s)                         # s までの変化の数（s 自身の変化を含む）
                if before < bisect_left(edges, e):
                    chars.append("x")
                else:
                    chars.append("1" if initial ^ (before & 1) else "0")
            out.append("".join(chars))
        return out


# --- 前段の追加の条件（U21）: グリッチフィルタとパルス幅のトリガ -------------------------------------------

_FILTER_MAX_CYCLES = 255
_PULSE_UNBOUNDED = 0xFFFFFFFF
_PULSE_MODE_WIDTH, _PULSE_MODE_TIMEOUT = 1, 2


@dataclass(frozen=True)
class Pulse:
    """パルス幅のトリガ。channel の level（1 = High の脈）の脈について:
    timeout = False: 脈が終わったとき、幅が min_ns 以上 max_ns 以下なら成立（「より長い」は max_ns = None、
    「より短い」は min_ns = 0）。始まりを見ていない脈（仕掛けた時点で既に level）は数えない。
    timeout = True: level が min_ns を超えて続いたら、その時点で（脈の途中で）成立（一定時間変化しない）。
    値一致・エッジの条件（mask・rise・fall）と両方満たしたときにトリガ。幅は sys の周期で数える。"""
    channel: int
    level: int = 1
    min_ns: float = 0.0
    max_ns: float | None = None
    timeout: bool = False

    @classmethod
    def of(cls, value: "Pulse | dict | None") -> "Pulse | None":
        return cls(**value) if isinstance(value, dict) else value


@dataclass(frozen=True)
class Front:
    """前段の追加の条件のレジスタ（la ブロックの filter・pulse・pulse_min・pulse_max）と、その説明。"""
    words: tuple[int, int, int, int]
    info: dict

    @classmethod
    def of(cls, caps, filter_channels: int = 0, filter_ns: float = 0.0, pulse: "Pulse | dict | None" = None) -> "Front | None":
        """ケイパビリティに合わせて作る。機能が無い構成で使おうとしたら RuntimeError、機能が無く使わないなら None
        （古い構成ではレジスタが無いので書かない）。"""
        pulse = Pulse.of(pulse)
        want_filter = bool(filter_channels) and filter_ns > 0
        if want_filter and not getattr(caps, "la_filter", False):
            raise RuntimeError("この構成にロジアナのグリッチフィルタが無い（ケイパビリティ la_filter）")
        if pulse is not None and not getattr(caps, "la_pulse", False):
            raise RuntimeError("この構成にパルス幅のトリガが無い（ケイパビリティ la_pulse）")
        if not (getattr(caps, "la_filter", False) or getattr(caps, "la_pulse", False)):
            return None
        clock = caps.sys_clock_hz
        info: dict = {}
        cycles = 0
        if want_filter:
            allowed = external_channels(caps)
            if filter_channels & ~sum(1 << c for c in allowed):
                raise ValueError(f"フィルタをかけられるのは外の信号の ch {', '.join(map(str, allowed))}"
                                 f"（拡張ヘッダのピンと専用の入力。専用の入力は構成 {g.DESIGN_FOR_LA_INPUTS}）")
            cycles = max(2, math.ceil(filter_ns * clock / 1e9 - 1e-9))
            if cycles > _FILTER_MAX_CYCLES:
                raise ValueError(f"filter_ns は {_FILTER_MAX_CYCLES / clock * 1e9:.0f} ns まで")
            info["filter"] = {"channels": [c for c in allowed if filter_channels >> c & 1],
                              "min_width_ns": cycles / clock * 1e9,
                              "delay_ns": (cycles - 1) / clock * 1e9}   # フィルタした ch の変化の遅れ
        filter_word = (filter_channels if want_filter else 0) << g.LA_FILTER_CHANNELS[0] | cycles << g.LA_FILTER_CYCLES[0]
        pulse_words = (0 << g.LA_PULSE_MODE[0] | 1 << g.LA_PULSE_LEVEL[0], 0, _PULSE_UNBOUNDED)
        if pulse is not None:
            if not 0 <= pulse.channel < caps.la_channels:
                raise ValueError(f"pulse の channel は 0〜{caps.la_channels - 1}")
            if pulse.level not in (0, 1):
                raise ValueError("pulse の level は 0 か 1")
            low = max(0, round(pulse.min_ns * clock / 1e9))
            high = _PULSE_UNBOUNDED if pulse.max_ns is None else round(pulse.max_ns * clock / 1e9)
            if pulse.timeout and low < 1:
                raise ValueError("timeout は min_ns が 1 周期以上")
            if not pulse.timeout and (high < low or high > _PULSE_UNBOUNDED or low > _PULSE_UNBOUNDED):
                raise ValueError("pulse は 0 <= min_ns <= max_ns（2^32 周期まで）")
            mode = _PULSE_MODE_TIMEOUT if pulse.timeout else _PULSE_MODE_WIDTH
            pulse_words = (mode << g.LA_PULSE_MODE[0] | pulse.level << g.LA_PULSE_LEVEL[0]
                           | pulse.channel << g.LA_PULSE_CHANNEL[0], low, high)
            info["pulse"] = {"channel": pulse.channel, "level": pulse.level, "timeout": pulse.timeout,
                             "min_ns": low / clock * 1e9,
                             "max_ns": None if high == _PULSE_UNBOUNDED else high / clock * 1e9}
        return cls((filter_word, *pulse_words), info)

    def write(self, dev: Device) -> None:
        dev.write(g.LA_FILTER, list(self.words))


class LogicAnalyzer:
    """ロジアナ（DEPTH サンプルの窓、プリトリガ指定）。内蔵バッファが無いボード（ケイパビリティの LA_DEPTH = 0、
    外部メモリあり）では、深いバッファを RAW で使って同じ結果（8192 サンプルの窓）を返す。"""

    WINDOW = 8192                                   # 深いバッファを使うときの窓（サンプル）

    def __init__(self, dev: Device):
        caps = dev.capabilities()
        if not caps.la_channels:
            raise RuntimeError("このデバイスにはロジックアナライザが無い")
        self.dev, self.caps = dev, caps
        self.channels, self.depth, self.clock_hz = caps.la_channels, caps.la_depth, caps.la_sample_hz
        self.front_info: dict = {}                      # 最後の start のグリッチフィルタ・パルス幅（Front.info）
        self._deep = None
        if not self.depth:
            if not caps.mem_bytes:
                raise RuntimeError("ロジアナのバッファが無い")
            self._deep = DeepAnalyzer(dev)
            self.depth = self.WINDOW
        self._pretrigger = 0

    def start(self, *, rate_hz: float | None = None, pretrigger: int = 0, mask: int = 0, value: int = 0,
              rise: int = 0, fall: int = 0, filter_channels: int = 0, filter_ns: float = 0.0,
              pulse: "Pulse | dict | None" = None) -> float:
        """捕捉を始める。実際のサンプル周期（秒）を返す。filter_channels（外の信号の ch のビット）は filter_ns より短い変化を
        捨てる（グリッチフィルタ）。pulse はパルス幅のトリガ（Pulse）。どちらもケイパビリティに無ければ RuntimeError。"""
        if not 0 <= pretrigger < self.depth:
            raise ValueError(f"pretrigger は 0〜{self.depth - 1}")
        check_trigger_channels(self.caps, mask=mask, rise=rise, fall=fall)
        front = Front.of(self.caps, filter_channels, filter_ns, pulse)
        self.front_info = front.info if front is not None else {}
        self._pretrigger = pretrigger
        if self._deep is not None:
            post_beats = -(-(self.depth - pretrigger) // 8)
            return self._deep.start(rate_hz=rate_hz, mask=mask, value=value, rise=rise, fall=fall, rle=False,
                                    post_beats=post_beats, pretrigger=pretrigger, filter_channels=filter_channels,
                                    filter_ns=filter_ns, pulse=pulse)
        divider = 1 if rate_hz is None else max(1, min(1 << 16, round(self.clock_hz / rate_hz)))
        dev = self.dev
        dev.write32(g.LA_CONTROL, 0)
        dev.write(g.LA_RATE, [divider - 1, pretrigger, mask, value, rise, fall])
        if front is not None:
            front.write(dev)
        dev.write32(g.LA_CONTROL, 1)
        return divider / self.clock_hz

    def wait(self, timeout_s: float = 5.0) -> None:
        if self._deep is not None:
            self._deep.wait(timeout_s)
            return
        deadline = time.monotonic() + timeout_s
        while not self.dev.read32(g.LA_STATUS) >> g.LA_STATUS_DONE[0] & 1:
            if time.monotonic() > deadline:
                self.dev.write32(g.LA_CONTROL, 0)
                raise TimeoutError("トリガが成立しない")
            time.sleep(0.001)

    def read(self, sample_period_s: float) -> Capture:
        if self._deep is not None:
            cap = self._deep.read(before_beats=-(-self._pretrigger // 8),
                                  after_beats=-(-(self.depth - self._pretrigger) // 8))
            first = max(0, cap.trigger - self._pretrigger)
            samples = cap.samples[first:first + self.depth]
            self.dev.write32(g.LA_DEEP_CONTROL, 0)
            return Capture(samples, cap.trigger - first, cap.trigger_time, cap.sample_period_s, self.channels)
        start, trigger, time_lo, time_hi = self.dev.read(g.LA_START_INDEX, 4)
        raw = self.dev.read(g.LA_MEM_BASE, self.depth)
        samples = raw[start:] + raw[:start]
        self.dev.write32(g.LA_CONTROL, 0)
        return Capture(samples, (trigger - start) % self.depth, time_hi << 32 | time_lo, sample_period_s,
                       self.channels)

    def capture(self, *, timeout_s: float = 5.0, **trigger) -> Capture:
        period = self.start(**trigger)
        self.wait(timeout_s)
        return self.read(period)


def write_vcd(capture: "Capture | Transitions", path: str | Path, names: dict[int, str] | None = None) -> None:
    """VCD（PulseView 等で表示）。時刻 0 = 最初のサンプル、単位は ns。トリガ位置にマーカー用の信号を置く。"""
    if isinstance(capture, Transitions):
        _write_vcd_transitions(capture, path, names)
        return
    ids, trigger_id, lines = _vcd_header(capture.channels, names, trigger=True)
    previous = None
    for i, sample in enumerate(capture.samples):
        changes = [f"{sample >> ch & 1}{ids[ch]}" for ch in range(capture.channels)
                   if previous is None or (sample ^ previous) >> ch & 1]
        if i in (0, capture.trigger, capture.trigger + 1):
            changes.append(f"{int(i == capture.trigger)}{trigger_id}")
        if changes:
            lines.append(f"#{round(i * capture.sample_period_s * 1e9)}")
            lines += changes
        previous = sample
    Path(path).write_text("\n".join(lines) + "\n", encoding="ascii")


def _vcd_header(channels: int, names: dict[int, str] | None, trigger: bool) -> tuple[dict[int, str], str, list[str]]:
    """チャンネルの識別子・トリガの識別子・ヘッダの行。"""
    names = {**DEFAULT_NAMES, **(names or {})}
    ids = {ch: chr(33 + ch) for ch in range(channels)}
    trigger_id = chr(33 + channels)
    lines = ["$timescale 1ns $end", "$scope module fprobe $end"]
    lines += [f"$var wire 1 {ids[ch]} {names.get(ch, f'ch{ch}')} $end" for ch in range(channels)]
    if trigger:
        lines.append(f"$var wire 1 {trigger_id} trigger $end")
    lines += ["$upscope $end", "$enddefinitions $end"]
    return ids, trigger_id, lines


def _write_vcd_transitions(tr: "Transitions", path: str | Path, names: dict[int, str] | None) -> None:
    ids, trigger_id, lines = _vcd_header(tr.channels, names, trigger=True)
    events: dict[int, list[str]] = {}
    previous = None
    for v, t in zip(tr.values, tr.starts):
        changes = [f"{v >> ch & 1}{ids[ch]}" for ch in range(tr.channels)
                   if previous is None or (v ^ previous) >> ch & 1]
        if changes:
            events.setdefault(t, []).extend(changes)
        previous = v
    events.setdefault(0, []).append(f"0{trigger_id}")
    events.setdefault(tr.trigger, []).append(f"1{trigger_id}")
    events.setdefault(tr.trigger + 1, []).append(f"0{trigger_id}")
    for t in sorted(events):
        lines.append(f"#{round(t * tr.sample_period_s * 1e9)}")
        lines += events[t]
    Path(path).write_text("\n".join(lines) + "\n", encoding="ascii")


class VcdWriter:
    """VCD を変化点の順に少しずつ書く（ストリーミング用。全体をメモリに持たない）。トリガの信号は無い。"""

    def __init__(self, path: str | Path, channels: int, sample_period_s: float, names: dict[int, str] | None = None):
        self._ids, _, header = _vcd_header(channels, names, trigger=False)
        self._period = sample_period_s
        self._previous: int | None = None
        self._file = open(path, "w", encoding="ascii", newline="\n")
        self._file.write("\n".join(header) + "\n")

    def write(self, values: Sequence[int], starts: Sequence[int]) -> None:
        """変化点（値, 通し番号の開始）を書き足す。starts は前回の続きから昇順。"""
        lines, previous = [], self._previous
        for v, t in zip(values, starts):
            changes = [f"{v >> ch & 1}{i}" for ch, i in self._ids.items()
                       if previous is None or (v ^ previous) >> ch & 1]
            if changes:
                lines.append(f"#{round(t * self._period * 1e9)}")
                lines += changes
            previous = v
        self._previous = previous
        if lines:
            self._file.write("\n".join(lines) + "\n")

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> "VcdWriter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# --- 深いバッファ（外部メモリ）の拍の復号（gateware/rtl/la/la_encoder.vhd の符号化） ---------------------

BEAT_BYTES = 16


def decode_beats(data: bytes, rle: bool) -> "array[int]":
    """拍の列（16 バイト単位）をサンプル列に戻す。RAW: 1 拍 = 8 サンプル。RLE: 1 拍 = 4 レコード（値, 繰り返し）。"""
    if len(data) % BEAT_BYTES:
        raise ValueError("拍（16 バイト）の倍数でない")
    if not rle:
        samples = array("H")
        samples.frombytes(data)                     # リトルエンディアンの 16 ビット（このホストもリトル）
        return samples
    records = array("I")
    records.frombytes(data)
    samples = array("H")
    for record in records:
        samples.extend(array("H", [record & 0xFFFF]) * ((record >> 16) + 1))
    return samples


def decode_transitions(data: bytes, rle: bool) -> tuple["array[int]", "array[int]", int]:
    """拍の列を変化点（値, 開始サンプル）と全サンプル数にする（RLE は展開しない）。"""
    values, starts, previous = array("H"), array("Q"), None
    if not rle:
        samples = decode_beats(data, False)
        for i, v in enumerate(samples):
            if v != previous:
                values.append(v)
                starts.append(i)
                previous = v
        return values, starts, len(samples)
    records = array("I")
    records.frombytes(data)
    position = 0
    for record in records:
        v = record & 0xFFFF
        if v != previous:
            values.append(v)
            starts.append(position)
            previous = v
        position += (record >> 16) + 1
    return values, starts, position


def trigger_sample(data: bytes, rle: bool, beat: int, slot: int) -> int:
    """拍 beat の slot 番目（トリガ）が、data の先頭から何サンプル目か。"""
    if not rle:
        return beat * 8 + slot
    records = array("I")
    records.frombytes(data[:beat * BEAT_BYTES + slot * 4])
    return sum((r >> 16) + 1 for r in records)


# --- 見出し付きのストリーム（R1: ロジアナとイベントを 1 本に流す構成。
#     docs/superpowers/specs/2026-10-19-r1-unified-stream-design.md §3） ------------------------------------

class StreamFormatError(RuntimeError):
    """ストリームの見出しが壊れている・通し番号が飛んだ（同期を失った。それまでの分は正しい）。"""


@dataclass(frozen=True)
class StreamSegment:
    """見出し付きのストリームの 1 かたまり。"""
    kind: int                    # g.STREAM_SEG_LA（ロジアナの拍）/ g.STREAM_SEG_EVENTS（イベント 1 件 = 1 拍）
    seq: int                     # ロジアナ: 最初の拍の、ストリームの先頭からの番号（32 ビットで折り返す）。
                                 # イベント: 最初のイベントのリングの位置（events.written で数えた番号 mod capacity）
    data: bytes                  # 中身（拍の整数倍）


class SegmentSplitter:
    """見出し付きのストリームをかたまりに分ける。届く量はかたまり・拍の境に揃っていなくてよい（続きは次の feed で）。"""

    def __init__(self):
        self._buf = bytearray()

    @property
    def pending(self) -> int:
        """まだかたまりにならないバイト数。"""
        return len(self._buf)

    def feed(self, data: bytes) -> list[StreamSegment]:
        self._buf += data
        out, pos = [], 0
        while len(self._buf) - pos >= BEAT_BYTES:
            head = int.from_bytes(self._buf[pos:pos + BEAT_BYTES], "little")
            kind, count = head & 0xFFFF, head >> 16 & 0xFFFF
            if (head >> 64 != g.STREAM_SEG_MAGIC or kind not in (g.STREAM_SEG_LA, g.STREAM_SEG_EVENTS)
                    or not 1 <= count <= g.STREAM_SEG_MAX_BEATS):
                raise StreamFormatError(f"ストリームの見出しが壊れている: {head:032x}")
            end = pos + BEAT_BYTES * (1 + count)
            if len(self._buf) < end:
                break
            out.append(StreamSegment(kind, head >> 32 & 0xFFFFFFFF, bytes(self._buf[pos + BEAT_BYTES:end])))
            pos = end
        del self._buf[:pos]
        return out


def split_segments(data: bytes) -> list[StreamSegment]:
    """見出し付きのストリーム全体をかたまりに分ける（途中で切れていれば誤り）。"""
    splitter = SegmentSplitter()
    out = splitter.feed(data)
    if splitter.pending:
        raise StreamFormatError(f"かたまりの途中で終わっている（{splitter.pending} バイト）")
    return out


class DeepAnalyzer:
    """深いバッファ（外部メモリ）のロジアナ。前段の設定（rate・トリガ）は LogicAnalyzer と同じ。

    読み出しは USB の拡張ストリーム。RAW は 1 拍 = 8 サンプル、RLE（変化点）は 1 拍 = 4 レコード。
    """

    def __init__(self, dev: Device):
        caps = dev.capabilities()
        if not caps.mem_bytes or not caps.la_channels:
            raise RuntimeError("このデバイスには深いバッファが無い")
        self.dev, self.caps = dev, caps
        self.front_info: dict = {}
        self.beats = dev.read32(g.LA_DEEP_RING_BEATS)               # ロジアナのリング（残りはイベント用）
        self.channels, self.clock_hz = caps.la_channels, caps.la_sample_hz
        self._rle = False
        self._period = 0.0

    def start(self, *, rate_hz: float | None = None, mask: int = 0, value: int = 0, rise: int = 0, fall: int = 0,
              rle: bool = False, post_beats: int | None = None, pretrigger: int = 0, filter_channels: int = 0,
              filter_ns: float = 0.0, pulse: "Pulse | dict | None" = None) -> float:
        """捕捉を始める（トリガ後 post_beats 拍。既定は容量の 1/2。開始から pretrigger サンプルまではトリガを受け付けない）。
        filter_channels・filter_ns・pulse は LogicAnalyzer.start と同じ。実際のサンプル周期（秒）を返す。"""
        post = self.beats // 2 if post_beats is None else post_beats
        if not 0 <= post < self.beats:
            raise ValueError(f"post_beats は 0〜{self.beats - 1}")
        self._configure(rate_hz, mask, value, rise, fall, pretrigger, filter_channels, filter_ns, pulse)
        self.dev.write32(g.LA_DEEP_POST_BEATS, post)
        self.dev.write32(g.LA_DEEP_CONTROL, 1 | int(rle) << g.LA_DEEP_CONTROL_RLE[0])
        self._rle = rle
        return self._period

    def _configure(self, rate_hz: float | None, mask: int = 0, value: int = 0, rise: int = 0, fall: int = 0,
                   pretrigger: int = 0, filter_channels: int = 0, filter_ns: float = 0.0,
                   pulse: "Pulse | dict | None" = None) -> None:
        """止めて前段（la ブロック）を設定する。pretrigger: 開始からこのサンプル数まではトリガを受け付けない。
        グリッチフィルタ・パルス幅は指定が無ければ切る（前の捕捉の設定を持ち越さない）。"""
        check_trigger_channels(self.caps, mask=mask, rise=rise, fall=fall)
        front = Front.of(self.caps, filter_channels, filter_ns, pulse)
        divider = 1 if rate_hz is None else max(1, min(1 << 16, round(self.clock_hz / rate_hz)))
        dev = self.dev
        dev.write32(g.LA_DEEP_CONTROL, 0)
        dev.write32(g.LA_CONTROL, 0)
        dev.write(g.LA_RATE, [divider - 1, pretrigger, mask, value, rise, fall])
        if front is not None:
            front.write(dev)
        self.front_info = front.info if front is not None else {}
        self._period = divider / self.clock_hz

    def stream(self, *, rate_hz: float | None = None, rle: bool = False, filter_channels: int = 0,
               filter_ns: float = 0.0, events_from: int | None = None) -> "LaStream":
        """連続ストリーミングを始める（トリガは使わない）。受け取りは返した LaStream の chunks で行う。
        イベントも流す構成（ケイパビリティ la_stream_events。R1）では、イベント記録の events_from 件目（events.written で
        数える。既定: 今の総数 = これから後の出来事）からのイベントも同じ流れで届く（StreamChunk.events）。"""
        events = None
        if getattr(self.caps, "la_stream_events", False):
            capacity = self.dev.read32(g.EVENTS_CAPACITY)
            start = self.dev.read32(g.EVENTS_WRITTEN) if events_from is None else events_from
            events = (start, capacity)
        elif events_from is not None:
            raise RuntimeError("この構成はストリームにイベントを流せない（ケイパビリティ la_stream_events が無い）")
        self._configure(rate_hz, filter_channels=filter_channels, filter_ns=filter_ns)
        if events is not None:
            self.dev.write32(g.LA_DEEP_POST_BEATS, events[0] % events[1])   # このストリームでは最初のイベントの位置
        self.dev.write32(g.LA_DEEP_CONTROL, 1 | int(rle) << g.LA_DEEP_CONTROL_RLE[0]
                         | 1 << g.LA_DEEP_CONTROL_STREAM[0])
        return LaStream(self.dev, rle, self._period, self.channels, events=events)

    def stream_to_vcd(self, path: str | Path, duration_s: float, *, rate_hz: float | None = None, rle: bool = False,
                      names: dict[int, str] | None = None) -> "StreamStats":
        """duration_s 秒ストリーミングして VCD に少しずつ書く（記録の長さはメモリに依らない）。"""
        stream = self.stream(rate_hz=rate_hz, rle=rle)
        names = {**channel_names(self.caps), **(names or {})}
        with VcdWriter(path, self.channels, stream.sample_period_s, names) as vcd:
            for chunk in stream.chunks(duration_s):
                vcd.write(*chunk.transitions())
        return stream.stats()

    def status(self) -> int:
        return self.dev.read32(g.LA_DEEP_STATUS)

    def wait(self, timeout_s: float = 10.0) -> None:
        deadline = time.monotonic() + timeout_s
        while not self.status() >> g.LA_DEEP_STATUS_DONE[0] & 1:
            if time.monotonic() > deadline:
                self.dev.write32(g.LA_DEEP_CONTROL, 0)
                raise TimeoutError("トリガが成立しない")
            time.sleep(0.001)

    def read(self, before_beats: int | None = None, after_beats: int | None = None) -> Capture:
        """トリガの前 before_beats 拍（既定: 残っている全部）から後 after_beats 拍（既定: 全部）までを読み、
        毎サンプルに展開する（RLE の長い記録は read_transitions を使う）。"""
        data, trigger, trigger_time = self._read_raw(before_beats, after_beats)
        return Capture(decode_beats(data, self._rle), trigger, trigger_time, self._period, self.channels)

    def read_transitions(self, before_beats: int | None = None, after_beats: int | None = None) -> Transitions:
        """read と同じ範囲を、毎サンプルに展開せず変化点として読む。"""
        data, trigger, trigger_time = self._read_raw(before_beats, after_beats)
        values, starts, length = decode_transitions(data, self._rle)
        return Transitions(values, starts, length, trigger, trigger_time, self._period, self.channels)

    def _read_raw(self, before_beats: int | None, after_beats: int | None) -> tuple[bytes, int, int]:
        status = self.status()
        if status >> g.LA_DEEP_STATUS_OVERRUN[0] & 1:
            raise RuntimeError("書き込みが追いつかず拍を捨てた（OVERRUN）。サンプルレートを下げるか RAW にする")
        trig_beat, written, time_lo, time_hi = self.dev.read(g.LA_DEEP_TRIGGER_BEAT, 4)
        post = self.dev.read32(g.LA_DEEP_POST_BEATS)
        slot = status >> g.LA_DEEP_STATUS_TRIGGER_SLOT[0] & 7
        available = written - post - 1                                    # トリガの前に残っている拍
        before = available if before_beats is None else min(before_beats, available)
        first = (trig_beat - before) % self.beats
        count = before + 1 + (post if after_beats is None else min(after_beats, post))
        self.dev.write32(g.LA_DEEP_READ_REGION, 0)
        self.dev.write(g.LA_DEEP_READ_FIRST, [first, count])
        self.dev.write32(g.LA_DEEP_COMMAND, 1 << g.LA_DEEP_COMMAND_READ[0])
        data = self.dev.read_stream(count * BEAT_BYTES, timeout_ms=5000)
        return data, trigger_sample(data, self._rle, before, slot), time_hi << 32 | time_lo

    def capture(self, *, timeout_s: float = 10.0, before_beats: int | None = None, after_beats: int | None = None,
                **trigger) -> Capture:
        self.start(**trigger)
        self.wait(timeout_s)
        return self.read(before_beats, after_beats)


# --- 連続ストリーミング（仕様: docs/superpowers/specs/2026-10-16-m8c-la-streaming-design.md） ---------------

@dataclass(frozen=True)
class StreamChunk:
    """ストリームの 1 回の受け取り（拍の整数倍）。復号は必要になったときに行う（受け取りを遅らせない）。"""
    data: bytes
    rle: bool
    first: int                   # 最初のサンプルの通し番号（ストリームの先頭 = 0）
    previous: int | None         # 直前のサンプルの値（最初のチャンクは None）
    events: tuple[DapEvent, ...] = ()   # 同じ受け取りに載っていたイベント（イベントも流す構成。古い順）

    def samples(self) -> "array[int]":
        return decode_beats(self.data, self.rle)

    @property
    def length(self) -> int:
        """このチャンクのサンプル数（次のチャンクの first = first + length）。展開しない。"""
        return _extent(self.data, self.rle)[0] if self.data else 0

    def transitions(self) -> tuple["array[int]", "array[int]"]:
        """変化点（値, 通し番号の開始）。直前のチャンクの最後と同じ値は含めない（つなぐと全体の変化点になる）。"""
        values, starts, _ = decode_transitions(self.data, self.rle)
        skip = 1 if len(values) and values[0] == self.previous else 0
        return values[skip:], array("Q", (s + self.first for s in starts[skip:]))


def _extent(data: bytes, rle: bool) -> tuple[int, int]:
    """拍の列のサンプル数と最後の値（展開しない）。"""
    words = array("H")
    words.frombytes(data)
    if not rle:
        return len(words), words[-1]
    return sum(words[1::2]) + len(words) // 2, words[-2]          # レコード = 値（下位）, 繰り返し − 1（上位）


@dataclass(frozen=True)
class StreamStats:
    samples: int
    bytes: int
    elapsed_s: float             # 始めてから出し切るまで
    sample_period_s: float
    overrun: bool                # ホストが遅れて書き込みが止まった（それまでの分は正しい）

    @property
    def duration_s(self) -> float:
        """記録の長さ。"""
        return self.samples * self.sample_period_s

    @property
    def mb_per_s(self) -> float:
        return self.bytes / self.elapsed_s / 1e6 if self.elapsed_s else 0.0


class LaStream:
    """連続ストリーミングの受け取り（DeepAnalyzer.stream が始める）。

    デバイスは外部メモリを FIFO にして拍（リングと同じ形式）を流し続け、書いた拍の総数を stream_beats に示す。
    USB にはそれと受け取った量の差（必ず届く量）だけを求める（届かない量を待って時間切れで捨てることがない）。
    止める（run = 0）とデバイスは書き込みを止めて未読を出し切り、status.reading を 0 に戻す。

    events = (最初のイベントの番号, イベントのリングの件数) なら、イベントも流す構成（R1）: 流れは見出し付きの
    かたまりの列で、stream_beats は出すと決めた拍の総数（見出し・イベントを含む）。かたまりを分け、ロジアナの拍は
    今と同じに、イベントは StreamChunk.events に渡す。通し番号が飛べば StreamFormatError（黙って欠けない）。
    """

    READ_MAX = 4 << 20           # 1 回の読みの上限（バイト）
    READ_MIN = 64 << 10          # 走っている間はこれだけ溜まるか
    READ_INTERVAL_S = 0.05       # これだけ経つまで読まない（小さな読みを繰り返さない）
    POLL_S = 0.001

    def __init__(self, dev: Device, rle: bool, sample_period_s: float, channels: int,
                 events: tuple[int, int] | None = None):
        self.dev, self.rle, self.sample_period_s, self.channels = dev, rle, sample_period_s, channels
        self._splitter = SegmentSplitter() if events is not None else None
        self._events_next, self._events_capacity = events if events is not None else (0, 0)
        self._events_received = 0
        self._la_beats = 0                                  # 受け取ったロジアナの拍（見出しの通し番号と照らす）
        self._received = 0                                  # 受け取ったバイト数
        self._samples = 0
        self._previous: int | None = None
        self._pending = b""                                 # 拍の途中（次の受け取りとつなぐ）
        self._overrun = False
        self._started = time.monotonic()
        self._deadline = float("inf")                       # chunks が duration_s から決める。stop で早める
        self._elapsed: float | None = None

    def chunks(self, duration_s: float, timeout_ms: int = 2000) -> Iterator[StreamChunk]:
        """duration_s 秒受け取ってから止め、残りを出し切るまでチャンクを返す。溢れたら（overrun）その時点で止める。
        途中で抜けると止めて残りを読み捨てる。stop（別のスレッドから）で時間の前に終えられる（残りも返す）。"""
        if self._elapsed is not None:
            raise RuntimeError("このストリームは終わっている")
        self._deadline = min(self._deadline, self._started + duration_s)      # 先に stop されていればそのまま
        running, deliver, last_read = True, True, time.monotonic()
        try:
            while True:
                status = self.dev.read32(g.LA_DEEP_STATUS)
                self._overrun |= bool(status >> g.LA_DEEP_STATUS_OVERRUN[0] & 1)
                drained = not running and not status >> g.LA_DEEP_STATUS_READING[0] & 1
                if running and (self._overrun or time.monotonic() >= self._deadline):
                    self.dev.write32(g.LA_DEEP_CONTROL, 0)
                    running = False
                available = self._available()               # status の後に読む（出し切った後なら確定値）
                if not available:
                    if drained:
                        return
                    time.sleep(self.POLL_S)
                    continue
                if running and available < self.READ_MIN and time.monotonic() - last_read < self.READ_INTERVAL_S:
                    time.sleep(self.POLL_S)
                    continue
                chunk = self._take(self.dev.read_stream_some(min(available, self.READ_MAX), timeout_ms))
                last_read = time.monotonic()
                if chunk is not None and deliver:
                    try:
                        yield chunk
                    except GeneratorExit:                   # 呼び出し側が抜けた: 止めて残りを読み捨てる
                        deliver = False
                        if running:
                            self.dev.write32(g.LA_DEEP_CONTROL, 0)
                            running = False
        except Exception:
            if running:
                self.dev.write32(g.LA_DEEP_CONTROL, 0)
            raise
        finally:
            self._elapsed = time.monotonic() - self._started

    def stop(self) -> None:
        """時間の前に終える（別のスレッドから呼んでよい）。chunks は書き込みを止め、残りを出し切ってから終わる。"""
        self._deadline = float("-inf")

    @property
    def carries_events(self) -> bool:
        """イベントも流れている（イベントも流す構成）。"""
        return self._splitter is not None

    @property
    def events_next(self) -> int:
        """次に届くはずのイベントの番号（events.written で数える。ここの前までを受け取った）。"""
        return self._events_next

    @property
    def events_received(self) -> int:
        return self._events_received

    def stats(self) -> StreamStats:
        elapsed = time.monotonic() - self._started if self._elapsed is None else self._elapsed
        return StreamStats(self._samples, self._received, elapsed, self.sample_period_s, self._overrun)

    def _available(self) -> int:
        """書かれたが受け取っていないバイト数（stream_beats は 32 ビットで折り返す）。"""
        total = self.dev.read32(g.LA_DEEP_STREAM_BEATS)
        beats = (total - self._received // BEAT_BYTES) % (1 << 32)
        return beats * BEAT_BYTES - self._received % BEAT_BYTES

    def _take(self, data: bytes) -> StreamChunk | None:
        self._received += len(data)
        events: tuple[DapEvent, ...] = ()
        if self._splitter is not None:
            data, events = self._split(data)
            if not data:
                return StreamChunk(b"", self.rle, self._samples, self._previous, events) if events else None
        data = self._pending + data
        whole = len(data) - len(data) % BEAT_BYTES
        data, self._pending = data[:whole], data[whole:]
        if not data:
            return None
        chunk = StreamChunk(data, self.rle, self._samples, self._previous, events)
        samples, self._previous = _extent(data, self.rle)
        self._samples += samples
        return chunk

    def _split(self, data: bytes) -> tuple[bytes, tuple[DapEvent, ...]]:
        """見出し付きの流れを、ロジアナの拍（つないだもの）とイベントに分ける。通し番号を確かめる。"""
        la, events = [], []
        for seg in self._splitter.feed(data):
            beats = len(seg.data) // BEAT_BYTES
            if seg.kind == g.STREAM_SEG_LA:
                if seg.seq != self._la_beats % (1 << 32):
                    raise StreamFormatError(f"ロジアナの拍の番号が飛んだ: {seg.seq}（{self._la_beats} のはず）")
                self._la_beats += beats
                la.append(seg.data)
            else:
                expected = self._events_next % self._events_capacity
                if seg.seq != expected:
                    raise StreamFormatError(f"イベントの位置が飛んだ: {seg.seq}（{expected} のはず。"
                                            "流している間にイベント記録を始め直した?）")
                self._events_next += beats
                self._events_received += beats
                events.extend(decode_events(seg.data))
        return b"".join(la), tuple(events)
