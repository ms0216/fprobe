"""ARMv7-A の命令の列を 1 回の往復で（fprobe.armv7a_debug.run / run_all、fprobe.arm_debug の *_all）。
非ブロックのモードの規則（前の命令が終わっていない ITR の書きは捨てられる・空の DTRTX の読みは不定の値・満ちた DTRRX への書きは
捨てられる）を守る偽物で、命令が遅いとき・DCC に値が残っているときも、値が正しく、状態が壊れないことを確かめる。
往復の数（積んだ転送のある flush）は性能の約束として上限を置く（増えたら、まとめが崩れていないかを見る）。"""
import pytest
from arm_a_fakes import ArmCore, bcm2836
from dap_fakes import FakeDapLink, FakeMemAp, FakeTarget
from jtag_fakes import FakeChain, Tap

from fprobe.arm_a_target import arm_a_part
from fprobe.arm_debug import CP15, MRS_R0_CPSR, REGISTER_NAMES
from fprobe.linux_halt.backend_arma import ArmACores
from fprobe.linux_halt.core_access import MOE_BKPT_EVT

IDCODE = 0x4BA00477
LINEAR = dict(page_offset=0xC0000000, phys_offset=0, linear_end=0xC8000000)


def make():
    cores = [ArmCore(mpidr=0x80000F00 | i) for i in range(4)]
    for n, c in enumerate(cores):
        c.regs = [0x1000 * n + i for i in range(15)] + [0x8000 + 0x100 * n]
        c.bank_sp = {0x13: 0x5000 + n, 0x12: 0x6000 + n}
        c.cp15["contextidr"] = 0x300 + n
    apb, _ = bcm2836(cores, True)
    target = FakeTarget(dpidr=IDCODE, swd=False, jtag_dp_index=0, aps={0: FakeMemAp(), 1: apb},
                        jtag_chain=FakeChain([Tap(4, IDCODE)]))
    link = FakeDapLink(target)
    a = ArmACores.open(link, arm_a_part("bcm2836"), frequency_hz=1_000_000, **LINEAR)
    return a, cores, link


def trips(link, fn):
    f0 = link.flushes
    out = fn()
    return out, link.flushes - f0


def expected_regs(core):
    """止める前のレジスタ（make が入れた値。r0・r1 は入るときに借りるので、コアの中の値は変わる）。"""
    n = core.mpidr & 3
    return [0x1000 * n + i for i in range(15)] + [0x8000 + 0x100 * n, core.cpsr]


def test_reading_all_registers_is_one_round_trip_and_right():
    a, cores, link = make()
    a.halt_all()
    values, n = trips(link, lambda: a.regs(2))
    assert values == expected_regs(cores[2]) and n == 1


def test_writing_all_registers_is_one_round_trip_and_reaches_the_core_at_resume():
    a, cores, link = make()
    a.halt_all()
    new = [0xA0000 + i for i in range(15)] + [0x9000, cores[1].cpsr]
    _, n = trips(link, lambda: a.set_regs(1, new))
    assert n == 1
    a.resume_all()
    assert cores[1].regs[:15] == new[:15] and cores[1].regs[15] == 0x9000


@pytest.mark.parametrize("latency", [2, 3, 5])
def test_slow_instructions_are_detected_and_redone_one_by_one(latency):
    """どの命令も読み書き latency 回ぶん終わらない: 列の DSCR で見つけ、待って空にしてから 1 命令ずつやり直す。値は正しい。"""
    a, cores, link = make()
    a.halt_all()
    for c in cores:
        c.latency = latency
    assert a.regs(3) == expected_regs(cores[3])
    assert cores[3].dropped_itr or latency < 4                       # 遅いと、列の中の ITR の書きが実際に捨てられる
    assert a._cores[3].port.round_trip_retries >= 1
    new = [0xB0000 + i for i in range(15)] + [cores[3].regs[15], cores[3].cpsr]
    a.set_regs(3, new)
    assert a.regs(3) == new
    a.resume_all()
    assert cores[3].regs[:15] == new[:15] and not cores[3].halted


def test_one_slow_instruction_in_the_middle_of_entering_does_not_corrupt_the_saved_registers():
    """入るの後半（MOV r0, pc → 読む → MRS r0, cpsr → 読む）の MRS だけが遅い。やり直しは後半を頭から（r0 を毎回入れ直す）。
    前半で読んだ r0・r1 と、PC・CPSR が正しく、再開で元に戻る。"""
    a, cores, link = make()
    for c in cores:
        c.latency, c.slow = 3, {MRS_R0_CPSR}
    a.halt_all()
    for n, c in enumerate(cores):
        assert a.regs(n) == expected_regs(c)
    a.resume_all()
    assert [c.regs for c in cores] == [expected_regs(c)[:16] for c in cores]          # 借りた r0・r1 も戻った


def test_a_value_left_in_dtrtx_by_the_target_is_not_taken_for_a_register():
    """目標のソフトウェアが DTRTX に値を残していた（TXfull）: 列の頭の DSCR で見つけ、読み捨ててからやり直す（前の道は
    その値を r2 の値として返していた）。"""
    a, cores, link = make()
    a.halt_all()
    cores[0].tx, cores[0].txfull = 0xDEADBEEF, True
    assert a.regs(0) == expected_regs(cores[0])


def test_halting_all_cores_enters_them_together_with_one_slow_core():
    a, cores, link = make()
    cores[2].latency = 4
    report = a.halt_all()
    assert report.cores_halted == (0, 1, 2, 3)
    for n, c in enumerate(cores):
        assert a.regs(n) == expected_regs(c)


def test_snapshot_reads_the_context_id_and_svc_sp_of_every_core():
    a, cores, link = make()
    a.halt_all()
    cores[1].cpsr = 0x10                                              # USR で止まっていた（入り直す）
    a._cores[1].forget()
    snap, n = trips(link, a.snapshot)
    # SVC で止まったコアは今の SP（r13）、USR で止まったコア 1 は SVC のバンクの SP
    assert {k: (s.ctx, s.sp_svc) for k, s in snap.items()} == {k: (0x300 + k, 0x5001 if k == 1 else 0x1000 * k + 13)
                                                              for k in range(4)}
    assert cores[1].cpsr == 0x10                                      # USR に戻した
    assert n <= 4


def test_snapshot_in_usr_mode_with_slow_mode_switches_restores_the_mode_and_reads_the_right_bank():
    a, cores, link = make()
    cores[0].cpsr = 0x10
    a.halt_all()
    cores[0].latency, cores[0].slow = 3, {CP15["contextidr"]}
    snap = a.snapshot([0])
    assert (snap[0].ctx, snap[0].sp_svc) == (0x300, 0x5000) and cores[0].cpsr == 0x10


def kernel_word(cores, va, value):
    for c in cores:
        for i in range(4):
            c.memory[va + i] = value >> 8 * i & 0xFF


@pytest.mark.parametrize("slow", ["load", "receive", "both"])
def test_a_one_word_read_executes_the_load_once_even_when_it_is_slow(slow):
    """1 語の読み: 後置きの加算の LDR（1 回だけ実行する）と受け取り（MCR r1。何度でも同じ）を 1 回の往復で。遅くて確かめられ
    なければ、LDR はやり直さず、DTRTX を空にして受け取りだけをやり直す（LDR を 2 回実行すると、MMIO の読みの副作用が 2 回起きる）。"""
    from fprobe.arm_debug import LOAD_STORE, _to_dtr
    a, cores, link = make()
    a.halt_all()
    kernel_word(cores, 0xC0001000, 0x12345678)
    ldr, mcr = LOAD_STORE[4][0], _to_dtr(1)
    cores[0].latency, cores[0].slow = 4, {"load": {ldr}, "receive": {mcr}, "both": {ldr, mcr}}[slow]
    assert a.read_kernel(0xC0001000, 4) == bytes.fromhex("78563412")
    assert cores[0].executed.count(ldr) == 1


def test_a_slow_load_that_aborts_is_reported_as_an_abort_at_its_address():
    from fprobe.arm_debug import LOAD_STORE, ArmDebugError
    a, cores, link = make()
    a.halt_all()
    cores[0].faults.append((0x2000, 0x3000))
    cores[0].latency, cores[0].slow = 4, {LOAD_STORE[4][0]}
    with pytest.raises(ArmDebugError, match="0x00002000"):
        a._cores[0].read_memory(0x2000, 1)
    assert not cores[0].sticky                                        # 印を消してから断った
    assert a.regs(0) == expected_regs(cores[0])                       # その後も使える


# --- 往復の数の約束（偽の DAP。1 往復 = 積んだ転送のある flush。2026-11-03 の前 → 後） --------------------------------

def test_round_trips_of_the_gdb_operations_stay_within_their_bounds():
    """gdb の操作ごとの往復（PYNQ の Ethernet の 1 往復 約 380 µs）。前の値は 2026-11-03 のまとめの前（偽の DAP、同じ数え方）:
    接続 152 → 29、止める 59 → 5、状態 24 → 2、g 26 → 1、G 13 → 1、continue 22 → 3、止まった後の停止 53 → 3、組の点検 44 → 1、
    ブレークポイントの組 3 → 1、ステップ 25 → 7、m 4 B 5 → 2、m 64 B 11 → 4、M 4 B 17 → 6。"""
    from fprobe.linux_halt.backend_arma import ArmACores as Cores
    cores = [ArmCore(mpidr=0x80000F00 | i) for i in range(4)]
    apb, _ = bcm2836(cores, True)
    link = FakeDapLink(FakeTarget(dpidr=IDCODE, swd=False, jtag_dp_index=0, aps={0: FakeMemAp(), 1: apb},
                                  jtag_chain=FakeChain([Tap(4, IDCODE)])))
    a, n = trips(link, lambda: Cores.open(link, arm_a_part("bcm2836"), frequency_hz=1_000_000, **LINEAR))
    assert n <= 29
    assert trips(link, a.halt_all)[1] <= 5
    assert trips(link, a.snapshot)[1] <= 2
    assert trips(link, lambda: a.regs(0))[1] <= 1
    vals = a.regs(1)
    assert trips(link, lambda: a.set_regs(1, vals))[1] <= 1
    assert trips(link, a.programmed)[1] <= 1
    assert trips(link, lambda: a.read_kernel(0xC0001000, 4))[1] <= 2
    assert trips(link, lambda: a.read_kernel(0xC0001000, 8))[1] <= 3                 # 2 語: 1 語ずつの道（速いモードは 4）
    assert trips(link, lambda: a.read_kernel(0xC0001000, 64))[1] <= 4
    assert trips(link, lambda: a.write_kernel(0xC0001000, b"abcd"))[1] <= 6
    assert trips(link, lambda: a.set_hw_bp(0, 0, 0x10004, 4))[1] <= 1
    assert trips(link, lambda: a.clear_hw_bp(0, 0))[1] <= 1
    assert trips(link, lambda: a.step(0))[1] <= 7
    assert trips(link, a.resume_all)[1] <= 3
    cores[2].halted, cores[2].moe = True, MOE_BKPT_EVT
    assert trips(link, lambda: a.wait_event(0.5))[1] <= 2
    assert trips(link, a.halt_all)[1] <= 3
    assert trips(link, a.snapshot)[1] <= 2


def test_the_raw_dap_counts_its_round_trips():
    a, cores, link = make()
    a.halt_all()
    r0, f0 = a._dap.round_trips, link.flushes
    a.regs(0)
    a.programmed()
    assert a._dap.round_trips - r0 == link.flushes - f0 == 2
    assert REGISTER_NAMES[-1] == "cpsr"
