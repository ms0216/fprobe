"""ロジックアナライザ: 設定・捕捉・読み出し（深いバッファ・連続ストリーミング）。

値の表現と復号は fprobe.la_wave、VCD 出力は fprobe.la_vcd（どちらもここから再公開する）。

仕様: docs/superpowers/specs/2026-10-16-m7-logic-analyzer-design.md
      docs/superpowers/specs/2026-10-16-m8c-la-streaming-design.md（連続ストリーミング）
"""

import math
import time
from array import array
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from . import generated as g
from .caps import LaChannel, legacy_la_layout
from .device import Device
from .events import DapEvent, decode_events
from .la_vcd import DEFAULT_NAMES, VcdWriter, vcd_blocks, write_vcd  # noqa: F401  (再公開)
from .la_wave import (BEAT_BYTES, Capture, Transitions, Waveform, decode_beats, decode_transitions,  # noqa: F401  (再公開)
                      trigger_sample)
from .la_wave import _array_of, _np

__all__ = ["Capture", "DeepAnalyzer", "Front", "channel_layout", "channel_names", "check_trigger_channels",
           "external_channels", "LaStream", "LogicAnalyzer", "Pulse", "SegmentSplitter", "StreamChunk",
           "StreamFormatError", "StreamSegment", "StreamStats", "Transitions", "VcdWriter", "Waveform", "decode_beats",
           "decode_transitions", "split_segments", "vcd_blocks", "write_vcd"]


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


def external_pattern(external_only: bool, mask: int, value: int, rise: int, fall: int,
                     pulse) -> tuple[int, int, int, int]:
    """external_only なら、パターンを決して成り立たない形（ch0 が 1 かつ ch0 が立ち下がった）にする: ロジアナの判定は
    「パターン または 外からのトリガ」で、パターンが空なら真（すぐ捕捉）になるため。パターンと一緒には指定できない。"""
    if not external_only:
        return mask, value, rise, fall
    if mask or value or rise or fall or pulse:
        raise ValueError("external_only はパターン（mask・value・rise・fall・pulse）と一緒に指定できない")
    return 1, 1, 0, 1


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
              pulse: "Pulse | dict | None" = None, external_only: bool = False) -> float:
        """捕捉を始める。実際のサンプル周期（秒）を返す。filter_channels（外の信号の ch のビット）は filter_ns より短い変化を
        捨てる（グリッチフィルタ）。pulse はパルス幅のトリガ（Pulse）。どちらもケイパビリティに無ければ RuntimeError。
        external_only: クロストリガ（xtrig.route の to_la・FPGA の条件）でだけ捕捉する（パターンは使わない）。"""
        mask, value, rise, fall = external_pattern(external_only, mask, value, rise, fall, pulse)
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
        return Capture(samples, (trigger - start) % self.depth,
                       pin_time(time_hi << 32 | time_lo, self.caps.la_delay_internal, self.caps.la_phase_span),
                       sample_period_s, self.channels)

    def capture(self, *, timeout_s: float = 5.0, **trigger) -> Capture:
        period = self.start(**trigger)
        self.wait(timeout_s)
        return self.read(period)


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


def pin_time(trigger_time: int, delay: int, span: int) -> int:
    """FPGA がトリガのサンプルに付けた時刻を、ピンの変化の時刻の下限に直す（delay = ケイパビリティ LA_DELAY の段数、
    span = 位相の幅。ピンの変化は 返す時刻 以上 返す時刻 + span 未満のどこか。どこかは測っていない）。"""
    return trigger_time - delay - span


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
              filter_ns: float = 0.0, pulse: "Pulse | dict | None" = None, external_only: bool = False) -> float:
        """捕捉を始める（トリガ後 post_beats 拍。既定は容量の 1/2。開始から pretrigger サンプルまではトリガを受け付けない）。
        filter_channels・filter_ns・pulse・external_only は LogicAnalyzer.start と同じ。実際のサンプル周期（秒）を返す。"""
        mask, value, rise, fall = external_pattern(external_only, mask, value, rise, fall, pulse)
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
        if not self.dev.read32(g.MEM_STATUS) >> g.MEM_STATUS_READY[0] & 1:
            # 準備のできていない外部メモリで始めると、書き手が止まって捕捉が終わらない（理由が PC に見えない）
            raise RuntimeError("外部メモリの準備ができていない（PYNQ-Z2 では PS の中継が DDR の領域 MEM_BASE を書くまで"
                               "使えない: /etc/fprobe/mem と起動の mem= の予約を確かめる）")
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
        self._discard_stream()
        if events is not None:
            self.dev.write32(g.LA_DEEP_POST_BEATS, events[0] % events[1])   # このストリームでは最初のイベントの位置
        self.dev.write32(g.LA_DEEP_CONTROL, 1 | int(rle) << g.LA_DEEP_CONTROL_RLE[0]
                         | 1 << g.LA_DEEP_CONTROL_STREAM[0])
        return LaStream(self.dev, rle, self._period, self.channels, events=events)

    def _discard_stream(self, timeout_s: float = 5.0) -> None:
        """止めた後（_configure の後）に、前の流れの残りを読み捨てる。止めると機器は未読を出し切るが、受け取り手が
        いなければ（例外で抜けた・デーモンが落ちた）残りが受け取り口（USB の EP・リンクの ch 4 の受け取り済み）に残り、
        次の流れの先頭として読まれる（見出しの無い所から読み始めて StreamFormatError）。出し切り（reading = 0）、
        50 ms 何も届かなくなるまで読む。"""
        deadline = time.monotonic() + timeout_s
        while True:
            reading = self.status() >> g.LA_DEEP_STATUS_READING[0] & 1
            try:
                self.dev.read_stream_some(LaStream.READ_MAX, timeout_ms=50)
            except TimeoutError:
                if not reading:
                    return
            if time.monotonic() > deadline:
                raise RuntimeError("前のストリームの残りが出し切れない（機器の status.reading が 0 にならない）")

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
        while not (status := self.status()) >> g.LA_DEEP_STATUS_DONE[0] & 1:
            if time.monotonic() > deadline:
                self.dev.write32(g.LA_DEEP_CONTROL, 0)
                if status >> g.LA_DEEP_STATUS_TRIGGERED[0] & 1:
                    raise TimeoutError("トリガは成立したが、トリガ後の拍がまだ埋まらない（RLE は信号の変化が少ないと"
                                       "遅い: after_beats を減らす・rle=False・timeout_s を延ばす）")
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
        return data, trigger_sample(data, self._rle, before, slot), pin_time(time_hi << 32 | time_lo,
                                                                             self.caps.la_delay_deep,
                                                                             self.caps.la_phase_span)

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
        if _np is not None:
            return values[skip:], _array_of("Q", _np.frombuffer(starts, dtype=_np.uint64)[skip:] + _np.uint64(self.first))
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
                # 足りなければ前の読みから READ_INTERVAL_S まで待つ（1 ms ごとに CSR を 2 回読み直すと、UDP のリンクでは
                # リンクと機器のロックをほぼ使い続け、ほかの道具が毎回 1 秒待った。2026-10-21 実機）。出し切りの間は短く
                wait = (self.POLL_S if not running else
                        max(self.POLL_S, self.READ_INTERVAL_S - (time.monotonic() - last_read)))
                if not available:
                    if drained:
                        return
                    time.sleep(wait)
                    continue
                if running and available < self.READ_MIN and time.monotonic() - last_read < self.READ_INTERVAL_S:
                    time.sleep(wait)
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
