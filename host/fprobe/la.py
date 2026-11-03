"""ロジックアナライザ: 設定・捕捉・読み出し（深いバッファ・連続ストリーミング）。

値の表現と復号は fprobe.la_wave、VCD 出力は fprobe.la_vcd（どちらもここから再公開する）。

仕様: docs/superpowers/specs/2026-10-16-m7-logic-analyzer-design.md
      docs/superpowers/specs/2026-10-16-m8c-la-streaming-design.md（連続ストリーミング）
"""

import contextlib
import dataclasses
import math
import struct
import time
from array import array
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from . import generated as g
from .caps import LaChannel, legacy_la_layout
from .device import Device, select_stream_path
from .events import KIND_LA_GAP, DapEvent, EventLog, decode_events, read_counts
from .frame_link import StreamBuffer
from .timing import la_channel_skew
from .la_raw import RawCapture, RawCaptureWriter, RawMeta, raw_to_vcd
from .la_vcd import DEFAULT_NAMES, VcdWriter, vcd_blocks, vcd_blocks_skewed, write_vcd  # noqa: F401  (再公開)
from .la_wave import (BEAT_BYTES, Capture, Transitions, Waveform, decode_beats, decode_transitions,  # noqa: F401  (再公開)
                      stream_transitions, trigger_sample)
from .la_wave import _array_of, _extent, _np, _samples_of  # noqa: F401  (_extent・_samples_of は la_wave に移した。ここからも読める)

__all__ = ["Capture", "DeepAnalyzer", "Front", "channel_layout", "channel_names", "check_trigger_channels",
           "external_channels", "GapPlacer", "LaStream", "STREAM_BUFFER_BYTES", "LogicAnalyzer", "Pulse", "SegmentSplitter", "StreamChunk",
           "StreamCursor", "StreamFormatError", "StreamGap", "StreamHoldExceeded", "StreamLinkLoss", "StreamSegment", "StreamStats", "Transitions", "VcdWriter", "Waveform", "decode_beats",
           "decode_transitions", "split_segments", "stream_transitions", "vcd_blocks", "vcd_blocks_skewed", "write_vcd"]


def channel_layout(caps) -> tuple[LaChannel, ...]:
    """デバイスが申告したロジアナの ch の並び（U22。申告が無い古いデバイスは ch 0〜7 = ピン、8・9 = 内部）。"""
    return caps.la_layout


def channel_names(caps) -> dict[int, str]:
    """ch → 既定の名前（拡張ヘッダのピン = ch<n>、内部 = probe_drives_swdio・dap_busy、専用の入力 = in<k>）。
    何もつながっていない ch は含めない。"""
    return {c.channel: c.name for c in channel_layout(caps) if c.kind != "none"}


def check_trigger_channels(caps, **masks: int) -> None:
    """トリガの条件（mask・rise・fall 等のビット）が、デバイスが申告した ch（la_layout で何かがつながっている ch）だけを
    指すか。何もつながっていない ch（常に 0）を指していたら ValueError（専用の入力が要るなら構成 fprobe_la）。
    ロジアナの無い構成（la_channels = 0 で la_layout が空）は調べない（ロジアナの道具が先に断る）。"""
    if not caps.la_layout:
        return
    present = sum(1 << c.channel for c in caps.la_layout if c.kind != "none")
    for name, bits in masks.items():
        absent = bits & ~present
        if absent:
            channels = [ch for ch in range(absent.bit_length()) if absent >> ch & 1]
            raise ValueError(f"{name} の ch {channels} はこの構成（{caps.design}）に無い。"
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
    値一致・エッジの条件（mask・rise・fall）と両方満たしたときにトリガ。幅は取り込みの周期（la_sample_hz）で数える。"""
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
        if want_filter and not caps.la_filter:
            raise RuntimeError("この構成にロジアナのグリッチフィルタが無い（ケイパビリティ la_filter）")
        if pulse is not None and not caps.la_pulse:
            raise RuntimeError("この構成にパルス幅のトリガが無い（ケイパビリティ la_pulse）")
        if not (caps.la_filter or caps.la_pulse):
            return None
        clock = caps.la_sample_hz           # フィルタとパルス幅の周期は取り込みのクロック（sys の整数倍の構成がある）
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
                              "delay_ns": (cycles - 1) / clock * 1e9,   # フィルタした ch の変化の遅れ
                              "delay_cycles": cycles - 1}               # 同（取り込みのクロックの周期。fprobe.timing が使う）
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
        # 取り込みのクロックが sys の整数倍（内蔵バッファの構成だけ）。1 なら sys と同じ
        self.cap_ratio = (caps.la_sample_hz // caps.sys_clock_hz
                          if self.depth and caps.sys_clock_hz and caps.la_sample_hz % caps.sys_clock_hz == 0 else 1)
        # 1 語（sys の 1 周期）のサンプルの数（端子の直並列変換の構成。ケイパビリティ LA_FINE を申告する構成だけ。
        # docs/superpowers/specs/2026-11-01-la-resolution-design.md）。数える単位（pretrigger・位置）が語になり、バッファは la_lanes_mem
        self.lanes = self.cap_ratio if self._deep is None and caps.la_fine is not None else 1

    def testgen(self, *, period: int, high: int, edge: int = 0) -> float:
        """試験用の方形波を、ピン（PYNQ: RPi ヘッダ 32 番）に出す（CSR la_testgen。ケイパビリティ la_testgen のある構成だけ）。
        period = 周期（方形波を刻むクロックの周期の数。2〜4095）、high = 1 の周期の数（1〜period − 1）、
        edge = 0 なら刻むクロックの立ち上がりで、1 なら立ち下がり（半周期遅れる）で変える。出力の周波数（Hz）を返す。
        範囲外の値は、回路が黙って丸めるので、ここで断る。止めるのは testgen_off。"""
        self._require_testgen()
        if not 2 <= period <= 0xFFF:
            raise ValueError(f"period は 2〜4095（方形波を刻むクロックの周期の数。回路は 12 ビット）: {period}")
        if not 1 <= high <= period - 1:
            raise ValueError(f"high は 1〜period − 1（period = {period}）: {high}")
        if edge not in (0, 1):
            raise ValueError(f"edge は 0（立ち上がり）か 1（立ち下がり）: {edge}")
        self.dev.write32(g.LA_TESTGEN_PERIOD, period)
        self.dev.write32(g.LA_TESTGEN_HIGH, high)
        self.dev.write32(g.LA_TESTGEN_CONTROL, 1 | edge << g.LA_TESTGEN_CONTROL_EDGE[0])      # 周期・high を書いてから有効にする
        return self.testgen_clock_hz / period

    def testgen_off(self) -> None:
        """試験用の方形波をやめる（ピンを駆動しない = Hi-Z）。"""
        self._require_testgen()
        self.dev.write32(g.LA_TESTGEN_CONTROL, 0)

    @property
    def testgen_clock_hz(self) -> int:
        """試験用の方形波を刻むクロックの周波数（Hz）。デバイスの申告（ケイパビリティ LA_TESTGEN_HZ）。申告の無い古いデバイスは、
        取り込みが sys の整数倍の構成（cap_ratio > 1）なら取り込みのクロック、そうでなければ sys（その頃のコアの選びと同じ規則）。"""
        declared = self.caps.la_testgen_hz
        if declared:
            return declared
        return self.clock_hz if self.cap_ratio > 1 else self.caps.sys_clock_hz

    def _require_testgen(self) -> None:
        if not self.caps.la_testgen:
            raise RuntimeError("この構成には試験用の方形波が無い（ケイパビリティ la_testgen が無い。"
                               "fprobe_pynq_cap か fprobe_pynq_deep のような構成だけ）")

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
        if self.lanes > 1 and divider != 1:
            raise ValueError(f"この構成（{self.caps.design}）の内蔵バッファは間引かない（1 周期に {self.lanes} "
                             f"サンプル、{self.clock_hz / 1e6:g} MS/s 固定）。rate_hz は指定しない")
        # 1 周期に複数のサンプルの構成: プリトリガは語（sys の周期）で数える。指定のサンプル数以上を残す語の数
        pre_words = -(-pretrigger // self.lanes)
        if pre_words >= self.depth // self.lanes:
            raise ValueError(f"pretrigger は 0〜{(self.depth // self.lanes - 1) * self.lanes}（{self.lanes} サンプルの語の単位）")
        self._pretrigger = pre_words * self.lanes
        dev = self.dev
        dev.write32(g.LA_CONTROL, 0)
        dev.write(g.LA_RATE, [divider - 1, pre_words, mask, value, rise, fall])
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
            return Capture(samples, cap.trigger - first, cap.trigger_time, cap.sample_period_s, self.channels,
                           channel_skew=cap.channel_skew)
        start, trigger, time_lo, time_hi = self.dev.read(g.LA_START_INDEX, 4)
        info = self.dev.read32(g.LA_CAP_INFO) if self.cap_ratio > 1 else 0
        if self.cap_ratio > 1:
            lanes = info >> g.LA_CAP_INFO_LANES[0] & 0xF or 1       # 0 = この欄の無い古い回路（1 周期 1 サンプル）
            if info >> g.LA_CAP_INFO_RATIO[0] & 0xF != self.cap_ratio or lanes != self.lanes:
                self.dev.write32(g.LA_CONTROL, 0)
                raise RuntimeError(f"取り込みの比が合わない（申告 比 {self.cap_ratio}・レーン {self.lanes}、デバイス 比 "
                                   f"{info >> g.LA_CAP_INFO_RATIO[0] & 0xF}・レーン {lanes}）")
        if self.lanes > 1:
            samples, trigger_pos = self._read_lanes(start, trigger, info >> g.LA_CAP_INFO_TRIGGER_PHASE[0] & 0xF)
        else:
            raw = self.dev.read(g.LA_MEM_BASE, self.depth)
            samples, trigger_pos = raw[start:] + raw[:start], (trigger - start) % self.depth
        self.dev.write32(g.LA_CONTROL, 0)
        raw_time, cap_cycles = time_hi << 32 | time_lo, None
        if self.cap_ratio > 1:
            # ピンが変わった細かいサンプルの番号 = タイムベース × 比 + 位相（レーン）− 固定の遅れ
            # （rtl/la/la_cap_domain.vhd・la_lane_capture.vhd）
            cap_cycles = (raw_time * self.cap_ratio + (info >> g.LA_CAP_INFO_TRIGGER_PHASE[0] & 0xF)
                          - (info >> g.LA_CAP_INFO_LATENCY[0] & 0xFF))
            # n はピンが変わった周期の下限そのもの（la_cap_info の latency に前段の段数が入っている）。sys の経路の
            # 段数 LA_DELAY は引かない（二重になる）
            pin = cap_cycles // self.cap_ratio
        else:
            pin = pin_time(raw_time, self.caps.la_delay_internal, self.caps.la_phase_span)
        fine = self.caps.la_fine
        return Capture(samples, trigger_pos, pin, sample_period_s, self.channels,
                       trigger_cap_cycles=cap_cycles, fine_channels=fine,
                       coarse_period_s=None if fine is None else 1 / self.caps.sys_clock_hz,
                       channel_skew=la_channel_skew(self.caps, deep=False, front_info=self.front_info))

    def _read_lanes(self, start: int, trigger: int, lane: int) -> tuple[list[int], int]:
        """la_lanes_mem から時間順のサンプルと、トリガのサンプルの位置。語 w のレーン 2j・2j + 1 は、ワード j × 語の数 + w の
        下位・上位 16 ビット（fprobe_map の la_lanes_mem）。"""
        words = self.depth // self.lanes
        slices = [self.dev.read(g.LA_LANES_MEM_BASE + 4 * j * words, words) for j in range(self.lanes // 2)]
        samples = []
        for w in range(words):
            word = (start + w) % words
            for part in slices:
                samples += [part[word] & 0xFFFF, part[word] >> 16]
        return samples, (trigger - start) % words * self.lanes + lane

    def capture(self, *, timeout_s: float = 5.0, **trigger) -> Capture:
        period = self.start(**trigger)
        self.wait(timeout_s)
        return self.read(period)


# --- 見出し付きのストリーム（R1: ロジアナとイベントを 1 本に流す構成。
#     docs/superpowers/specs/2026-10-19-r1-unified-stream-design.md §3） ------------------------------------

class StreamFormatError(RuntimeError):
    """ストリームの見出しが壊れている・通し番号が飛んだ（同期を失った。それまでの分は正しい）。"""


class StreamLinkLoss(StreamFormatError):
    """流れを運ぶリンクで欠けた（または欠けた可能性がある）: 欠けた後のサンプルの位置が決まらないので、そこで止める
    （黙って位置をずらさない。それまでに返した分は正しい）。lost = 欠けの事実（数えたもの）の辞書。"""

    def __init__(self, message: str, lost: dict):
        super().__init__(message)
        self.lost = lost


class StreamHoldExceeded(RuntimeError):
    """欠けの区間を記録する流れ（gaps）で、位置の決まらない拍（届いていない欠けの記録の後ろ）が、使う側が明示した上限
    （gap_hold_bytes）を超えた。推定で置かずに止める（それまでに返した分の位置は正しい）。属性は数えた事実。"""

    def __init__(self, held_bytes: int, hold_bytes: int, missing_records: int, first_unplaced_beat: int):
        self.held_bytes, self.hold_bytes = held_bytes, hold_bytes
        self.missing_records, self.first_unplaced_beat = missing_records, first_unplaced_beat
        super().__init__(f"欠けの記録 {missing_records} 件が届かず、位置の決まらない拍が {held_bytes} バイト（上限 gap_hold_bytes = "
                         f"{hold_bytes}）。拍 {first_unplaced_beat} から後ろの位置が決まらないので止めた")


@dataclass(frozen=True)
class StreamSegment:
    """見出し付きのストリームの 1 かたまり。"""
    kind: int                    # g.STREAM_SEG_LA（ロジアナの拍）/ g.STREAM_SEG_EVENTS（イベント 1 件 = 1 拍）
    seq: int                     # ロジアナ: 最初の拍の、ストリームの先頭からの番号（32 ビットで折り返す）。
                                 # イベント: 最初のイベントのリングの位置（events.written で数えた番号 mod capacity）
    data: bytes                  # 中身（拍の整数倍。bytes か、受け取ったバッファを写さずに指す memoryview）


_SEG_HEAD = struct.Struct("<HHIQ")               # 見出しの拍: 種類(16) | 拍の数(16) | 番号(32) | 印(64)


class SegmentSplitter:
    """見出し付きのストリームをかたまりに分ける。届く量はかたまり・拍の境に揃っていなくてよい（続きは次の feed で）。

    かたまりの中身は、届いたバッファ（bytes・memoryview）を写さずに指す memoryview（届いた側のバッファは変えないこと）。
    写すのは、届いた量の境で切れたかたまり（次の feed で続きをつなぐ）だけ。"""

    def __init__(self):
        self._carry = bytearray()        # 切れたかたまりの頭から（見出しを含む）

    @property
    def pending(self) -> int:
        """まだかたまりにならないバイト数。"""
        return len(self._carry)

    @staticmethod
    def _head(buf, pos: int) -> int:
        """pos の見出しを確かめ、かたまり全体のバイト数を返す。"""
        kind, count, _, magic = _SEG_HEAD.unpack_from(buf, pos)
        if (magic != g.STREAM_SEG_MAGIC or kind not in (g.STREAM_SEG_LA, g.STREAM_SEG_EVENTS)
                or not 1 <= count <= g.STREAM_SEG_MAX_BEATS):
            head = int.from_bytes(bytes(buf[pos:pos + BEAT_BYTES]), "little")
            raise StreamFormatError(f"ストリームの見出しが壊れている: {head:032x}")
        return BEAT_BYTES * (1 + count)

    @staticmethod
    def _segment(buf, pos: int, end: int) -> StreamSegment:
        kind, _, seq, _ = _SEG_HEAD.unpack_from(buf, pos)
        return StreamSegment(kind, seq, buf[pos + BEAT_BYTES:end])

    def feed(self, data) -> list[StreamSegment]:
        view = memoryview(data).cast("B") if not isinstance(data, memoryview) or data.format != "B" else data
        out: list[StreamSegment] = []
        pos, size = 0, len(view)
        carry = self._carry
        if carry:                                       # 前の feed で切れたかたまりを、要る分だけつないで仕上げる
            if len(carry) < BEAT_BYTES:
                take = min(BEAT_BYTES - len(carry), size)
                carry += view[:take]
                pos = take
                if len(carry) < BEAT_BYTES:
                    return out
            need = self._head(carry, 0) - len(carry)
            take = min(need, size - pos)
            carry += view[pos:pos + take]
            pos += take
            if take < need:
                return out
            whole = bytes(carry)
            carry.clear()
            out.append(self._segment(memoryview(whole), 0, len(whole)))
        head = _SEG_HEAD.unpack_from
        magic, la, ev, top = g.STREAM_SEG_MAGIC, g.STREAM_SEG_LA, g.STREAM_SEG_EVENTS, g.STREAM_SEG_MAX_BEATS
        while size - pos >= BEAT_BYTES:
            kind, count, seq, mark = head(view, pos)
            if mark != magic or (kind != la and kind != ev) or not 1 <= count <= top:
                self._head(view, pos)                   # 誤りの文を作る
            end = pos + BEAT_BYTES * (1 + count)
            if end > size:
                break
            out.append(StreamSegment(kind, seq, view[pos + BEAT_BYTES:end]))
            pos = end
        if pos < size:
            carry += view[pos:]
        return out


def split_segments(data: bytes) -> list[StreamSegment]:
    """見出し付きのストリーム全体をかたまりに分ける（途中で切れていれば誤り）。中身は bytes。"""
    splitter = SegmentSplitter()
    out = [StreamSegment(seg.kind, seg.seq, bytes(seg.data)) for seg in splitter.feed(data)]
    if splitter.pending:
        raise StreamFormatError(f"かたまりの途中で終わっている（{splitter.pending} バイト）")
    return out


def pin_time(trigger_time: int, delay: int, span: int) -> int:
    """FPGA がトリガのサンプルに付けた時刻を、ピンの変化の時刻の下限に直す（delay = ケイパビリティ LA_DELAY の段数、
    span = 位相の幅。ピンの変化は 返す時刻 以上 返す時刻 + span 未満のどこか。どこかは測っていない）。

    時刻 T の瞬間 = タイムベースが T になる立ち上がり。回路の段数は「時刻が T になる辺の直後（周期 [T, T + 1) の中）に変わった
    入力 → 時刻 T + delay」で数えるので、変化は 時刻 − delay 以上 時刻 − delay + span 未満（gateware の la_capture_tb が
    シミュレーションの ps で確かめる）。2026-11-02（時刻の正しさ S1b）までは 時刻 − delay − span を返していた（1 刻み前）。
    span は範囲の幅で、下限には入らない（呼び手が範囲 [返す時刻, 返す時刻 + span) を返すときの約束として受ける）。"""
    return trigger_time - delay


class DeepAnalyzer:
    """深いバッファ（外部メモリ）のロジアナ。前段の設定（rate・トリガ）は LogicAnalyzer と同じ。

    読み出しは USB の拡張ストリーム。RAW は 1 拍 = 8 サンプル、RLE（変化点）は 1 拍 = 4 レコード。

    1 周期に複数のサンプルの構成（lanes > 1。fprobe_pynq_deep_fast。docs/superpowers/specs/2026-11-01-la-deep-fast-design.md）:
    la_sample_hz（800 MS/s）の細かさのまま垂れ流す（stream だけ。トリガ取り込みは無い）。RAW のサンプルの間隔は細かいサンプルの
    1・2・4 個か 8 の倍数（回路の選び方）、pack_channels は 1 も使える。
    """

    def __init__(self, dev: Device):
        caps = dev.capabilities()
        if not caps.mem_bytes or not caps.la_channels:
            raise RuntimeError("このデバイスには深いバッファが無い")
        self.dev, self.caps = dev, caps
        self.front_info: dict = {}
        self.beats = dev.read32(g.LA_DEEP_RING_BEATS)               # ロジアナのリング（残りはイベント用）
        self.channels, self.clock_hz = caps.la_channels, caps.la_sample_hz
        # 符号器の 1 語のサンプルの数（細かいサンプル / sys の周期）: LA_FINE を申告する 1 周期に複数のサンプルの構成だけ > 1
        sys_hz = caps.sys_clock_hz or 0
        self.lanes = (caps.la_sample_hz // sys_hz if caps.la_fine is not None and sys_hz
                      and caps.la_sample_hz % sys_hz == 0 else 1)
        self._rle = False
        self._period = 0.0
        self._step = 0                                               # 1 周期に複数のサンプル: 細かいサンプルの 2^step 個ごと
        self._divisor = 1                                            # サンプルの間隔（細かいサンプルの数）

    def start(self, *, rate_hz: float | None = None, mask: int = 0, value: int = 0, rise: int = 0, fall: int = 0,
              rle: bool = False, post_beats: int | None = None, pretrigger: int = 0, filter_channels: int = 0,
              filter_ns: float = 0.0, pulse: "Pulse | dict | None" = None, external_only: bool = False) -> float:
        """捕捉を始める（トリガ後 post_beats 拍。既定は容量の 1/2。開始から pretrigger サンプルまではトリガを受け付けない）。
        filter_channels・filter_ns・pulse・external_only は LogicAnalyzer.start と同じ。実際のサンプル周期（秒）を返す。"""
        if self.lanes > 1:
            raise RuntimeError(f"この構成（{self.caps.design}）の深いバッファは垂れ流しだけ（1 周期に {self.lanes} "
                               "サンプルの符号器にトリガ取り込みは無い。stream を使う）")
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
        if self.lanes > 1:
            self._divisor, self._step, rate_field = lane_divisor(self.clock_hz, self.lanes, rate_hz)
        else:
            divider = 1 if rate_hz is None else max(1, min(1 << 16, round(self.clock_hz / rate_hz)))
            self._divisor, self._step, rate_field = divider, 0, divider - 1
        dev = self.dev
        dev.write32(g.LA_DEEP_CONTROL, 0)
        dev.write32(g.LA_CONTROL, 0)
        dev.write(g.LA_RATE, [rate_field, pretrigger, mask, value, rise, fall])
        if front is not None:
            front.write(dev)
        self.front_info = front.info if front is not None else {}
        self._period = self._divisor / self.clock_hz

    def stream(self, *, rate_hz: float | None = None, rle: bool = False, filter_channels: int = 0,
               filter_ns: float = 0.0, events_from: int | None = None, ps_read: bool | None = None,
               pack_channels: int | None = None, max_bytes_per_s: float | None = None,
               first_channel: int = 0, gaps: bool = False, gap_hold_bytes: int | None = None,
               control_read_retries: int = 0) -> "LaStream":
        """連続ストリーミングを始める（トリガは使わない）。受け取りは返した LaStream の chunks で行う。
        イベントも流す構成（ケイパビリティ la_stream_events。R1）では、イベント記録の events_from 件目（events.written で
        数える。既定: 今の総数 = これから後の出来事）からのイベントも同じ流れで届く（StreamChunk.events）。

        ps_read: PS が DDR のリングを直接読んで流す形（ケイパビリティ la_stream_ps。PYNQ の深いバッファ。窓の語ごとの
        読みを通らないので速い。docs/superpowers/specs/2026-10-30-la-stream-ps-direct-design.md）。None = ケイパビリティが
        あれば使う。True でケイパビリティが無ければ RuntimeError。流れの形（見出し付きのかたまり）は同じ。

        pack_channels: 使うチャンネルだけ詰めて流す（ケイパビリティ la_pack。CSR la_deep.control.pack）。8 = ch 0〜7、4 = ch 0〜3、
        2 = ch 0〜1 だけを流し、データ量が 1/2・1/4・1/8 になる（そのぶん、同じ速さの受け取りで速いサンプルレートを流せる）。選んだ
        チャンネルは 1 サンプルも欠けない。選ばなかったチャンネル（上位）は記録されない。None = 詰めない（16 ch）。rle とは同時に使えない。
        1 周期に複数のサンプルの構成（lanes > 1）では 1 = ch 0 だけ も使え、first_channel で詰める最初の ch を選べる
        （ch first_channel〜first_channel + pack_channels − 1。例: PMOD JB の 1 番 = ch 10 を 1 ch で。0 = ch 0 から）。

        max_bytes_per_s: RAW の流れの量の上限（B/s）。RAW の量は始める前に決まる（ch 数 / 8 × サンプルレート）ので、上限を超える組は
        始める前に ValueError で断る（黙って捨てない）。None = デバイスが申告する持続できると測った速さ（ケイパビリティ
        la_stream_bytes_per_s。申告が無ければ断らない）。RLE は断らない（量は信号の変化の頻度で決まり、始める前には分からない。
        溢れたら overrun として事実を返す）。流している間のリングと帯域の使用量は LaStream.usage()。

        gaps: 溢れても止めず、回路が捨てた拍の区間（欠け）を記録して続ける（ケイパビリティ la_stream_gaps。CSR la_deep.control.gaps。
        docs/superpowers/specs/2026-11-01-la-deep-fast-design.md §12）。False（既定）= 今までの形: 溢れたら書き込みが止まり、届いた分は
        欠けの無い先頭の部分 + overrun。True: 欠けの区間は StreamChunk.gap（流れのサンプルの位置 start から samples サンプルは値が無い。
        埋めない・補わない）と LaStream.gaps で返し、流れは続く（overrun は、欠けを記録できなかった・回路の符号器そのものが溢れた等、
        それでも止まったときだけ）。欠けの記録はイベントのリングを通るので、イベントの記録をしていなければ始める（EventLog.ensure_started。
        記録中ならそのまま続ける）。

        gap_hold_bytes: gaps = True のとき必ず明示する（既定は持たない）。欠けの記録が届くまで位置の決まらない拍を PC に持つ上限
        （バイト）。超えたら推定で置かずに StreamHoldExceeded で止める（GapPlacer の説明）。

        control_read_retries: 流している間の見回りの READ（la_deep.status〜stream_beats。読みに副作用が無い）の応答が時間切れに
        なったとき、送り直してよい回数（既定 0 = 送り直さず TimeoutError で止まる）。使う側が明示する。送り直した回数と読み捨てた
        遅れた応答は StreamStats.control（docs/superpowers/specs/2026-11-03-host-link-correctness-design.md §4）。"""
        if control_read_retries < 0:
            raise ValueError(f"control_read_retries は 0 以上: {control_read_retries}")
        if gaps and not self.caps.la_stream_gaps:
            raise RuntimeError("この構成は欠けの区間を記録して続けられない（ケイパビリティ la_stream_gaps が無い。溢れたら止まる形だけ）")
        if gaps and (gap_hold_bytes is None or gap_hold_bytes <= 0):
            raise ValueError(f"gaps = True は gap_hold_bytes（位置の決まらない拍を PC に持つ上限、バイト。正の値）を明示する: {gap_hold_bytes}")
        pack = _pack_code(pack_channels, rle, self.caps, self.lanes)
        if first_channel:
            if self.lanes <= 1:
                raise ValueError("first_channel はこの構成では 0 だけ（詰める ch を選べるのは 1 周期に複数のサンプルの構成）")
            if pack_channels is None or not 0 < first_channel <= self.channels - pack_channels:
                raise ValueError(f"first_channel は pack_channels と一緒に、ch {first_channel}〜{first_channel + (pack_channels or 16) - 1} が "
                                 f"0〜{self.channels - 1} に収まる値（{first_channel} は収まらない）")
        if self.lanes > 1 and rle and rate_hz is not None and lane_divisor(self.clock_hz, self.lanes, rate_hz)[0] != 1:
            raise ValueError("1 周期に複数のサンプルの RLE はサンプルの間隔を変えない（rate_hz は指定しない。RLE の量は変化の数で決まる）")
        can_ps = bool(self.caps.la_stream_ps)
        if ps_read and not can_ps:
            raise RuntimeError("この構成は PS の直接読みでストリームを流せない（ケイパビリティ la_stream_ps が無い）")
        use_ps = can_ps if ps_read is None else ps_read
        events = None
        if gaps:
            EventLog(self.dev).ensure_started()                 # 欠けの記録はイベントのリングに書かれる
        if self.caps.la_stream_events:
            capacity = self.dev.read32(g.EVENTS_CAPACITY)
            start = read_counts(self.dev)[0] if events_from is None else events_from
            events = (start, capacity)
        elif events_from is not None:
            raise RuntimeError("この構成はストリームにイベントを流せない（ケイパビリティ la_stream_events が無い）")
        self._configure(rate_hz, filter_channels=filter_channels, filter_ns=filter_ns)
        if not rle:
            limit = max_bytes_per_s if max_bytes_per_s is not None else self.caps.la_stream_bytes_per_s or None
            need = raw_stream_bytes_per_s(pack_channels or self.channels, 1 / self._period)
            if limit is not None and need > limit:
                source = "指定の max_bytes_per_s" if max_bytes_per_s is not None else "デバイスが申告する持続できると測った速さ"
                raise ValueError(f"RAW の流れ {need / 1e6:.1f} MB/s（{pack_channels or self.channels} ch × {1e-6 / self._period:.1f} "
                                 f"MS/s）が上限 {limit / 1e6:.1f} MB/s（{source}）を超える。ch を減らす（pack_channels）・サンプル"
                                 "レートを下げる・RLE にする・max_bytes_per_s で上限を明示する")
        self._discard_stream()
        select_stream_path(self.dev, use_ps)
        # 欠けの数えの基準は開始の書きの前に取る（後で取ると、開始から基準までに落ちたフレームが基準に入って数えから漏れ、
        # 見出しの無い流れがずれたサンプルを返す。レビュー 2026-11-03）
        buffer_start = _stream_buffer(self.dev, reset_peak=True)
        if events is not None:
            self.dev.write32(g.LA_DEEP_POST_BEATS, events[0] % events[1])   # このストリームでは最初のイベントの位置
        self.dev.write32(g.LA_DEEP_CONTROL, 1 | int(rle) << g.LA_DEEP_CONTROL_RLE[0]
                         | 1 << g.LA_DEEP_CONTROL_STREAM[0] | int(use_ps) << g.LA_DEEP_CONTROL_PS_READ[0]
                         | pack << g.LA_DEEP_CONTROL_PACK[0] | self._step << g.LA_DEEP_CONTROL_STEP[0]
                         | first_channel << g.LA_DEEP_CONTROL_FIRST[0] | int(gaps) << g.LA_DEEP_CONTROL_GAPS[0])
        timing = None
        if self.lanes > 1:
            info = self.dev.read32(g.LA_DEEP_INFO)
            if info >> g.LA_DEEP_INFO_LANES[0] & 0xF != self.lanes:
                self.dev.write32(g.LA_DEEP_CONTROL, 0)
                raise RuntimeError(f"符号器の 1 語のサンプルの数が合わない（申告 {self.lanes}、デバイス "
                                   f"{info >> g.LA_DEEP_INFO_LANES[0] & 0xF}）")
            timing = {"lanes": self.lanes, "divisor": self._divisor, "latency": info >> g.LA_DEEP_INFO_LATENCY[0] & 0xFF}
        return LaStream(self.dev, rle, self._period, pack_channels or self.channels, events=events, pack=pack,
                        fine_timing=timing, first_channel=first_channel, gaps=gaps, gap_hold_bytes=gap_hold_bytes,
                        control_read_retries=control_read_retries, buffer_start=buffer_start)

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

    def stream_to_raw(self, raw_path: str | Path, duration_s: float, *, rate_hz: float | None = None, rle: bool = False,
                      names: dict[int, str] | None = None, pack_channels: int | None = None,
                      max_bytes_per_s: float | None = None, usage_interval_s: float | None = None,
                      first_channel: int = 0, gaps: bool = False, gap_hold_bytes: int | None = None,
                      control_read_retries: int = 0) -> "StreamStats":
        """duration_s 秒ストリーミングして、受け取った拍をそのまま生の保存ファイル（fprobe.la_raw）に追記する（取り込みだけ。
        変化点の取り出しも VCD の整形もしないので、受け取りが書き出しの速さに律速されない）。後から raw_to_vcd（または
        stream_to_vcd の raw_path）で VCD にする。引数と返す StreamStats は stream_to_vcd と同じ。
        仕様: docs/superpowers/specs/2026-11-03-la-raw-capture-design.md"""
        stream, meta = self._raw_stream(rate_hz=rate_hz, rle=rle, names=names, pack_channels=pack_channels,
                                        max_bytes_per_s=max_bytes_per_s, first_channel=first_channel, gaps=gaps,
                                        gap_hold_bytes=gap_hold_bytes, control_read_retries=control_read_retries)
        return self._capture(stream, meta, raw_path, duration_s, usage_interval_s)

    def stream_to_vcd(self, path: str | Path, duration_s: float, *, rate_hz: float | None = None, rle: bool = False,
                      names: dict[int, str] | None = None, pack_channels: int | None = None,
                      max_bytes_per_s: float | None = None, usage_interval_s: float | None = None,
                      first_channel: int = 0, gaps: bool = False, gap_hold_bytes: int | None = None,
                      control_read_retries: int = 0, raw_path: str | Path | None = None) -> "StreamStats":
        """duration_s 秒ストリーミングして VCD に書く（記録の長さはメモリに依らない）。pack_channels・max_bytes_per_s は stream と
        同じ（使うチャンネルだけ詰めて流す・RAW の量の上限）。VCD には、選んだチャンネル（ch 0〜pack_channels − 1）だけを並べる。
        usage_interval_s: 流している間、この秒数ごとに LaStream.usage を読む（None = 読まない）。終わった後にも 1 回読む
        （ケイパビリティ la_stream_usage のある構成）。読んだ結果は返す StreamStats.usage に古い順。
        gaps・gap_hold_bytes: stream と同じ（溢れても止めず欠けの区間を記録して続ける）。VCD では欠けの区間の全信号を x（値が分からない）にする。

        取り込み（stream_to_raw）と書き出し（raw_to_vcd）を続けて行う: 受け取った拍をまず生の保存ファイルに書き、終わってから VCD にする。
        raw_path = 生の保存ファイルの置き場所（残る。None = VCD の隣の一時ファイルで、書き出しの後に消す）。途中で例外（リンクの
        喪失・保持の上限等）が出ても、それまでに取り込んだ分を VCD にしてから、その例外をそのまま出す（隠さない）。
        VCD のバイト列は、取り込みと書き出しを分ける前と同じ。"""
        stream, meta = self._raw_stream(rate_hz=rate_hz, rle=rle, names=names, pack_channels=pack_channels,
                                        max_bytes_per_s=max_bytes_per_s, first_channel=first_channel, gaps=gaps,
                                        gap_hold_bytes=gap_hold_bytes, control_read_retries=control_read_retries)
        raw = Path(raw_path) if raw_path is not None else Path(f"{path}.fplaraw.tmp")
        try:
            try:
                stats = self._capture(stream, meta, raw, duration_s, usage_interval_s)
            finally:
                raw_to_vcd(RawCapture(raw), path)                  # 取り込めた分は、途中の例外でも VCD にする
        finally:
            if raw_path is None:
                with contextlib.suppress(FileNotFoundError):
                    raw.unlink()
        return stats

    def _raw_stream(self, *, rate_hz, rle, names, pack_channels, max_bytes_per_s, first_channel, gaps, gap_hold_bytes,
                    control_read_retries) -> "tuple[LaStream, RawMeta]":
        """ストリームを始め、生の保存ファイルのヘッダ（流れの性質。信号 i = ch first_channel + i、名前もその ch のもの）を作る。"""
        stream = self.stream(rate_hz=rate_hz, rle=rle, pack_channels=pack_channels, max_bytes_per_s=max_bytes_per_s,
                             first_channel=first_channel, gaps=gaps, gap_hold_bytes=gap_hold_bytes,
                             control_read_retries=control_read_retries)
        count = pack_channels or self.channels
        names = {ch - first_channel: name for ch, name in {**channel_names(self.caps), **(names or {})}.items()
                 if first_channel <= ch < first_channel + count}
        # ch ごとの時刻のずれ（fprobe.timing）。分からない ch はずらさず、VCD の $comment に書く
        timebase_hz = self.caps.timebase_hz
        skew = tuple(self.channel_skew[first_channel:first_channel + count]) if timebase_hz else ()
        return stream, RawMeta(rle=rle, pack=stream.pack, channels=count, first_channel=first_channel,
                               sample_period_s=stream.sample_period_s, names=names, skew=skew,
                               timebase_hz=timebase_hz or None)

    def _capture(self, stream: "LaStream", meta: "RawMeta", raw_path: str | Path, duration_s: float,
                 usage_interval_s: float | None) -> "StreamStats":
        """受け取りのループ: 受け取ったかたまりを生の保存ファイルにそのまま追記するだけ（復号しない）。"""
        last = time.monotonic()
        with RawCaptureWriter(raw_path, meta) as raw:
            for chunk in stream.chunks(duration_s):
                raw.write(chunk)
                if usage_interval_s is not None and time.monotonic() - last >= usage_interval_s:
                    stream.usage()
                    last = time.monotonic()
        if self.caps.la_stream_usage:
            stream.usage()
        return dataclasses.replace(stream.stats(), usage=tuple(stream.usage_history))

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
        return Capture(decode_beats(data, self._rle), trigger, trigger_time, self._period, self.channels,
                       channel_skew=self.channel_skew)

    def read_transitions(self, before_beats: int | None = None, after_beats: int | None = None) -> Transitions:
        """read と同じ範囲を、毎サンプルに展開せず変化点として読む。"""
        data, trigger, trigger_time = self._read_raw(before_beats, after_beats)
        values, starts, length = decode_transitions(data, self._rle)
        return Transitions(values, starts, length, trigger, trigger_time, self._period, self.channels,
                           self.channel_skew)

    @property
    def channel_skew(self) -> tuple[float | None, ...]:
        """ch ごとの時刻のずれ（最後に設定した前段で。fprobe.timing.la_channel_skew）。"""
        return la_channel_skew(self.caps, deep=True, front_info=self.front_info)

    def _read_command(self) -> int:
        """読み出しの命令。PS が DDR から直接読んで TCP で送る形（read_ps）は、デバイスにその能力（ケイパビリティ la_stream_ps）があり、
        TCP のストリームが開いているときだけ（UDP で 31 MB を一気に送ると、欠けても気づけない）。書き込みが止まった取り込みの読み出し専用
        （書き込み中のイベントのリングは、応答の来ていない書きを PS が読む恐れがあるので、今まで通り）。"""
        ps = bool(self.caps.la_stream_ps) and bool(getattr(self.dev, "stream_tcp_open", False))
        return 1 << (g.LA_DEEP_COMMAND_READ_PS[0] if ps else g.LA_DEEP_COMMAND_READ[0])

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
        command = self._read_command()
        select_stream_path(self.dev, command == 1 << g.LA_DEEP_COMMAND_READ_PS[0])
        self.dev.write32(g.LA_DEEP_COMMAND, command)
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
    """ストリームの 1 回の受け取り（拍の整数倍）。復号は必要になったときに行う（受け取りを遅らせない）。
    data は bytes か、受け取ったバッファを写さずに指す memoryview（どちらも bytes のように読める。変えないこと）。"""
    data: bytes
    rle: bool
    first: int                   # 最初のサンプルの通し番号（ストリームの先頭 = 0）
    previous: int | None         # 直前のサンプルの値（最初のチャンクは None）
    events: tuple[DapEvent, ...] = ()   # 同じ受け取りに載っていたイベント（イベントも流す構成。古い順）
    pack: int = 0                # 詰めの種類（0 = 詰めない、1 = 8 ビット、2 = 4 ビット、3 = 2 ビット。1 語に 1 << pack サンプル）
    # 欠けの区間を記録する流れ（gaps）: このチャンクの直前の欠け（gap.start + gap.samples = first。previous は None）。None = 欠けなし
    gap: "StreamGap | None" = None
    count: int = field(default=-1, repr=False, compare=False)   # サンプル数（受け取りで数えた値。-1 = 数えていない）

    def samples(self) -> "array[int]":
        return _expand_words(decode_beats(self.data, self.rle), self.pack)

    @property
    def length(self) -> int:
        """このチャンクのサンプル数（次のチャンクの first = first + length）。展開しない（受け取りで数えた値があればそれ）。"""
        if self.count >= 0:
            return self.count
        return _extent(self.data, self.rle)[0] << self.pack if self.data else 0

    def transitions(self) -> tuple["array[int]", "array[int]"]:
        """変化点（値, 通し番号の開始）を array（"H"・"Q"）で。直前のチャンクの最後と同じ値は含めない（つなぐと全体の
        変化点になる）。中身は changes と同じ。"""
        values, starts = self.changes()
        if _np is not None:
            return _array_of("H", values), _array_of("Q", starts)
        return values, starts

    def changes(self):
        """transitions と同じ変化点を、numpy があれば numpy の配列（値 uint16・開始 uint64）のまま（VcdWriter.write 等へ
        写さずに渡す口）。numpy が無ければ array。詰めた語・RLE は展開せずに数える（la_wave.stream_transitions）。"""
        return stream_transitions(self.data, self.rle, self.pack, self.first, self.previous)


class StreamCursor:
    """流れのサンプルの位置（先頭 = 0。欠けの区間も数える）と直前の値。LaStream のサンプルの数えはここ 1 か所
    （欠けの区間を記録する流れでは GapPlacer がこれを進める）。"""

    __slots__ = ("rle", "pack", "position", "previous", "gap_samples")

    def __init__(self, rle: bool, pack: int):
        self.rle, self.pack = rle, pack
        self.position = 0                     # 次のサンプルの位置
        self.previous: int | None = None      # 直前のサンプルの値（欠けの後は None）
        self.gap_samples = 0                  # 欠けの区間のサンプルの数の和

    @property
    def samples(self) -> int:
        """値のあるサンプルの数。"""
        return self.position - self.gap_samples

    def take(self, data) -> tuple[int, int | None, int]:
        """拍の列 data を進める。(最初の位置, 直前の値, サンプル数)。"""
        first, previous = self.position, self.previous
        count, self.previous = _samples_of(data, self.rle, self.pack)
        self.position += count
        return first, previous, count

    def skip(self, samples: int) -> int:
        """値の無い区間（欠け）を進める。最初の位置を返す。"""
        first = self.position
        self.position += samples
        self.gap_samples += samples
        self.previous = None
        return first


@dataclass(frozen=True)
class StreamGap:
    """垂れ流しの欠けの区間: 回路が拍を捨てた区間（CSR la_deep.control.gaps の流れ。docs/superpowers/specs/2026-11-01-la-deep-fast-design.md §12）。
    流れのサンプルの位置 start〜start + samples − 1 は値が無い（埋めない・補わない）。次に値のあるサンプルは start + samples。
    位置の時刻は値のあるサンプルと同じ式（1 周期に複数のサンプルの構成は LaStream.sample_time、ほかは起点 + 位置 × サンプルの間隔）。"""
    start: int                   # 流れのサンプルの位置（先頭 = 0。前の欠けも数える）
    samples: int                 # 値の無いサンプルの数（回路が数えた事実）
    beat: int                    # 欠けの前に届いたロジアナの拍の数（流れの開始から）
    drop_time48: int             # 回路が拍を捨て始めた周期のタイムベースの下位 48 ビット（サンプルの時刻ではない。回路の記録の写し）

    def as_dict(self) -> dict:
        return {"start": self.start, "samples": self.samples, "beat": self.beat}


class GapPlacer:
    """欠けの記録（種類 19 のイベント）と届いたロジアナの拍から、欠けの区間の位置を決める（ホスト側。docs/superpowers/specs/2026-11-01-la-deep-fast-design.md §12）。

    回路は欠けの記録を、その後の拍より先にイベントのリングへ書くが、流れの上ではイベントとロジアナの拍が別のかたまりで届き、
    どちらが先に届くかは決まらない。そこで「拍を受け取った後に読んだ CSR の gaps（記録の件数 N）」を使う: 受け取った拍 R 個に
    関わる記録（拍の位置 < R）は、その拍が書かれる前に出ているので N 件の中にある。届いた記録が N 件そろうまで、関わりうる拍を
    出さずに持つ（推定しない）。届いていない記録が 1 件だけなら、その位置は gap_last_beat（gaps より先に読む）なので、その前の拍は出せる。
    2 件以上なら、届いた最後の記録の位置より前だけ出せる（届いていない記録の位置はそれ以上）。
    finish で残った拍（届かなかった記録の後ろで位置が決まらない拍）は出さず、unplaced_beats に数える（黙って位置をずらさない）。

    持つ量の上限 hold_bytes（使う側が明示する。既定は持たない）: 位置の決まらない拍がそれを超えたら StreamHoldExceeded で止める。
    超えた拍を「位置の決まらない区間」として返して続ける形は取らない: 届いていない記録のサンプルの数が分からないので、その後ろの
    すべての拍の位置が決まらない（区間は流れの終わりまで伸び、返せるものが無い）。止めれば、それまでに返した分は正しく、
    何が欠けたか（届いていない記録の件数・最初の位置の決まらない拍）を事実として返せる。

    拍は届いたバッファを写さずに持つ（deque）。出すときは、1 つの塊に収まれば memoryview、またがれば 1 回つないで渡す。"""

    def __init__(self, rle: bool, pack: int, *, hold_bytes: int, cursor: StreamCursor | None = None):
        if hold_bytes <= 0:
            raise ValueError(f"hold_bytes（位置の決まらない拍を持つ上限）が不正: {hold_bytes}")
        self.cursor = cursor if cursor is not None else StreamCursor(rle, pack)
        self.rle, self.pack = rle, pack
        self.hold_bytes = hold_bytes
        self._held: deque = deque()       # まだ出していない拍（_released 拍目から。届いた塊のまま）
        self._held_bytes = 0
        self._received = 0                # 受け取った拍の数
        self._released = 0                # 位置を決めて出した拍の数
        self._pending: deque[tuple[int, int, int]] = deque()   # 届いたがまだ置いていない記録（拍の位置, サンプルの数, 時刻）古い順
        self._records = 0                 # 届いた記録の件数（分けた件も 1 件ずつ）
        self._last_beat = 0               # 最後に届いた記録の拍の位置
        self.gaps: list[StreamGap] = []
        self.unplaced_beats = 0
        self.peak_held_bytes = 0          # 持った量の最大（事実）

    @property
    def position(self) -> int:
        """次に出すサンプルの位置（cursor）。"""
        return self.cursor.position

    @property
    def previous(self) -> int | None:
        return self.cursor.previous

    @property
    def received_beats(self) -> int:
        return self._received

    @property
    def held_bytes(self) -> int:
        return self._held_bytes

    def _unwrap(self, beat32: int) -> int:
        """32 ビットで折り返す拍の位置を、出した拍の数より後の値に伸ばす（置いていない記録の位置は、それ以上）。"""
        return self._released + ((beat32 - self._released) % (1 << 32))

    def add_beats(self, data) -> None:
        n = len(data)
        if n % BEAT_BYTES:
            raise StreamFormatError("ロジアナの拍の途中で切れた")
        if n:
            self._held.append(data)
            self._held_bytes += n
            self._received += n // BEAT_BYTES
            if self._held_bytes > self.peak_held_bytes:
                self.peak_held_bytes = self._held_bytes

    def add_record(self, beat32: int, samples: int, time48: int) -> None:
        beat = self._unwrap(beat32)
        if self._records and beat < self._last_beat:
            raise StreamFormatError(f"欠けの記録の位置が戻った: {beat}（前は {self._last_beat}）")
        self._pending.append((beat, samples, time48))
        self._records += 1
        self._last_beat = beat

    def release(self, received_at_read: int, reported: int, last_beat32: int) -> list[tuple]:
        """received_at_read = CSR を読む前に受け取っていた拍の数、reported・last_beat32 = その後に読んだ la_deep.gaps・gap_last_beat
        （gap_last_beat → gaps の順に読んだ値）。位置の決まった分を、古い順の ("data", 拍のバイト列, 最初の位置, 直前の値) と
        ("gap", StreamGap) の並びで返す。出した後も持つ量が hold_bytes を超えていれば StreamHoldExceeded。"""
        out = self._release(received_at_read, reported, last_beat32)
        if self._held_bytes > self.hold_bytes:
            raise StreamHoldExceeded(self._held_bytes, self.hold_bytes, max(0, reported - self._records), self._released)
        return out

    def _release(self, received_at_read: int, reported: int, last_beat32: int) -> list[tuple]:
        missing = reported - self._records
        if missing <= 0:
            limit = received_at_read
        elif missing == 1:
            limit = min(received_at_read, self._unwrap(last_beat32))
        else:
            limit = min(received_at_read, self._last_beat if self._records else self._released)
        return self._emit(max(self._released, limit))

    def finish(self, reported: int, last_beat32: int) -> list[tuple]:
        """流れが終わった（出し切った）後: 最後の CSR の読みで出せる分を返し、残りを unplaced_beats に数える。"""
        out = self._release(self._received, reported, last_beat32)
        self.unplaced_beats = self._received - self._released
        self._held.clear()
        self._held_bytes = 0
        return out

    def _take(self, n: int):
        """持っている拍の先頭 n バイト（1 つの塊に収まれば写さない）。"""
        held = self._held
        first = held[0]
        if len(first) == n:
            held.popleft()
            out = first
        elif len(first) > n:
            view = first if isinstance(first, memoryview) else memoryview(first)
            out = view[:n]
            held[0] = view[n:]
        else:
            parts, left = [], n
            while left:
                piece = held[0]
                if len(piece) <= left:
                    parts.append(held.popleft())
                    left -= len(piece)
                else:
                    view = piece if isinstance(piece, memoryview) else memoryview(piece)
                    parts.append(view[:left])
                    held[0] = view[left:]
                    left = 0
            out = b"".join(parts)
        self._held_bytes -= n
        return out

    def _emit(self, limit: int) -> list[tuple]:
        out: list[tuple] = []
        cursor, pending = self.cursor, self._pending
        while True:
            nxt = pending[0][0] if pending else None
            stop = limit if nxt is None or nxt > limit else nxt
            if stop > self._released:
                data = self._take((stop - self._released) * BEAT_BYTES)
                first, previous, count = cursor.take(data)
                out.append(("data", data, first, previous, count))
                self._released = stop
            if nxt is None or nxt > limit:
                return out
            beat, samples, time48 = pending.popleft()
            gap = StreamGap(cursor.skip(samples), samples, beat, time48)
            self.gaps.append(gap)
            out.append(("gap", gap))


_PACK_CODES = {8: 1, 4: 2, 2: 3, 1: 4}        # pack_channels → CSR la_deep.control.pack（1 は 1 周期に複数のサンプルの構成だけ）


def raw_stream_bytes_per_s(channels: int, sample_hz: float) -> float:
    """RAW の流れの符号の量（B/s）= ch 数 / 8 × サンプルレート（詰めた ch は 1 サンプル ch 数ビット。見出しを除く）。"""
    return channels / 8 * sample_hz


def lane_divisor(sample_hz: int, lanes: int, rate_hz: float | None) -> tuple[int, int, int]:
    """1 周期に複数のサンプルの構成の RAW のサンプルの間隔: rate_hz → (細かいサンプルの数 d, CSR の step, CSR の la.rate)。
    d は 1・2・4 …（< lanes の 2 の累乗: 語の中のレーンを 2^step ごとに選ぶ）か lanes の倍数（語を la.rate + 1 語ごとに 1 つ・
    レーン 0）。回路が作れない間隔は丸めずに断る（間引きの代わりに使う側が明示した、作れるレートだけ）。"""
    if rate_hz is None:
        return 1, 0, 0
    d = sample_hz / rate_hz
    di = round(d)
    if di < 1 or abs(d - di) > 1e-9 * d:
        raise ValueError(f"サンプルレート {rate_hz} Hz は {sample_hz} Hz の整数分の 1 でない")
    log_lanes = lanes.bit_length() - 1
    if di < lanes and di & (di - 1) == 0:
        return di, di.bit_length() - 1, 0
    if di % lanes == 0 and di // lanes <= 1 << 16:
        return di, log_lanes, di // lanes - 1
    raise ValueError(f"サンプルの間隔 {di}（細かいサンプル）はこの構成では作れない: {lanes} より小さい 2 の累乗か {lanes} の倍数"
                     f"（{lanes} × 65536 まで）。作れる近いレートを指定する")


def _pack_code(pack_channels: int | None, rle: bool, caps, lanes: int = 1) -> int:
    """pack_channels（None・8・4・2、1 周期に複数のサンプルの構成は 1 も）を CSR の pack の値にする。合わない指定・ケイパビリティの
    無い構成は、黙って詰めずに断る。"""
    if pack_channels is None:
        return 0
    if pack_channels not in _PACK_CODES or (pack_channels == 1 and lanes <= 1):
        allowed = "8・4・2・1" if lanes > 1 else "8・4・2"
        raise ValueError(f"pack_channels は {allowed}（ch 0 から数えて、その数だけ流す）か None（詰めない）。{pack_channels} は詰められない")
    if rle:
        raise ValueError("pack_channels は rle と同時には使えない（RLE は 16 ビットの値の繰り返し）")
    if not caps.la_pack:
        raise RuntimeError("この構成はチャンネルを詰めて流せない（ケイパビリティ la_pack が無い）")
    return _PACK_CODES[pack_channels]


def _expand_words(words: "array[int]", pack: int) -> "array[int]":
    """詰めた 16 ビットの語を、サンプルに展開する（1 語 = 1 << pack サンプル。時間の古いサンプルが下位）。pack = 0 はそのまま。"""
    if not pack:
        return words
    if _np is not None:
        return _array_of("H", _expand_np(_np.frombuffer(words, dtype=_np.uint16), pack).astype(_np.uint16, copy=False))
    width, count = 16 >> pack, 1 << pack
    mask = (1 << width) - 1
    out = array("H")
    for word in words:
        out.extend(array("H", ((word >> (width * j)) & mask for j in range(count))))
    return out


def _expand_np(words, pack: int):
    """_expand_words の numpy 版（numpy の配列を返す。1 ビットの語は unpackbits で uint8、ほかは uint16）。"""
    if pack == 4:                                       # 1 語 = 1 ビット × 16: リトルエンディアンの語の下位ビットが古い = バイトの下位から
        return _np.unpackbits(words.view(_np.uint8), bitorder="little")
    width, count = 16 >> pack, 1 << pack
    shifts = (_np.arange(count, dtype=_np.uint16) * width)[None, :]
    return ((words[:, None] >> shifts) & ((1 << width) - 1)).astype(_np.uint16).ravel()


@dataclass(frozen=True)
class StreamStats:
    samples: int
    bytes: int
    elapsed_s: float             # 始めてから出し切るまで
    sample_period_s: float
    overrun: bool                # ホストが遅れて書き込みが止まった（それまでの分は正しい）
    link_buffer: StreamBuffer | None = None   # PC の受け取り済みで未読の量（この流れの間の最大・止めた回数と時間）。None = 測れない道（USB 等）
    usage: tuple[dict, ...] = ()   # リングと帯域の使用量の読み（LaStream.usage。stream_to_vcd が読んだもの、古い順）
    gaps: tuple["StreamGap", ...] = ()   # 欠けの区間（gaps の流れ。古い順。samples は値のあるサンプルだけを数える）
    unplaced_beats: int = 0      # 欠けの記録が届かず位置が決まらないので返さなかった拍（gaps の流れ。ふつうは 0）
    relay: dict | None = None    # 出し切った後の PYNQ の中継の状態（Device.relay_status: ゼロコピーの道か複写の道か・copied 等）。None = 中継が無い・返事なし
    gap_held_peak_bytes: int = 0   # gaps の流れ: 位置の決まらない拍を持った量の最大（バイト。上限 gap_hold_bytes と比べる事実）
    # この流れの間の CONTROL の数え {read_retries: 時間切れで送り直した READ, stale_responses: 読み捨てた遅れた応答}（事実）
    control: dict = field(default_factory=lambda: {"read_retries": 0, "stale_responses": 0})

    @property
    def gap_samples(self) -> int:
        """欠けの区間のサンプルの数の和（値の無いサンプル）。"""
        return sum(gap.samples for gap in self.gaps)

    @property
    def duration_s(self) -> float:
        """記録の長さ（流れの先頭から最後の値のあるサンプルまで。欠けの区間を含む）。"""
        return (self.samples + self.gap_samples) * self.sample_period_s

    @property
    def mb_per_s(self) -> float:
        return self.bytes / self.elapsed_s / 1e6 if self.elapsed_s else 0.0

    @property
    def link_buffer_dict(self) -> dict | None:
        """link_buffer を API の応答の形（StreamBuffer.as_dict。測れない道では None）。"""
        return None if self.link_buffer is None else self.link_buffer.as_dict()


_NOT_GIVEN = object()   # LaStream の buffer_start を渡していない印（None は「測れない機器」の意味）


class LaStream:
    """連続ストリーミングの受け取り（DeepAnalyzer.stream が始める）。

    デバイスは外部メモリを FIFO にして拍（リングと同じ形式）を流し続け、書いた拍の総数を stream_beats に示す。
    USB にはそれと受け取った量の差（必ず届く量）だけを求める（届かない量を待って時間切れで捨てることがない）。
    止める（run = 0）とデバイスは書き込みを止めて未読を出し切り、status.reading を 0 に戻す。

    events = (最初のイベントの番号, イベントのリングの件数) なら、イベントも流す構成（R1）: 流れは見出し付きの
    かたまりの列で、stream_beats は出すと決めた拍の総数（見出し・イベントを含む）。かたまりを分け、ロジアナの拍は
    今と同じに、イベントは StreamChunk.events に渡す。通し番号が飛べば StreamFormatError（黙って欠けない）。

    gaps = True（DeepAnalyzer.stream の gaps。イベントも流す構成だけ）: 欠けの記録（種類 19）はイベントに入れず、GapPlacer で
    欠けの区間にする。チャンクは欠けの区間で分かれ、欠けの直後のチャンクの gap に欠けを載せる（first は欠けの後の位置）。
    gap_hold_bytes（gaps のとき必ず明示する）は位置の決まらない拍を持つ上限（GapPlacer。超えたら StreamHoldExceeded で止める）。

    リンクの欠け（A4）: フレームのリンク（UDP・UART）で受けるとき、流れのバイトが欠けると後ろのサンプルの位置が決まらない。
    - リンクが流れのチャンネルのフレームを捨てた（PC に溜める上限を超えた: StreamBuffer.dropped_frames が増えた）→ StreamLinkLoss。
    - 見出しの無い流れ（イベントを流さない RAW・RLE）は流れの中に番号が無いので、流れを運ぶ道でフレームが落ちた・壊れた
      （StreamBuffer.lost_frames が増えた。どのチャンネルのものかは分からない）→ StreamLinkLoss（欠けた可能性。黙って続けない）。
      見出し付きの流れは見出しと番号で確かめる（欠ければ StreamFormatError）。
    - 出し切った（reading = 0）のに、機器が書いたと言う量（stream_beats）まで届かない（UDP の 8 ビットの seq で数えきれない欠け等）
      → StreamLinkLoss。
    どれも、それまでに返した分は正しい。StreamStats.link_buffer にこの流れの間の数え（差）を返す。

    CSR の往復は、1 回の見回りで 1 回: status から stream_beats まで（gaps の流れは gaps まで）を 1 つの READ で読む（CONTROL の
    READ は番地の順に読むので、status → stream_beats → gap_last_beat → gaps の順の約束はそのまま）。見回りの READ の応答が
    時間切れになったら、使う側が control_read_retries で明示した回数まで送り直す（既定 0。止める書きは送り直さない。
    docs/superpowers/specs/2026-11-03-host-link-correctness-design.md §4）。
    """

    READ_MAX = 4 << 20           # 1 回の読みの上限（バイト）
    READ_MIN = 64 << 10          # 走っている間はこれだけ溜まるか
    READ_INTERVAL_S = 0.05       # これだけ経つまで読まない（小さな読みを繰り返さない）
    POLL_S = 0.001

    def __init__(self, dev: Device, rle: bool, sample_period_s: float, channels: int,
                 events: tuple[int, int] | None = None, pack: int = 0, fine_timing: dict | None = None,
                 first_channel: int = 0, gaps: bool = False, gap_hold_bytes: int | None = None,
                 control_read_retries: int = 0, buffer_start: "StreamBuffer | None | object" = _NOT_GIVEN):
        """buffer_start: 欠けの数えの基準（機器に流れを頼む前に取った _stream_buffer(dev, reset_peak=True)）。渡さなければここで取る
        （流れを頼んだ後に作るなら、その間に落ちたフレームを数えない）。"""
        self.dev, self.rle, self.sample_period_s, self.channels = dev, rle, sample_period_s, channels
        if control_read_retries < 0:
            raise ValueError(f"control_read_retries は 0 以上: {control_read_retries}")
        self._control_read_retries = control_read_retries   # 見回りの READ を時間切れで送り直してよい回数（使う側が明示する）
        self._control_start = dev.control_counters()        # この流れの間の CONTROL の数えを測り始める
        self.pack = pack                                    # 詰めの種類（1 語 = 1 << pack サンプル。0 = 詰めない）
        # 1 周期に複数のサンプルの構成の時刻の式の材料（lanes・divisor・latency。sample_time で使う。None = 1 周期 1 サンプル）
        self.fine_timing = fine_timing
        self.first_channel = first_channel                  # 値のビット i = ch first_channel + i（詰めた流れ）
        self._usage_prev: dict | None = None                # usage の前の読み（毎秒の量の差を取る）
        self._written_total = 0                             # la_written（32 ビットで折り返す）を伸ばした総数
        self.usage_history: list[dict] = []                 # usage を呼んだ結果の並び（古い順）
        self._splitter = SegmentSplitter() if events is not None else None
        if gaps and events is None:
            raise RuntimeError("欠けの区間の記録はイベントも流す構成だけ（欠けの記録はイベントの流れで届く）")
        if gaps and gap_hold_bytes is None:
            raise ValueError("gaps の流れは gap_hold_bytes（位置の決まらない拍を持つ上限。既定は持たない）を明示する")
        self._cursor = StreamCursor(rle, pack)              # サンプルの位置の数え（ここ 1 か所。GapPlacer もこれを進める）
        self._placer = GapPlacer(rle, pack, hold_bytes=gap_hold_bytes, cursor=self._cursor) if gaps else None
        self._events_next, self._events_capacity = events if events is not None else (0, 0)
        self._events_received = 0
        # ケイパビリティは 1 回だけ読む（usage が毎回読み直すと UDP で何往復もする）
        self._caps = dev.known_capabilities()
        self._line_time = self._caps.dap_line_time if events is not None else False
        self._la_beats = 0                                  # 受け取ったロジアナの拍（見出しの通し番号と照らす）
        self._received = 0                                  # 受け取ったバイト数
        self._pending = b""                                 # 拍の途中（次の受け取りとつなぐ）
        self._overrun = False
        self._started = time.monotonic()
        self._deadline = float("inf")                       # chunks が duration_s から決める。stop で早める
        self._elapsed: float | None = None
        self._relay: dict | None = None                     # 出し切った後の中継の状態（StreamStats.relay）
        # この流れの間の最大・止めた回数・欠けを測り始める
        self._buffer_start = _stream_buffer(dev, reset_peak=True) if buffer_start is _NOT_GIVEN else buffer_start
        # 1 回の見回りで読む CSR（status から stream_beats まで。gaps の流れは gaps まで）
        self._poll_last = g.LA_DEEP_GAPS if gaps else g.LA_DEEP_STREAM_BEATS
        self._poll_count = (self._poll_last - g.LA_DEEP_STATUS) // 4 + 1

    def chunks(self, duration_s: float, timeout_ms: int = 2000) -> Iterator[StreamChunk]:
        """duration_s 秒受け取ってから止め、残りを出し切るまでチャンクを返す。溢れたら（overrun）その時点で止める。
        途中で抜けると（呼び出し側が閉じる・GC）、機器の書き込みを止めて終わる（残りを読み捨てない: 次の流れの開始
        （DeepAnalyzer._discard_stream）が読み捨てる。閉じる側で時間のかかる読みをしない）。stop（別のスレッドから）で時間の前に
        終えられる（残りも返す）。誤りで抜けるときも書き込みを止める（止める書き込みの失敗は元の誤りに注として足す）。"""
        if self._elapsed is not None:
            raise RuntimeError("このストリームは終わっている")
        self._deadline = min(self._deadline, self._started + duration_s)      # 先に stop されていればそのまま
        running, last_read = True, time.monotonic()
        progress = last_read                                # 最後に進んだ（始めた・止めた・データが届いた）時刻
        completed = False
        try:
            while True:
                status, available, last_beat, reported = self._poll()
                self._overrun |= bool(status >> g.LA_DEEP_STATUS_OVERRUN[0] & 1)
                drained = not running and not status >> g.LA_DEEP_STATUS_READING[0] & 1
                if self._placer is not None:
                    yield from self._chunks_of(self._placer.release(self._placer.received_beats, reported, last_beat), ())
                if running and (self._overrun or time.monotonic() >= self._deadline):
                    self.dev.write32(g.LA_DEEP_CONTROL, 0)
                    running = False
                    progress = time.monotonic()
                # 足りなければ前の読みから READ_INTERVAL_S まで待つ（1 ms ごとに CSR を読み直すと、UDP のリンクでは
                # リンクと機器のロックをほぼ使い続け、ほかの道具が毎回 1 秒待った。2026-10-21 実機）。出し切りの間は短く
                wait = (self.POLL_S if not running else
                        max(self.POLL_S, self.READ_INTERVAL_S - (time.monotonic() - last_read)))
                if not available:
                    if drained:
                        yield from self._finish_gaps(reported, last_beat)   # 欠けの区間を記録する流れ: 最後に読んだ位置で出せる分
                        completed = True
                        return
                    if not running and time.monotonic() - progress > timeout_ms / 1000:
                        # 止めたのに、機器が読み出し中のまま、データも届かない（PS 直読みを提供できない中継の先など）。永久に待たない
                        raise TimeoutError(f"止めた後、機器が読み出し中のまま出し切らない（{timeout_ms} ms 進まない）")
                    time.sleep(wait)
                    continue
                if running and available < self.READ_MIN and time.monotonic() - last_read < self.READ_INTERVAL_S:
                    time.sleep(wait)
                    continue
                try:
                    data = self.dev.read_stream_some(min(available, self.READ_MAX), timeout_ms)
                except TimeoutError as e:
                    if drained:                             # 機器は出し切ったのに、書いたと言う量まで届かない
                        raise self._loss(f"機器は出し切ったが、書いたと言う量のうち {available} バイトが {timeout_ms} ms 届かない"
                                         "（リンクで欠けた）") from e
                    raise
                last_read = progress = time.monotonic()
                taken = self._take_all(data)
                self._check_link()
                yield from taken
        except GeneratorExit:                               # 呼び出し側が抜けた: 書き込みを止めて終わる（読み捨てない）
            if running:
                self.dev.write32(g.LA_DEEP_CONTROL, 0)
            raise
        except BaseException as error:
            if running:
                try:
                    self.dev.write32(g.LA_DEEP_CONTROL, 0)
                except Exception as cleanup:  # noqa: BLE001 - 元の誤りを隠さない（止める書き込みの失敗は注として足す）
                    error.add_note(f"止める書き込み（la_deep.control = 0）も失敗した: {type(cleanup).__name__}: {cleanup}")
            raise
        finally:
            self._elapsed = time.monotonic() - self._started
            if completed:                                   # 出し切ったときだけ中継の状態を問い合わせる（抜ける・誤りの後は待たない）
                get = getattr(self.dev, "relay_status", None)
                try:
                    self._relay = None if get is None else get()
                except OSError:
                    self._relay = None                      # リンクが壊れた後（記録は出し切った分まで）

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

    @property
    def gaps(self) -> tuple[StreamGap, ...]:
        """これまでに位置を決めた欠けの区間（gaps の流れ。古い順）。"""
        return tuple(self._placer.gaps) if self._placer is not None else ()

    def stats(self) -> StreamStats:
        elapsed = time.monotonic() - self._started if self._elapsed is None else self._elapsed
        return StreamStats(self._cursor.samples, self._received, elapsed, self.sample_period_s, self._overrun, self.link_buffer(),
                           gaps=self.gaps, unplaced_beats=self._placer.unplaced_beats if self._placer is not None else 0,
                           relay=self._relay,
                           gap_held_peak_bytes=self._placer.peak_held_bytes if self._placer is not None else 0,
                           control=self.control_counters())

    def control_counters(self) -> dict[str, int]:
        """この流れの間の CONTROL の数え（Device.control_counters の差。事実）。"""
        now = self.dev.control_counters()
        return {name: now[name] - self._control_start[name] for name in now}

    def link_buffer(self) -> StreamBuffer | None:
        """PC の受け取り済みで、まだ読んでいない量（リンクの StreamBuffer）。最大・止めた回数・時間・捨てた数・落ちた数は、この流れを
        始めてからの分。受け取り側に溜める上限の無い道（USB・偽の機器）では None。"""
        now = _stream_buffer(self.dev)
        start = self._buffer_start
        if now is None or start is None:
            return None
        return StreamBuffer(now.capacity_bytes, now.used_bytes, now.peak_bytes, now.full_count - start.full_count,
                            now.full_s - start.full_s, now.backpressure, now.dropped_frames - start.dropped_frames,
                            now.dropped_bytes - start.dropped_bytes, now.lost_frames - start.lost_frames)

    def _check_link(self) -> None:
        """リンクの欠けの数え（この流れの間の差）を見て、欠けた・欠けた可能性があれば StreamLinkLoss（クラスの説明）。"""
        if self._buffer_start is None:
            return
        buf = self.link_buffer()
        if buf.dropped_frames:
            raise self._loss(f"リンクが流れのフレームを {buf.dropped_frames} 個（{buf.dropped_bytes} バイト）捨てた"
                             f"（PC に溜める上限 {buf.capacity_bytes} バイトを超えた）", buf)
        if buf.lost_frames and self._splitter is None:
            raise self._loss(f"流れを運ぶリンクでフレームが {buf.lost_frames} 個以上落ちた・壊れた（どのチャンネルのものかは分からない。"
                             "この流れには番号が無いので、欠けていないと確かめられない）", buf)

    def _loss(self, message: str, buf: StreamBuffer | None = None) -> StreamLinkLoss:
        buf = buf if buf is not None else self.link_buffer()
        lost = {"received_bytes": self._received, "samples": self._cursor.samples}
        if buf is not None:
            lost.update(dropped_frames=buf.dropped_frames, dropped_bytes=buf.dropped_bytes, lost_frames=buf.lost_frames)
        return StreamLinkLoss(message, lost)

    def usage(self) -> dict:
        """流している間のリング（DDR）と帯域の使用量の事実（CSR la_deep.la_written・ring_unread・ring_peak・ring_beats・status を
        1 回ずつ読む。docs/superpowers/specs/2026-11-01-la-deep-fast-design.md §6）。判定はしない（止めるかは使う側が決める。
        本体も自動では止めない）。呼ぶたびに usage_history に足す。

        - ring_beats: リングの拍の数、ring_unread_beats: 書いた − 読んだ（約 16 周期ごとの値）、ring_peak_beats: 流れの開始からの最大
        - encoded_bytes: 符号のバイト数の総数（リングに書いたロジアナの拍 × 16）、received_bytes: PC が受け取ったバイト数（見出し・イベントを含む）
        - encoded_bytes_per_s・received_bytes_per_s: 前の usage から（最初は流れの開始から）の差 ÷ 時間（PC の時計）
        - overrun: 溢れて書き込みを止めた（それまでに届いた分は欠けの無い先頭の部分）
        回路がこの数えを持たない古い構成（ケイパビリティ la_stream_usage が無い）では、その欄を None にし unknown に名前を入れる。"""
        now = time.monotonic()
        out: dict = {"t_s": now - self._started, "received_bytes": self._received, "unknown": []}
        if self._caps.la_stream_usage:
            # status（0x708）から ring_peak（0x73C）までを 1 回で読む（番地の順に読むので値の順は個別の読みと同じ）
            words = self.dev.read(g.LA_DEEP_STATUS, (g.LA_DEEP_RING_PEAK - g.LA_DEEP_STATUS) // 4 + 1)
            at = lambda address: words[(address - g.LA_DEEP_STATUS) // 4]   # noqa: E731
            status, ring = at(g.LA_DEEP_STATUS), at(g.LA_DEEP_RING_BEATS)
            written, unread, peak = at(g.LA_DEEP_LA_WRITTEN), at(g.LA_DEEP_RING_UNREAD), at(g.LA_DEEP_RING_PEAK)
        else:
            written = unread = peak = ring = None
            out["unknown"] += ["ring_beats", "ring_unread_beats", "ring_peak_beats", "encoded_bytes", "encoded_bytes_per_s"]
            status = self.dev.read32(g.LA_DEEP_STATUS)
        out["overrun"] = bool(status >> g.LA_DEEP_STATUS_OVERRUN[0] & 1)
        if written is not None:
            prev_raw = self._usage_prev["written_raw"] if self._usage_prev else 0
            self._written_total += (written - prev_raw) % (1 << 32)
            out.update(ring_beats=ring, ring_unread_beats=unread, ring_peak_beats=peak,
                       encoded_bytes=self._written_total * BEAT_BYTES)
        else:
            out.update(ring_beats=None, ring_unread_beats=None, ring_peak_beats=None, encoded_bytes=None)
        prev = self._usage_prev or {"t_s": 0.0, "received_bytes": 0, "encoded_bytes": 0}
        dt = out["t_s"] - prev["t_s"]
        out["interval_s"] = dt
        out["received_bytes_per_s"] = (out["received_bytes"] - prev["received_bytes"]) / dt if dt > 0 else None
        out["encoded_bytes_per_s"] = ((out["encoded_bytes"] - (prev["encoded_bytes"] or 0)) / dt
                                      if dt > 0 and out["encoded_bytes"] is not None else None)
        self._usage_prev = {**out, "written_raw": written if written is not None else 0}
        self.usage_history.append(out)
        return out

    def sample_time(self, s: int, start_time: int | None = None) -> dict:
        """流れのサンプル s（先頭 = 0）の値に変わったピンの変化の時刻（細かいサンプルの辺の番号。1 辺 = 1 / la_sample_hz）。
        1 周期に複数のサンプルの構成だけ（fine_timing）。式は la_lane_encoder: n = start_time × lanes + s × divisor − latency
        （変化は辺 n − divisor + 1〜n の後のどこか）。start_time は CSR la_deep.start_time（None なら読む）。
        欠けの区間を記録する流れ（gaps）でも同じ式: s は欠けを数えた位置（StreamChunk.first・StreamGap.start。回路の符号器は欠けの
        間も止まらず、捨てたのは符号の後の拍なので、欠けの後のサンプルの番号は止めずに流した場合と同じ）。
        返す: {"edge": n, "edge_lo": 変化が起きえた最初の辺の後, "edge_hi": n, "edge_hz": la_sample_hz の辺の周波数}。"""
        if self.fine_timing is None:
            raise RuntimeError("この流れは 1 周期 1 サンプル（細かいサンプルの時刻の式は無い）")
        if start_time is None:
            if not self.dev.read32(g.LA_DEEP_INFO) >> g.LA_DEEP_INFO_START_VALID[0] & 1:
                raise RuntimeError("起点の時刻がまだ無い（流れの最初の語を符号にする前）")
            lo, hi = self.dev.read(g.LA_DEEP_START_TIME_LO, 2)
            start_time = hi << 32 | lo
        t = self.fine_timing
        n = start_time * t["lanes"] + s * t["divisor"] - t["latency"]
        return {"edge": n, "edge_lo": n - t["divisor"] + 1, "edge_hi": n, "edge_hz": round(1 / self.sample_period_s * t["divisor"])}

    def _poll(self) -> tuple[int, int, int, int]:
        """1 回の見回りの CSR（1 つの READ）: (status, 書かれたが受け取っていないバイト数, gap_last_beat, gaps)。
        stream_beats は 32 ビットで折り返す。gaps の流れでなければ後ろの 2 つは 0。"""
        words = self.dev.read(g.LA_DEEP_STATUS, self._poll_count, timeout_retries=self._control_read_retries)
        status, total = words[0], words[(g.LA_DEEP_STREAM_BEATS - g.LA_DEEP_STATUS) // 4]
        beats = (total - self._received // BEAT_BYTES) % (1 << 32)
        available = beats * BEAT_BYTES - self._received % BEAT_BYTES
        if self._placer is None:
            return status, available, 0, 0
        return status, available, words[(g.LA_DEEP_GAP_LAST_BEAT - g.LA_DEEP_STATUS) // 4], words[-1]

    def _take(self, data) -> StreamChunk | None:
        self._received += len(data)
        events: tuple[DapEvent, ...] = ()
        if self._splitter is not None:
            data, events = self._split(data)
            if not data:
                return (StreamChunk(b"", self.rle, self._cursor.position, self._cursor.previous, events, self.pack, count=0)
                        if events else None)
        else:
            data = self._whole_beats(data)
        if not data:
            return None
        first, previous, count = self._cursor.take(data)
        return StreamChunk(data, self.rle, first, previous, events, self.pack, count=count)

    def _whole_beats(self, data):
        """見出しの無い流れ: 拍の境に揃える（前の受け取りの拍の途中をつなぎ、末尾の拍の途中は次へ）。揃っていれば写さない。"""
        if self._pending:
            data = self._pending + bytes(data)
        whole = len(data) - len(data) % BEAT_BYTES
        if whole == len(data):
            self._pending = b""
            return data
        self._pending = bytes(data[whole:])
        return memoryview(data)[:whole] if whole else b""

    def _take_all(self, data) -> list[StreamChunk]:
        """受け取ったバイト列のチャンク（欠けの区間を記録する流れは、位置の決まった分を欠けで分けたもの。ほかは _take の 1 つ）。
        欠けの区間の流れの位置は、次の見回りの CSR（受け取った後に読む gap_last_beat・gaps）で決まる（chunks が release する）。"""
        if self._placer is None:
            chunk = self._take(data)
            return [] if chunk is None else [chunk]
        self._received += len(data)
        la, events = self._split(data)
        self._placer.add_beats(la)
        if not events:
            return []
        return [StreamChunk(b"", self.rle, self._cursor.position, self._cursor.previous, events, self.pack, count=0)]

    def _finish_gaps(self, reported: int, last_beat: int) -> list[StreamChunk]:
        if self._placer is None:
            return []
        return self._chunks_of(self._placer.finish(reported, last_beat), ())

    def _chunks_of(self, pieces: list[tuple], events: tuple[DapEvent, ...]) -> list[StreamChunk]:
        """GapPlacer の並び → チャンク。欠けは直後のチャンクの gap に載せる（欠けで終わるなら中身の無いチャンク）。
        イベントは最初のチャンクに載せる。"""
        out: list[StreamChunk] = []
        gap = None
        for piece in pieces:
            if piece[0] == "gap":
                if gap is not None:                         # 欠けが続く（同じ位置の続きの件）: 前の欠けを中身なしで出す
                    out.append(StreamChunk(b"", self.rle, gap.start + gap.samples, None, (), self.pack, gap, count=0))
                gap = piece[1]
                continue
            _, data, first, previous, count = piece
            out.append(StreamChunk(data, self.rle, first, previous, (), self.pack, gap, count=count))
            gap = None
        if gap is not None:
            out.append(StreamChunk(b"", self.rle, gap.start + gap.samples, None, (), self.pack, gap, count=0))
        if events:
            if out:
                out[0] = dataclasses.replace(out[0], events=events)
            else:
                out.append(StreamChunk(b"", self.rle, self._cursor.position, self._cursor.previous, events, self.pack, count=0))
        return out

    def _split(self, data) -> tuple[bytes, tuple[DapEvent, ...]]:
        """見出し付きの流れを、ロジアナの拍（つないだもの。かたまりが 1 つなら写さない）とイベントに分ける。通し番号を確かめる。"""
        la, events = [], []
        la_beats = self._la_beats
        for seg in self._splitter.feed(data):
            beats = len(seg.data) // BEAT_BYTES
            if seg.kind == g.STREAM_SEG_LA:
                if seg.seq != la_beats & 0xFFFFFFFF:
                    self._la_beats = la_beats
                    raise StreamFormatError(f"ロジアナの拍の番号が飛んだ: {seg.seq}（{la_beats} のはず）")
                la_beats += beats
                la.append(seg.data)
            else:
                expected = self._events_next % self._events_capacity
                if seg.seq != expected:
                    self._la_beats = la_beats
                    raise StreamFormatError(f"イベントの位置が飛んだ: {seg.seq}（{expected} のはず。"
                                            "流している間にイベント記録を始め直した?）")
                self._events_next += beats
                self._events_received += beats
                for e in decode_events(seg.data, self._line_time):
                    if self._placer is not None and e.kind == KIND_LA_GAP:
                        self._placer.add_record(e.address, e.data, e.time)   # ロジアナの欠けの記録（DAP のイベントではない）
                    else:
                        events.append(e)
        self._la_beats = la_beats
        if not la:
            return b"", tuple(events)
        return (la[0] if len(la) == 1 else b"".join(la)), tuple(events)


# PC の受け取り済みのストリームを溜める上限（EthLink.open_stream_tcp の buffer_bytes。Session.open_eth が渡す）。
# 根拠（docs/superpowers/specs/2026-11-01-bandwidth-observation-design.md 1.7.7）: 消費側（LaStream.chunks）は 1 回に最大 READ_MAX を
# 読む。その 1 回を処理している間に、次の 1 回分（READ_MAX）が届いて溜まれば、消費側が付いていける限り受け取りは止まらない
# （二重のバッファ）。それより多く溜めても、消費側が遅れている時に止まるのが遅れるだけで、溢れるかどうかは変わらない
# （遅れを溜める場所は機器の DDR のリング。溢れれば機器が overrun を記録する）。値は速さを左右するだけで、正しさは左右しない。
STREAM_BUFFER_BYTES = 2 * LaStream.READ_MAX


def _stream_buffer(dev, reset_peak: bool = False) -> StreamBuffer | None:
    """機器（Device）のリンクの StreamBuffer。測れない機器（USB・偽の機器）なら None。"""
    get = getattr(dev, "stream_buffer", None)
    return None if get is None else get(reset_peak=reset_peak)
