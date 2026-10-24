"""JTAG の解読（TAP の状態と IR / DR のシフト）と、その上の ARM の JTAG-DP（DPACC・APACC・IDCODE）。
仕様: docs/superpowers/specs/2026-10-18-u20-decoders-design.md

TCK の立ち上がりで TMS・TDI・TDO（直前の値）を読み、TAP の状態機械（IEEE 1149.1）を進める。Shift-IR / Shift-DR の
状態の立ち上がりで 1 ビットずつ移す（最初に移したビットが値のビット 0）。Pause を挟んだシフトは Update まで 1 つにまとめる。
初めの状態が分からなければ、TMS = 1 が 5 回続く（必ず Test-Logic-Reset になる）まで状態を追わない。
"""

from ..la import Transitions
from .core import Annotation, Channel, Decoding, project

__all__ = ["ARM_DP_IR", "STATES", "decode_jtag", "decode_jtag_arm"]

# 状態: (TMS = 0 の次, TMS = 1 の次)
STATES = {
    "test_logic_reset": ("run_test_idle", "test_logic_reset"),
    "run_test_idle": ("run_test_idle", "select_dr_scan"),
    "select_dr_scan": ("capture_dr", "select_ir_scan"),
    "capture_dr": ("shift_dr", "exit1_dr"),
    "shift_dr": ("shift_dr", "exit1_dr"),
    "exit1_dr": ("pause_dr", "update_dr"),
    "pause_dr": ("pause_dr", "exit2_dr"),
    "exit2_dr": ("shift_dr", "update_dr"),
    "update_dr": ("run_test_idle", "select_dr_scan"),
    "select_ir_scan": ("capture_ir", "test_logic_reset"),
    "capture_ir": ("shift_ir", "exit1_ir"),
    "shift_ir": ("shift_ir", "exit1_ir"),
    "exit1_ir": ("pause_ir", "update_ir"),
    "pause_ir": ("pause_ir", "exit2_ir"),
    "exit2_ir": ("shift_ir", "update_ir"),
    "update_ir": ("run_test_idle", "select_dr_scan"),
}
ARM_DP_IR = {0x8: "ABORT", 0xA: "DPACC", 0xB: "APACC", 0xE: "IDCODE", 0xF: "BYPASS"}   # ADIv5 の JTAG-DP（IR 4 ビット）
_NAMED_TABLES = {"arm_dp": ARM_DP_IR}


class _Scan:
    def __init__(self, register: str, start: int):
        self.register, self.start = register, start
        self.tdi = self.tdo = self.bits = 0
        self.end = start + 1


def decode_jtag(tr: Transitions, *, tck: Channel, tms: Channel, tdi: Channel | None, tdo: Channel | None,
                trst: Channel | None, initial_state: str | None = None, ir_names: str | dict | None = None) -> Decoding:
    """JTAG を解読する。initial_state を省くと TMS = 1 が 5 回続くまで待つ。ir_names は IR の値の名前の表
    （"arm_dp" か {値: 名前}）。trst は負論理の TRST（0 の間 Test-Logic-Reset）。tdi・tdo・trst は既定を持たない
    （つながない線は None を明示）。"""
    if initial_state is not None and initial_state not in STATES:
        raise ValueError(f"状態は {sorted(STATES)} のどれか（{initial_state!r}）")
    names = _ir_names(ir_names)
    lines = [ch for ch in (tck, tms, tdi, tdo, trst) if ch is not None]
    bit = {ch: 1 << k for k, ch in enumerate(lines)}
    b_tck, b_tms = bit[tck], bit[tms]
    b_tdi = bit[tdi] if tdi is not None else 0
    b_tdo = bit[tdo] if tdo is not None else 0
    b_trst = bit[trst] if trst is not None else 0
    starts, values = project(tr, lines)
    params = {"tck": tck, "tms": tms, "tdi": tdi, "tdo": tdo, "trst": trst, "initial_state": initial_state,
              "ir_names": ir_names}
    out: list[Annotation] = []
    state = initial_state
    state_start = 0
    ones = 0                                          # 続いた TMS = 1 の数（状態が分からない間の同期用）
    scan: _Scan | None = None

    def enter(new: str | None, at: int) -> None:
        nonlocal state, state_start
        if new == state:
            return
        if state is not None:
            out.append(Annotation(state_start, max(at, state_start + 1), "state", state, fields={"state": state}))
        if new == "test_logic_reset":
            out.append(Annotation(at, at + 1, "reset", "TLR"))
        state, state_start = new, at

    def finish(at: int, error: str | None) -> None:
        nonlocal scan
        s = scan
        scan = None
        fields = {"bits": s.bits, "tdi": s.tdi if tdi is not None else None, "tdo": s.tdo if tdo is not None else None}
        if s.register == "ir":
            name = names.get(s.tdi) if tdi is not None else None
            fields["name"] = name
            text = f"IR 0x{s.tdi:X}" + (f" {name}" if name else "") if tdi is not None else f"IR {s.bits} bits"
        else:
            digits = max(1, (s.bits + 3) // 4)
            text = f"DR {s.bits} bits" + (f" TDI 0x{s.tdi:0{digits}X}" if tdi is not None else "") + \
                (f" TDO 0x{s.tdo:0{digits}X}" if tdo is not None else "")
        out.append(Annotation(s.start, max(at, s.start + 1), s.register, text, value=s.tdi if tdi is not None else None,
                              error=error, fields=fields))

    prev = values[0] if len(values) else 0
    if trst is not None and not prev & b_trst:
        enter("test_logic_reset", 0)
    for i in range(1, len(values)):
        now, at = values[i], starts[i]
        if trst is not None and not now & b_trst:
            if prev & b_trst:
                if scan is not None:
                    finish(at, "aborted")
                enter("test_logic_reset", at)
            prev = now
            continue
        if (prev ^ now) & b_tck and now & b_tck:                    # TCK の立ち上がり: 直前の値を読む
            m = bool(prev & b_tms)
            if state is None:
                ones = ones + 1 if m else 0
                if ones >= 5:
                    enter("test_logic_reset", at)
                prev = now
                continue
            if state in ("shift_dr", "shift_ir"):
                if scan is None:
                    scan = _Scan(state[-2:], at)
                scan.tdi |= bool(prev & b_tdi) << scan.bits
                scan.tdo |= bool(prev & b_tdo) << scan.bits
                scan.bits += 1
            nxt = STATES[state][m]
            if nxt in ("update_dr", "update_ir") and scan is not None:
                finish(at, None)
            elif nxt == "test_logic_reset" and scan is not None:
                finish(at, "aborted")
            enter(nxt, at)
        prev = now
    if scan is not None:
        finish(tr.length, "truncated")
    if state is not None:
        out.append(Annotation(state_start, max(tr.length, state_start + 1), "state", state, fields={"state": state}))
    return Decoding("jtag", tuple(out), tr.sample_period_s, tr.trigger, tr.length, params)


def _ir_names(ir_names) -> dict[int, str]:
    if ir_names is None:
        return {}
    if isinstance(ir_names, str):
        try:
            return _NAMED_TABLES[ir_names]
        except KeyError:
            raise ValueError(f"IR の名前の表は {sorted(_NAMED_TABLES)} か {{値: 名前}}（{ir_names!r}）") from None
    return {int(k, 0) if isinstance(k, str) else int(k): str(v) for k, v in ir_names.items()}


# ---- 上位: ARM の JTAG-DP ----

_JTAG_ACK = {0b010: "OK/FAULT", 0b001: "WAIT"}
_DP_REGS = {0x0: "ABORT/CTRL", 0x4: "CTRL/STAT", 0x8: "SELECT", 0xC: "RDBUFF"}


def decode_jtag_arm(lower: Decoding, *, ir_offset: int, dr_offset: int, ir_bits: int = 4) -> Decoding:
    """JTAG の解読の上で ADIv5 の JTAG-DP を読む。DPACC / APACC の 35 ビットの DR を要求（RnW・A[3:2]・書く値）と
    応答（ACK・前の読みの値）に分ける。IDCODE の DR は 32 ビットの IDCODE。
    鎖に他の TAP があるときは、DP の位置を最初に移すビットからの数で与える: ir_offset は DP より TDO 側の TAP の IR の
    長さの和、dr_offset は DP より TDO 側の TAP の数（BYPASS で 1 ビットずつ）。例: STM32F4（TDO 側が DP）は 0 / 0。
    ir_offset・dr_offset は鎖の構成で決まるので既定を持たない。ir_bits の既定 4 は ADIv5 の JTAG-DP の IR の長さ（規格の値）。"""
    if lower.protocol != "jtag":
        raise ValueError("jtag の解読の上に重ねる")
    if ir_bits < 1 or ir_offset < 0 or dr_offset < 0:
        raise ValueError("ir_bits は 1 以上、ir_offset・dr_offset は 0 以上")
    out: list[Annotation] = []
    ir = None
    pending: tuple[str, str] | None = None             # 前の読みの (DP/AP, レジスタ)。値は次の DR で返る
    for a in lower.annotations:
        if a.kind == "ir" and a.fields["tdi"] is not None:
            ir = a.fields["tdi"] >> ir_offset & ((1 << ir_bits) - 1) if a.fields["bits"] >= ir_offset + ir_bits else None
            continue
        if a.kind != "dr" or a.error or a.fields["tdi"] is None:
            continue
        n = a.fields["bits"] - dr_offset
        tdi = a.fields["tdi"] >> dr_offset
        tdo = a.fields["tdo"] >> dr_offset if a.fields["tdo"] is not None else None
        name = ARM_DP_IR.get(ir)
        if name == "IDCODE" and n >= 32:
            out.append(Annotation(a.start, a.end, "idcode", f"IDCODE 0x{(tdo or 0) & 0xFFFFFFFF:08X}",
                                  value=(tdo or 0) & 0xFFFFFFFF if tdo is not None else None,
                                  fields={"designer": ((tdo or 0) >> 1) & 0x7FF, "part": ((tdo or 0) >> 12) & 0xFFFF,
                                          "version": ((tdo or 0) >> 28) & 0xF}))
            continue
        if name not in ("DPACC", "APACC") or n < 35:
            continue
        rnw, addr, data = tdi & 1, (tdi >> 1 & 3) << 2, tdi >> 3 & 0xFFFFFFFF
        ack = tdo & 7 if tdo is not None else None
        tdo = tdo if tdo is None else tdo & ((1 << 35) - 1)
        returned = tdo >> 3 & 0xFFFFFFFF if tdo is not None else None
        port = "AP" if name == "APACC" else "DP"
        register = f"0x{addr:X}" if port == "AP" else _DP_REGS[addr]
        fields = {"port": port, "address": addr, "rnw": bool(rnw), "write_data": None if rnw else data,
                  "ack": _JTAG_ACK.get(ack, None if ack is None else f"0b{ack:03b}"),
                  "returned": returned, "previous": list(pending) if pending else None}
        text = f"{port} {'R' if rnw else 'W'} {register}" + ("" if rnw else f" = 0x{data:08X}")
        if returned is not None and pending is not None:
            text += f" (← {pending[0]} {pending[1]} = 0x{returned:08X})"
        error = None if ack in (None, 0b010) else ("wait" if ack == 0b001 else "ack")
        out.append(Annotation(a.start, a.end, "access", text, value=data if not rnw else returned, error=error,
                              fields=fields))
        if ack == 0b001:
            continue                                    # WAIT: 要求は受け付けられていない
        pending = (port, register) if rnw else None
    return lower.derive("jtag_arm", out, {"ir_bits": ir_bits, "ir_offset": ir_offset, "dr_offset": dr_offset})
