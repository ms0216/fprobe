"""JTAG のチェーンの低レベル操作（U29）: TAP リセット、IR / DR のシフト、チェーンの走査（IDCODE・IR 長の推定）、
生のビット列。

線の口は JtagWire（CMSIS-DAP の DAP_JTAG_Sequence 1 つ分: cycles ≤ 64 の間 TMS を一定にし、TDI を出し、
求めれば TDO を取る）。fprobe の CMSIS-DAP（`dap_link_wire`）でも試験の偽物でも同じ。

約束（すべての関数）: 呼ぶ前と後で、すべての TAP は Run-Test/Idle にいる（reset_tap だけは前提なし）。
チェーンの装置の番号は TDO 側から 0, 1, ...（pyOCD・target_connect の jtag_chain と同じ）。

IR 長の推定: Shift-IR で 0 を詰めてから 1 を詰め、1 が TDO に出るまでの数が IR の合計長。各装置の Capture-IR の値の
下位 2 ビットは 1149.1 で "01"（LSB が 1）なので、取り出した値の "1,0" の並びが装置の境目の候補。既知の IDCODE の
IR 長（表）と合わせて分ける。境目が一意でなければ推測せず ir_length_source = "ambiguous"（ir_candidates に候補）。
走査は IR に 0 を入れたまま Update-IR を通らない（0 = EXTEST の装置があるため。最後は全ビット 1 = BYPASS）。
"""

from dataclasses import dataclass, field
from typing import Protocol

from .coresight_id import decode_idcode

__all__ = ["ChainDevice", "ChainScan", "JtagWire", "KNOWN_IDCODES", "bits_from_text", "clock", "dap_link_wire", "known_device",
           "reset_tap", "scan_chain", "shift", "shift_device"]

SEQUENCE_MAX = 64


class JtagWire(Protocol):
    def sequence(self, cycles: int, tms: int, tdi: int, capture: bool) -> int | None:
        """cycles（1〜64）の間 TMS を tms に保ち、TDI に tdi（LSB から）を出す。capture なら TDO（LSB が最初）。"""


def dap_link_wire(link) -> JtagWire:
    """pyOCD の DAPAccessCMSISDAP（JTAG で connect 済み）を JtagWire にする。"""
    class _Wire:
        def sequence(self, cycles, tms, tdi, capture):
            data = link.jtag_sequence(cycles, tms, capture, tdi)
            return int.from_bytes(bytes(data or b""), "little") & ((1 << cycles) - 1) if capture else None
    return _Wire()


# (マスク, 値, 名前, IR 長)。版（上位 4 ビット）を無視するものはマスクで落とす
KNOWN_IDCODES: list[tuple[int, int, str, int]] = [
    (0x0FFFFFFF, 0x0BA00477, "Arm JTAG-DP (ADIv5)", 4),
    (0x0FFFFFFF, 0x0BA01477, "Arm JTAG-DP (ADIv5)", 4),
    (0x0FFFFFFF, 0x0BA02477, "Arm JTAG-DP (ADIv5)", 4),
    (0x0FFFFFFF, 0x0BA04477, "Arm JTAG-DP (ADIv5)", 4),
    (0x0FFFFFFF, 0x0BA05477, "Arm JTAG-DP (ADIv5)", 4),
    (0x0FFFFFFF, 0x0BA06477, "Arm JTAG-DP (ADIv6)", 4),
    (0x0FFFFFFF, 0x07B7617F, "ARM1176JZF-S (BCM2835)", 5),
    (0x0FF00FFF, 0x06400041, "STM32 boundary scan", 5),
    (0x0FFFFFFF, 0x0000081B, "Gowin GW2A-18", 8),
    (0x0FFFFFFF, 0x0100481B, "Gowin GW1N-9", 8),
    (0x0FFFFFFF, 0x0100981B, "Gowin GW1NSR-4C", 8),
]


def known_device(idcode: int) -> tuple[str, int] | None:
    for mask, value, name, ir_length in KNOWN_IDCODES:
        if idcode & mask == value & mask:
            return name, ir_length
    return None


# --- 線の操作 -------------------------------------------------------------------------------------

def clock(wire: JtagWire, tms: list[int], tdi: list[int]) -> list[int]:
    """生のビット列（時間順）を TCK ごとに出し、TDO を時間順で返す。TMS が同じ間を 64 までまとめて送る。"""
    if len(tms) != len(tdi):
        raise ValueError("tms と tdi の長さが違う")
    out: list[int] = []
    i = 0
    while i < len(tms):
        j = i
        while j < len(tms) and j - i < SEQUENCE_MAX and tms[j] == tms[i]:
            j += 1
        bits = sum(b << k for k, b in enumerate(tdi[i:j]))
        tdo = wire.sequence(j - i, tms[i], bits, True) or 0
        out += [(tdo >> k) & 1 for k in range(j - i)]
        i = j
    return out


def _send(wire: JtagWire, tms: int, cycles: int) -> None:
    while cycles > 0:
        n = min(cycles, SEQUENCE_MAX)
        wire.sequence(n, tms, 0 if not tms else (1 << n) - 1, False)
        cycles -= n


def reset_tap(wire: JtagWire) -> None:
    """TMS = 1 を 6 回で Test-Logic-Reset（IR はすべて IDCODE か BYPASS）、TMS = 0 で Run-Test/Idle へ。"""
    _send(wire, 1, 6)
    _send(wire, 0, 1)


def shift(wire: JtagWire, ir: bool, value: int, length: int) -> int:
    """Run-Test/Idle から IR（ir=True）か DR を length ビット シフトし（value を LSB から入れる）、Update を通って
    Run-Test/Idle に戻る。取り出した値（最初に出たビットが LSB）を返す。"""
    if length < 1:
        raise ValueError("length は 1 以上")
    head = [1, 1, 0, 0] if ir else [1, 0, 0]               # Select-DR(, Select-IR), Capture, Shift へ
    tms = head + [0] * (length - 1) + [1] + [1, 0]         # 最後のビットで Exit1、Update、Run-Test/Idle
    tdi = [0] * len(head) + [(value >> k) & 1 for k in range(length)] + [0, 0]
    tdo = clock(wire, tms, tdi)[len(head):len(head) + length]
    return sum(b << k for k, b in enumerate(tdo))


def shift_device(wire: JtagWire, chain: list[int], device: int, ir: int | None = None,
                 dr_value: int = 0, dr_length: int = 0) -> dict:
    """チェーンの device 番目だけに IR（ほかは BYPASS = すべて 1）を入れ、DR を dr_length ビット シフトする
    （ほかの装置は BYPASS の 1 ビットずつ）。ir が None なら IR は変えない（ほかの装置が BYPASS にいる前提）。"""
    if not 0 <= device < len(chain):
        raise ValueError(f"device は 0〜{len(chain) - 1}")
    out: dict = {}
    if ir is not None:
        if ir >> chain[device]:
            raise ValueError(f"ir が IR 長 {chain[device]} ビットに入らない")
        offset = sum(chain[:device])
        total = sum(chain)
        value = ((1 << total) - 1) & ~(((1 << chain[device]) - 1) << offset) | ir << offset
        captured = shift(wire, True, value, total)
        out["ir_capture"] = (captured >> offset) & ((1 << chain[device]) - 1)
    if dr_length:
        if dr_value >> dr_length:
            raise ValueError(f"dr_value が {dr_length} ビットに入らない")
        before, after = device, len(chain) - 1 - device
        captured = shift(wire, False, dr_value << before | ((1 << after) - 1) << (before + dr_length),
                         before + dr_length + after)
        out["dr_capture"] = (captured >> before) & ((1 << dr_length) - 1)
    return out


# --- チェーンの走査 --------------------------------------------------------------------------------

@dataclass
class ChainDevice:
    index: int
    idcode: int | None                      # None = リセット後に BYPASS（IDCODE を持たない）
    ir_length: int | None
    ir_length_source: str                   # table / measured / ambiguous（Capture-IR から一つに決まらない）/ unknown
    name: str | None = None
    ir_candidates: list[int] | None = None  # ambiguous のときの IR 長の候補（Capture-IR の境目から）

    def as_dict(self) -> dict:
        out = {"index": self.index, "idcode": self.idcode, "name": self.name, "ir_length": self.ir_length,
               "ir_length_source": self.ir_length_source}
        if self.ir_candidates is not None:
            out["ir_candidates"] = self.ir_candidates
        if self.idcode is not None:
            d = decode_idcode(self.idcode)
            out.update(designer=d["designer"], partno=d["partno"], version=d["version"])
        return out


@dataclass
class ChainScan:
    devices: list[ChainDevice]
    total_ir_length: int | None
    ir_capture: int | None
    notes: list[str] = field(default_factory=list)
    tdo: str = "ok"                         # ok / stuck_high / stuck_low

    @property
    def chain(self) -> list[int] | None:
        """target_connect の jtag_chain にそのまま渡せる IR 長の列（すべて分かったときだけ）。"""
        lengths = [d.ir_length for d in self.devices]
        return lengths if self.devices and None not in lengths else None

    def as_dict(self) -> dict:
        return {"devices": [d.as_dict() for d in self.devices], "count": len(self.devices),
                "total_ir_length": self.total_ir_length, "ir_capture": self.ir_capture, "jtag_chain": self.chain,
                "tdo": self.tdo, "notes": self.notes}


def scan_chain(wire: JtagWire, max_devices: int = 16, max_ir_bits: int = 256) -> ChainScan:
    """チェーンを走査する: TAP リセット → DR を読んで IDCODE / BYPASS の並び → IR の合計長と Capture-IR → 装置ごとの IR 長。"""
    reset_tap(wire)
    dr_bits = 32 * (max_devices + 1)
    dr = shift(wire, False, (1 << dr_bits) - 1, dr_bits)          # 1 を詰める: 装置の後ろから 1 が続く
    if dr == 0:
        reset_tap(wire)
        return ChainScan([], None, None, ["TDO がずっと 0"], "stuck_low")
    devices: list[ChainDevice] = []
    pos = 0
    notes: list[str] = []
    while pos < dr_bits - 32:
        if not (dr >> pos) & 1:
            devices.append(ChainDevice(len(devices), None, None, "unknown"))
            pos += 1
            continue
        idcode = (dr >> pos) & 0xFFFFFFFF
        if idcode == 0xFFFFFFFF:
            break
        known = known_device(idcode)
        devices.append(ChainDevice(len(devices), idcode, known[1] if known else None,
                                   "table" if known else "unknown", known[0] if known else None))
        pos += 32
        if len(devices) > max_devices:
            notes.append(f"装置が {max_devices} 台を超えた: 走査を打ち切った")
            break
    if not devices:
        reset_tap(wire)
        return ChainScan([], None, None, ["TDO がずっと 1: チェーンに装置が見えない"], "stuck_high")
    # IR の合計長: 0 を max_ir_bits 詰め、続けて 1 を max_ir_bits 詰める。1 が TDO に出た位置 - max_ir_bits
    ir = shift(wire, True, ((1 << max_ir_bits) - 1) << max_ir_bits, 2 * max_ir_bits)
    first_one = next((k for k in range(max_ir_bits, 2 * max_ir_bits) if (ir >> k) & 1), None)
    reset_tap(wire)                                                # IDCODE に戻す
    if first_one is None:
        notes.append(f"IR の合計長が {max_ir_bits} ビットを超えるか、TDO が途中で切れている")
        return ChainScan(devices, None, None, notes)
    total = first_one - max_ir_bits
    capture = ir & ((1 << total) - 1)
    _split_ir(devices, total, capture, notes)
    return ChainScan(devices, total, capture, notes)


def _split_ir(devices: list[ChainDevice], total: int, capture: int, notes: list[str]) -> None:
    """IR の合計長と Capture-IR の値を装置に分ける（TDO 側の装置の IR が下位）。"""
    bit = lambda k: (capture >> k) & 1                                         # noqa: E731
    starts = {k for k in range(total) if bit(k) == 1 and (k + 1 >= total or bit(k + 1) == 0)}
    pos = 0
    for i, dev in enumerate(devices):
        rest = devices[i + 1:]
        if pos >= total:
            notes.append(f"装置 {i} 以降の IR が合計長に入らない: 表の IR 長か走査の結果が違う")
            break
        if pos not in starts:
            notes.append(f"装置 {i} の Capture-IR の下位 2 ビットが 01 でない（位置 {pos}）")
        if dev.ir_length is not None:                                         # 表で分かっている
            pos += dev.ir_length
            continue
        if not rest:
            dev.ir_length, dev.ir_length_source = total - pos, "measured"
        elif all(d.ir_length is not None for d in rest):
            dev.ir_length, dev.ir_length_source = total - pos - sum(d.ir_length for d in rest), "measured"
        else:
            # 境目になれるのは、残りの装置に 1 台 2 ビット以上が残る位置だけ。候補の数が残りの台数と同じなら、
            # いちばん近いものがこの装置の終わり（残りの装置がほかの候補を 1 つずつ使う）
            candidates = sorted(k for k in starts if k >= pos + 2 and total - k >= 2 * len(rest))
            if not candidates:
                notes.append(f"装置 {i} の IR 長を分けられない")
                break
            nxt = candidates[0]
            if len(candidates) > len(rest):                                  # 境目が一つに決まらない: 推測しない
                dev.ir_length_source = "ambiguous"
                dev.ir_candidates = [k - pos for k in candidates]
                notes.append(f"装置 {i} の IR 長は Capture-IR から一つに決まらない（候補 {dev.ir_candidates}）。"
                             "データシートの値で jtag_chain を与える")
                break
            dev.ir_length = nxt - pos
            dev.ir_length_source = "measured"
        if dev.ir_length is not None and dev.ir_length < 2:
            notes.append(f"装置 {i} の IR 長 {dev.ir_length} は短すぎる（1149.1 は 2 以上）")
        pos += dev.ir_length
    if pos != total and not any("入らない" in n or "決まらない" in n for n in notes):
        notes.append(f"装置の IR 長の和 {pos} が合計長 {total} と合わない")


def bits_from_text(text: str) -> list[int]:
    """"0110" を時間順のビットの列に（空白・_ は無視）。"""
    clean = text.replace(" ", "").replace("_", "")
    if any(c not in "01" for c in clean):
        raise ValueError(f"0 と 1 だけで書く: {text!r}")
    return [int(c) for c in clean]

