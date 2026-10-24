"""接続の診断（U29）:「なぜつながらないか」を順に確かめ、各段で読めた事実を返す。J-Link の EmuDiag・BMP の
swdp_scan に当たる。

順番（前の段が駄目でも、分かることは続けて調べる）:
1. 電圧（補助マイコンの ADC）  2. ピン（nRESET・SWDIO・SWCLK）
3. SWD: 使う側が渡した速さ（frequencies_hz）を順に、複数ドロップ（RP2040 の TARGETSEL）→ JTAG→SWD → 休眠からの起こし。
   どの ACK で駄目だったか
4. SWD が駄目なら JTAG のチェーンを走査（同じ速さの並びを順に。JTAG だけのターゲット: Raspberry Pi 等）。JTAG-DP が
   あればそこから続ける
5. DP: DPIDR・TARGETID・誤りの印・デバッグ電源の要求への応答
6. AP の一覧（ADIv5 は APSEL、ADIv6 は BASEPTR の ROM テーブル）と、MEM-AP ごとの ROM テーブル → 部品
7. コア（CPUID・DHCSR: 止まっている・眠っている・ロックアップ）と、部品の判別（pyOCD のターゲット名の候補）
8. 保護（STM32 の RDP・nRF の APPROTECT・MEM-AP の DeviceEn）

段ごとに status（ok = その段で読もうとしたものを読めた / fail = 読めなかった / skip = 測る手段が無い）と日本語の
message（読めた事実）・data を返す。良し悪しの判定（warn）・結論の文・接続の引数のおすすめは本体ではしない
（プラグイン fprobe.plugins.heuristics.diagnose_verdict。CLAUDE.md「fprobe 本体にヒューリスティックを入れない」）。
試す速さは使う側が渡す（暗黙に下げない）。機器は RawDap（CMSIS-DAP の層）で触る。pyOCD のターゲットは作らない。
"""

from collections.abc import Callable
from dataclasses import dataclass, field

from . import coresight_id as ci
from .jtag_chain import ChainScan, dap_link_wire, scan_chain
from .multidrop import PARTS
from .raw_dap import AP_BASE, AP_CSW, AP_IDR, CSW_DEVICE_EN, DP_BASEPTR0, DP_CTRL_STAT, DP_TARGETID, Ack, DapError, RawDap

__all__ = ["Diagnosis", "Step", "diagnose"]

IO_VOLTS = 3.3                         # fprobe の入出力の電圧（LVCMOS33）。比べた判定はしない（事実として並べる）
_DHCSR, _CPUID = 0xE000EDF0, 0xE000ED00
_NORDIC_CTRL_AP_IDR = 0x02880000
_MEM_AP_ARCHID = 0x0A17               # ADIv6 の DEVARCH の ARCHID（MEM-AP）

Protection = Callable[[ci.Part, Callable[[int], int]], dict | None]


@dataclass
class Step:
    name: str
    status: str                        # ok（読めた）/ fail（読めなかった）/ skip（測る手段が無い）
    message: str
    data: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"name": self.name, "status": self.status, "message": self.message, **({"data": self.data} if self.data else {})}


@dataclass
class Diagnosis:
    steps: list[Step] = field(default_factory=list)
    frequencies_hz: list[int] = field(default_factory=list)   # 試した速さ（使う側が渡した順）
    protocol: str | None = None
    frequency_hz: int | None = None    # DP が応答した速さ
    reached: bool = False              # DP に届き、デバッグ電源の要求に応答があった
    dp: dict | None = None
    aps: list[dict] = field(default_factory=list)
    core: dict | None = None
    part: ci.Part | None = None
    jtag: ChainScan | None = None

    def add(self, name: str, status: str, message: str, /, **data) -> Step:
        step = Step(name, status, message, data)
        self.steps.append(step)
        return step

    def as_dict(self) -> dict:
        return {"reached": self.reached, "frequencies_hz": list(self.frequencies_hz), "protocol": self.protocol,
                "frequency_hz": self.frequency_hz, "dp": self.dp, "aps": self.aps, "core": self.core,
                "part": self.part.as_dict() if self.part else None,
                "jtag": self.jtag.as_dict() if self.jtag else None,
                "steps": [s.as_dict() for s in self.steps]}


def diagnose(dap: RawDap, frequencies_hz: list[int], voltage: Callable[[], dict] | None = None,
             pins: Callable[[], int] | None = None, protection: Protection | None = None, max_aps: int = 8,
             jtag: bool = True) -> Diagnosis:
    """frequencies_hz: 試す速さ（Hz）。この順に試し、最初に応答した速さで続ける（使う側が渡す。暗黙に下げない）。"""
    if not frequencies_hz:
        raise ValueError("試す速さ frequencies_hz を 1 つ以上渡す")
    d = Diagnosis(frequencies_hz=list(dict.fromkeys(frequencies_hz)))
    _check_voltage(d, voltage)
    _check_pins(d, pins)
    attempt = _connect_swd(d, dap, d.frequencies_hz)
    if attempt is None and jtag:
        attempt = _connect_jtag(d, dap, d.frequencies_hz)
    if attempt is None:
        return d
    d.protocol, d.frequency_hz = dap.protocol, dap.frequency_hz
    if not _check_dp(d, dap):
        return d
    d.reached = True
    mem_aps = _list_aps(d, dap, max_aps)
    _check_core_and_part(d, dap, mem_aps, protection)
    return d


DEMCR, TRCENA = 0xE000EDFC, 1 << 24


def _walk_with_trcena(dap: RawDap, ap: int, base: int) -> "ci.Component":
    """ROM テーブルをたどる。Cortex-M の DWT・ITM・TPIU は DEMCR.TRCENA = 1 の間しか読めないので、0 なら一時的に
    立てて読み、元に戻す（実機で 3 つが「読めない」になった。2026-10-18）。DEMCR が無い（Cortex-M でない）なら
    そのまま。"""
    try:
        demcr = dap.mem_read32(ap, DEMCR)
    except Exception:
        _clear(dap)
        demcr = None
    set_here = demcr is not None and not demcr & TRCENA
    if set_here:
        dap.mem_write32(ap, DEMCR, demcr | TRCENA)
    try:
        return ci.walk_rom_table(lambda a: dap.mem_read32(ap, a), base)
    finally:
        if set_here:
            dap.mem_write32(ap, DEMCR, demcr)


# --- 1. 電圧・2. ピン ------------------------------------------------------------------------------

def _check_voltage(d: Diagnosis, voltage) -> None:
    if voltage is None:
        d.add("voltage", "skip", "電圧を測る手段が無い")
        return
    try:
        v = voltage()
    except Exception as e:
        d.add("voltage", "skip", f"電圧を測れない（{e}）")
        return
    if not v.get("live"):
        d.add("voltage", "skip", "電圧を測れない（補助マイコンが計測値を送っていない）", **v)
    elif v.get("over_range"):
        d.add("voltage", "ok", "ターゲットの電圧が測れる上限を超えている", **v)
    else:                               # 閾値で「電源が入っていない」等と判定しない（計測の線が外れても 0 V になる）
        d.add("voltage", "ok", f"Vtarget = {v['volts']:.2f} V（fprobe の入出力は {IO_VOLTS} V）", **v)


def _check_pins(d: Diagnosis, pins) -> None:
    if pins is None:
        d.add("pins", "skip", "ピンを読めない")
        return
    value = pins()
    state = {"swclk": value & 1, "swdio": (value >> 1) & 1, "nreset": (value >> 7) & 1}
    low = [name for key, name in (("nreset", "nRESET"), ("swdio", "SWDIO")) if not state[key]]
    text = " / ".join(f"{name} = Low" for name in low) or "nRESET = High、SWDIO = High"
    d.add("pins", "ok", text, **state)                  # Low の良し悪し・原因の候補は決めない（事実だけ）


# --- 3. SWD・4. JTAG ------------------------------------------------------------------------------

def _try_dpidr(dap: RawDap) -> tuple[int | None, Ack]:
    try:
        return dap.read_dpidr(), Ack.OK
    except DapError as e:
        return None, e.ack


def _dp_targetsel(dap: RawDap, dpidr: int) -> int | None:
    """応答した DP の TARGETSEL の値（DPv2 の TARGETID の下位 28 ビットと DLPIDR の TINSTANCE）。DPv1 なら None。"""
    if ci.decode_dpidr(dpidr)["version"] < 2:
        return None
    try:
        return (dap.read_dp("TARGETID") & 0x0FFFFFFF) | (dap.read_dp("DLPIDR") & 0xF0000000)
    except DapError:
        _clear(dap)
        return None


def _rescue_dp(dap: RawDap, dpidr: int) -> int | None:
    """応答した DP が複数ドロップの部品の救済の DP なら、その TARGETSEL。救済の DP は CTRL/STAT の CDBGPWRUPREQ で
    チップ全体をリセットして止める（RP2040）ので、電源を要求してはいけない。"""
    sel = _dp_targetsel(dap, dpidr)
    return sel if sel in {p.rescue for p in PARTS if p.rescue is not None} else None


def _connect_swd(d: Diagnosis, dap: RawDap, speeds: list[int]) -> int | None:
    attempts: list[dict] = []
    rescue_seen: int | None = None
    core_sels = [core.targetsel for part in PARTS for core in part.cores]   # 複数ドロップの部品の表（multidrop.PARTS）
    for hz in speeds:
        dap.start("swd", hz)
        # 複数ドロップを先に: TARGETSEL の無い線では複数ドロップの DP が全部同時に応答し、書き込みが救済の DP にも
        # 届いてチップがリセットされたまま止まる（2026-10-22、RP2040）。選んだ DP の TARGETID・DLPIDR が選んだ値と
        # 合ったときだけ複数ドロップのコアとし、合わなければ（TARGETSEL を無視する普通の DP）素の切り替えへ
        for how in ("multidrop", "jtag_to_swd", "dormant"):
            if how == "multidrop":
                dap.switch_to_swd(dormant=True)
                for sel in core_sels:
                    dap.line_reset()
                    dap.link.swj_sequence(8, 0)
                    dap.targetsel(sel)
                    value, ack = _try_dpidr(dap)
                    attempts.append({"frequency_hz": hz, "method": f"targetsel 0x{sel:08X}", "ack": ack.value})
                    if value is None:
                        continue
                    if _dp_targetsel(dap, value) == sel:
                        return _swd_ok(d, dap, value, attempts, speeds[0], targetsel=sel)
                    attempts[-1]["ignored_targetsel"] = True       # 複数ドロップでない DP が応答した
                    break
                continue
            dap.switch_to_swd(dormant=how == "dormant")
            value, ack = _try_dpidr(dap)
            attempts.append({"frequency_hz": hz, "method": how, "ack": ack.value})
            if value is not None:
                rescue = _rescue_dp(dap, value)
                if rescue is None:
                    return _swd_ok(d, dap, value, attempts, speeds[0])
                attempts[-1]["rescue_dp"] = f"0x{rescue:08X}"         # 救済の DP: 電源を要求せず、コアの DP を選びに行く
                rescue_seen = rescue
    acks = {a["ack"] for a in attempts}
    if rescue_seen is not None:
        msg = (f"TARGETSEL なしで応答したのは救済の DP（TARGETSEL 0x{rescue_seen:08X}）だけで、コアの DP は"
               "応答しなかった（救済の DP には電源を要求しない: チップ全体がリセットされて止まる）")
    elif acks == {Ack.NO_ACK.value}:
        msg = f"SWD に応答が無い（試した速さ {', '.join(f'{hz:,}' for hz in speeds)} Hz のどの試し方でも ACK が来ない）"
    elif Ack.PROTOCOL.value in acks:
        msg = "SWD の応答にパリティ誤りがある"
    elif Ack.WAIT.value in acks:
        msg = "SWD が WAIT を返し続ける"
    else:
        msg = f"SWD の DPIDR を読めない（{', '.join(sorted(acks))}）"
    d.add("swd", "fail", msg, attempts=attempts)
    return None


def _swd_ok(d: Diagnosis, dap: RawDap, dpidr: int, attempts: list[dict], first_hz: int,
            targetsel: int | None = None) -> int:
    info = ci.decode_dpidr(dpidr)
    method = attempts[-1]["method"]
    how = {"jtag_to_swd": "JTAG→SWD の切り替え", "dormant": "休眠からの起こし"}.get(method, f"複数ドロップの {method}")
    text = f"SWD で応答（DPIDR 0x{dpidr:08X}、{info['designer']} DPv{info['version']}、{how}、{dap.frequency_hz:,} Hz）"
    if dap.frequency_hz != first_hz:
        text += f"。{first_hz:,} Hz では応答せず {dap.frequency_hz:,} Hz で応答"
    d.add("swd", "ok", text, attempts=attempts, frequency_hz=dap.frequency_hz,
          **({"targetsel": targetsel} if targetsel is not None else {}))
    d.dp = {"dpidr": info}
    return dpidr


def _connect_jtag(d: Diagnosis, dap: RawDap, speeds: list[int]) -> int | None:
    """JTAG のチェーンを、渡された速さの順に装置が見えるまで走査する（暗黙の上限の速さを置かない）。"""
    scans = []
    for hz in speeds:
        dap.start("jtag", hz)
        dap.switch_to_jtag()
        scan = scan_chain(dap_link_wire(dap.link))
        scans.append({"frequency_hz": hz, "devices": len(scan.devices)})
        if scan.devices:
            break
    d.jtag = scan
    if not scan.devices:
        d.add("jtag", "fail", f"JTAG にも装置が見えない（試した速さ {', '.join(f'{f:,}' for f in speeds)} Hz）: "
              + " / ".join(scan.notes), scan=scan.as_dict(), scans=scans)
        return None
    names = ", ".join(f"{dev.name or 'IDCODE ' + (f'0x{dev.idcode:08X}' if dev.idcode is not None else 'なし')}"
                      for dev in scan.devices)
    dp_index = next((dev.index for dev in scan.devices if dev.name and "JTAG-DP" in dev.name), None)
    if dp_index is None or scan.chain is None:
        reason = "IR 長が分からない装置がある" if scan.chain is None else "Arm の JTAG-DP が無い"
        d.add("jtag", "fail", f"SWD は応答しないが JTAG に {len(scan.devices)} 台（{names}）。{reason}",
              scan=scan.as_dict())
        return None
    dap.start("jtag", hz, scan.chain, dap_index=dp_index)
    value, ack = _try_dpidr(dap)
    if value is None:
        d.add("jtag", "fail", f"JTAG に {len(scan.devices)} 台（{names}）。JTAG-DP の DPIDR を読めない（{ack.value}）",
              scan=scan.as_dict())
        return None
    swd = next(s for s in d.steps if s.name == "swd")
    swd.status, swd.message = "skip", swd.message + "。JTAG で応答したので JTAG を使う"
    d.add("jtag", "ok", f"SWD は応答しないが JTAG で応答（{names}。jtag_chain = {scan.chain}、{hz:,} Hz）",
          scan=scan.as_dict(), scans=scans, frequency_hz=hz)
    d.dp = {"dpidr": ci.decode_dpidr(value), "jtag_dp_index": dp_index}
    return value


# --- 5. DP -----------------------------------------------------------------------------------------

def _check_dp(d: Diagnosis, dap: RawDap) -> bool:
    try:
        ctrl = dap.read_dp(DP_CTRL_STAT)
        if ctrl & 0xB2:                                         # STICKYORUN / STICKYCMP / STICKYERR / WDATAERR
            d.add("dp_errors", "ok", f"DP に誤りの印が残っていた（CTRL/STAT 0x{ctrl:08X}）: 消した", ctrl_stat=ctrl)
            dap.clear_errors()
        if d.dp["dpidr"]["version"] >= 2 and dap.protocol == "swd":
            d.dp["targetid"] = ci.decode_targetid(dap.read_dp(DP_TARGETID))
        power = dap.power_up()
    except DapError as e:
        d.add("dp", "fail", f"DP のレジスタを読み書きできない: {e}")
        return False
    d.dp["power"] = power
    if not power["debug_power"]:
        d.add("power", "fail", "デバッグ電源の要求に応答しない（CDBGPWRUPACK = 0）", **power)
        return False
    d.add("power", "ok", "デバッグ電源が上がった" + ("" if power["system_power"] else
                                                    "。システム電源の応答は無い（CSYSPWRUPACK = 0）"), **power)
    return True


# --- 6. AP -----------------------------------------------------------------------------------------

def _list_aps(d: Diagnosis, dap: RawDap, max_aps: int) -> list[tuple[int, ci.Component | None]]:
    """AP を並べ、MEM-AP ごとに ROM テーブルをたどる。[(AP の番号か番地, ROM の根)]。"""
    found: list[tuple[int, dict]] = []
    if dap.adi_v6:
        try:
            ptr = dap.read_dp(DP_BASEPTR0)
        except DapError as e:
            d.add("aps", "fail", f"BASEPTR0 を読めない: {e}")
            return []
        if not ptr & 1:
            d.add("aps", "fail", "BASEPTR0 が無効（ADIv6 の ROM テーブルが無い）")
            return []
        root = ci.walk_rom_table(lambda a: dap.read_ap(a & ~0xFFF, a & 0xFFF), ptr & ~0xFFF)
        for comp in root.flatten():
            if comp.cls == ci.CLASS_CORESIGHT and comp.devarch is not None and comp.devarch & 0xFFFF == _MEM_AP_ARCHID:
                found.append((comp.address, _read_ap_info(dap, comp.address)))
    else:
        for apsel in range(max_aps):
            try:
                idr = dap.read_ap(apsel, AP_IDR)
            except DapError as e:
                d.add("ap_idr", "fail", f"AP {apsel} の IDR を読めない: {e}", ap=apsel)
                _clear(dap)
                continue
            if idr:
                found.append((apsel, _read_ap_info(dap, apsel, idr)))
    mem_aps: list[tuple[int, ci.Component | None]] = []
    for ap, info in found:
        d.aps.append(info)
        if info["kind"] != "mem-ap":
            continue
        if not info.get("device_enabled", True):
            d.add("ap_disabled", "ok", f"MEM-AP {_ap_name(ap)} が無効（CSW.DeviceEn = 0）", ap=ap)
            mem_aps.append((ap, None))
            continue
        root = None
        base = info.get("base")
        if base is not None and base & 1 and base != 0xFFFFFFFF:
            root = _walk_with_trcena(dap, ap, base & ~0xFFF)
            if root.error:
                _clear(dap)
            info["rom_table"] = root.as_dict()
        mem_aps.append((ap, root))
    if not found:
        d.add("aps", "fail", "AP が見つからない")
    else:
        d.add("aps", "ok", "AP: " + ", ".join(f"{_ap_name(i['ap'])} {i['kind']}" + (f"（{i['bus']}）" if i.get("bus") else "")
                                         for _, i in found))
    _check_nordic(d, dap, found)
    return mem_aps


def _ap_name(ap: int) -> str:
    return f"0x{ap:X}" if ap > 0xFF else str(ap)


def _read_ap_info(dap: RawDap, ap: int, idr: int | None = None) -> dict:
    try:
        if idr is None:
            idr = dap.read_ap(ap, dap.mem_register(AP_IDR))
        info = {"ap": ap, **ci.decode_ap_idr(idr)}
        if info["kind"] == "mem-ap":
            csw = dap.read_ap(ap, dap.mem_register(AP_CSW))
            info["csw"] = csw
            info["device_enabled"] = bool(csw & CSW_DEVICE_EN)
            info["base"] = dap.read_ap(ap, dap.mem_register(AP_BASE))
        return info
    except DapError as e:
        _clear(dap)
        return {"ap": ap, "kind": "unreadable", "error": str(e)}


def _check_nordic(d: Diagnosis, dap: RawDap, found) -> None:
    for ap, info in found:
        if info.get("value") == _NORDIC_CTRL_AP_IDR:
            try:
                status = dap.read_ap(ap, 0x0C)                      # APPROTECTSTATUS
            except DapError:
                _clear(dap)
                continue
            if status & 1 == 0:
                d.add("protection", "ok", f"nRF の APPROTECT が有効（CTRL-AP {ap}）: メモリは読めない。CTRL-AP の "
                                          "ERASEALL で全消去すると解除される（データは消える）", ap=ap, approtect=True)


def _clear(dap: RawDap) -> None:
    try:
        dap.clear_errors()
    except DapError:
        pass


# --- 7. コア・部品・8. 保護 --------------------------------------------------------------------------

def _check_core_and_part(d: Diagnosis, dap: RawDap, mem_aps, protection: Protection | None) -> None:
    if not mem_aps:
        return
    with_scs = [ap for ap, root in mem_aps if root is not None and any(c.is_scs for c in root.flatten())]
    ap = with_scs[0] if with_scs else next((ap for ap, root in mem_aps if root is not None), mem_aps[0][0])

    def read(address: int) -> int:
        try:
            return dap.mem_read32(ap, address)
        except DapError:
            _clear(dap)
            raise
    try:
        cpuid = ci.decode_cpuid(read(_CPUID))
        dhcsr = read(_DHCSR)
    except DapError as e:
        d.add("core", "fail", f"コアのレジスタを読めない（MEM-AP {_ap_name(ap)}）: {e}")
        cpuid, dhcsr = None, None
    if cpuid is not None:
        d.core = {**cpuid, "ap": ap, "dhcsr": dhcsr, "halted": bool(dhcsr >> 17 & 1), "sleeping": bool(dhcsr >> 18 & 1),
                  "lockup": bool(dhcsr >> 19 & 1), "debug_enabled": bool(dhcsr & 1)}
        state = "止まっている" if d.core["halted"] else "眠っている" if d.core["sleeping"] else "走っている"
        if d.core["lockup"]:
            state = "ロックアップしている（故障処理の中で故障。DHCSR.S_LOCKUP）"
        d.add("core", "ok", f"{cpuid['core']}（{state}）", **d.core)
    designers = {root.designer for _, root in mem_aps if root is not None}
    if d.dp and "targetid" in d.dp:
        designers.add(ci.designer11((d.dp["targetid"]["value"] >> 1) & 0x7FF))
    d.part = ci.identify_part(read, designers)
    if d.part is None:
        d.add("part", "fail", "部品を判別できない（DBGMCU・FICR・SYSINFO の表に当たらない）")
    else:
        cands = d.part.candidates
        extra = (f"。pyOCD のターゲット: {cands[0]}" if len(cands) == 1 else
                 f"。候補: {', '.join(cands)}" if cands else "。pyOCD の内蔵ターゲットに無い（target_pack_find で探す）")
        d.add("part", "ok", f"{d.part.name}（{d.part.source}）{extra}", **d.part.as_dict())
        if protection is not None:
            try:
                prot = protection(d.part, read)
            except DapError as e:
                prot = None
                d.add("protection", "fail", f"保護の状態を読めない: {e}")
            if prot:
                d.add("protection", "ok", prot.pop("message"), **prot)
