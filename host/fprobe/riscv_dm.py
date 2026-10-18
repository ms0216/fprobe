"""RISC-V の Debug Module（RISC-V External Debug Support 0.13 / 1.0）を DMI の読み書きで使う（U33）。

DMI の運び方（JTAG の DTM、Arm の APB-AP 等）とは分けてあり、Dmi（read(番号) / write(番号, 値)）を受け取る。
RP2350 の Hazard3 は SWD の APB-AP の向こうにある（fprobe.riscv_target の ApbDmi）。

- 実行制御: dmcontrol の haltreq / resumereq と dmstatus。ステップは dcsr.step。
- レジスタ: GPR は抽象コマンド（Access Register）。CSR は抽象コマンドを試し、無ければ（Hazard3 は GPR だけ）
  プログラムバッファで csrr / csrw を実行する（s0 を借りて戻す）。
- メモリ: システムバスのアクセス（sbcs・sbaddress0・sbdata0）。8 / 16 / 32 ビット。
- ブレークポイント: トリガ（tselect・tdata1・tdata2）の mcontrol（type 2）の実行アドレスの一致。読み・書きの一致を
  受け付けないトリガ（Hazard3）はウォッチポイントを断る（書いた tdata1 を読み直して判断する）。

どの操作もハートが止まっている必要があるもの（抽象コマンド・プログラムバッファ・トリガ）は、呼び手が止めておく。
"""

import time
from dataclasses import dataclass
from typing import Protocol

__all__ = ["CAUSES", "CSR", "DebugModule", "Dmi", "DmError", "GPR_NAMES"]

# DM のレジスタ（DMI の番号）
DATA0, DMCONTROL, DMSTATUS, HARTINFO, ABSTRACTCS, COMMAND = 0x04, 0x10, 0x11, 0x12, 0x16, 0x17
PROGBUF0, SBCS, SBADDRESS0, SBDATA0 = 0x20, 0x38, 0x39, 0x3C

# dmcontrol
HALTREQ, RESUMEREQ, ACKHAVERESET, DMACTIVE = 1 << 31, 1 << 30, 1 << 28, 1 << 0
# dmstatus
ALLHAVERESET, ALLRESUMEACK, ALLNONEXISTENT, ALLUNAVAIL = 1 << 19, 1 << 17, 1 << 15, 1 << 13
ALLRUNNING, ALLHALTED, AUTHENTICATED = 1 << 11, 1 << 9, 1 << 7
# abstractcs
ABSTRACT_BUSY = 1 << 12
CMDERR_SHIFT, CMDERR_MASK = 8, 0x7
# command（Access Register）
AARSIZE_32, POSTEXEC, TRANSFER, WRITE = 2 << 20, 1 << 18, 1 << 17, 1 << 16
REGNO_GPR = 0x1000
# sbcs
SBBUSYERROR, SBBUSY, SBREADONADDR, SBAUTOINCREMENT, SBREADONDATA = 1 << 22, 1 << 21, 1 << 20, 1 << 16, 1 << 15
SBACCESS_SHIFT, SBERROR_SHIFT = 17, 12
SBACCESS_SIZES = {1: (0, 1 << 0), 2: (1, 1 << 1), 4: (2, 1 << 2)}     # バイト数 → (sbaccess, sbcs の対応ビット)

CSR = {"tselect": 0x7A0, "tdata1": 0x7A1, "tdata2": 0x7A2, "dcsr": 0x7B0, "dpc": 0x7B1,
       "mstatus": 0x300, "misa": 0x301, "mie": 0x304, "mtvec": 0x305, "mscratch": 0x340, "mepc": 0x341,
       "mcause": 0x342, "mtval": 0x343, "mip": 0x344, "mhartid": 0xF14}
# dcsr
DCSR_EBREAKM, DCSR_EBREAKU, DCSR_STEP = 1 << 15, 1 << 12, 1 << 2
DCSR_CAUSE_SHIFT = 6
CAUSES = {1: "ebreak", 2: "trigger", 3: "haltreq", 4: "step", 5: "resethaltreq"}
# mcontrol（tdata1、XLEN 32）: type 2・dmode・action 1（デバッグモードへ）・M と U モード・execute / store / load
MCONTROL_BASE = (2 << 28) | (1 << 27) | (1 << 12) | (1 << 6) | (1 << 3)
MC_EXECUTE, MC_STORE, MC_LOAD = 1 << 2, 1 << 1, 1 << 0

GPR_NAMES = ["zero", "ra", "sp", "gp", "tp", "t0", "t1", "t2", "s0", "s1", "a0", "a1", "a2", "a3", "a4", "a5",
             "a6", "a7", "s2", "s3", "s4", "s5", "s6", "s7", "s8", "s9", "s10", "s11", "t3", "t4", "t5", "t6"]
EBREAK = 0x00100073
S0 = 8
_CMDERR = {1: "busy", 2: "not_supported", 3: "exception", 4: "halt_resume", 5: "bus", 7: "other"}
_SBERROR = {1: "timeout", 2: "bad_address", 3: "alignment", 4: "unsupported_size", 7: "other"}


class Dmi(Protocol):
    def read(self, index: int) -> int: ...

    def write(self, index: int, value: int) -> None: ...


class DmError(RuntimeError):
    def __init__(self, message: str, cmderr: int | None = None):
        super().__init__(message)
        self.cmderr = cmderr                      # 抽象コマンドの誤りなら abstractcs.cmderr


def _csrr(rd: int, csr: int) -> int:                       # csrrs rd, csr, x0
    return (csr << 20) | (2 << 12) | (rd << 7) | 0x73


def _csrw(csr: int, rs1: int) -> int:                      # csrrw x0, csr, rs1
    return (csr << 20) | (rs1 << 15) | (1 << 12) | 0x73


@dataclass
class DmInfo:
    version: int                  # dmstatus.version（2 = 0.13、3 = 1.0）
    progbuf_size: int
    data_count: int
    system_bus: bool              # 32 ビットのシステムバスのアクセスがある
    harts: list[int]

    def as_dict(self) -> dict:
        return {"version": {2: "0.13", 3: "1.0"}.get(self.version, str(self.version)),
                "progbuf_size": self.progbuf_size, "data_count": self.data_count, "system_bus": self.system_bus,
                "harts": self.harts}


class DebugModule:
    """DMI の上の Debug Module。hart は操作の対象のハート（select で替える）。"""

    def __init__(self, dmi: Dmi, hart: int = 0, timeout_s: float = 0.2):
        self.dmi = dmi
        self.hart = hart
        self.timeout_s = timeout_s
        self.info: DmInfo | None = None
        self._csr_abstract: bool | None = None     # CSR を抽象コマンドで読み書きできるか（試すまで不明）
        self._triggers: int | None = None

    # --- 起動 ------------------------------------------------------------------------------------

    def activate(self, max_harts: int = 32) -> DmInfo:
        """dmactive を立て、版・プログラムバッファ・システムバス・ハートの並びを調べる。"""
        self.dmi.write(DMCONTROL, DMACTIVE)
        self._wait(lambda: self.dmi.read(DMCONTROL) & DMACTIVE, "dmactive が立たない（DM が応答しない）")
        status = self.dmi.read(DMSTATUS)
        version = status & 0xF
        if version not in (2, 3):
            raise DmError(f"Debug Module の版 {version} は扱えない（0.13 = 2・1.0 = 3）: DMI の経路を確かめる")
        if not status & AUTHENTICATED:
            raise DmError("Debug Module が認証を求めている（authenticated = 0）")
        abstractcs = self.dmi.read(ABSTRACTCS)
        sbcs = self.dmi.read(SBCS)
        harts = []
        for hart in range(max_harts):
            self._control(0, hart)
            if self.dmi.read(DMSTATUS) & ALLNONEXISTENT:
                break
            harts.append(hart)
        self.info = DmInfo(version, (abstractcs >> 24) & 0x1F, abstractcs & 0xF,
                           (sbcs >> 29) != 0 and bool(sbcs & SBACCESS_SIZES[4][1]), harts)
        if self.hart not in harts:
            raise DmError(f"ハート {self.hart} が無い（ある: {harts}）")
        self._control(0)
        return self.info

    def select(self, hart: int) -> None:
        if self.info is not None and hart not in self.info.harts:
            raise DmError(f"ハート {hart} が無い（ある: {self.info.harts}）")
        self.hart = hart
        self._triggers = None
        self._control(0)

    def _control(self, bits: int, hart: int | None = None) -> None:
        hart = self.hart if hart is None else hart
        sel = ((hart & 0x3FF) << 16) | (((hart >> 10) & 0x3FF) << 6)
        self.dmi.write(DMCONTROL, DMACTIVE | sel | bits)

    def _wait(self, ready, what: str):
        """ready() が真になるまで待つ。"""
        self._poll(ready, bool, what)

    def _poll(self, read, done, what: str) -> int:
        """read() の値が done を満たすまで読み、その値を返す。"""
        deadline = time.monotonic() + self.timeout_s
        while True:
            value = read()
            if done(value):
                return value
            if time.monotonic() >= deadline:
                raise DmError(what)

    # --- 実行制御 ----------------------------------------------------------------------------------

    def status(self) -> int:
        return self.dmi.read(DMSTATUS)

    def state(self) -> str:
        """halted / running / unavailable（リセット中・電源が無い等）/ nonexistent。"""
        status = self.status()
        if status & ALLNONEXISTENT:
            return "nonexistent"
        if status & ALLUNAVAIL:
            return "unavailable"
        if status & ALLHALTED:
            return "halted"
        return "running"

    def halt(self) -> None:
        if self.status() & ALLHALTED:
            return
        self._control(HALTREQ)
        try:
            self._wait(lambda: self.status() & ALLHALTED, f"ハート {self.hart} が止まらない")
        finally:
            self._control(0)

    def resume(self) -> None:
        if not self.status() & ALLHALTED:
            return
        self._control(RESUMEREQ)
        try:
            self._wait(lambda: self.status() & ALLRESUMEACK, f"ハート {self.hart} が再開しない")
        finally:
            self._control(0)

    def step(self) -> None:
        """1 命令進める（dcsr.step を立てて再開し、止まるのを待って下ろす）。"""
        dcsr = self.read_csr(CSR["dcsr"])
        self.write_csr(CSR["dcsr"], dcsr | DCSR_STEP)
        self._control(RESUMEREQ)
        try:
            self._wait(lambda: self.status() & ALLRESUMEACK, f"ハート {self.hart} が再開しない")
            self._wait(lambda: self.status() & ALLHALTED, f"ハート {self.hart} がステップの後で止まらない")
        finally:
            self._control(0)
            self.write_csr(CSR["dcsr"], self.read_csr(CSR["dcsr"]) & ~DCSR_STEP)

    def halt_cause(self) -> str | None:
        return CAUSES.get((self.read_csr(CSR["dcsr"]) >> DCSR_CAUSE_SHIFT) & 0x7)

    def ack_reset(self) -> bool:
        """havereset を確かめて消す。立っていれば True。"""
        if self.status() & ALLHAVERESET:
            self._control(ACKHAVERESET)
            self._control(0)
            return True
        return False

    # --- 抽象コマンド ------------------------------------------------------------------------------

    def _command(self, command: int, what: str) -> None:
        self.dmi.write(COMMAND, command)
        abstractcs = self._poll(lambda: self.dmi.read(ABSTRACTCS), lambda v: not v & ABSTRACT_BUSY,
                                f"{what}: 抽象コマンドが終わらない")
        err = (abstractcs >> CMDERR_SHIFT) & CMDERR_MASK
        if err:
            self.dmi.write(ABSTRACTCS, CMDERR_MASK << CMDERR_SHIFT)          # 書いて 1 で消す
            raise DmError(f"{what}: 抽象コマンドの誤り {_CMDERR.get(err, err)}"
                          + ("（ハートが止まっていない）" if err == 4 else ""), err)

    def read_gpr(self, n: int) -> int:
        self._command(AARSIZE_32 | TRANSFER | (REGNO_GPR + n), f"x{n} の読み出し")
        return self.dmi.read(DATA0)

    def write_gpr(self, n: int, value: int) -> None:
        self.dmi.write(DATA0, value & 0xFFFFFFFF)
        self._command(AARSIZE_32 | TRANSFER | WRITE | (REGNO_GPR + n), f"x{n} の書き込み")

    def _program(self, instruction: int) -> None:
        if not self.info or self.info.progbuf_size < 1:
            raise DmError("CSR を読み書きする手段が無い（抽象コマンドもプログラムバッファも無い）")
        self.dmi.write(PROGBUF0, instruction)
        if self.info.progbuf_size >= 2:
            self.dmi.write(PROGBUF0 + 1, EBREAK)                   # 1 語だけなら impebreak が前提

    def read_csr(self, csr: int) -> int:
        if self._csr_abstract is not False:
            try:
                self._command(AARSIZE_32 | TRANSFER | csr, f"CSR 0x{csr:03X} の読み出し")
                self._csr_abstract = True
                return self.dmi.read(DATA0)
            except DmError as e:
                if self._csr_abstract or e.cmderr != 2:          # 未対応（not_supported）のときだけ切り替える
                    raise
                self._csr_abstract = False
        saved = self.read_gpr(S0)
        try:
            self._program(_csrr(S0, csr))
            self._command(AARSIZE_32 | POSTEXEC | (REGNO_GPR + S0), f"CSR 0x{csr:03X} の読み出し（プログラムバッファ）")
            return self.read_gpr(S0)
        finally:
            self.write_gpr(S0, saved)

    def write_csr(self, csr: int, value: int) -> None:
        if self._csr_abstract is not False:
            try:
                self.dmi.write(DATA0, value & 0xFFFFFFFF)
                self._command(AARSIZE_32 | TRANSFER | WRITE | csr, f"CSR 0x{csr:03X} の書き込み")
                self._csr_abstract = True
                return
            except DmError as e:
                if self._csr_abstract or e.cmderr != 2:          # 未対応（not_supported）のときだけ切り替える
                    raise
                self._csr_abstract = False
        saved = self.read_gpr(S0)
        try:
            self._program(_csrw(csr, S0))
            self.dmi.write(DATA0, value & 0xFFFFFFFF)
            self._command(AARSIZE_32 | TRANSFER | WRITE | POSTEXEC | (REGNO_GPR + S0),
                          f"CSR 0x{csr:03X} の書き込み（プログラムバッファ）")
        finally:
            self.write_gpr(S0, saved)

    # --- システムバス -----------------------------------------------------------------------------

    def _sb(self, size: int, extra: int) -> None:
        if not self.info or not self.info.system_bus:
            raise DmError("システムバスのアクセス（sbcs）が無い")
        sbaccess, _ = SBACCESS_SIZES[size]
        self.dmi.write(SBCS, (sbaccess << SBACCESS_SHIFT) | extra | (7 << SBERROR_SHIFT) | SBBUSYERROR)

    def _sb_idle(self, what: str) -> None:
        sbcs = self._poll(lambda: self.dmi.read(SBCS), lambda v: not v & SBBUSY, f"{what}: システムバスが終わらない")
        err = (sbcs >> SBERROR_SHIFT) & 0x7
        if err or sbcs & SBBUSYERROR:
            self.dmi.write(SBCS, (7 << SBERROR_SHIFT) | SBBUSYERROR)
            raise DmError(f"{what}: システムバスの誤り {_SBERROR.get(err, 'busy') if err else 'busy'}")

    def read_memory(self, address: int, count: int, size: int = 4) -> list[int]:
        """size バイト（1 / 2 / 4）の値を count 個、番地を size ずつ進めて読む。"""
        if count <= 0:
            return []
        what = f"0x{address:08X} の読み出し"
        self._sb(size, SBREADONADDR | SBAUTOINCREMENT | (SBREADONDATA if count > 1 else 0))
        self.dmi.write(SBADDRESS0, address)
        values = []
        for i in range(count):
            self._sb_idle(what)
            if i == count - 1 and count > 1:
                self._sb(size, SBAUTOINCREMENT)                     # 最後の 1 個の後ろは読みに行かない
            values.append(self.dmi.read(SBDATA0) & ((1 << (8 * size)) - 1))
        self._sb_idle(what)
        return values

    def write_memory(self, address: int, values: list[int], size: int = 4) -> None:
        if not values:
            return
        what = f"0x{address:08X} への書き込み"
        self._sb(size, SBAUTOINCREMENT)
        self.dmi.write(SBADDRESS0, address)
        for value in values:
            self.dmi.write(SBDATA0, value & ((1 << (8 * size)) - 1))
            self._sb_idle(what)

    # --- トリガ -------------------------------------------------------------------------------------

    def trigger_count(self) -> int:
        """トリガの数（tselect に書いて読み直せる番号まで、type が 0 でないもの）。"""
        if self._triggers is None:
            count = 0
            for i in range(32):
                self.write_csr(CSR["tselect"], i)
                if self.read_csr(CSR["tselect"]) != i or self.read_csr(CSR["tdata1"]) >> 28 == 0:
                    break
                count += 1
            self._triggers = count
        return self._triggers

    def set_trigger(self, index: int, address: int, kinds: int) -> bool:
        """トリガ index を address の一致（kinds: MC_EXECUTE / MC_LOAD / MC_STORE の和）にする。トリガが
        その種類を受け付けなければ外して False。"""
        wanted = MCONTROL_BASE | kinds
        self.write_csr(CSR["tselect"], index)
        self.write_csr(CSR["tdata1"], 0)
        self.write_csr(CSR["tdata2"], address)
        self.write_csr(CSR["tdata1"], wanted)
        if self.read_csr(CSR["tdata1"]) & (MC_EXECUTE | MC_STORE | MC_LOAD) != kinds:
            self.write_csr(CSR["tdata1"], 0)
            return False
        return True

    def clear_trigger(self, index: int) -> None:
        self.write_csr(CSR["tselect"], index)
        self.write_csr(CSR["tdata1"], 0)
