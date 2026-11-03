"""DAP トランザクションのイベント記録の復号と、ポストされた読みの対応付け。

仕様: docs/superpowers/specs/2026-10-16-m9b-dap-events-design.md
イベント 1 件 = 16 バイト: [47:0] 時刻、[79:48] 番地、[111:80] データ、[114:112] ACK、[118:115] 要求
（APnDP, RnW, A[3:2]）、[119] パリティ異常、[127:120] 種類。
種類 3 / 4 / 5（UART 受信・UART 送信・SWO）はデータの下位 8 ビットがバイト（波形・DAP と同じ時間軸で並べられる）。
種類 11（割り込み書き込み。R20）は [119:112] が停止の書き込みまでの転送数（旧い回路の種類 2 は要求・ACK の欄で、転送数なし）。
旧いホストは種類 11 を知らないので、回路とホストは一緒に更新する。
種類 13 / 14 / 15（UART 受信・UART 送信・SWO を詰めた形。ケイパビリティ FEATURES の EVENT_BYTE_PACK を申告する回路が出す）は
1 件に最大 4 バイト: [47:0] 先頭のバイトの時刻、[79:48] バイト（i 番目が [48+8i+7 : 48+8i]）、[115:80] 2〜4 バイト目の
前のバイトからの時刻の差（各 12 ビット）、[117:116] バイトの数 − 1。解読はバイトごとに古い種類 3 / 4 / 5 と同じ形の
1 件に戻す（各バイトの時刻の意味も古い種類と同じ）。記録の番号（書いた数）は 16 バイトの記録の件数で数える。
設計: docs/superpowers/specs/2026-11-01-bandwidth-observation-design.md 3.5。

DAP の時刻が線上の点の回路（ケイパビリティ FEATURES の DAP_LINE_TIME。docs/superpowers/specs/2026-11-01-time-accuracy-design.md
段階 S2）: 種類 1・11・12 の時刻は転送の線上の点（SWD は ACK の後の立ち上がり、JTAG は Update-DR を出る立ち上がり。
fprobe.timing）。種類 7（サンプラの語）の時刻は読みに行った DRW の転送の点、[119:115] は値を返した転送の WAIT の回数
（31 で止まる。解読は line_time=True のときだけこの意味で読む。古い回路は [118:115] = 要求 1111、[119] = 0）。
種類 16 = PHY の設定（その時刻から効く。fprobe.timing.DapConfig）、17 = サンプラの値を返した転送の点（直前の語の窓の後端）、
18 = サンプラの誤りの区間（値を出さなかった読み: 始め・見つけた転送の点・最初の番号・数・ACK・STICKYERR・線を失った）。
サンプラの語の窓の後端は、件の並びで次のサンプラの件（7・17・18）の時刻（resolve が time_hi に入れる）。

種類 19（ロジアナの垂れ流しの欠けの記録。fprobe_gw.csr.fprobe_map の EVENT_KIND_LA_GAP。CSR la_deep.control.gaps の流れで回路が出す）:
[47:0] 拍を捨て始めた周期の時刻、[79:48] 欠けの前に出したロジアナの拍の数（流れの開始から。32 ビットで折り返す）、[119:80] 捨てた
サンプルの数（流れのサンプルの単位）。解読は address = 拍の数、data = サンプルの数。DAP の線の上の転送ではない（ロジアナの事実）。
LaStream が流れの中で受けて欠けの区間にする（docs/superpowers/specs/2026-11-01-la-deep-fast-design.md §12）。
"""

from dataclasses import dataclass, replace

from . import generated as g
from .device import Device, select_stream_path
from .timing import DapConfig, access_start_bound, decode_dap_config, posted_read_window_line, sampler_window

__all__ = ["Access", "DapEvent", "EventCursor", "EventLog", "KIND_AUX", "KIND_HOST", "KIND_HOST_UNTRACKED", "KIND_INJECT", "KIND_INJECT_TRANSFERS", "KIND_LA_GAP", "KIND_OBS_RECORD", "KIND_OBS_SYNC", "KIND_SAMPLE", "KIND_SWO", "KIND_SYNC", "KIND_UART_RX", "KIND_UART_TX",
           "KIND_SWO_PACKED", "KIND_UART_RX_PACKED", "KIND_UART_TX_PACKED", "PACK_DELTA_BITS", "PACKED_BYTE_KINDS",
           "KIND_DAP_CONFIG", "KIND_SAMPLE_BOUND", "KIND_SAMPLE_GAP", "SAMPLER_KINDS", "SampleGap",
           "decode_events", "decode_records", "packed_record", "resolve"]

EVENT_BYTES = 16
KIND_HOST = 1            # ホスト（pyOCD 等）の転送
KIND_INJECT = 2          # クロストリガの割り込み書き込み（旧い回路。転送数は分からない）
KIND_UART_RX = 3         # UART の受信バイト（ターゲット → PC）
KIND_UART_TX = 4         # UART の送信バイト（PC → ターゲット）
KIND_SWO = 5             # SWO のバイト
KIND_AUX = 6             # 補助マイコンの計測値（data = ch << 16 | mV）
KIND_SAMPLE = 7          # DAP サンプラ（F4）の 1 語（番地・値。FPGA が自分で周期的に読んだもの）
KIND_OBS_RECORD = 8      # 対象の FPGA の観測の記録（data = 記録の語、address = スキャンの番号。fprobe.fpga_obs）
KIND_OBS_SYNC = 9        # 同、時刻合わせ（data = 対象の時刻の下位 32 ビット、address = 上位 16 ビット）
KIND_SYNC = 10           # ボードの間の時刻の同期の組（時刻 = 子の時刻、address = 親の時刻の下位 32、data = 上位 32。fprobe.timesync）
KIND_INJECT_TRANSFERS = 11   # 同（R20）: 書き込みまでの SWD の転送数つき（bit 112〜119。WAIT の繰り返しも数える）
KIND_HOST_UNTRACKED = 12     # ホストの転送のうち DRW で、メモリの番地を追えなかったもの（address = レジスタの番地。
                             # CSW・TAR を同じ AP に書く前・別の AP・packed の増加・1 KB の境界の先。dap_monitor）
KIND_UART_RX_PACKED = 13     # 種類 3 を詰めた形（1 件に最大 4 バイトと各バイトの時刻）
KIND_UART_TX_PACKED = 14     # 種類 4 を詰めた形
KIND_SWO_PACKED = 15         # 種類 5 を詰めた形
KIND_DAP_CONFIG = 16         # PHY の設定（DAP_LINE_TIME の回路。config）
KIND_SAMPLE_BOUND = 17       # サンプラの値を返した転送の点（直前の語の窓の後端。address = その語の番地）
KIND_SAMPLE_GAP = 18         # サンプラの誤りの区間（gap）
SAMPLER_KINDS = (KIND_SAMPLE, KIND_SAMPLE_BOUND, KIND_SAMPLE_GAP)
WAITS_SATURATED = 31         # 種類 7 の WAIT の回数の上限（31 は 31 回以上）
KIND_LA_GAP = g.EVENT_KIND_LA_GAP   # ロジアナの垂れ流しの欠けの記録（address = 欠けの前の拍の数、data = 捨てたサンプルの数）
PACKED_BYTE_KINDS = {KIND_UART_RX_PACKED: KIND_UART_RX, KIND_UART_TX_PACKED: KIND_UART_TX, KIND_SWO_PACKED: KIND_SWO}
PACK_DELTA_BITS = 12         # 詰めた形の、前のバイトからの時刻の差のビット数（収まらない差のバイトは回路が次の件にする）
PACK_MAX_BYTES = 4
INJECT_KINDS = (KIND_INJECT, KIND_INJECT_TRANSFERS)
# DAP の線の上の転送（1 本ずつ順に通る。ポストされた読みの範囲の下限に使う）
WIRE_KINDS = (KIND_HOST, KIND_HOST_UNTRACKED, KIND_SAMPLE, KIND_SAMPLE_BOUND, *INJECT_KINDS)
BYTE_KINDS = {KIND_UART_RX: "uart_rx", KIND_UART_TX: "uart_tx", KIND_SWO: "swo"}
ACK_OK = 0b001
DP_RDBUFF = 0xC


@dataclass(frozen=True)
class SampleGap:
    """サンプラが値を出さなかった読みの区間（種類 18）。時刻は線上の点。"""
    end: int                 # それが分かった転送の点（線を失ったときは最後の転送の点）
    index: int               # 最初の読みの番号（か所 × 16 + 語）
    words: int               # 読みの数
    ack: int                 # 分かった転送の ACK（線を失ったときは 0）
    sticky: bool             # CTRL/STAT の STICKYERR で分かった
    dropped: bool            # 線を失った（切断・接続の方式の切り替え）


@dataclass(frozen=True)
class DapEvent:
    """線上の 1 転送。AP の読みの data はポストされた値（1 つ前の AP 読みの結果）のまま。"""
    time: int
    kind: int
    ap: bool
    read: bool
    register: int            # A[3:2] << 2（0x0 / 0x4 / 0x8 / 0xC）
    ack: int
    parity_error: bool
    address: int             # DRW ならメモリの番地（TAR から。種類 12 は追えなかったのでレジスタの番地）、それ以外はレジスタの番地
    data: int
    transfers: int | None = None   # 割り込み書き込み（種類 11）の転送数。それ以外・旧い種類 2 は None
    # この転送の前に線上で終わった転送の時刻（かたまりをまたいで持ち越すポストされた読みにだけ付ける。collector）
    preceded_by: int | None = None
    waits: int | None = None         # サンプラの語（種類 7、line_time）: 値を返した転送の WAIT の回数（31 は 31 回以上）
    config: DapConfig | None = None  # 種類 16
    gap: SampleGap | None = None     # 種類 18（time = 最初の読みの点）

    @property
    def ok(self) -> bool:
        return self.ack == ACK_OK and not self.parity_error


@dataclass(frozen=True)
class Access:
    """意味づけした 1 アクセス（ポストされた読みを対応付けた後）。time は値が確定した転送の時刻。"""
    time: int
    kind: str                # "ap_read" / "ap_write" / "dp_read" / "dp_write" / "fail" / "uart_rx" / "uart_tx" / "swo"
                             # / "drw_read" / "drw_write"（メモリの番地を追えなかった DRW。address = レジスタの番地 0xC）
                             # / "inject"（クロストリガの割り込み書き込み）/ "aux"（address = チャンネル、value = mV）/ "sample"（サンプラが読んだ番地と値）
                             # / "obs_record"（address = スキャンの番号、value = 記録の語）
                             # / "obs_sync"（address = 対象の時刻の上位 16 ビット、value = 下位 32 ビット）
                             # / "sync_pair"（address = 親の時刻の下位 32 ビット、value = 上位 32 ビット）
                             # / "la_gap"（ロジアナの垂れ流しの欠け: address = 欠けの前に出した拍の数（32 ビットで折り返す）、
                             #   value = 捨てたサンプルの数。time = 回路が拍を捨て始めた時刻。欠けの区間のサンプルの位置は LaStream）
    address: int
    value: int
    ack: int
    transfers: int | None = None   # "inject"（割り込み書き込み）の書き込みまでの転送数（旧い回路は None）
    # ポストされた AP の読み（ap_read・drw_read で値が後の転送で返ったもの）だけ: 対象が読んだ瞬間の範囲
    # （fprobe.timing.posted_read_window）。requested = 読みを頼んだ転送の時刻、time_lo = その前に線上で終わった転送の
    # 時刻（確かな下限。見えていなければ None）、time = 値を返した転送の時刻（確かな上限）。それ以外は None
    requested: int | None = None
    time_lo: int | None = None
    # サンプラの語（line_time）: time = 読みに行った転送の点、time_lo = 対象が読み始めた確かな下限（設定が分からなければ None）、
    # time_hi = 値を返した転送の点（確かな上限。並びの中に見えていなければ None）。"sample_error"（誤りの区間）の time_hi は
    # 見つけた転送の点。detail に根拠（fprobe.timing.sampler_window の basis・unknown、WAIT の回数、誤りの区間の中身）。
    # "dap_config"（PHY の設定）の detail は設定そのもの
    time_hi: int | None = None
    detail: dict | None = None


_MASK48 = (1 << 48) - 1
_DELTA_MASK = (1 << PACK_DELTA_BITS) - 1


def packed_record(kind: int, time: int, data: bytes, deltas: list[int]) -> bytes:
    """詰めた形の 1 件（16 バイト）を組む（試験の偽物のデバイス用。回路の形と同じ）。deltas = 2 バイト目以降の差。"""
    if kind not in PACKED_BYTE_KINDS or not 1 <= len(data) <= PACK_MAX_BYTES or len(deltas) != len(data) - 1:
        raise ValueError("詰めた形は種類 13〜15、1〜4 バイト、差はバイトの数 − 1 個")
    if any(not 0 <= d <= _DELTA_MASK for d in deltas):
        raise ValueError(f"差は {PACK_DELTA_BITS} ビット（回路は収まらない差のバイトを次の件にする）")
    v = time & _MASK48 | (len(data) - 1) << 116 | kind << 120
    for i, b in enumerate(data):
        v |= b << (48 + 8 * i)
    for i, d in enumerate(deltas):
        v |= d << (80 + PACK_DELTA_BITS * i)
    return v.to_bytes(EVENT_BYTES, "little")


def _unpack_bytes(v: int, kind: int) -> list[DapEvent]:
    """詰めた形の 1 件を、バイトごとの古い形（種類 3 / 4 / 5）の件に戻す。"""
    t = v & _MASK48
    out = []
    for i in range((v >> 116 & 3) + 1):
        if i:
            t = (t + (v >> (80 + PACK_DELTA_BITS * (i - 1)) & _DELTA_MASK)) & _MASK48
        out.append(DapEvent(time=t, kind=kind, ap=False, read=False, register=0, ack=0, parity_error=False,
                            address=0, data=v >> (48 + 8 * i) & 0xFF))
    return out


def decode_records(data: bytes, line_time: bool = False) -> list[list[DapEvent]]:
    """16 バイトの記録ごとの、解読したイベント（詰めた形は 1〜4 件、ほかは 1 件）。line_time = 回路が DAP_LINE_TIME を
    申告する（種類 7 の [119:115] を WAIT の回数として読む）。"""
    if len(data) % EVENT_BYTES:
        raise ValueError("イベント（16 バイト）の倍数でない")
    return [_decode_one(int.from_bytes(data[i:i + EVENT_BYTES], "little"), line_time)
            for i in range(0, len(data), EVENT_BYTES)]


def decode_events(data: bytes, line_time: bool = False) -> list[DapEvent]:
    """記録を古い順に解読する（詰めた形はバイトごとの件に戻す）。line_time は decode_records と同じ。"""
    return [e for record in decode_records(data, line_time) for e in record]


def _decode_one(v: int, line_time: bool = False) -> list[DapEvent]:
    kind = v >> 120 & 0xFF
    t = v & _MASK48
    if kind in PACKED_BYTE_KINDS:
        return _unpack_bytes(v, PACKED_BYTE_KINDS[kind])
    if kind == KIND_DAP_CONFIG:
        return [DapEvent(time=t, kind=kind, ap=False, read=False, register=0, ack=0, parity_error=False, address=0,
                         data=0, config=decode_dap_config(t, v >> 48 & ((1 << 72) - 1)))]
    if kind == KIND_SAMPLE_BOUND:                        # 値を返した転送の点（[115:112] = その要求）
        request = v >> 112 & 0xF
        return [DapEvent(time=t, kind=kind, ap=bool(request & 1), read=bool(request & 2),
                         register=(request >> 2) << 2, ack=ACK_OK, parity_error=False,
                         address=v >> 48 & 0xFFFFFFFF, data=0)]
    if kind == KIND_SAMPLE_GAP:
        gap = SampleGap(end=v >> 48 & _MASK48, index=v >> 96 & 0x7F, words=v >> 103 & 0x1FF, ack=v >> 112 & 7,
                        sticky=bool(v >> 115 & 1), dropped=bool(v >> 116 & 1))
        return [DapEvent(time=t, kind=kind, ap=True, read=True, register=0xC, ack=gap.ack, parity_error=False,
                         address=0, data=0, gap=gap)]
    if kind == KIND_SAMPLE and line_time:                # 読みに行った DRW の点、[119:115] = 値を返した転送の WAIT の回数
        return [DapEvent(time=t, kind=kind, ap=True, read=True, register=0xC, ack=v >> 112 & 7, parity_error=False,
                         address=v >> 48 & 0xFFFFFFFF, data=v >> 80 & 0xFFFFFFFF, waits=v >> 115 & 0x1F)]
    if kind == KIND_LA_GAP:                              # ロジアナの欠けの記録（要求・ACK の欄は無い）
        return [DapEvent(time=v & _MASK48, kind=kind, ap=False, read=False, register=0, ack=0, parity_error=False,
                         address=v >> 48 & 0xFFFFFFFF, data=v >> 80 & ((1 << 40) - 1))]
    if kind == KIND_INJECT_TRANSFERS:                    # 要求・ACK の欄の代わりに転送数（書き込みは常に AP の DRW・OK）
        return [DapEvent(time=v & _MASK48, kind=kind, ap=True, read=False, register=0xC, ack=ACK_OK,
                         parity_error=False, address=v >> 48 & 0xFFFFFFFF, data=v >> 80 & 0xFFFFFFFF,
                         transfers=v >> 112 & 0xFF)]
    request = v >> 115 & 0xF
    return [DapEvent(time=v & _MASK48, kind=kind, ap=bool(request & 1), read=bool(request & 2),
                     register=(request >> 2) << 2, ack=v >> 112 & 7, parity_error=bool(v >> 119 & 1),
                     address=v >> 48 & 0xFFFFFFFF, data=v >> 80 & 0xFFFFFFFF)]


def resolve(events: list[DapEvent], line_time: bool = False, config: DapConfig | None = None,
            output_ticks: int | None = None) -> list[Access]:
    """ポストされた AP の読みを、値が返った転送（次の AP 読みか DP の RDBUFF 読み）と対応付ける（DAP.c と同じ規則）。
    line_time = 回路が DAP_LINE_TIME を申告する（時刻は線上の点。ポストされた読みとサンプラの語の範囲を、PHY の設定の
    件（種類 16）から fprobe.timing の式で出す。設定の件が並びに無ければ下限は前の転送か None）。
    サンプラの語の窓の後端（time_hi）は、並びの中の次のサンプラの件（7・17・18）の時刻。並びの最後の語は None。
    config = 並びの前から効いている PHY の設定（前のかたまりの最後の種類 16。collector が持ち越す）。
    output_ticks = 端子の段（ケイパビリティ dap_output_ticks。None = 分からない: 範囲の端を出さない）。範囲は端子の時刻、
    time は点のイベントの時刻のまま。ポストされた読みの time（値を返した転送）もイベントの時刻のまま（範囲の上限は time_hi）。"""
    out: list[Access] = []
    pending: DapEvent | None = None          # 値がまだ返っていない AP の読み
    pending_lo: int | None = None            # その読みを頼んだ転送の前に、線上で終わった転送の時刻
    last_wire: int | None = None             # 直前の、DAP の線の上の転送（ホスト・サンプラ・割り込み。成否を問わない）の時刻
    cfg: DapConfig | None = config           # いま効いている PHY の設定（種類 16）
    open_sample: int | None = None           # 窓の後端がまだ分からないサンプラの語（out の位置）

    def returned(e: DapEvent) -> Access:
        if line_time:
            w = posted_read_window_line(pending.time, e.time, cfg, pending_lo, output_ticks)
            return Access(e.time, _read_kind(pending), pending.address, e.data, e.ack, requested=pending.time,
                          time_lo=w["t_lo"], time_hi=w["t_hi"], detail={"basis": w["basis"], "unknown": w["unknown"]})
        return Access(e.time, _read_kind(pending), pending.address, e.data, e.ack, requested=pending.time,
                      time_lo=pending_lo)

    def close_sample(end: int) -> None:          # 開いている語の窓の後端 = このサンプラの件の点
        nonlocal open_sample
        if open_sample is not None:
            a = out[open_sample]
            w = sampler_window(a.time, end, cfg, a.detail.get("waits") if a.detail else None, output_ticks)
            out[open_sample] = replace(a, time_lo=w["t_lo"], time_hi=w["t_hi"],
                                       detail={**(a.detail or {}), "basis": w["basis"], "unknown": w["unknown"]})
            open_sample = None

    for e in events:
        wire_before = last_wire
        if e.kind in WIRE_KINDS:
            last_wire = e.time
        if e.kind == KIND_DAP_CONFIG:
            cfg = e.config
            out.append(Access(e.time, "dap_config", 0, e.config.half_period, 0, detail=_config_detail(e.config)))
            continue
        if e.kind in SAMPLER_KINDS and line_time:
            close_sample(e.time)
        if e.kind == KIND_SAMPLE_BOUND:          # 値を返した転送の点（直前の語の窓の後端。それ自体はアクセスではない）
            continue
        if e.kind == KIND_SAMPLE_GAP:            # 値を出さなかった読みの区間
            gp = e.gap
            lo, lo_basis = access_start_bound(e.time, cfg, output_ticks)
            hi = None if output_ticks is None else gp.end + output_ticks
            out.append(Access(e.time, "sample_error", gp.index, gp.words, gp.ack, time_lo=lo, time_hi=hi,
                              detail={"index": gp.index, "words": gp.words, "ack": gp.ack, "sticky": gp.sticky,
                                      "dropped": gp.dropped, "basis": {"t_lo": lo_basis, "t_hi": "found_at"}}))
            continue
        if e.kind in BYTE_KINDS:                 # バイトはそのまま（DAP のポストの連鎖には関係しない）
            out.append(Access(e.time, BYTE_KINDS[e.kind], 0, e.data & 0xFF, 0))
            continue
        if e.kind == KIND_AUX:
            out.append(Access(e.time, "aux", e.data >> 16, e.data & 0x3FFF, 0))
            continue
        if e.kind == KIND_SAMPLE:                # サンプラの読みは値が確定済み（ホストのポストの連鎖とは別）
            if line_time:
                w = sampler_window(e.time, None, cfg, e.waits, output_ticks)
                out.append(Access(e.time, "sample", e.address, e.data, e.ack, time_lo=w["t_lo"],
                                  detail={"waits": e.waits, "basis": w["basis"], "unknown": w["unknown"]}))
                open_sample = len(out) - 1
            else:
                out.append(Access(e.time, "sample", e.address, e.data, e.ack))
            continue
        if e.kind == KIND_LA_GAP:                # ロジアナの垂れ流しの欠け（DAP とは別）
            out.append(Access(e.time, "la_gap", e.address, e.data, 0))
            continue
        if e.kind == KIND_SYNC:                  # ボードの間の時刻の同期（DAP とは別の線）
            out.append(Access(e.time, "sync_pair", e.address, e.data, 0))
            continue
        if e.kind in (KIND_OBS_RECORD, KIND_OBS_SYNC):   # 対象の FPGA の観測（DAP とは別の線）
            out.append(Access(e.time, "obs_record" if e.kind == KIND_OBS_RECORD else "obs_sync", e.address, e.data,
                              0))
            continue
        if e.kind in INJECT_KINDS:               # 割り込み書き込み: ホストのポストの連鎖とは別（ホストの値を戻してから返す）
            out.append(Access(e.time, "inject" if e.ok else "fail", e.address, e.data, e.ack, e.transfers))
            continue
        if not e.ok:
            out.append(Access(e.time, "fail", e.address, e.data, e.ack))
            pending = None                   # 失敗でポストの連鎖は途切れる
            continue
        if e.ap and e.read:
            if pending is not None:
                out.append(returned(e))
            lo = e.preceded_by if e.preceded_by is not None else wire_before
            pending, pending_lo = e, lo if lo is not None and lo < e.time else None   # 前後が崩れた列は下限にしない
        elif not e.ap and e.read and e.register == DP_RDBUFF:
            if pending is not None:
                out.append(returned(e))
                pending = None
            else:
                out.append(Access(e.time, "dp_read", e.register, e.data, e.ack))
        elif e.read:
            out.append(Access(e.time, "dp_read", e.register, e.data, e.ack))
        else:
            out.append(Access(e.time, "drw_write" if e.kind == KIND_HOST_UNTRACKED else "ap_write" if e.ap else "dp_write",
                              e.address, e.data, e.ack))
    return out


def _config_detail(c: DapConfig) -> dict:
    return {"half_period": c.half_period, "idle_cycles": c.idle_cycles, "turnaround": c.turnaround,
            "data_phase": c.data_phase, "swd_sample_delay": c.swd_sample_delay, "jtag_sample_delay": c.jtag_sample_delay,
            "jtag": c.jtag, "enable": c.enable, "dr_before": c.dr_before, "dr_after": c.dr_after,
            "ir_before": c.ir_before, "ir_after": c.ir_after, "ir_len": c.ir_len}


def _read_kind(e: DapEvent) -> str:
    """値が返った AP の読みの種類: メモリの番地を追えなかった DRW は drw_read。"""
    return "drw_read" if e.kind == KIND_HOST_UNTRACKED else "ap_read"


def read_counts(dev) -> tuple[int, int]:
    """(書いた総数, 記録できなかった数)。総数は written_hi:written の 64 ビット。written_hi は written を読んだ時点の
    上位を保持するので、written に続けて同じ連続読み出しの中で読む（別々に読むと 2**32 の境で下位だけ 0 に戻って化ける）。"""
    written, lost, _capacity, written_hi = dev.read(g.EVENTS_WRITTEN, 4)
    return written_hi << 32 | written, lost


class EventLog:
    """DAP のイベント記録（外部メモリのリング）。ロジアナの深いバッファと同時に使える（読み出しは捕捉していないときに）。"""

    def __init__(self, dev: Device):
        self.dev = dev
        self.capacity = dev.read32(g.EVENTS_CAPACITY)
        if not self.capacity:
            raise RuntimeError("このデバイスにはイベント記録が無い")
        caps = dev.capabilities()
        self.line_time = bool(caps.dap_line_time)   # 時刻が線上の点の回路（S2）
        self.output_ticks = caps.dap_output_ticks     # その点から端子までの段（TLV DAP_TIMING）
        self.overwritten = 0          # 直前の read で、読む間に書き手に上書きされて捨てた件数
        self.records = 0              # 直前の read で返した記録（16 バイト）の件数。詰めた形の記録は複数のイベントになるので、
                                      # 番号（書いた数）を進めるのはこの数（返したイベントの数ではない）

    def start(self) -> None:
        """記録を始める（リングの先頭から）。"""
        self.dev.write32(g.EVENTS_CONTROL, 0)
        self.dev.write32(g.EVENTS_CONTROL, 1)

    def running(self) -> bool:
        return bool(self.dev.read32(g.EVENTS_CONTROL) & 1)

    def ensure_started(self) -> None:
        """記録していなければ始める。記録中なら続ける（ほかの道具が読んでいる途中のリングを捨てない）。"""
        if not self.running():
            self.start()

    def stop(self) -> None:
        self.dev.write32(g.EVENTS_CONTROL, 0)

    def counts(self) -> tuple[int, int]:
        """(書いた総数, 記録できなかった数)。総数は 64 ビット（2**32 件を超えても戻らない）。"""
        return read_counts(self.dev)

    def cursor(self) -> "EventCursor":
        """いまから後に書かれるイベントを順に読む読み手。"""
        return EventCursor(self)

    def read(self, last: int | None = None, end: int | None = None) -> list[DapEvent]:
        """end 件目（書いた総数で数える。既定: 読んだ時点の総数）で終わる last 件（既定: 残っている全部）を古い順に。
        先に counts で数を読んだ呼び手は、その数を end に渡す（間に書かれた分で範囲がずれ、欠けと二重が出る。
        2026-10-18 に実機で、記録中の SWO のバイトがそうなった）。last・end は記録（16 バイト）の件数で数え、返すのは
        解読したイベント（詰めた形の記録は 1〜4 件）。返した記録の件数は records に残す。"""
        written = self.counts()[0] if end is None else end
        available = min(written, self.capacity)
        n = available if last is None else min(last, available)
        self.overwritten = self.records = 0
        if n <= 0:
            return []
        select_stream_path(self.dev, False)                  # イベントのリングの読み出しは窓経由（フレーム）で届く
        self.dev.write32(g.LA_DEEP_READ_REGION, 1)
        self.dev.write(g.LA_DEEP_READ_FIRST, [(written - n) % self.capacity, n])
        self.dev.write32(g.LA_DEEP_COMMAND, 1 << g.LA_DEEP_COMMAND_READ[0])
        records = decode_records(self.dev.read_stream(n * EVENT_BYTES, timeout_ms=5000), self.line_time)
        # 読む間に書き手が読み手を追い越した分は、要求より新しいイベントにすり替わっている（リングが小さい構成・遅いリンクで
        # 起きる）。読み終えた時点の総数から、まだ残っていたはずの番号より前の先頭を捨てて数える
        after = self.counts()[0]
        drop = n if after < written else min(n, max(0, after - self.capacity - (written - n)))
        self.overwritten = drop
        self.records = n - drop
        return [e for record in records[drop:] for e in record]


class EventCursor:
    """イベント記録を前回の続きから読む（読んでもリングは消えない。ほかの読み手と同時に使える）。読む前にリングが
    一周して上書きされた分・limit を超えた分は lost に数える（古い方を捨て、最新を読む）。"""

    def __init__(self, log: EventLog):
        self.log = log
        self.seen = log.counts()[0]
        self.lost = 0
        self.restarts = 0

    def take(self, limit: int = 1 << 16) -> list[DapEvent]:
        written, _ = self.log.counts()
        if written < self.seen:                    # 記録が始め直された（記録の開始等）
            self.seen, self.restarts = 0, self.restarts + 1
        new = written - self.seen
        n = min(new, self.log.capacity, limit)
        self.lost += new - n
        self.seen = written
        if not n:
            return []
        events = self.log.read(n, end=written)
        self.lost += self.log.overwritten
        return events
