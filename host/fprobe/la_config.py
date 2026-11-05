"""ロジアナの設定の部品: ch の並び・トリガ ch の検査・前段の条件（フィルタ・パルス幅）・サンプル間隔と詰め方の計算。

デバイスに触れない純粋な計算と値の検査（LogicAnalyzer・DeepAnalyzer が使う）。fprobe.la が再公開する。
"""

import math
from dataclasses import dataclass

from . import generated as g
from .caps import LaChannel
from .device import Device

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
_RATE_DIVIDER_MAX = 1 << 16            # CSR la.rate（サンプルの間隔の分周）の最大値（16 ビットの欄）
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
    if di % lanes == 0 and di // lanes <= _RATE_DIVIDER_MAX:
        return di, log_lanes, di // lanes - 1
    raise ValueError(f"サンプルの間隔 {di}（細かいサンプル）はこの構成では作れない: {lanes} より小さい 2 の累乗か {lanes} の倍数"
                     f"（{lanes} × {_RATE_DIVIDER_MAX} まで）。作れる近いレートを指定する")


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


def pin_time(trigger_time: int, delay: int, span: int) -> int:
    """FPGA がトリガのサンプルに付けた時刻を、ピンの変化の時刻の下限に直す（delay = ケイパビリティ LA_DELAY の段数、
    span = 位相の幅。ピンの変化は 返す時刻 以上 返す時刻 + span 未満のどこか。どこかは測っていない）。

    時刻 T の瞬間 = タイムベースが T になる立ち上がり。回路の段数は「時刻が T になる辺の直後（周期 [T, T + 1) の中）に変わった
    入力 → 時刻 T + delay」で数えるので、変化は 時刻 − delay 以上 時刻 − delay + span 未満（gateware の la_capture_tb が
    シミュレーションの ps で確かめる）。2026-11-02（時刻の正しさ S1b）までは 時刻 − delay − span を返していた（1 刻み前）。
    span は範囲の幅で、下限には入らない（呼び手が範囲 [返す時刻, 返す時刻 + span) を返すときの約束として受ける）。"""
    return trigger_time - delay
