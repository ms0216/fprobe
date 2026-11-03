"""時刻の事実: 各情報源の時刻が何を意味し、本当の出来事がどの範囲にあるか（推定はしない）。

仕様: docs/superpowers/specs/2026-11-01-time-accuracy-design.md §4.5（段階 S0: いまの回路の意味で、ホストだけで出せるもの。
段階 S1: 回路が記録する連続記録の起点（R1）と、情報源ごとの点の申告 TLV TIMING（UART・SWO の開始ビットの辺。R3））。
値は、回路が申告するケイパビリティ（LA_DELAY・LA_CH_TIMING・TIMING。正は gateware/fprobe_gw/timing.py）と、ホストが書いた設定
（グリッチフィルタの周期・SWO の方式）と、イベントの列だけから出す。ホストに段数の数字は持たない。TIMING の無い古いデバイス
（申告の無い情報源）は、その版の回路の意味（_LEGACY。段数の数字ではなく「どの点か・分からない部分」）で読む。

返す窓は「確かに言える範囲」で、そのどこかは選ばない（4.1 の原則 4）。分からない部分は unknown に名前で返す。

時刻の約束（gateware/fprobe_gw/timing.py の冒頭と同じ）: 時刻 T の瞬間 = タイムベースが T になる立ち上がり。申告の段数 delay は
「時刻が T になる辺の直後（周期 [T, T + 1) の中）に変わった入力 → 時刻 T + delay」なので、非同期の入力の点は
時刻 − delay 以上 時刻 − delay + span 未満（span = 位相の幅）。2026-11-02（段階 S1b、設計書 §10.4 の 2）までは
[時刻 − delay − span, 時刻 − delay) を返していた（1 刻み前。gateware の la_capture_tb・uart_rx_tb がシミュレーションの ps で
確かめて直した）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

__all__ = ["DapConfig", "access_start_bound", "byte_edge_window", "decode_dap_config", "ext_trigger_window",
           "xtrig_out_window",
           "la_channel_skew", "la_channel_timing", "la_trigger_facts", "la_xtrig_source", "posted_read_window",
           "posted_read_window_line", "sampler_window", "seq_fire_window", "skew_of", "source_timing",
           "stream_start_lanes", "stream_start_recorded", "stream_start_window", "swd_header_to_point_ticks"]


# --- ロジアナ: ch ごとの時刻 -------------------------------------------------------------------------------------

def _filter_delay_ticks(caps: Any, front_info: dict | None) -> tuple[set[int], float]:
    """グリッチフィルタをかけた ch と、その ch がさらに遅れる刻み（設定 cycles − 1 周期。周期は取り込みのクロック）。"""
    flt = (front_info or {}).get("filter")
    if not flt:
        return set(), 0.0
    clock = caps.la_sample_hz
    return set(flt["channels"]), flt["delay_cycles"] * caps.timebase_hz / clock


def la_channel_timing(caps: Any, deep: bool, front_info: dict | None = None) -> list[dict]:
    """ロジアナの ch ごとの時刻の事実。ch の変化の時刻 = 時間軸の時刻（ピンの時刻: trigger_time）+ skew_ticks で、
    そこから span_ticks 未満のどこか。skew_ticks が None なら分からない（波形はずらさずに置き、ここで分からないと返す）。

    - from "LA_CH_TIMING": 回路の申告（内部の ch はフィルタを通らない等）。無い古いデバイスは、ピン・専用の入力を時間軸
      そのもの（LA_DELAY の申告の対象）とし、内部の ch は分からない（None）。
    - from "config": グリッチフィルタをかけた ch は cycles − 1 周期さらに遅れる（la_filter.vhd。ホストが書いた設定）。
    """
    from .la import channel_layout                       # la がこのモジュールを読む

    declared = tuple(caps.la_channel_timing)
    filtered, delay = _filter_delay_ticks(caps, front_info)
    out = []
    for c in channel_layout(caps):
        sources: list[str] = []
        if c.channel < len(declared):
            internal, deep_skew, span = declared[c.channel]
            skew = deep_skew if deep else internal
            sources.append("LA_CH_TIMING")
        elif c.kind in ("pin", "input"):
            skew, span = 0, caps.la_phase_span
            sources.append("LA_DELAY")
        else:
            skew, span = None, None
        if c.channel in filtered and skew is not None:
            skew = skew - delay
            sources.append("config")
        out.append({"channel": c.channel, "kind": c.kind, "skew_ticks": skew, "span_ticks": span, "from": sources})
    return out


def la_channel_skew(caps: Any, deep: bool, front_info: dict | None = None) -> tuple[float | None, ...]:
    """la_channel_timing の skew_ticks だけを ch の順に（Capture・Transitions の channel_skew）。"""
    return tuple(row["skew_ticks"] for row in la_channel_timing(caps, deep, front_info))


# --- 情報源ごとの点（TLV TIMING） -----------------------------------------------------------------------------------

# TIMING の申告が無い古い回路（段階 S1 より前）の意味。段数の数字は持たず、どの点か・何が分からないかだけを持つ
# （設計書 §2.1 の表のその版の行）。UART 受信は受け渡しの周期 + 1（ホストへの経路の待ちが入り、記録されない）、
# SWO はバイトを出した周期 + 1（開始の辺からの段数は divisor の式で、ここでは出さない）。
_LEGACY = {
    "uart_rx": {"point": "hand_off", "unit": "tick", "delay": None, "span": None,
                "excludes": ("hand_off_wait", "input_routing")},
    "uart_tx": {"point": "start_bit_edge", "unit": "tick", "delay": 0, "span": 0, "excludes": ("output_delay",)},
    "swo_uart": {"point": "hand_off", "unit": "tick", "delay": None, "span": None,
                 "excludes": ("input_routing", "target_internal")},
    "swo_manchester": {"point": "hand_off", "unit": "tick", "delay": None, "span": None,
                       "excludes": ("input_routing", "target_internal", "sender_rate")},
    # 補助マイコンの組（種類 6）: 段階 S3（B2）の前は組の 3 バイト目を受けた周期（BAUD の式で、ここでは出さない）
    "aux": {"point": "hand_off", "unit": "tick", "delay": None, "span": None,
            "excludes": ("input_routing", "target_internal")},
}
# 古い申告の読み方（段階 S3、設計書 §13）: S3 より前の回路の uart_tx・sync_tx・dap の申告は、PYNQ-Z2 の端子の IOB の FF 1 段を
# 段数に入れていない（DAP は S2 から入れた）。その 1 段は含まない項目 output_delay（FF → 端子）に入っていたことになる:
# 申告の点は「端子の辺はこれ以後」として正しいまま（ちょうどの位置ではない）。新しい回路は段を delay に入れて申告する


def source_timing(caps: Any, source: str) -> dict | None:
    """情報源 source（"uart_rx"・"swo_uart"・"la_deep_start" 等。TLV TIMING の名前）の時刻の意味:
    {"source", "point", "unit", "delay", "span", "excludes", "from": "TIMING" | "legacy"}。申告が無く、古い回路の意味も
    持たない情報源は None（例: 連続記録の起点を記録しない回路の "la_deep_start"）。"""
    declared = caps.timing.get(source)
    if declared is not None:
        return {"source": source, "point": declared.point, "unit": declared.unit, "delay": declared.delay,
                "span": declared.span, "excludes": list(declared.excludes), "from": "TIMING"}
    legacy = _LEGACY.get(source)
    if legacy is None:
        return None
    return {"source": source, **legacy, "excludes": list(legacy["excludes"]), "from": "legacy"}


def byte_edge_window(caps: Any, kind: str, time: int, swo_mode: str | None = None) -> dict:
    """UART・SWO のバイトのイベント（kind "uart_rx" / "uart_tx" / "swo"、time = イベントの時刻）と補助マイコンの組（kind "aux"。
    段階 S3 の B2）について、端子の上の点（UART・補助マイコン: 開始ビットの立ち下がり（補助マイコンは組の先頭のバイト）。
    Manchester: 新しい回路は最初のビットの中央の変化（R4。S1〜S2 の回路は最後のビットの中央。点の名前で返す））の範囲 [t_lo, t_hi) = [time − delay, time − delay + span)
    （span = 0 なら t_lo = t_hi ちょうど）。回路の申告（TLV TIMING）だけから出す。SWO は方式（swo_mode: "uart" / "manchester"。ホストが設定したもの）で
    点が違うので、分からなければ範囲を返さない。返す: {"t_lo", "t_hi", "point", "source", "from", "unknown"}
    （範囲が決まらなければ t_lo = t_hi = None で、unknown に理由の名前）。"""
    if kind == "swo":
        source = {"uart": "swo_uart", "manchester": "swo_manchester"}.get(swo_mode or "")
        if source is None:
            return {"t_lo": None, "t_hi": None, "point": None, "source": None, "from": None, "unknown": ["swo_mode"]}
    elif kind in ("uart_rx", "uart_tx", "aux"):
        source = kind
    else:
        raise ValueError(f"バイトのイベントの種類ではない: {kind!r}")
    info = source_timing(caps, source)
    if info is None or info["delay"] is None or info["span"] is None or info["unit"] != "tick":
        return {"t_lo": None, "t_hi": None, "point": None if info is None else info["point"], "source": source,
                "from": None if info is None else info["from"],
                "unknown": ["line_point"] + ([] if info is None else info["excludes"])}
    lo = time - info["delay"]
    return {"t_lo": lo, "t_hi": lo + info["span"], "point": info["point"], "source": source, "from": info["from"],
            "unknown": info["excludes"]}


# --- ロジアナのトリガ時刻と、クロストリガとの間（段階 S1b） ------------------------------------------------------

def _declared(caps: Any, source: str):
    return caps.timing.get(source)


def la_trigger_facts(caps: Any, buffer: str, raw: int, *, phase: int = 0, latency: int | None = None, rate: int = 0,
                     pack: int = 0) -> dict:
    """ロジアナのトリガ時刻の CSR の生の値 raw（trigger_time）から、トリガのサンプルのピンの時間軸の時刻と、そのトリガを
    クロストリガの源（bit0）にしたときの源のパルスの周期を返す（回路の申告 LA_DELAY・TLV TIMING・CSR の la_cap.info だけから）。

    - buffer: "internal"（内蔵のロジアナ）/ "deep"（深いバッファ）。内蔵で取り込みの領域・1 周期に複数のサンプルの構成
      （la_sample_hz が sys の整数倍）は phase（CSR la_cap.info.trigger_phase = 位相・レーン）と latency（同 latency）が要る。
    - rate: CSR la.rate（サンプルの間隔 − 1）。pack: CSR la_deep.control.pack（深いバッファの詰め。1 語 = 1 << pack サンプル）。

    返す: {"pin_time": トリガのサンプルのピンの変化の時刻の下限（刻み。細かい構成は細かいサンプルを刻みに切り捨てた値）,
    "pin_time_hi": 上限（未満）, "fine_sample": 細かいサンプルの番号 n（細かい構成だけ。辺 n の後に変わった）,
    "xtrig_source_time": 源のパルスの周期（刻み。申告が無ければ None）, "from": [根拠], "unknown": [名前]}。
    詰め（pack ≠ 0）では、トリガのサンプルが語の何番目かは記録されないので、ピンの時刻は範囲（語の分 (k − 1) × (rate + 1) 周期）。"""
    unknown: list[str] = []
    sources: list[str] = []
    sys_hz = caps.sys_clock_hz or 0
    sample_hz = caps.la_sample_hz
    ratio = sample_hz // sys_hz if buffer == "internal" and sys_hz and sample_hz % sys_hz == 0 else 1
    out: dict = {"fine_sample": None}
    if ratio > 1:
        if latency is None:
            raise ValueError("取り込みの領域・1 周期に複数のサンプルの構成は la_cap.info の latency が要る")
        n = raw * ratio + phase - latency                  # ピンが辺 n の後に変わった（細かいサンプル・cap の周期）
        out.update(fine_sample=n, pin_time=n // ratio, pin_time_hi=-(-(n + 1) // ratio))
        sources.append("la_cap.info")
        lanes = caps.la_fine is not None
        x = _declared(caps, "la_lanes_xtrig" if lanes else "la_cap_xtrig")
        if x is not None and x.delay is not None:
            out["xtrig_source_time"] = (n + x.delay) // ratio  # n は 源 × 比 − delay 以上 + span（= 比）未満: 源はただ 1 つ
            sources.append("TIMING")
        else:
            out["xtrig_source_time"] = None
            unknown.append("xtrig_source")
    else:
        deep = buffer == "deep"
        delay = caps.la_delay_deep if deep else caps.la_delay_internal
        span = caps.la_phase_span
        sources.append("LA_DELAY")
        hi_extra = 0
        if deep and pack:
            packed = _declared(caps, "la_deep_packed")
            if packed is None or packed.delay is None:
                out.update(pin_time=None, pin_time_hi=None, xtrig_source_time=raw if _declared(caps, "la_deep_xtrig") else None)
                return {**out, "from": sources, "unknown": ["pack_delay"] + ([] if out["xtrig_source_time"] is not None
                                                                              else ["xtrig_source"])}
            delay = packed.delay
            hi_extra = ((1 << pack) - 1) * (rate + 1)      # トリガのサンプルが語の最も古いものなら、ここまで前
            sources.append("config")
        lo = raw - delay - hi_extra
        out.update(pin_time=lo, pin_time_hi=raw - delay + span)
        x = _declared(caps, "la_deep_xtrig" if deep else "la_xtrig")
        base = _declared(caps, "la_deep" if deep else "la")
        if x is not None and x.delay is not None and base is not None and base.delay is not None:
            out["xtrig_source_time"] = raw + (x.delay - base.delay)
            sources.append("TIMING")
        else:
            out["xtrig_source_time"] = None
            unknown.append("xtrig_source")
    return {**out, "from": sources, "unknown": unknown}


def la_xtrig_source(caps: Any, deep: bool, pin_time: int | None, fine_sample: int | None = None) -> int | None:
    """捕捉したロジアナのトリガ（Capture・Transitions の trigger_time = ピンの時間軸の時刻、細かい構成は trigger_cap_cycles = n）を
    クロストリガの源（bit0）にしたときの源のパルスの周期（刻み）。TLV TIMING の la_xtrig・la_deep_xtrig（点 = トリガのサンプルの
    ピン、時刻 = 源の周期）・la_cap_xtrig・la_lanes_xtrig（n は 源 × 比 − delay 以上 + 比 未満なので源はただ 1 つ）から。
    申告の無い古い回路は None（分からない）。深いバッファは詰めない捕捉（ホストの DeepAnalyzer.start）を前提にする。"""
    if fine_sample is not None:
        sys_hz = caps.sys_clock_hz or 0
        ratio = caps.la_sample_hz // sys_hz if sys_hz else 0
        lanes = caps.la_fine is not None
        x = _declared(caps, "la_lanes_xtrig" if lanes else "la_cap_xtrig")
        if x is None or x.delay is None or ratio < 2:
            return None
        return (fine_sample + x.delay) // ratio
    x = _declared(caps, "la_deep_xtrig" if deep else "la_xtrig")
    if x is None or x.delay is None or pin_time is None:
        return None
    return pin_time + x.delay


def ext_trigger_window(caps: Any, facts: dict, rate: int = 0) -> dict:
    """外部トリガ（クロストリガ → ロジアナ）で捕捉したとき、源のパルスの周期 S の範囲 [t_lo, t_hi]（両端を含む。刻み）。
    回路は外部トリガ（la_ext が 1 の周期 S + XTRIG_LA_EXT_DELAY）を、その周期の頭の後に変わったピンのサンプルに付ける
    （rate > 0 なら、それ以後の最初のサンプル: 待ちは 0〜rate 取り込み周期）。facts = la_trigger_facts の結果（細かい構成は
    fine_sample = n を使う: (S + delay) × 比 は n − rate 以上 n 以下）。rate = CSR la.rate（取り込みの周期の数）。
    外部トリガかパターンかは回路が記録しないので、使う側が自分で仕掛けた条件（external_only 等）で読む。申告（TLV TIMING の
    la_ext）の無い古い回路は外部トリガを揃えていない（範囲を返さない）。"""
    x = _declared(caps, "la_ext")
    if x is None or x.delay is None or facts.get("pin_time") is None:
        return {"t_lo": None, "t_hi": None, "from": None, "unknown": ["ext_trigger_alignment"]}
    n = facts.get("fine_sample")
    if n is not None:
        ratio = max(1, caps.la_sample_hz // (caps.sys_clock_hz or 1))
        lo, hi = -(-(n - rate) // ratio), n // ratio
    else:
        lo, hi = facts["pin_time"] - rate, facts["pin_time"]
    return {"t_lo": lo - x.delay, "t_hi": hi - x.delay, "from": "TIMING", "unknown": []}


def seq_fire_window(caps: Any, seq_time: int, cause: str) -> dict:
    """多段トリガの発火の時刻 seq_time（CSR trigseq.fire_time）から、最後の段を進めた事象の点の範囲 [t_lo, t_hi)（刻み）。
    cause: "pin"（パターン A・B のピン・専用の入力）/ "internal"（内部の ch）/ "input_lanes"（1 周期に複数のサンプルの構成の
    専用の入力）/ "event"（match・host。点 = そのパルスの周期で、t_lo = t_hi）。どの事象が進めたかは回路が記録しないので、
    使う側が最後の段の条件（cond）から選ぶ（複数あれば、それぞれの範囲を並べて返すのは使う側）。申告が無ければ範囲を返さない。"""
    source = {"pin": "seq_pin", "internal": "seq_internal", "input_lanes": "seq_input_lanes", "event": "seq_event"}.get(cause)
    if source is None:
        raise ValueError(f"多段トリガの事象の種類ではない: {cause!r}")
    p = _declared(caps, source)
    if p is None or p.delay is None or p.span is None:
        return {"t_lo": None, "t_hi": None, "point": None, "source": source, "from": None, "unknown": ["seq_timing"]}
    if p.unit == "fine_sample":
        lanes = caps.la_sample_hz // caps.sys_clock_hz
        n_lo = seq_time * lanes - p.delay
        return {"t_lo": n_lo / lanes, "t_hi": (n_lo + p.span) / lanes, "fine_lo": n_lo, "fine_hi": n_lo + p.span,
                "point": p.point, "source": source, "from": "TIMING", "unknown": list(p.excludes)}
    lo = seq_time - p.delay
    return {"t_lo": lo, "t_hi": lo + p.span if p.span else lo, "point": p.point, "source": source, "from": "TIMING",
            "unknown": list(p.excludes)}


# --- ロジアナ: 深いバッファの連続記録のサンプル 0 ---------------------------------------------------------------

def stream_start_recorded(start_time: int, delay: int, span: int, before: int | None = None,
                          after: int | None = None) -> dict:
    """回路が記録した連続記録のサンプル 0 の時刻（CSR la_deep.start_time。段階 S1 の R1。TLV TIMING の la_deep_start を
    申告する回路）を、ピンの時間軸の時刻にする: start_time − delay（トリガ時刻と同じ式 fprobe.la.pin_time。delay・span は申告の
    段数・位相の幅）。1 点で、範囲ではない（ピンの位相の幅は ch ごとの時刻 la_channel_timing が持つ: ピンの変化はこの時刻以上
    + span 未満）。before・after（流し始める前後の CSR の読み）は根拠として残すだけで、範囲には使わない。"""
    t = start_time - delay
    return {"t_lo": t, "t_hi": t,
            "basis": {"start_time": start_time, "before": before, "after": after, "delay_ticks": delay, "span_ticks": span,
                      "bounds": {"t_lo": "start_time", "t_hi": "start_time"}},
            "unknown": []}


def stream_start_lanes(start_time: int, lanes: int, latency: int, before: int | None = None,
                       after: int | None = None) -> dict:
    """1 周期に複数のサンプルの深いバッファ（TLV TIMING の la_deep_lanes。la_lane_encoder）の連続記録のサンプル 0 の時刻: 細かい
    サンプルの辺 n0 = start_time × lanes − latency（サンプル s は n0 + s × 間隔。ピンは辺 n の後に変わった = n 以上 n + 1 未満）。
    記録の時刻（刻み）は整数なので、刻みに直すと端数が出る: t_lo = 切り捨て、t_hi = 切り上げ（n0 が刻みの倍数なら同じ）。
    範囲は推定ではなく、回路の 1 点を刻みに丸めたときの丸めの幅（端数そのものは fine_sample・lanes で返す）。"""
    n0 = start_time * lanes - latency
    return {"t_lo": n0 // lanes, "t_hi": -(-n0 // lanes), "fine_sample": n0, "lanes": lanes,
            "basis": {"start_time": start_time, "before": before, "after": after, "latency": latency, "lanes": lanes,
                      "bounds": {"t_lo": "start_time", "t_hi": "start_time"}},
            "unknown": []}


def stream_start_window(before: int, after: int, trigger_raw: int | None, delay: int, span: int) -> dict:
    """連続記録のサンプル 0 の時刻（ピンの時間軸）の、回路と往復から確かに言える範囲。サンプル 0 の時刻を記録しない回路
    （TLV TIMING の la_deep_start を申告しない古い回路。記録する回路は stream_start_recorded）のためのもの。

    - before・after: 流し始める（run を書く）前と後の CSR の時刻の読み（タイムベース）。サンプル 0 は run を書いた後に
      取られ、after の読みより前に取られる。
    - trigger_raw: 深いバッファのトリガ時刻（符号器がトリガを見た時刻。None = トリガしていない）。条件なしのトリガは
      最初のサンプル以後のどこかで成立するので、サンプル 0 はそれより後ではない（何サンプル後かは回路が記録しない）。
    - delay・span: ケイパビリティ LA_DELAY の深いバッファの段数・位相の幅（ピンの時間軸 = 時刻 − delay。fprobe.la.pin_time）。

    返す: t_lo・t_hi（サンプル 0 の時間軸の時刻の範囲。両端を含む）と、それを決めた根拠 basis。"""
    shift = delay
    lo, hi = before - shift, after - shift
    bounds = {"t_lo": "before", "t_hi": "after"}
    trig = None if trigger_raw is None else trigger_raw - shift
    if trig is not None and lo <= trig < hi:              # このトリガはこの流れのもの（前の流れの古い値は lo より前）
        hi = trig
        bounds["t_hi"] = "trigger"
    return {"t_lo": lo, "t_hi": hi,
            "basis": {"before": before, "after": after, "trigger_time": trigger_raw, "delay_ticks": delay,
                      "span_ticks": span, "bounds": bounds},
            "unknown": ["sample0_time"]}


# --- ポストされた AP の読み ---------------------------------------------------------------------------------------

def posted_read_window(requested: int, returned: int, before: int | None) -> dict:
    """ポストされた AP の読みが対象のバスで読んだ瞬間の範囲（いまの回路の時刻の意味。段階 S0）。

    - requested: 読みを頼んだ転送のイベントの時刻、returned: 値を返した転送（次の AP の読みか RDBUFF）の時刻。
      どちらも転送の完了 + 2 刻み（debug_port・dap_monitor の段。設計書 §2.1 の 5・6）。
    - 対象が読むのは、頼んだ転送の ACK の後〜返した転送の ACK の前。頼んだ転送の ACK は、その転送の完了より前
      （データ相の分）なので、requested は下限にならない。下限は、頼んだ転送の前に線上で終わった転送の時刻 before
      （同じ線の転送は 1 本ずつなので、それより前に完了している）。無ければ None（分からない）。
    - ACK の線上の時刻は PHY の設定で決まる式（段階 S2 の R5・R7）で、いまは出さない。"""
    return {"t_lo": before, "t_hi": returned, "requested": requested,
            "basis": {"t_lo": "previous_transfer" if before is not None else None, "t_hi": "returning_transfer"},
            "unknown": ["request_ack_time", "target_internal"] + (["previous_transfer"] if before is None else [])}


# --- DAP の転送: 線上の点（段階 S2。ケイパビリティ FEATURES の DAP_LINE_TIME） ------------------------------------
#
# 回路（swd_phy・jtag_phy の rsp_time）は、転送ごとに線上の決まった点の時刻を付ける（設計書 §6 の主担当の判断 1）:
#   SWD: ACK の 3 ビット目の Low 相が終わって SWCLK が 1 になった最初の周期（ACK の値に依らない）。
#   JTAG: その転送の DR スキャン（DP の読みで続けて RDBUFF をスキャンするときは最初のスキャン）の Update-DR を出る TCK が
#         1 になった最初の周期。JTAG-DP はここで AP / DP のアクセスを始める。
# ホストの転送（種類 1・12）・割り込み書き込み（11）・サンプラの語（7）・値を返した転送（17）・誤りの区間（18）の時刻はこの点。
# 点の前後の位置は PHY の設定（種類 16 の件）で決まる式で、ここに置く（cocotb が RTL の振る舞いと比べる）。
# 点は PHY の出力の FF で数える。端子に出るのは、ボードの出力の段（ケイパビリティ dap_output_ticks = TLV DAP_TIMING。
# PYNQ-Z2 は IOB の FF で 1）だけ後。範囲（t_lo・t_hi）は端子の時刻で返す。申告が無ければ（None）範囲は出さず、分からないと返す。

@dataclass(frozen=True)
class DapConfig:
    """PHY の設定（イベント 16。その時刻から次の 16 まで効く）。"""
    time: int
    half_period: int             # SWCLK / TCK の半周期（sys の周期）
    idle_cycles: int             # SWD: 転送の後のアイドル（ビット）。JTAG: Run-Test/Idle の TCK
    turnaround: int              # SWD のターンアラウンド（1〜4 ビット）
    data_phase: bool             # SWD: WAIT・FAULT でもデータ相を出す
    swd_sample_delay: int
    jtag_sample_delay: int
    jtag: bool                   # 接続の方式
    enable: bool                 # 接続中
    dr_before: int
    dr_after: int
    ir_before: int
    ir_after: int
    ir_len: int


def decode_dap_config(time: int, payload: int) -> DapConfig:
    """イベント 16 の [119:48]（payload。並びは gateware/rtl/core/event_collector.vhd の先頭）を解く。SWD の取り込みの遅れは
    下位 2 ビットが [28:27]、bit2 が [57]（0〜7 に広げた分。古い回路は 0）。"""
    def bits(lsb: int, width: int) -> int:
        return payload >> lsb & ((1 << width) - 1)
    return DapConfig(time=time, half_period=bits(0, 16), idle_cycles=bits(16, 8), turnaround=bits(24, 2) + 1,
                     data_phase=bool(bits(26, 1)), swd_sample_delay=bits(27, 2) | bits(57, 1) << 2,
                     jtag_sample_delay=bits(29, 3),
                     jtag=bool(bits(32, 1)), enable=bool(bits(33, 1)), dr_before=bits(34, 3), dr_after=bits(37, 3),
                     ir_before=bits(40, 6), ir_after=bits(46, 6), ir_len=bits(52, 5) + 1)


def swd_header_to_point_ticks(half_period: int, turnaround: int) -> int:
    """SWD: 転送のヘッダの最初のビットの Low 相の始め（SWCLK が 0 になった最初の周期）から、線上の点までの周期数（刻み）。
    ヘッダ 8 + ターン turnaround + ACK の 2 ビットが各 2 × half_period、ACK の 3 ビット目の Low 相が half_period
    （swd_phy.vhd。ヘッダの前の待ちは入らない。cocotb の swd_phy_tb が hp・turnaround・sample_delay の組で確かめる）。"""
    return (10 + turnaround) * 2 * half_period + half_period


def access_start_bound(point: int, cfg: DapConfig | None, output_ticks: int | None) -> tuple[int | None, str | None]:
    """読み・書きを頼んだ転送の点 point から、対象のアクセスが始まる時刻（端子）の確かな下限 (時刻, 根拠)。
    JTAG: point そのもの（Update-DR で JTAG-DP がアクセスを始める）。SWD: その転送の始め（ヘッダの最初のビット）。SW-DP が
    AP の読みを始める点は ADIv5 で確かめていないので、安全側の転送の始め（帯域の設計書 2.5）。どちらも端子の段 output_ticks を
    足す。設定・段が分からなければ None。"""
    if cfg is None or output_ticks is None:
        return None, None
    if cfg.jtag:
        return point + output_ticks, "jtag_update_dr"
    return point - swd_header_to_point_ticks(cfg.half_period, cfg.turnaround) + output_ticks, "swd_header_start"


def sampler_window(start: int, end: int | None, cfg: DapConfig | None, waits: int | None,
                   output_ticks: int | None) -> dict:
    """サンプラの語の、対象のバスで読んだ瞬間の範囲（設計書 R6）。

    - start: 語のイベント（種類 7）の時刻 = 読みに行った DRW の転送の点。
    - end: 値を返した転送の点（件の並びで次のサンプラの件の時刻: 次の語の 7、種類 17、誤りの区間 18 の始め）。まだ見えて
      いなければ None。SWD: その転送の ACK の後の点で、値はそこで揃っている（揃っていなければ WAIT になる）= 確かな上限。
      JTAG: その転送の Update-DR の点。値の取り込み（Capture-DR）はその前なので、確かな上限（その Capture-DR までの差は
      設定で決まるが、ここでは縮めない）。
    - waits: 値を返した転送の WAIT の回数（31 で止まる。31 は 31 回以上）。窓は WAIT の分だけ広い（end に入っている）。
      か所の境界（SWD で TAR の書きを挟む）・JTAG の境界の待ち（S_SETTLE 等）があっても end はその転送の点そのもの（式で
      出さない）ので、窓は正しい（TAR の書きの ACK のような、より早い確かな点があっても縮めない）。
    - output_ticks: 端子の段（dap_output_ticks）。t_lo・t_hi は端子の時刻。"""
    lo, lo_basis = access_start_bound(start, cfg, output_ticks)
    unknown = ["target_internal", "pin_to_wire"]
    if cfg is None:
        unknown.append("dap_config")
    if output_ticks is None:
        unknown.append("dap_output_ticks")
    if end is None:
        unknown.append("returning_transfer")
    hi = None if end is None or output_ticks is None else end + output_ticks
    hi_basis = None if end is None else ("jtag_returning_update_dr" if cfg is not None and cfg.jtag else
                                         "swd_returning_ack" if cfg is not None else "returning_transfer")
    return {"t": start, "t_lo": lo, "t_hi": hi,
            "basis": {"t": "read_request_point", "t_lo": lo_basis, "t_hi": hi_basis, "waits": waits,
                      "output_ticks": output_ticks},
            "unknown": unknown}


def posted_read_window_line(requested: int, returned: int, cfg: DapConfig | None, before: int | None,
                            output_ticks: int | None) -> dict:
    """ポストされた AP の読みの範囲（線上の点の回路。端子の時刻）。requested・returned は頼んだ転送・返した転送の点。
    下限は access_start_bound（設定・段が分からなければ、いまの回路の意味と同じく前の転送 before。それは端子の段を足さなくても
    前の転送の点 < その完了 < 次の転送の始め なので下限のまま）、上限は返した転送の点 + 端子の段（SWD は ACK の後 = 値は
    揃っている、JTAG は Update-DR = 取り込みの後）。段が分からなければ上限は返した転送の点のイベントの時刻のまま（端子では
    それより後: 上限として使えない）ので None。"""
    lo, basis = access_start_bound(requested, cfg, output_ticks)
    if lo is None:
        lo, basis = before, ("previous_transfer" if before is not None else None)
    hi = None if output_ticks is None else returned + output_ticks
    unknown = (["target_internal"] + ([] if cfg is not None else ["dap_config"])
               + ([] if output_ticks is not None else ["dap_output_ticks"])
               + ([] if lo is not None else ["previous_transfer"]))
    return {"t_lo": lo, "t_hi": hi, "requested": requested,
            "basis": {"t_lo": basis, "t_hi": "returning_transfer", "output_ticks": output_ticks}, "unknown": unknown}


def skew_of(skews: Sequence[float | None], ch: int) -> float:
    """波形を置くときの ch のずれ（分からない・申告の無い ch は 0 = ずらさない）。"""
    return (skews[ch] or 0.0) if ch < len(skews) else 0.0


# --- クロストリガの出力の端子（段階 S3。TLV TIMING の xtrig_out・xtrig_out_pins） --------------------------------------

def xtrig_out_window(caps: Any, source_time: int, mode: int = 0, length: int = 0, unit: int = 0) -> dict:
    """クロストリガの源のパルス（周期 source_time = その源の時刻: match_time・seq_time 等）で出力の端子を動かしたとき、端子が
    動き始める時刻 t_on と、脈（mode 0）なら止まる時刻 t_off（その周期の頭で止まる。t_on 以上 t_off 未満が駆動）。
    回路（xtrig_out.vhd）の脈の長さは刻み 16^unit 周期の length 個で、最初の刻みは時刻のビット 4·unit の変化で数えるので、
    源の時刻から 1 つに決まる（範囲ではない）: (length − 1)·16^unit + 1 以上 length·16^unit 以下。保持（1）は無効にするまで、
    条件の間（2・3）は源のパルスの 1 周期だけ（源で動かすとき）なので t_off = t_on + 1。端子の段（ボードの出力の段）は申告の
    delay に入っている。出力の緩衝器・配線は含まない（unknown）。申告（TLV TIMING の xtrig_out）が無ければ範囲を返さない。
    途中で源がもう一度来た（読み直し）場合は、その源の時刻で呼び直す。"""
    p = _declared(caps, "xtrig_out")
    if p is None or p.delay is None:
        return {"t_on": None, "t_off": None, "from": None, "unknown": ["xtrig_out_timing"]}
    first = source_time - p.delay                     # 出力の FF が変わる辺 + 端子の段（delay = −(2 + 段)）
    if mode == 0:
        if length == 0:
            return {"t_on": None, "t_off": None, "from": "TIMING", "unknown": [], "pulse_cycles": 0}
        tick = 16 ** unit
        stage = caps.dap_output_ticks     # ボードの出力の段（全部の出力で同じ。TLV TIMING の dap_*）
        if unit and stage is None:
            return {"t_on": first, "t_off": None, "from": "TIMING", "unknown": list(p.excludes) + ["output_stage"]}
        drive = first - (stage or 0)                    # 出力の FF が変わった周期（その周期の時刻の値で刻みを数える）
        lead = (-drive) % tick if unit else 0          # drive の周期から最初の刻みの周期まで
        cycles = lead + (length - 1) * tick + 1
        return {"t_on": first, "t_off": first + cycles, "pulse_cycles": cycles, "from": "TIMING",
                "unknown": list(p.excludes)}
    if mode == 1:
        return {"t_on": first, "t_off": None, "from": "TIMING", "unknown": list(p.excludes) + ["until_disabled"]}
    return {"t_on": first, "t_off": first + 1, "from": "TIMING", "unknown": list(p.excludes)}
