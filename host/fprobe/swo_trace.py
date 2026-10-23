"""SWO の DWT・ITM の設定の計算（U11）と、受けたパケットの組み立て。入出力を持たない。

- `plan_trace`: 頼まれた出力（ITM のスティミュラス・ポート、ローカルのタイムスタンプ、例外トレース、イベントカウンタ、
  DWT の比較器によるデータトレース・値一致のウォッチポイント）を、書く番地と値の一覧（`RegisterWrite`）にする。
  比較器は機器の調べ（`DwtInfo`: 数・使用中・値の比較のできるもの・最大の MASK）を受けて割り当てる。
  書く順は 2 つに分ける: `setup`（DEMCR.TRCENA・ITM の鍵。パケットを出さない）と `enable`（ITM → DWT_CTRL →
  比較器。パケットを出し始める）。本流は setup → SWO の受信を始める → enable の順に書く（逆だとパケットの途中から
  受信して、次の同期パケットまで解読がずれる）。
- `TimedItmStream`: デバイスの時刻付きの SWO のバイト（記録の stream "swo"・イベント記録）を増分で解読し、
  パケットの先頭のバイトの時刻を付ける。
- `DataTraceAssembler`: 同じ比較器の PC（または番地の下位）と値のパケットを 1 回のアクセスにまとめる。
- `CounterTotals`: DWT のイベントカウンタ（8 ビット）の溢れのパケットを数えて 256 倍する。

根拠: ARMv7-M Architecture Reference Manual C1.7（ITM）・C1.8（DWT。DWT_FUNCTIONn の FUNCTION の符号化は
C1.8.17 の表）・Appendix D4（パケット）。ARMv8-M の DWT は FUNCTION の符号化が違う（MATCH・ACTION）ので、
比較器を使う機能は ARMv7-M に限る（ITM・例外トレース・イベントカウンタは共通）。
"""

from dataclasses import asdict, dataclass, field

from .itm import (DataTraceAddress, DataTracePc, DataTraceValue, DecodeError, EventCounter, ItmDecoder, Overflow,
                  Packet, Sync)
from .profile.dwt import DEMCR, DEMCR_TRCENA, DWT_CTRL, ITM_LAR, ITM_TCR, ITM_TER, ITM_UNLOCK, RegisterWrite

__all__ = ["COUNTERS", "CPUID", "DWT_CYCCNT", "ComparatorUse", "CoreInfo", "CounterTotals", "DataAccess",
           "DataTrace", "DataTraceAssembler", "DwtInfo", "TimedItmStream", "TracePlan", "TraceRequest", "ValueWatch",
           "address_mask", "core_info", "dwt_comp", "dwt_function", "dwt_mask", "plan_trace"]

# --- レジスタ ---------------------------------------------------------------------------------
CPUID = 0xE000ED00
DWT_CYCCNT = 0xE0001004
_COMP_BASE = 0xE0001020


def dwt_comp(n: int) -> int:
    return _COMP_BASE + 16 * n


def dwt_mask(n: int) -> int:
    return _COMP_BASE + 16 * n + 4


def dwt_function(n: int) -> int:
    return _COMP_BASE + 16 * n + 8


# DWT_CTRL
_CYCCNTENA = 1 << 0
_SYNCTAP_SHIFT = 10
_SYNCTAP_FIELD = 3 << _SYNCTAP_SHIFT
_SYNCTAP_FAST = 1                       # CYCCNT のビット 24 ごと（16 MHz で約 1 秒、84 MHz で 0.2 秒）に同期パケット
_EXCTRCENA = 1 << 16
NOTRCPKT = 1 << 27                      # 読み出し専用: トレースのパケットが無い
NOPRFCNT = 1 << 24                      # 読み出し専用: イベントカウンタが無い

# イベントカウンタ: 名前 → (DWT_CTRL の有効ビット, 8 ビットのカウンタの番地, EventCounter のフラグ)
COUNTERS = {"cpi": (1 << 17, 0xE0001008, 0x01), "exc": (1 << 18, 0xE000100C, 0x02),
            "sleep": (1 << 19, 0xE0001010, 0x04), "lsu": (1 << 20, 0xE0001014, 0x08),
            "fold": (1 << 21, 0xE0001018, 0x10)}

# ITM_TCR
_ITMENA, _TSENA, _SYNCENA, _TXENA = 1 << 0, 1 << 1, 1 << 2, 1 << 3
_TSPRESCALE_SHIFT = 8
_TSPRESCALE = {1: 0, 4: 1, 16: 2, 64: 3}
_TRACE_BUS_ID_SHIFT = 16
_TRACE_BUS_ID_FIELD = 0x7F << _TRACE_BUS_ID_SHIFT

# DWT_FUNCTIONn（ARMv7-M）
_EMITRANGE = 1 << 5
_DATAVMATCH = 1 << 8
LNK1ENA = 1 << 9                        # 読み出し専用: 2 つ目の番地の比較器をつなげられる
_DATAVSIZE_SHIFT = 10
_DATAVADDR0_SHIFT = 12
_DATAVSIZE = {1: 0, 2: 1, 4: 2}
_WATCH = {"read": 0b0101, "write": 0b0110, "rw": 0b0111}         # 止める（デバッグ事象）
# データトレース（EMITRANGE で PC の代わりに番地の下位 16 ビット）: (access, pc か offset, value) → FUNCTION
_TRACE = {("rw", True, False): 0b0001, ("rw", False, True): 0b0010, ("rw", True, True): 0b0011,
          ("read", False, True): 0b1100, ("read", True, True): 0b1110,
          ("write", False, True): 0b1101, ("write", True, True): 0b1111}
_TRACE_COMPARATORS = 4                  # データトレースのパケットは比較器 0〜3 しか表せない

# パケットの大きさ（バイト。SWO の UART 方式は 1 バイト 10 ビット）
_PC_BYTES, _OFFSET_BYTES, _COUNTER_BYTES, _BITS_PER_BYTE = 5, 3, 2, 10
_TIMESTAMP_BYTES = 2

_PARTS = {0xC20: ("Cortex-M0", "v6m"), 0xC60: ("Cortex-M0+", "v6m"), 0xC21: ("Cortex-M1", "v6m"),
          0xC23: ("Cortex-M3", "v7m"), 0xC24: ("Cortex-M4", "v7m"), 0xC27: ("Cortex-M7", "v7m"),
          0xD20: ("Cortex-M23", "v8m"), 0xD21: ("Cortex-M33", "v8m"), 0xD22: ("Cortex-M55", "v8m"),
          0xD23: ("Cortex-M85", "v8m"), 0xD24: ("Cortex-M52", "v8m"), 0xD31: ("Cortex-M35P", "v8m")}


@dataclass(frozen=True)
class CoreInfo:
    cpuid: int
    name: str
    arch: str                           # v6m / v7m / v8m / unknown


def core_info(cpuid: int) -> CoreInfo:
    """CPUID（0xE000ED00）からコアの名前とアーキテクチャ（Arm 社のコアだけ。ほかは unknown）。"""
    implementer, part = cpuid >> 24, (cpuid >> 4) & 0xFFF
    name, arch = _PARTS.get(part, ("unknown", "unknown")) if implementer == 0x41 else ("unknown", "unknown")
    return CoreInfo(cpuid, name, arch)


def address_mask(address: int, size: int) -> tuple[int, int]:
    """変数 [address, address + size) を覆う最小の比較の範囲: (先頭の番地, MASK = 無視する下位のビット数)。"""
    if size <= 0:
        raise ValueError("大きさは正")
    mask = 0
    while address >> mask != (address + size - 1) >> mask:
        mask += 1
    return address & ~((1 << mask) - 1), mask


# --- 頼むもの ----------------------------------------------------------------------------------

@dataclass(frozen=True)
class DataTrace:
    """変数へのアクセスを止めずに SWO に出す（比較器 1 つ）。access: read / write / rw。出すもの: pc（命令の番地）・
    value（読み書きした値）・offset（番地の下位 16 ビット。配列・構造体のどこか）。pc と offset は同時に出せない。"""
    name: str
    address: int
    size: int
    access: str = "write"
    pc: bool = True
    value: bool = True
    offset: bool = False


@dataclass(frozen=True)
class ValueWatch:
    """値一致: 変数（address。None なら番地を問わない）に value が読み書きされたとき。action: halt（止める =
    ウォッチポイント）/ trace（止めずに SWO に出す。access は rw だけ）。値の比較のできる比較器（多くは 1 番）を使い、
    番地があれば番地の比較器をもう 1 つつなぐ。"""
    name: str
    value: int
    size: int
    address: int | None = None
    access: str | None = None                     # 省略: halt は write、trace は rw
    action: str = "halt"
    pc: bool = True


@dataclass(frozen=True)
class TraceRequest:
    ports: tuple[int, ...] = ()
    timestamps: bool = False
    timestamp_prescale: int = 1
    exception_trace: bool = False
    counters: tuple[str, ...] = ()
    data_traces: tuple[DataTrace, ...] = ()
    value_watches: tuple[ValueWatch, ...] = ()


@dataclass(frozen=True)
class DwtInfo:
    """機器を調べた結果（本流が読む）。busy は使えない比較器（pyOCD のウォッチポイント・ファームが使用中）。"""
    numcomp: int
    arch: str
    trace_packets: bool = True                    # DWT_CTRL.NOTRCPKT が 0
    profiling_counters: bool = True               # DWT_CTRL.NOPRFCNT が 0
    busy: frozenset[int] = frozenset()
    value_match: frozenset[int] = frozenset()     # DATAVMATCH が立つ比較器
    linkable: frozenset[int] = frozenset()        # LNK1ENA が立つ比較器（番地の比較器をつなげる）
    max_mask: int = 0


@dataclass(frozen=True)
class ComparatorUse:
    index: int
    role: str                                     # data_trace / value_watch / link
    name: str
    function: int                                 # DWT_FUNCTIONn に書く値
    comp: int                                     # DWT_COMPn に書く値（番地、値一致は値）
    mask: int = 0
    address: int | None = None                    # 見張る変数の番地（値一致の番地なしは None）
    size: int = 0
    access: str = ""
    emits: tuple[str, ...] = ()                   # pc / offset / value（SWO に出るもの）
    value: int | None = None                      # 値一致の値
    action: str | None = None                     # 値一致: halt / trace

    def as_dict(self) -> dict:
        d = asdict(self)
        d["emits"] = list(self.emits)
        return d


@dataclass
class TracePlan:
    setup: list[RegisterWrite]
    enable: list[RegisterWrite]
    comparators: list[ComparatorUse]
    warnings: list[str] = field(default_factory=list)
    bytes_per_access: dict[str, int] = field(default_factory=dict)
    swo_bytes_per_s_max: float | None = None      # SWO の帯域（バイト/秒）

    @property
    def uses_swo(self) -> bool:
        return any(w.address in (ITM_TCR, ITM_TER) for w in self.enable)

    def as_dict(self) -> dict:
        return {"comparators": [c.as_dict() for c in self.comparators], "warnings": list(self.warnings),
                "bytes_per_access": dict(self.bytes_per_access), "swo_bytes_per_s_max": self.swo_bytes_per_s_max,
                "writes": [asdict(w) for w in self.setup + self.enable]}


# --- 計画 ------------------------------------------------------------------------------------

def plan_trace(request: TraceRequest, dwt: DwtInfo, *, core_clock_hz: float | None = None,
               swo_baud: float | None = None) -> TracePlan:
    """頼まれた出力の設定。SWO に出すものがあるのに swo_baud が無ければ ValueError（受けられない）。"""
    for c in request.counters:
        if c not in COUNTERS:
            raise ValueError(f"イベントカウンタは {', '.join(COUNTERS)} のどれか: {c!r}")
    if request.counters and not dwt.profiling_counters:
        raise ValueError("このコアの DWT にはイベントカウンタが無い（DWT_CTRL.NOPRFCNT）")
    for p in request.ports:
        if not 0 <= p < 32:
            raise ValueError(f"ITM のポートは 0〜31: {p}")
    if request.timestamp_prescale not in _TSPRESCALE:
        raise ValueError("timestamp_prescale は 1 / 4 / 16 / 64")
    comparators = _assign(request, dwt)
    trace_cmp = [u for u in comparators if u.emits]
    dwt_packets = bool(request.exception_trace or request.counters or trace_cmp)
    if dwt_packets and not dwt.trace_packets:
        raise ValueError("このコアの DWT にはトレースのパケットが無い（DWT_CTRL.NOTRCPKT。Cortex-M0/M0+ 等）")
    swo = bool(request.ports or dwt_packets)
    if swo and swo_baud is None:
        raise ValueError("SWO に出すものがある: swo_baud が要る（受信の速度。swo_start か swo_baud）")

    plan = TracePlan([RegisterWrite(DEMCR, DEMCR_TRCENA, DEMCR_TRCENA, "DEMCR.TRCENA: DWT・ITM を使う")], [],
                     comparators)
    if swo:
        plan.setup.append(RegisterWrite(ITM_LAR, ITM_UNLOCK, note="ITM の鍵を外す"))
        tcr = _ITMENA | _SYNCENA | 1 << _TRACE_BUS_ID_SHIFT
        tcr_mask = _ITMENA | _SYNCENA | _TRACE_BUS_ID_FIELD
        if dwt_packets:
            tcr, tcr_mask = tcr | _TXENA, tcr_mask | _TXENA
        if request.timestamps:
            tcr |= _TSENA | _TSPRESCALE[request.timestamp_prescale] << _TSPRESCALE_SHIFT
            tcr_mask |= _TSENA | 3 << _TSPRESCALE_SHIFT
        plan.enable.append(RegisterWrite(ITM_TCR, tcr, tcr_mask, "ITM: 有効・同期" + ("・DWT のパケット" if dwt_packets
                                                                                   else "")
                                         + ("・ローカルのタイムスタンプ" if request.timestamps else "")))
        if request.ports:
            ter = sum(1 << p for p in set(request.ports))
            plan.enable.append(RegisterWrite(ITM_TER, ter, ter, f"ITM: ポート {sorted(set(request.ports))} を有効に"))
    if swo or request.counters:
        ctrl = _CYCCNTENA | _SYNCTAP_FAST << _SYNCTAP_SHIFT
        ctrl_mask = _CYCCNTENA | _SYNCTAP_FIELD
        if request.exception_trace:
            ctrl, ctrl_mask = ctrl | _EXCTRCENA, ctrl_mask | _EXCTRCENA
        for c in request.counters:
            ctrl, ctrl_mask = ctrl | COUNTERS[c][0], ctrl_mask | COUNTERS[c][0]
        plan.enable.append(RegisterWrite(DWT_CTRL, ctrl, ctrl_mask, "DWT: CYCCNT・同期パケット（CYCCNT[24]）"
                                         + ("・例外トレース" if request.exception_trace else "")
                                         + "".join(f"・{c.upper()} の溢れ" for c in request.counters)))
    for u in comparators:                                  # FUNCTION は COMP・MASK の後（途中の値で一致させない）
        plan.enable += [RegisterWrite(dwt_comp(u.index), u.comp, note=f"DWT_COMP{u.index}: {u.name}"),
                        RegisterWrite(dwt_mask(u.index), u.mask, note=f"DWT_MASK{u.index}: 下位 {u.mask} ビットを無視"),
                        RegisterWrite(dwt_function(u.index), u.function,
                                      note=f"DWT_FUNCTION{u.index}: {u.role} {u.name}")]
    _estimate(plan, request, trace_cmp, core_clock_hz, swo_baud)
    return plan


def _assign(request: TraceRequest, dwt: DwtInfo) -> list[ComparatorUse]:
    if not (request.data_traces or request.value_watches):
        return []
    if dwt.arch != "v7m":
        arch = {"v8m": "ARMv8-M", "v6m": "ARMv6-M"}.get(dwt.arch, dwt.arch)
        raise ValueError(f"比較器のデータトレース・値一致は ARMv7-M（Cortex-M3/M4/M7）だけ: このコアは {arch}"
                         "（ARMv8-M の DWT は FUNCTION の符号化が違う。ARMv6-M はトレースが無い）")
    free = [n for n in range(dwt.numcomp - 1, -1, -1) if n not in dwt.busy]      # pyOCD は 0 から使う: 上から取る
    out: list[ComparatorUse] = []

    def take(candidates, what: str) -> int:
        for n in candidates:
            if n in free:
                free.remove(n)
                return n
        raise ValueError(f"比較器が足りない（{what}。比較器 {dwt.numcomp} 個のうち使用中 {sorted(dwt.busy)}、"
                         f"割り当て済み {len(out)}）")

    for w in request.value_watches:
        if w.size not in _DATAVSIZE:
            raise ValueError(f"{w.name}: 値一致の大きさは 1 / 2 / 4 バイト（{w.size}）")
        if w.action not in ("halt", "trace"):
            raise ValueError(f"{w.name}: action は halt か trace")
        access = w.access or ("write" if w.action == "halt" else "rw")
        if access not in _WATCH:
            raise ValueError(f"{w.name}: access は read / write / rw")
        capable = [n for n in free if n in dwt.value_match and (w.address is None or n in dwt.linkable)]
        if not capable:
            raise ValueError(f"{w.name}: 値の比較のできる比較器が空いていない（DATAVMATCH。Cortex-M3/M4 は 1 番だけ"
                             + ("、番地を限るには LNK1ENA も要る" if w.address is not None else "") + "）")
        index = take(capable, w.name)
        function = _DATAVMATCH | _DATAVSIZE[w.size] << _DATAVSIZE_SHIFT
        emits: tuple[str, ...] = ()
        if w.action == "halt":
            function |= _WATCH[access]
        else:
            if access != "rw":
                raise ValueError(f"{w.name}: 止めない値一致（trace）は access rw だけ（読み・書きを分ける符号化は値の比較と組めない）")
            function |= _TRACE[("rw", w.pc, True)]
            emits = ("pc", "value") if w.pc else ("value",)
        link = None
        if w.address is not None:
            link = take([n for n in free], f"{w.name} の番地")
            function |= link << _DATAVADDR0_SHIFT
        value = w.value & ((1 << 8 * w.size) - 1)
        out.append(ComparatorUse(index, "value_watch", w.name, function, value * (0x01010101 if w.size == 1 else
                                                                                   0x00010001 if w.size == 2 else 1),
                                 0, w.address, w.size, access, emits, value, w.action))
        if link is not None:
            base, mask = address_mask(w.address, w.size)
            _check_mask(w.name, mask, dwt)
            out.append(ComparatorUse(link, "link", w.name, 0, base, mask, w.address, w.size))
    for d in request.data_traces:
        if d.access not in ("read", "write", "rw"):
            raise ValueError(f"{d.name}: access は read / write / rw")
        if d.pc and d.offset:
            raise ValueError(f"{d.name}: pc と offset は同時に出せない（どちらか）")
        key = (d.access, d.pc or d.offset, d.value)
        if key not in _TRACE:
            if not d.value and d.access != "rw":
                raise ValueError(f"{d.name}: 読みだけ・書きだけの PC・番地は値と一緒にしか出せない（value=true か access rw）")
            raise ValueError(f"{d.name}: 出すもの（pc・value・offset）が無い")
        base, mask = address_mask(d.address, d.size)
        _check_mask(d.name, mask, dwt)
        index = take([n for n in free if n < _TRACE_COMPARATORS and n not in dwt.value_match]
                     + [n for n in free if n < _TRACE_COMPARATORS and n in dwt.value_match], d.name)
        emits = (("pc",) if d.pc else ()) + (("offset",) if d.offset else ()) + (("value",) if d.value else ())
        out.append(ComparatorUse(index, "data_trace", d.name, _TRACE[key] | (_EMITRANGE if d.offset else 0), base,
                                 mask, d.address, d.size, d.access, emits))
    return out


def _check_mask(name: str, mask: int, dwt: DwtInfo) -> None:
    if mask > dwt.max_mask:
        raise ValueError(f"{name}: 範囲が大きすぎる（2^{mask} バイト。この DWT の MASK は最大 {dwt.max_mask}）")


def _estimate(plan: TracePlan, request: TraceRequest, trace_cmp: list[ComparatorUse], core_clock_hz: float | None,
              swo_baud: float | None) -> None:
    ts = _TIMESTAMP_BYTES if request.timestamps else 0
    for u in trace_cmp:
        n = (_PC_BYTES if "pc" in u.emits else 0) + (_OFFSET_BYTES if "offset" in u.emits else 0)
        n += (1 + min(4, max(u.size, 1)) if "value" in u.emits else 0) + ts
        plan.bytes_per_access[u.name] = n
        if u.mask > 0 and (1 << u.mask) > u.size:
            plan.warnings.append(f"{u.name}: 比較器は 0x{u.comp:08X} から {1 << u.mask} バイトに一致する（変数の外の"
                                 "アクセスも SWO に出る" + ("。番地で外のものは捨てる" if "offset" in u.emits
                                                      else "。offset=true にすると外のものを捨てられる") + "）")
    if swo_baud is None:
        return
    plan.swo_bytes_per_s_max = swo_baud / _BITS_PER_BYTE
    for name, n in plan.bytes_per_access.items():
        plan.warnings.append(f"{name}: 1 回 {n} バイト。SWO が溢れずに運べるのは毎秒約 "
                             f"{plan.swo_bytes_per_s_max / n:,.0f} 回まで（超えると Overflow で欠ける）")
    if request.counters and core_clock_hz:
        worst = len(request.counters) * core_clock_hz / 256 * _COUNTER_BYTES
        share = worst / plan.swo_bytes_per_s_max                       # 閾値で出す・出さないを決めない
        plan.warnings.append(f"イベントカウンタの溢れは最悪で毎秒 {worst:,.0f} バイト（SWO の帯域の {share:.0%}）"
                             + ("。帯域を超える間は Overflow で欠ける" if share > 1 else ""))


# --- 時刻付きの解読 ------------------------------------------------------------------------------

class TimedItmStream:
    """デバイスの時刻付きの SWO のバイト (時刻, バイト) を増分で解読し、(パケットの先頭のバイトの時刻, パケット)。
    synced=False なら最初の同期パケットまで捨てる（ITM が前から動いていて、受信がパケットの途中から始まるとき）。"""

    def __init__(self, synced: bool = True):
        self._decoder = ItmDecoder(synced)
        self._times: list[int] = []
        self._base = 0                    # _times[0] のバイトの通し番号

    @property
    def pending_bytes(self) -> int:
        return len(self._times)

    def feed(self, byte_events) -> list[tuple[int, Packet]]:
        data = bytearray()
        for t, b in byte_events:
            self._times.append(t)
            data.append(b)
        packets = self._decoder.feed(bytes(data))
        out = [(self._times[min(p.offset - self._base, len(self._times) - 1)], p) for p in packets]
        drop = self._decoder.consumed - self._base     # 解読し終えたバイトの時刻は要らない
        del self._times[:drop]
        self._base += drop
        return out


# --- データトレースの組み立て ------------------------------------------------------------------

@dataclass(frozen=True)
class DataAccess:
    t: int                                # デバイスのタイムベース（最初のパケットの先頭のバイト）
    local_time: int | None                # ITM のローカルのタイムスタンプの累積（有効なとき）
    comparator: int
    name: str
    role: str                             # data_trace / value_watch
    pc: int | None
    address: int | None                   # offset を出すとき（または変数の大きさが比較の範囲と同じとき）の番地
    value: int | None
    size: int | None                      # アクセスの大きさ（値のパケットから）
    write: bool | None
    complete: bool                        # 頼んだパケットがそろった（溢れで欠けると False）


_KINDS = {DataTracePc: "pc", DataTraceAddress: "offset", DataTraceValue: "value"}


class DataTraceAssembler:
    """比較器ごとに、1 回のアクセスのパケット（PC か番地の下位 → 値。ARMv7-M D4.3 の順）を 1 つにまとめる。
    先頭のパケット（PC・番地）が来たら値を待ち、値が来たら出す。値を待っている間に次の先頭が来た・先頭なしに値が来た
    （間が溢れで欠けた）ものは complete=False で出す。Overflow・解読の誤りで組み立て中のものは捨てる。"""

    def __init__(self, comparators: list[ComparatorUse]):
        self._uses = {u.index: u for u in comparators if u.emits}
        self._pending: dict[int, tuple[int, int | None, dict]] = {}
        self.lost = 0                     # Overflow・解読の誤りで捨てた組み立て中のもの
        self.outside = 0                  # 番地が変数の外（比較の範囲が変数より広い）で捨てたもの
        self.unknown = 0                  # 割り当てていない比較器・頼んでいない種類のパケット

    def feed(self, items) -> list[DataAccess]:
        """items: (時刻, ローカルの時刻か None, パケット) の列。"""
        out: list[DataAccess] = []
        for t, local, p in items:
            if isinstance(p, (Overflow, DecodeError)) or (isinstance(p, Sync) and p.skipped):
                self.lost += len(self._pending)
                self._pending.clear()
                continue
            kind = _KINDS.get(type(p))
            if kind is None:
                continue
            use = self._uses.get(p.comparator)
            if use is None or kind not in use.emits:
                self.unknown += 1
                continue
            pending = self._pending.pop(use.index, None)
            if kind != "value":                                       # 先頭: 前のものは値が来ないまま
                if pending is not None:
                    self._emit(use, pending, out)
                pending = (t, local, {kind: p})
                if "value" in use.emits:
                    self._pending[use.index] = pending
                else:
                    self._emit(use, pending, out)
                continue
            if pending is None:
                pending = (t, local, {})
            elif local is not None and pending[1] is None:
                pending = (pending[0], local, pending[2])
            pending[2]["value"] = p
            self._emit(use, pending, out)
        return out

    def flush(self) -> list[DataAccess]:
        out: list[DataAccess] = []
        for index in list(self._pending):
            self._emit(self._uses[index], self._pending[index], out)
        return out

    def _emit(self, use: ComparatorUse, pending, out: list[DataAccess]) -> None:
        self._pending.pop(use.index, None)
        t, local, parts = pending
        pc = parts.get("pc")
        offset = parts.get("offset")
        value = parts.get("value")
        address = None
        if offset is not None:
            address = (use.comp & ~0xFFFF) | offset.address_low
        elif use.address is not None and use.mask is not None and (1 << use.mask) <= max(use.size, 1):
            address = use.address
        if address is not None and use.address is not None and use.role == "data_trace" \
                and not use.address <= address < use.address + use.size:
            self.outside += 1
            return
        out.append(DataAccess(t, local, use.index, use.name, use.role, pc.pc if pc else None, address,
                              value.value if value else None, value.size if value else None,
                              value.write if value else None, set(parts) == set(use.emits)))


# --- イベントカウンタ ----------------------------------------------------------------------------

class CounterTotals:
    """EventCounter のパケット（8 ビットのカウンタが溢れた印）を数える。合計はカウンタの増分の 256 単位の部分。"""

    def __init__(self):
        self.overflows = {name: 0 for name in COUNTERS}

    def feed(self, packets) -> None:
        for p in packets:
            if isinstance(p, EventCounter):
                for name, (_, _, flag) in COUNTERS.items():
                    if p.flags & flag:
                        self.overflows[name] += 1

    def as_dict(self) -> dict[str, int]:
        return {name: 256 * n for name, n in self.overflows.items()}

