"""ケイパビリティ（TLV）の解析。未知の種類は読み飛ばす。

仕様: docs/superpowers/specs/2026-10-15-m3-control-plane-design.md §6
"""

import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import generated as g

__all__ = ["Build", "Capabilities", "LaChannel", "Output", "legacy_la_layout", "parse", "total_length"]


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
    kind: str
    index: int

    @property
    def name(self) -> str:
        """波形・VCD の既定の名前。"""
        if self.kind == "internal":
            return _INTERNAL_NAMES.get(self.index, f"internal{self.index}")
        if self.kind == "input":
            return f"in{self.index}"
        return f"ch{self.channel}"


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
    inject: bool = True            # 故障注入 = 割り込み書き込み（DEBUG が無い古いデバイスはクロストリガがあればある）
    outputs: tuple[Output, ...] = ()   # クロストリガの出力の端子の候補（OUTPUTS が無ければ無い）
    output_actions: int = 0            # 同時に動かせる出力の動作の数
    la_filter: bool = False            # ロジアナのグリッチフィルタ（U21。FEATURES に bit が無ければ無い）
    la_pulse: bool = False             # ロジアナのパルス幅のトリガ（U21）
    la_stream_events: bool = False     # 深いバッファのストリームにイベントも見出し付きで流す（R1。FEATURES に bit が無ければ無い）
    build: Build | None = None         # ビットストリームの版（U49。BUILD が無い・版なしのビルドなら None）
    la_layout: tuple[LaChannel, ...] = ()   # ロジアナの ch の並び（la_channels 本。LA_LAYOUT が無ければ古い並び）
    swo_manchester: bool = False       # SWO の Manchester 方式（U51。DEBUG に bit が無ければ無い）
    uart_flow: tuple[int, ...] = ()    # UART の RTS/CTS の端子の候補のロジアナの ch（U52。UART_FLOW が無ければ回路が無い）
    unknown: dict[int, bytes] = field(default_factory=dict)


_U32 = {g.CAPS_PROTOCOL_VERSION: "protocol_version", g.CAPS_SYS_CLOCK_HZ: "sys_clock_hz",
        g.CAPS_TIMEBASE_HZ: "timebase_hz", g.CAPS_HOST_LINK: "host_link",
        g.CAPS_LA_CHANNELS: "la_channels", g.CAPS_LA_DEPTH: "la_depth", g.CAPS_LA_SAMPLE_HZ: "la_sample_hz",
        g.CAPS_MEM_BYTES: "mem_bytes"}
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
    if "inject" not in values:                          # DEBUG が無い古いデバイス: 故障注入はクロストリガの一部
        values["inject"] = values.get("xtrig", True)
    if "la_layout" not in values:                       # LA_LAYOUT が無い古いデバイス
        values["la_layout"] = legacy_la_layout(values.get("la_channels", 0))
    return Capabilities(**values)
