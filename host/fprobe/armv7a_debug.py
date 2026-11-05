"""ARMv7-A（Cortex-A7 等）の外部デバッグへの道（U32）: JTAG-DP → APB-AP の向こうの CoreSight のデバッグレジスタ
（コアごとに 4 KB の窓。番地は ROM テーブルから見つける）。fprobe.arm_debug.CoreDebugPort の実装。

レジスタ（ARMv7 の外部デバッグ。窓の中の番地）:
  DBGDIDR 0x000、DBGDTRRX 0x080、DBGITR 0x084、DBGDSCR 0x088、DBGDTRTX 0x08C、DBGDRCR 0x090、
  DBGBVR / BCR 0x100 / 0x140 + 4n、DBGWVR / WCR 0x180 / 0x1C0 + 4n、DBGOSLAR 0x300、DBGOSLSR 0x304、
  DBGPRSR 0x314、DBGLAR 0xFB0、DEVTYPE 0xFCC（0x15 = 処理系のデバッグ）。

DCC は非ブロックのモード（DSCR.ExtDCCmode = 0）で使い、ITR に書く前と後に DSCR.InstrCompl_l を、DTRTX を読む前に
TXfull_l を見る（OpenOCD の cortex_a と同じ）。

往復をまとめる（`V7DebugPort.run`）: 命令の列（ITR の書き・DTRRX の書き・DTRTX の読み）を、命令ごとの DSCR の読みと一緒に 1 回の
往復（`RawDap.mem_batch`）で送り、返ってきた DSCR を順に確かめる。非ブロックのモードでは、前の命令が終わっていない（InstrCompl_l = 0）
と ITR の書きは捨てられ、TXfull_l = 0 の DTRTX の読みは不定の値を返し、RXfull_l = 1 の DTRRX の書きは捨てられる。よって列の頭で
「前の命令が終わっていて、DTRTX・DTRRX が空」を読み、各命令の後の DSCR で「終わった（読みなら TXfull_l、書きなら RXfull_l = 0）」を
読めば、帰納的に全部の書きが受け付けられ、全部の読みが正しい値だったと分かる（各 DSCR はその命令より後、次の書きより前に読む）。
1 つでも確かめられなければ（遅い命令・sticky の印）、命令が止まるのを待ち、DTRTX・DTRRX を空にし、sticky を消してから、
列を 1 命令ずつ（今までの道）頭からやり直す。やり直せるよう、列は「頭からもう一度実行しても同じ結果になる」ものだけにする
（列の中で、読む前のレジスタを書き替えない。メモリの後置きの加算は、番地を置く命令から列に入れる）。呼ぶ側（ArmDebugCore）が守る。
"""

from .arm_debug import DSCR_INSTRCOMPL, DSCR_RXFULL, DSCR_STICKY, DSCR_TXFULL, ArmDebugError, arm_mrc
from .coresight_id import CLASS_CORESIGHT, decode_ap_idr, walk_rom_table
from .raw_dap import AP_BASE, AP_IDR, DapError, RawDap

__all__ = ["V7DebugPort", "dscr_all", "find_apb_ap", "find_debug_bases", "halt_request_all", "read_all",
           "restart_request_all", "run_all", "unlock_all", "write_dscr_all"]

DIDR, DTRRX, ITR, DSCR, DTRTX, DRCR = 0x000, 0x080, 0x084, 0x088, 0x08C, 0x090
BVR, BCR, WVR, WCR = 0x100, 0x140, 0x180, 0x1C0
OSLAR, OSLSR, PRSR, LAR, DEVTYPE = 0x300, 0x304, 0x314, 0xFB0, 0xFCC
DRCR_HRQ, DRCR_RRQ, DRCR_CSE = 1 << 0, 1 << 1, 1 << 2
OSLSR_LOCKED, PRSR_POWERED = 1 << 1, 1 << 0
UNLOCK_KEY = 0xC5ACCE55
DEVTYPE_CORE_DEBUG = 0x15
_POLLS = 200
_TX_ANY = DSCR_TXFULL | 1 << 26                    # DTRTX に値がある（TXfull・TXfull_l）
_RX_ANY = DSCR_RXFULL | 1 << 27                    # DTRRX に値がある（RXfull・RXfull_l）
_MRC_R0_DTRRX = arm_mrc(14, 0, 0, 0, 5, 0)         # MRC p14, 0, r0, c0, c5, 0（DTRRX → r0。DTRRX を空にする）


def find_apb_ap(dap: RawDap, max_aps: int = 16) -> int | None:
    """ADIv5 の AP を順に見て、最初の APB の MEM-AP（APB2/3・APB4）の番号。"""
    for ap in range(max_aps):
        try:
            idr = decode_ap_idr(dap.read_ap(ap, AP_IDR))
        except DapError:
            dap.clear_errors()
            continue
        if idr["kind"] == "mem-ap" and idr["type"] in (2, 6):
            return ap
    return None


def find_mem_ap(dap: RawDap, max_aps: int = 16) -> int | None:
    """ADIv5 の AP を順に見て、最初の MEM-AP（種類は問わない。APB-AP が見えない DAP で AHB-AP を使うとき）の番号。"""
    for ap in range(max_aps):
        try:
            idr = decode_ap_idr(dap.read_ap(ap, AP_IDR))
        except DapError:
            dap.clear_errors()
            continue
        if idr["kind"] == "mem-ap":
            return ap
    return None


def find_debug_bases(dap: RawDap, ap: int) -> list[int]:
    """APB-AP の ROM テーブルをたどり、コアのデバッグの窓（CoreSight の class 9、DEVTYPE 0x15）の番地を番地順に。
    ROM テーブルが無い・読めなければ空。"""
    base = dap.read_ap(ap, AP_BASE)
    if base in (0xFFFFFFFF, 0) or not base & 1:                    # 「ROM テーブルが無い」の印
        return []

    def read(address: int) -> int:
        return read_many([address])[0]

    def read_many(addresses: list[int]) -> list[int]:               # 1 回の往復で（失敗なら誤りの印を消して投げる）
        try:
            return dap.mem_read32_many(ap, addresses)
        except DapError:
            dap.clear_errors()
            raise
    comps = [c for c in walk_rom_table(read, base & 0xFFFFF000, read_many=read_many).flatten()
             if not c.error and c.cls == CLASS_CORESIGHT]
    try:
        devtypes = read_many([c.address + DEVTYPE for c in comps]) if comps else []
    except DapError:                                                 # どれかが読めない: 1 つずつ（読めない部品は外す）
        devtypes = []
        for c in comps:
            try:
                devtypes.append(read(c.address + DEVTYPE))
            except DapError:
                devtypes.append(None)
    return sorted(c.address for c, d in zip(comps, devtypes) if d is not None and d & 0xFF == DEVTYPE_CORE_DEBUG)


def _same_window(ports) -> bool:
    return bool(ports) and all(isinstance(p, V7DebugPort) and p.dap is ports[0].dap and p.ap == ports[0].ap for p in ports)


def halt_request_all(ports) -> None:
    """全コアに停止要求を出す。同じ DAP・APB-AP の V7DebugPort なら DRCR への書き込みを 1 回の往復にまとめる（CTI の無い SoC で、
    コアの止まる時刻の差が USB の往復の分だけ開かないように）。そうでなければ 1 つずつ。"""
    if _same_window(ports):
        ports[0].dap.mem_write32_many(ports[0].ap, [(p.base + DRCR, DRCR_HRQ) for p in ports])
    else:
        for p in ports:
            p.halt_request()


def restart_request_all(ports) -> list[int]:
    """全コアに再開の要求（DBGDRCR.RRQ、sticky も消す CSE）を書き、その後に各コアの DSCR を読んで返す。同じ DAP・APB-AP の
    V7DebugPort なら、全コアの書き込みと読みを 1 回の往復にまとめる（再開の時刻の差が往復の分だけ開かないように）。そうでなければ
    1 つずつ書いてから読む。読んだ DSCR は、どの要求よりも後のもの（RESTARTED が 0 なら、まだデバッグ状態を出ていない）。"""
    if _same_window(ports):
        ops = [("w", p.base + DRCR, DRCR_RRQ | DRCR_CSE) for p in ports] + [("r", p.base + DSCR) for p in ports]
        return ports[0].dap.mem_batch(ports[0].ap, ops)
    for p in ports:
        p.restart_request()
    return [p.dscr() for p in ports]


def dscr_all(ports) -> list[int]:
    """全コアの DSCR。同じ DAP・APB-AP の V7DebugPort なら 1 回の往復で読む。"""
    if _same_window(ports):
        return ports[0].dap.mem_read32_many(ports[0].ap, [p.base + DSCR for p in ports])
    return [p.dscr() for p in ports]


def read_all(reads: list[tuple["V7DebugPort", int]]) -> list[int]:
    """(ポート, 窓の中の番地) の列を読む。同じ DAP・APB-AP なら 1 回の往復で（ブレークポイントの組の状態等、命令を使わない読み）。"""
    ports = [p for p, _ in reads]
    if _same_window(ports):
        return ports[0].dap.mem_read32_many(ports[0].ap, [p.base + offset for p, offset in reads])
    return [p._read(offset) for p, offset in reads]


def _ops(port: "V7DebugPort", steps: list[tuple]) -> tuple[list[tuple], list[str]]:
    """列を APB の読み書きに直す。返す kinds は読みの意味（順に）: "pre" = 最初の命令の前の DSCR、"exec" / "read" / "put" = その
    命令の後の DSCR、"value" = DTRTX の値。"""
    b, ops, kinds = port.base, [], []
    for s in steps:
        if s[0] == "dscr":
            ops.append(("w", b + DSCR, s[1] & 0xFFFFFFFF))
            continue
        if s[0] == "clear_sticky":
            ops.append(("w", b + DRCR, DRCR_CSE))
            continue
        if "pre" not in kinds:
            ops.append(("r", b + DSCR))
            kinds.append("pre")
        if s[0] == "put":
            ops += [("w", b + DTRRX, s[1] & 0xFFFFFFFF), ("w", b + ITR, s[2])]
        else:
            ops.append(("w", b + ITR, s[1]))
        ops.append(("r", b + DSCR))
        kinds.append(s[0])
        if s[0] == "read":
            ops.append(("r", b + DTRTX))
            kinds.append("value")
    return ops, kinds


def _verified(results: list[int], kinds: list[str], puts: bool) -> tuple[list[int] | None, list[int]]:
    """読んだ値を確かめる。全部確かめられれば (値, 命令ごとの DSCR)、どこかで確かめられなければ (None, 命令ごとの DSCR)。"""
    values, dscrs, ok = [], [], True
    for kind, r in zip(kinds, results):
        if kind == "value":
            values.append(r)
            continue
        done = bool(r & DSCR_INSTRCOMPL) and not r & DSCR_STICKY
        if kind == "pre":
            ok = ok and done and not r & _TX_ANY and not (puts and r & _RX_ANY)
            continue
        dscrs.append(r)
        if kind == "read":
            ok = ok and done and bool(r & _TX_ANY)
        elif kind == "put":
            ok = ok and done and not r & _RX_ANY
        else:
            ok = ok and done
    return (values if ok else None), dscrs


def run_all(jobs: list[tuple["V7DebugPort", list[tuple]]]) -> list[list[int]]:
    """複数のコアの命令の列（`V7DebugPort.run`）を、同じ DAP・APB-AP なら全部で 1 回の往復で。確かめられなかったコアだけ、
    そのコアで待って空にしてから、1 命令ずつやり直す。コアごとに読んだ値の列を返す。"""
    built = [_ops(p, steps) for p, steps in jobs]
    ports = [p for p, _ in jobs]
    if _same_window(ports):
        flat = ports[0].dap.mem_batch(ports[0].ap, [op for ops, _ in built for op in ops])
        results, at = [], 0
        for ops, _ in built:
            n = sum(1 for op in ops if op[0] == "r")
            results.append(flat[at:at + n])
            at += n
    else:
        results = [p.dap.mem_batch(p.ap, ops) for p, (ops, _) in zip(ports, built)]
    out = []
    for (port, steps), (_, kinds), res in zip(jobs, built, results):
        puts = any(s[0] == "put" for s in steps)
        values, dscrs = _verified(res, kinds, puts)
        if values is None:
            port.round_trip_retries += 1
            port._settle(rx_scratch=puts)
            values = port._run_slow(steps)
        else:
            port.last_dscrs = dscrs
            if dscrs:
                port._ready = True
        out.append(values)
    return out


def unlock_all(ports: list["V7DebugPort"]) -> list[dict]:
    """各コアの `unlock` と DBGDIDR の読みを、同じ DAP・APB-AP なら全コアで 1 回の往復で。各コアの {"prsr", "didr"}。"""
    if not _same_window(ports):
        return [dict(p.unlock(), didr=p.didr()) for p in ports]
    ops = [op for p in ports for op in (("w", p.base + LAR, UNLOCK_KEY), ("w", p.base + OSLAR, 0), ("r", p.base + OSLSR),
                                         ("r", p.base + PRSR), ("r", p.base + DIDR))]
    values = ports[0].dap.mem_batch(ports[0].ap, ops)
    out = []
    for k, p in enumerate(ports):
        oslsr, prsr, didr = values[3 * k:3 * k + 3]
        p._check_unlocked(oslsr, prsr)
        out.append({"prsr": prsr, "didr": didr})
    return out


def write_dscr_all(writes: list[tuple["V7DebugPort", int]]) -> None:
    """(ポート, DSCR に書く値) の列。同じ DAP・APB-AP なら 1 回の往復で。"""
    ports = [p for p, _ in writes]
    if _same_window(ports):
        ports[0].dap.mem_write32_many(ports[0].ap, [(p.base + DSCR, v & 0xFFFFFFFF) for p, v in writes])
    else:
        for p, v in writes:
            p.write_dscr(v)


class V7DebugPort:
    """1 つのコアのデバッグの窓（APB-AP の番地 base）。"""

    architecture = 7
    run_many = staticmethod(run_all)               # 複数のコアの列を 1 回の往復で（fprobe.arm_debug の *_all が使う）
    write_dscr_many = staticmethod(write_dscr_all)
    dscr_many = staticmethod(dscr_all)

    def __init__(self, dap: RawDap, ap: int, base: int):
        self.dap, self.ap, self.base = dap, ap, base
        self._ready = False                       # 前の命令が終わったのを見た（InstrCompl）。分からなければ、次の命令の前に待つ
        self.last_dscrs: list[int] = []           # 最後の `run` の命令ごとの DSCR（アボートの確かめ等）
        self.round_trip_retries = 0               # `run` の列を確かめられず 1 命令ずつやり直した回数（計測用）

    def _read(self, offset: int) -> int:
        return self.dap.mem_read32(self.ap, self.base + offset)

    def _write(self, offset: int, value: int) -> None:
        self.dap.mem_write32(self.ap, self.base + offset, value & 0xFFFFFFFF)

    def unlock(self) -> dict:
        """ソフトウェアのロック（LAR）と OS ロック（OSLAR）を解き、電源の様子（PRSR。読むと sticky の印が消える）。"""
        b = self.base                             # 解く書きと、解けたかの読みを 1 回の往復で（書きは読みより前に APB に出る）
        oslsr, prsr = self.dap.mem_batch(self.ap, [("w", b + LAR, UNLOCK_KEY), ("w", b + OSLAR, 0), ("r", b + OSLSR),
                                                   ("r", b + PRSR)])
        self._check_unlocked(oslsr, prsr)
        return {"prsr": prsr}

    def _check_unlocked(self, oslsr: int, prsr: int) -> None:
        if oslsr & OSLSR_LOCKED:
            raise ArmDebugError(f"0x{self.base:08X}: OS ロックが解けない（DBGOSLSR）")
        if not prsr & PRSR_POWERED:
            raise ArmDebugError(f"0x{self.base:08X}: コアのデバッグの電源が落ちている（DBGPRSR 0x{prsr:08X}）")

    # --- CoreDebugPort ---------------------------------------------------------------------------------

    def didr(self) -> int:
        return self._read(DIDR)

    def dscr(self) -> int:
        return self._read(DSCR)

    def write_dscr(self, value: int) -> None:
        self._write(DSCR, value)

    def write_dscr_and_read(self, value: int) -> int:
        """DSCR に書き、その後の DSCR を読む（1 回の往復）。"""
        return self.dap.mem_batch(self.ap, [("w", self.base + DSCR, value & 0xFFFFFFFF), ("r", self.base + DSCR)])[0]

    def restart_and_read(self) -> int:
        """再開を求め（sticky も消す）、その後の DSCR を読む（1 回の往復）。"""
        return restart_request_all([self])[0]

    def halt_request(self) -> None:
        self._write(DRCR, DRCR_HRQ)

    def restart_request(self) -> None:
        self._write(DRCR, DRCR_RRQ | DRCR_CSE)

    def clear_sticky(self) -> None:
        self._write(DRCR, DRCR_CSE)

    def _wait(self, mask: int, what: str) -> int:
        for _ in range(_POLLS):
            dscr = self._read(DSCR)
            if dscr & mask:
                if dscr & DSCR_INSTRCOMPL:
                    self._ready = True
                return dscr
        raise ArmDebugError(f"{what}（DSCR 0x{dscr:08X}）")

    def execute(self, instruction: int) -> int:
        """ITR で 1 命令を実行し、完了を待って DSCR を返す。ITR の書き込みと DSCR の読みを 1 回の往復にまとめる（JTAG は遅いので、
        ほとんどの命令は読む前に終わっている。終わっていなければ読み直す）。"""
        return self._issue([("w", self.base + ITR, instruction)], instruction)

    def put_and_execute(self, value: int, instruction: int) -> int:
        """DTRRX に value を置き、instruction（MRC p14 で受け取る命令）を実行する。3 つを 1 回の往復で。"""
        return self._issue([("w", self.base + DTRRX, value & 0xFFFFFFFF), ("w", self.base + ITR, instruction)], instruction)

    def _issue(self, writes: list[tuple], instruction: int) -> int:
        if not self._ready:
            self._wait(DSCR_INSTRCOMPL, "前の命令が終わらない")
        dscr = self.dap.mem_batch(self.ap, [*writes, ("r", self.base + DSCR)])[0]
        if not dscr & DSCR_INSTRCOMPL:
            self._ready = False
            dscr = self._wait(DSCR_INSTRCOMPL, f"命令 0x{instruction:08X} が終わらない")
        self._ready = True
        return dscr

    def run(self, steps: list[tuple]) -> list[int]:
        """命令の列を 1 回の往復で実行し、読んだ値（"read" の順）を返す（モジュールの説明: 確かめ方とやり直し）。
        steps: ("dscr", 値) = DSCR に書く、("clear_sticky",) = sticky の印を消す、("exec", 命令)、("read", 命令) = 命令の後に
        DTRTX を読む、("put", 値, 命令) = DTRRX に値を置いてから命令。列は頭から実行し直しても同じ結果になること。"put" を含む列では
        r0 を壊してよいこと（やり直しの前に DTRRX を空にするのに使う）。各命令の後の DSCR は `last_dscrs` に残す。"""
        return run_all([(self, steps)])[0]

    def _settle(self, rx_scratch: bool) -> None:
        """確かめられなかった列の後: 命令が終わるのを待ち、sticky の印を消し、DTRTX を読み捨て、（rx_scratch なら）DTRRX を r0 に
        受け取って空にする。前の命令が終わり、DTRTX・DTRRX が空で、印の無い状態にする。"""
        for _ in range(_POLLS):
            dscr = self._read(DSCR)
            if not dscr & DSCR_INSTRCOMPL:
                continue
            if dscr & DSCR_STICKY:
                self.clear_sticky()
            elif dscr & _TX_ANY:
                self._read(DTRTX)
            elif dscr & _RX_ANY and rx_scratch:
                self._write(ITR, _MRC_R0_DTRRX)
            else:
                self._ready = True
                return
        raise ArmDebugError(f"0x{self.base:08X}: 命令の列の後、前の命令が終わらない・DCC が空にならない（DSCR 0x{dscr:08X}）")

    def _run_slow(self, steps: list[tuple]) -> list[int]:
        """列を 1 命令ずつ（命令ごとに終わるのを待つ。今までの道）。"""
        out, self.last_dscrs = [], []
        for s in steps:
            if s[0] == "dscr":
                self.write_dscr(s[1])
            elif s[0] == "clear_sticky":
                self.clear_sticky()
            elif s[0] == "put":
                self.last_dscrs.append(self.put_and_execute(s[1], s[2]))
            else:
                dscr = self.execute(s[1])
                self.last_dscrs.append(dscr)
                if s[0] == "read":
                    out.append(self.from_core(dscr))
        return out

    def execute_once_and_read(self, instruction: int, read_instruction: int) -> tuple[int, int | None]:
        """instruction（1 回だけ実行する命令。例 後置きの加算の LDR）を実行し、read_instruction（何度実行しても同じ命令。例
        MCR r1 → DTRTX）で値を受け取る。1 回の往復で（DSCR・ITR・DSCR・ITR・DSCR・DTRTX）。前の命令が終わっているのを見て
        から（`_ready`。見ていなければ待つ）書くので、instruction は必ず受け付けられ、1 回だけ実行される（やり直さない）。
        返すのは (instruction の後の DSCR, 値)。instruction が sticky の印を立てたら値は None（呼ぶ側がアボートとして扱う）。
        確かめられなければ（遅い命令・DTRTX に値が残っていた）、命令が終わるのを待ち、DTRTX を空にしてから、受け取りの命令だけを
        1 命令ずつやり直す（instruction の結果はレジスタに残っている）。"""
        if not self._ready:
            self._wait(DSCR_INSTRCOMPL, "前の命令が終わらない")
        b = self.base
        pre, d1, d2, value = self.dap.mem_batch(self.ap, [("r", b + DSCR), ("w", b + ITR, instruction), ("r", b + DSCR),
                                                          ("w", b + ITR, read_instruction), ("r", b + DSCR),
                                                          ("r", b + DTRTX)])
        self.last_dscrs = [d1, d2]
        if (pre & DSCR_INSTRCOMPL and not pre & (_TX_ANY | DSCR_STICKY) and d1 & DSCR_INSTRCOMPL
                and not d1 & DSCR_STICKY and d2 & DSCR_INSTRCOMPL and d2 & _TX_ANY and not d2 & DSCR_STICKY):
            self._ready = True
            return d1, value
        self._ready = False
        self.round_trip_retries += 1
        dscr = self._wait(DSCR_INSTRCOMPL, f"命令 0x{instruction:08X} が終わらない")
        if dscr & DSCR_STICKY:
            self.last_dscrs = [dscr]
            return dscr, None
        for _ in range(_POLLS):                    # 受け取りの命令の値か、前から残っていた値か分からない: 読み捨てる
            if not dscr & _TX_ANY:
                break
            self._read(DTRTX)
            dscr = self._read(DSCR)
        else:
            raise ArmDebugError(f"0x{self.base:08X}: DTRTX が空にならない（DSCR 0x{dscr:08X}）")
        done = self.execute(read_instruction)
        value = self.from_core(done)
        self.last_dscrs = [dscr, done]
        return dscr, value

    def read_dtrtx_fast(self, instruction: int, count: int, fast_dscr: int | None = None) -> list[int]:
        """速いモード（DSCR.ExtDCCmode = 2）で: ITR に instruction を書き（この時点では実行されない。実機の Cortex-A7）、
        DTRTX を count 回まとめて読む（CMSIS-DAP のブロック転送。読むたびに今の値を返してから instruction を実行する。
        最初の値は前の値）。前の命令が終わっているのを見ていなければ（`_ready`）、まず待つ。fast_dscr を渡せば、速いモードに
        する DSCR の書きを ITR の書きの前に出す。書きと読みは 1 回の往復で（JTAG。`RawDap.mem_write32_then_read_repeat`）。"""
        if not self._ready:
            self._wait(DSCR_INSTRCOMPL, "前の命令が終わらない")
        writes = [] if fast_dscr is None else [(self.base + DSCR, fast_dscr & 0xFFFFFFFF)]
        self._ready = False                       # 読むたびに命令が走る: 次の命令の前に、終わったのを確かめる
        return self.dap.mem_write32_then_read_repeat(self.ap, [*writes, (self.base + ITR, instruction)],
                                                     self.base + DTRTX, count)

    def to_core(self, value: int) -> None:
        self._write(DTRRX, value)

    def from_core(self, dscr: int | None = None) -> int:
        """DTRTX を受け取る。dscr（直前に読んだ DSCR）に TXfull が立っていれば、待たずに読む。"""
        if dscr is None or not dscr & (DSCR_TXFULL | (1 << 26)):
            self._wait(DSCR_TXFULL | (1 << 26), "DTRTX に値が来ない")
        return self._read(DTRTX)

    def write_breakpoint(self, index: int, value: int, control: int) -> None:
        """無効にしてから番地を変え、制御を書く（3 つの書きを順に、1 回の往復で）。"""
        b = self.base
        self.dap.mem_write32_many(self.ap, [(b + BCR + 4 * index, 0), (b + BVR + 4 * index, value & 0xFFFFFFFF),
                                            (b + BCR + 4 * index, control)])

    def write_watchpoint(self, index: int, value: int, control: int) -> None:
        b = self.base
        self.dap.mem_write32_many(self.ap, [(b + WCR + 4 * index, 0), (b + WVR + 4 * index, value & 0xFFFFFFFF),
                                            (b + WCR + 4 * index, control)])
