"""ケイパビリティ（TLV）の解析。未知の種類は読み飛ばす。

Capabilities の各欄の既定の値は「その TLV が無い（申告の無い古いデバイス）」ときの意味で、ここ 1 か所で決める
（parse は届いた TLV を欄に写すだけ。ほかの欄から決まる欄は Capabilities.__post_init__ と property が決める）。
使う側は欄を直接読む（getattr で既定の値を渡さない: 打ち間違いが黙って既定の値になり、偽物と本物のずれも隠れる）。

仕様: docs/superpowers/specs/2026-10-15-m3-control-plane-design.md §6
"""

import struct
from typing import Literal
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import generated as g

__all__ = ["Build", "Capabilities", "LaChannel", "Output", "TimingPoint", "legacy_la_layout", "parse", "total_length"]


@dataclass(frozen=True)
class Output:
    """クロストリガの動作「出力の端子」の端子の候補 1 本（index = CSR xtrig_out の ctrl.pin の番号）。"""
    index: int
    channel: int          # その端子のロジアナのチャンネル
    pull_up: bool         # 外せないプルアップがある（動いていない間も弱く High）


@dataclass(frozen=True)
class LaChannel:
    """ロジアナの ch 1 本（ケイパビリティ LA_LAYOUT。U22）。kind: "pin" = 拡張ヘッダ（デバッグコネクタ）のピン
    （index = コネクタの番号。デバッグの線と兼用）、"internal" = 内部の信号（0 = プローブが SWDIO を駆動中、
    1 = DAP の処理中）、"input" = 専用の入力（index = 入力の番号）、"none" = 何もつながっていない（常に 0）。"""
    channel: int
    kind: Literal["pin", "internal", "input", "none"]
    index: int

    @property
    def name(self) -> str:
        """波形・VCD の既定の名前。"""
        if self.kind == "internal":
            return _INTERNAL_NAMES.get(self.index, f"internal{self.index}")
        if self.kind == "input":
            return f"in{self.index}"
        return f"ch{self.channel}"


@dataclass(frozen=True)
class TimingPoint:
    """1 つの情報源の時刻の意味（TLV TIMING の 1 件。正は gateware/fprobe_gw/timing.py）: 時刻 = 点 + delay（unit の単位。
    "tick" = タイムベースの刻み、"cap_cycle" = 取り込みの領域の周期、"fine_sample" = 1 周期に複数のサンプルの細かいサンプル）。
    非同期の入力は、点が 時刻 − delay 以上 時刻 − delay + span 未満のどこか（span = 0 なら時刻 − delay ちょうど）。delay・span が None = 回路で決まっていない。
    excludes = 時刻に含まない項目（"input_routing" 等。分からない部分の名前）。"""
    source: str
    point: str
    unit: str
    delay: int | None
    span: int | None
    excludes: tuple[str, ...]


def _names(prefix: str) -> dict[int, str]:
    return {getattr(g, k): k[len(prefix):].lower() for k in dir(g) if k.startswith(prefix)}


_TIMING_SOURCES, _TIMING_POINTS, _TIMING_UNITS = _names("TIMING_SRC_"), _names("TIMING_POINT_"), _names("TIMING_UNIT_")
_TIMING_EXCLUDES = _names("TIMING_EXCLUDES_")


def _timing(value: bytes) -> dict[str, TimingPoint]:
    out = {}
    for src, point, unit, excludes, delay, span in struct.iter_unpack("<BBBBhH", value[:len(value) // 8 * 8]):
        if src not in _TIMING_SOURCES:                      # 知らない情報源（新しい回路）は読み飛ばす
            continue
        name = _TIMING_SOURCES[src]
        out[name] = TimingPoint(name, _TIMING_POINTS.get(point, f"point{point}"), _TIMING_UNITS.get(unit, f"unit{unit}"),
                                None if delay == g.TIMING_UNKNOWN_DELAY else delay,
                                None if span == g.TIMING_UNKNOWN_SPAN else span,
                                tuple(n for bit, n in sorted(_TIMING_EXCLUDES.items()) if excludes & bit))
    return out


def _dap_output_ticks(timing: dict[str, TimingPoint]) -> int | None:
    """TLV TIMING の DAP の情報源（dap_swd・dap_jtag）から、端子までの段数（刻み）。申告が無い・揃わない・分からなければ None。"""
    delays = {p.delay for name, p in timing.items() if name in ("dap_swd", "dap_jtag")}
    if len(delays) != 1 or None in delays:
        return None
    return -delays.pop()


_INTERNAL_NAMES = {0: "probe_drives_swdio", 1: "dap_busy"}
_LA_KINDS = {g.LA_CH_NONE: "none", g.LA_CH_PIN: "pin", g.LA_CH_INTERNAL: "internal", g.LA_CH_INPUT: "input"}


def legacy_la_layout(channels: int) -> tuple[LaChannel, ...]:
    """LA_LAYOUT の無い古いデバイス（U22 の前）の並び: ch 0〜7 = ピン、8・9 = 内部、残りは無し。"""
    def one(ch: int) -> LaChannel:
        if ch < 8:
            return LaChannel(ch, "pin", ch)
        if ch < 10:
            return LaChannel(ch, "internal", ch - 8)
        return LaChannel(ch, "none", 0)
    return tuple(one(ch) for ch in range(channels))


@dataclass(frozen=True)
class TokenInfo:
    """TLV LA_RLE_TOKEN_INFO: RLE のトークン形式の中身（docs/superpowers/specs/2026-11-07-la-rle2-design.md §7.2）。"""
    version: int          # 形式の版（ホストが知っているのは fprobe.la_tokens.TOKEN_FORMAT_VERSION）
    repeat: bool          # REPEAT を出す回路か
    window: bool          # 使う ch の窓を絞れるか
    accept_bytes: int     # 1 周期に符号器が受けるバイト数の上限


@dataclass(frozen=True)
class Build:
    """ビットストリームの版（U49。fprobe_gw build がビルドのたびに書く）。id は入力の要約（ビルドディレクトリの
    inputs.sha256）の先頭 16 桁で、同じ入力なら同じ。git はリポジトリの HEAD の先頭 8 桁（取れなければ None）。"""
    id: str
    time: int                      # ビルドした時刻（UNIX 秒）
    git: str | None
    dirty: bool                    # ゲートウェアにコミットしていない変更があった

    def describe(self) -> str:
        when = datetime.fromtimestamp(self.time, timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        git = "" if self.git is None else f" git {self.git}{'+変更あり' if self.dirty else ''}"
        return f"{self.id}（{when} ビルド{git}）"


@dataclass(frozen=True)
class Capabilities:
    protocol_version: int | None = None
    board: str | None = None
    design: str | None = None
    sys_clock_hz: int | None = None
    timebase_hz: int | None = None
    host_link: int | None = None
    channels: frozenset[int] = frozenset()
    la_channels: int = 0
    la_depth: int = 0
    la_sample_hz: int = 0
    mem_bytes: int = 0             # 外部メモリ（深いサンプルバッファ）の容量。0 なら無し
    mem_test: bool = False         # メモリ試験回路がある（基板立ち上げ用の構成）
    xtrig: bool = True             # クロストリガ（FEATURES が無い古いデバイスは全部ある）
    trig_seq: bool = True          # 多段トリガ
    aux: bool = False              # 補助マイコンの計測値（FEATURES に bit が無ければ無い）
    sampler: bool = False          # DAP サンプラ（変数のライブ観測。FEATURES に bit が無ければ無い）
    jtag: bool = True              # JTAG の PHY（DEBUG が無い古いデバイスはある）
    # 故障注入 = 割り込み書き込み。None（DEBUG が無い古いデバイス）は __post_init__ がクロストリガ（xtrig）と同じにする:
    # 古い回路では故障注入はクロストリガの一部。作った後は必ず bool
    inject: bool | None = field(default=None, metadata={"api_type": bool})   # 応答（作った後）は必ず bool
    outputs: tuple[Output, ...] = ()   # クロストリガの出力の端子の候補（OUTPUTS が無ければ無い）
    output_actions: int = 0            # 同時に動かせる出力の動作の数
    la_filter: bool = False            # ロジアナのグリッチフィルタ（U21。FEATURES に bit が無ければ無い）
    la_pulse: bool = False             # ロジアナのパルス幅のトリガ（U21）
    la_stream_events: bool = False     # 深いバッファのストリームにイベントも見出し付きで流す（R1。FEATURES に bit が無ければ無い）
    la_stream_ps: bool = False         # ストリームを PS が DDR のリングを直接読んで流せる（PYNQ。FEATURES に bit が無ければ無い）
    la_pack: bool = False              # ストリームで使うチャンネルだけ詰められる（CSR la_deep.control.pack。FEATURES に bit が無ければ無い）
    la_testgen: bool = False           # 試験用の方形波（CSR la_testgen。FEATURES に bit が無ければ無い）
    fpga_obs: bool = False             # 対象の FPGA の観測（CSR obs・イベント 8・9。FEATURES に bit が無ければ無い）
    obs_match: bool = False            # 観測の値の一致をクロストリガの源 bit4 に（CSR obs.match。FEATURES bit8）
    sampler_match: bool = False        # サンプラの値の一致をクロストリガの源 bit5 に（CSR sampler_match。FEATURES bit10。構成によって無い）
    sync: bool = False                 # ボードの間の時刻の同期（CSR sync・イベント 10。FEATURES bit9）
    event_byte_pack: bool = False      # UART・SWO のバイトのイベントを詰めた形（種類 13〜15）で出す（FEATURES bit14。無ければ
                                       # 古い形 3〜5。fprobe.events はどちらも読む）
    watch_start: bool = False          # 割り込み書き込みの始動の列が成功したら値の一致の門を開ける経路（FEATURES bit18。
                                       # docs/superpowers/specs/2026-11-03-halt-watch-fpga-restart-design.md。無ければその CSR は無い）
    dap_line_time: bool = False        # DAP の時刻が線上の点（FEATURES bit16。docs/superpowers/specs/2026-11-01-time-accuracy-design.md
                                       # 段階 S2: 種類 1・7・11・12 の時刻の意味、種類 16・17・18、CSR sampler.check。無ければ古い意味）
    la_delay_internal: int = 0         # ロジアナの時刻がピンの変化より遅い段数（刻み。内蔵。LA_DELAY が無ければ 0 = 直さない）
    la_delay_deep: int = 0             # 同（深いバッファ）
    la_phase_span: int = 0             # 非同期のピンを取り込む位相の幅（刻み）: ピンの変化は 時刻 − 段数 以上 + この幅 未満
    # ロジアナの ch ごとの時刻（LA_CH_TIMING。ch の並びの順に (内蔵のずれ, 深いバッファのずれ, 位相の幅)、刻み）: ch の変化 =
    # 時間軸の時刻（ピンの時刻）+ ずれ。None = 回路で決まっていない・確かめていない。無い古いデバイスは ()（ch を区別しない）
    la_channel_timing: tuple[tuple[int | None, int | None, int | None], ...] = ()
    build: Build | None = None         # ビットストリームの版（U49。BUILD が無い・版なしのビルドなら None）
    # ロジアナの ch の並び（la_channels 本）。空（LA_LAYOUT が無い古いデバイス）は __post_init__ が古い並び（legacy_la_layout）にする
    la_layout: tuple[LaChannel, ...] = ()
    swo_manchester: bool = False       # SWO の Manchester 方式（U51。DEBUG に bit が無ければ無い）
    jtag_bit_extra_cycles: int = 1     # JTAG の 1 ビット = 2 × half_period + この値（sys の周期）。DEBUG の bit3 があれば 0、
                                       # 無い古いデバイスは 1（2026-11-01 より前の jtag_phy）。TCK = sys / (2 × half_period + この値)
    min_half_period: int = 2           # SWCLK / TCK の half_period の下限（DEBUG の bit4 があれば 1 = sys / 2、無い古いデバイスは 2）。
                                       # SWD の取り込みの遅れの上限も同じ版で 3 → 7（swd_sample_delay_max）
    swd_sample_delay_max: int = 3      # CSR debug.sample_delay.swd の上限
    uart_flow: tuple[int, ...] = ()    # UART の RTS/CTS の端子の候補のロジアナの ch（U52。UART_FLOW が無ければ回路が無い）
    # 試験用の方形波を刻むクロック（Hz。LA_TESTGEN_HZ。0 = 申告が無い古いデバイス: la_sample_hz が sys の整数倍なら la_sample_hz、
    # そうでなければ sys_clock_hz）
    la_testgen_hz: int = 0
    # 内蔵バッファで la_sample_hz の細かさで取る ch のビットマップ（LA_FINE）。None = 申告が無い = 全部の ch。
    # ほかの ch は sys の周期で取り、1 周期の la_sample_hz / sys_clock_hz 個のサンプルに同じ値が並ぶ
    la_fine: int | None = None
    # 深いバッファの垂れ流しを、この構成で持続できると測った速さ（B/s。LA_STREAM_BYTES_PER_S。0 = 申告が無い）。
    # ホストは RAW の流れがこれを超える組を始める前に断る（DeepAnalyzer.stream の max_bytes_per_s で使う側が上書きできる）
    la_stream_bytes_per_s: int = 0
    la_stream_usage: bool = False      # 深いバッファのリングと符号の数え（CSR la_deep.la_written 等。FEATURES bit15。無ければ古い回路）
    la_stream_gaps: bool = False       # 深いバッファのストリームで溢れても止めず欠けの区間を記録して続けられる（CSR la_deep.control.gaps。
                                       # FEATURES bit17。無ければ溢れたら止まる形だけ）
    # 深いバッファの垂れ流し RLE にトークン形式（変化 1 回がふつう 1 バイト。CSR la_deep.control.rle_fmt・tick。
    # docs/superpowers/specs/2026-11-07-la-rle2-design.md）がある（FEATURES bit19 LA_RLE_TOKENS）。中身は la_rle_token_info
    # （TLV LA_RLE_TOKEN_INFO。形式の版をホストが知らなければ使わない: DeepAnalyzer.stream が断る）
    la_rle_tokens: bool = False
    la_rle_token_info: "TokenInfo | None" = None
    # 情報源ごとの時刻の点（TLV TIMING。情報源の名前 → TimingPoint）。空 = 申告の無い古いデバイス、申告の無い情報源は
    # その版の回路のいまの意味（fprobe.timing が持つ。docs/superpowers/specs/2026-11-01-time-accuracy-design.md §4.2）
    timing: dict[str, TimingPoint] = field(default_factory=dict)
    unknown: dict[int, bytes] = field(default_factory=dict)
    # DAP の線上の点（rsp_time を数える PHY の出力の FF）から端子までの段数（刻み。None = 申告が無い = 分からない）。
    # TLV TIMING の情報源 dap_swd・dap_jtag の delay の符号を反した値（時刻 = 端子の辺 − 段数）。申告した DAP の情報源の
    # 段数が揃わない・分からないものがあれば None（どちらかを選ばない）。timing から決まる（作るときに渡せない）
    dap_output_ticks: int | None = field(init=False)

    def __post_init__(self):
        if self.inject is None:                             # DEBUG が無い古いデバイス: 故障注入はクロストリガの一部
            object.__setattr__(self, "inject", self.xtrig)
        if not self.la_layout and self.la_channels:         # LA_LAYOUT が無い古いデバイス
            object.__setattr__(self, "la_layout", legacy_la_layout(self.la_channels))
        object.__setattr__(self, "dap_output_ticks", _dap_output_ticks(self.timing))


_U32 = {g.CAPS_PROTOCOL_VERSION: "protocol_version", g.CAPS_SYS_CLOCK_HZ: "sys_clock_hz",
        g.CAPS_TIMEBASE_HZ: "timebase_hz", g.CAPS_HOST_LINK: "host_link",
        g.CAPS_LA_CHANNELS: "la_channels", g.CAPS_LA_DEPTH: "la_depth", g.CAPS_LA_SAMPLE_HZ: "la_sample_hz",
        g.CAPS_MEM_BYTES: "mem_bytes", g.CAPS_LA_TESTGEN_HZ: "la_testgen_hz", g.CAPS_LA_FINE: "la_fine",
        g.CAPS_LA_STREAM_BYTES_PER_S: "la_stream_bytes_per_s"}
_TEXT = {g.CAPS_BOARD: "board", g.CAPS_DESIGN: "design"}
_BUILD_FLAG_GIT, _BUILD_FLAG_DIRTY = 1 << 0, 1 << 1     # fprobe_gw.build_info の旗


def total_length(header: bytes) -> int:
    """先頭 8 バイトから総バイト数を得る（magic を検査する）。"""
    magic, total = struct.unpack_from("<II", header)
    if magic != g.CAPS_MAGIC:
        raise ValueError(f"ケイパビリティの magic が不正: 0x{magic:08X}")
    return total


def parse(data: bytes) -> Capabilities:
    total = total_length(data)
    if total > len(data):
        raise ValueError(f"ケイパビリティが途中で切れている（{len(data)}/{total} バイト）")
    values: dict = {"unknown": {}}
    pos = 8
    while pos + 4 <= total:
        kind, length = struct.unpack_from("<HH", data, pos)
        if pos + 4 + length > total:
            raise ValueError(f"ケイパビリティ種類 {kind} が総バイト数 {total} をはみ出す")
        value = data[pos + 4:pos + 4 + length]
        pos += 4 + length + (-length % 4)
        if kind == g.CAPS_END:
            break
        if (kind in _U32 or kind == g.CAPS_CHANNELS) and length != 4:
            raise ValueError(f"ケイパビリティ種類 {kind} の長さ {length} が 4 でない")
        if kind in _U32:
            values[_U32[kind]] = struct.unpack("<I", value)[0]
        elif kind in _TEXT:
            values[_TEXT[kind]] = value.decode()
        elif kind == g.CAPS_MEM_TEST:
            values["mem_test"] = True
        elif kind == g.CAPS_FEATURES:
            features = struct.unpack("<I", value)[0]
            values["xtrig"] = bool(features & g.FEATURE_XTRIG)
            values["trig_seq"] = bool(features & g.FEATURE_TRIG_SEQ)
            values["aux"] = bool(features & g.FEATURE_AUX)
            values["sampler"] = bool(features & g.FEATURE_SAMPLER)
            values["la_filter"] = bool(features & g.FEATURE_LA_FILTER)
            values["la_pulse"] = bool(features & g.FEATURE_LA_PULSE)
            values["la_stream_events"] = bool(features & g.FEATURE_LA_STREAM_EVENTS)
            values["la_stream_ps"] = bool(features & g.FEATURE_LA_STREAM_PS)
            values["la_pack"] = bool(features & g.FEATURE_LA_PACK)
            values["la_stream_usage"] = bool(features & g.FEATURE_LA_STREAM_USAGE)
            values["la_stream_gaps"] = bool(features & g.FEATURE_LA_STREAM_GAPS)
            values["la_testgen"] = bool(features & g.FEATURE_LA_TESTGEN)
            values["fpga_obs"] = bool(features & g.FEATURE_FPGA_OBS)
            values["obs_match"] = bool(features & g.FEATURE_OBS_MATCH)
            values["sampler_match"] = bool(features & g.FEATURE_SAMPLER_MATCH)
            values["sync"] = bool(features & g.FEATURE_SYNC)
            values["event_byte_pack"] = bool(features & g.FEATURE_EVENT_BYTE_PACK)
            values["dap_line_time"] = bool(features & g.FEATURE_DAP_LINE_TIME)
            values["watch_start"] = bool(features & g.FEATURE_WATCH_START)
            values["la_rle_tokens"] = bool(features & g.FEATURE_LA_RLE_TOKENS)
        elif kind == g.CAPS_LA_RLE_TOKEN_INFO and length == 4:
            word = struct.unpack("<I", value)[0]
            values["la_rle_token_info"] = TokenInfo(
                version=word & 0xFF, repeat=bool(word & g.LA_RLE_TOKEN_REPEAT), window=bool(word & g.LA_RLE_TOKEN_WINDOW),
                accept_bytes=word >> g.LA_RLE_TOKEN_ACCEPT_BYTES_SHIFT & 0xFF)
        elif kind == g.CAPS_BUILD and length >= 20:
            when, id_hi, id_lo, commit, flags = struct.unpack_from("<5I", value)
            if when:                                    # 0 は版なし（シミュレーション用の既定のまま）
                values["build"] = Build(id=f"{id_hi << 32 | id_lo:016x}", time=when,
                                        git=f"{commit:08x}" if flags & _BUILD_FLAG_GIT else None,
                                        dirty=bool(flags & _BUILD_FLAG_DIRTY))
        elif kind == g.CAPS_DEBUG and length == 4:
            debug = struct.unpack("<I", value)[0]
            values["jtag"] = bool(debug & g.DEBUG_JTAG)
            values["inject"] = bool(debug & g.DEBUG_INJECT)
            values["swo_manchester"] = bool(debug & g.DEBUG_SWO_MANCHESTER)
            values["jtag_bit_extra_cycles"] = 0 if debug & g.DEBUG_JTAG_BIT_2HP else 1
            values["min_half_period"] = 1 if debug & g.DEBUG_HALF_PERIOD_1 else 2
            values["swd_sample_delay_max"] = 7 if debug & g.DEBUG_HALF_PERIOD_1 else 3
        elif kind == g.CAPS_LA_DELAY and length >= 6:
            values["la_delay_internal"], values["la_delay_deep"], values["la_phase_span"] = struct.unpack_from("<HHH", value)
        elif kind == g.CAPS_LA_CH_TIMING:
            values["la_channel_timing"] = tuple(
                (None if a == g.LA_CH_TIMING_UNKNOWN_SKEW else a, None if b == g.LA_CH_TIMING_UNKNOWN_SKEW else b,
                 None if span == g.LA_CH_TIMING_UNKNOWN_SPAN else span)
                for a, b, span in struct.iter_unpack("<bbB", value[:len(value) // 3 * 3]))
        elif kind == g.CAPS_TIMING:
            values["timing"] = _timing(value)
        elif kind == g.CAPS_UART_FLOW:
            values["uart_flow"] = tuple(value)
        elif kind == g.CAPS_OUTPUTS and length >= 1:
            values["output_actions"] = value[0]
            values["outputs"] = tuple(Output(k, b & g.OUTPUT_CHANNEL_MASK, bool(b & g.OUTPUT_PULL_UP))
                                      for k, b in enumerate(value[1:]))
        elif kind == g.CAPS_LA_LAYOUT:
            values["la_layout"] = tuple(LaChannel(ch, _LA_KINDS.get(b >> 4, "none"), b & 0xF)
                                        for ch, b in enumerate(value))
        elif kind == g.CAPS_CHANNELS:
            mask = struct.unpack("<I", value)[0]
            values["channels"] = frozenset(i for i in range(32) if mask >> i & 1)
        else:
            values["unknown"][kind] = value
    else:
        raise ValueError("ケイパビリティに終端が無い")
    return Capabilities(**values)
