"""fprobe.fault: 故障レジスタの解読と、積まれた例外のフレームの位置。偽のコア（レジスタとメモリ）で試す。"""

from fprobe import fault


class FakeCore:
    def __init__(self, regs, memory, partno=0xC24):
        self.regs = regs
        self.memory = {fault.CPUID: 0x410F0000 | partno << 4, **memory}

    def read32(self, address):
        return self.memory.get(address, 0)

    def read_core_register(self, name):
        return self.regs[name]


def stack(base, words):
    return {base + 4 * i: w for i, w in enumerate(words)}


def test_fpu_disabled_is_explained_with_the_faulting_instruction():
    """今日の実機で当たった故障: FPU を有効にせずに浮動小数点の命令 → UsageFault（NOCP）→ HardFault に格上げ。"""
    frame = [1, 2, 3, 4, 12, 0x20000101, 0x20000140, 0x01000000]
    core = FakeCore({"xpsr": 0x01000003, "lr": 0xFFFFFFF9, "msp": 0x20007F20, "psp": 0},
                    {fault.CFSR: 0x80000, fault.HFSR: 0x40000000, **stack(0x20007F20, frame)})
    r = fault.analyze(core)
    assert r["faulted"] and r["exception"]["name"] == "HardFault"
    assert [c["bit"] for c in r["causes"]] == ["FORCED", "NOCP"] and "CPACR" in r["causes"][1]["hint"]
    assert r["frame"]["stack"] == "MSP" and r["frame"]["pc"]["address"] == 0x20000140
    assert r["frame"]["caller_sp"] == 0x20007F40


def test_precise_bus_fault_gives_the_address_and_psp_fp_frame():
    core = FakeCore({"xpsr": 0x01000005, "lr": 0xFFFFFFED, "msp": 0x20007000, "psp": 0x20001000},
                    {fault.CFSR: (1 << 9) | (1 << 15), fault.BFAR: 0x40023800,
                     **stack(0x20001000, [0, 0, 0, 0, 0, 0x08000201, 0x08000300, 0x01000200])})
    r = fault.analyze(core)
    assert r["exception"]["name"] == "BusFault" and r["fault_address"]["address"] == 0x40023800
    assert [c["bit"] for c in r["causes"]] == ["PRECISERR", "BFARVALID"]
    f = r["frame"]
    assert f["stack"] == "PSP" and f["fp_context"] and f["caller_sp"] == 0x20001000 + 0x68 + 4   # 揃えの詰め物


def test_v6m_has_only_hardfault_and_running_code_is_not_a_fault():
    core = FakeCore({"xpsr": 0x01000003, "lr": 0xFFFFFFF1, "msp": 0x20000F00, "psp": 0},
                    stack(0x20000F00, [0] * 6 + [0x100, 0]), partno=0xC60)
    r = fault.analyze(core)
    assert r["faulted"] and r["causes"][0]["bit"] == "HardFault" and r["frame"]["pc"]["address"] == 0x100
    idle = fault.analyze(FakeCore({"xpsr": 0x01000000, "lr": 0x08000101}, {fault.DFSR: 0b10}))
    assert not idle["faulted"] and idle["exception"] is None and idle["halt_reasons"][0]["bit"] == "BKPT"


def test_a_handler_that_pushed_registers_needs_the_entry_sp():
    """ハンドラが push {r7, lr} した後で止まった: いまの MSP はフレームを 8 バイト外れる。CFA（入った時の sp）で正す。"""
    frame = [0] * 6 + [0x20000140, 0x01000000]
    core = FakeCore({"xpsr": 0x01000003, "lr": 0xFFFFFFF9, "msp": 0x20007F18, "psp": 0},
                    {fault.CFSR: 0x80000, **stack(0x20007F18, [0x20007F00, 0xFFFFFFF9]), **stack(0x20007F20, frame)})
    assert fault.analyze(core)["frame"]["pc"]["address"] != 0x20000140                  # ずれる
    assert fault.analyze(core)["frame"]["sp_source"] == "msp_now"                        # どの SP を使ったか（?17）
    assert fault.analyze(core, entry_sp=0x20007F20)["frame"]["pc"]["address"] == 0x20000140
    assert fault.analyze(core, entry_sp=0x20007F20)["frame"]["sp_source"] == "entry_sp"


def test_halt_reason_names_why_the_core_stopped():
    # DFSR（pyOCD が再開・ステップのたびにクリアする）と PC の命令から、止まった理由を 1 語で
    assert fault.halt_reason(0x01, 0x4770) == "request"                  # 停止要求（デバッガ・クロストリガ）・ステップ
    assert fault.halt_reason(0x02, 0xBE00) == "bkpt_instruction"         # ファームウェアの BKPT（panic・assert 等）
    assert fault.halt_reason(0x02, 0xBE00, breakpoint=True) == "breakpoint"   # デバッガが置いたソフトの BKPT
    assert fault.halt_reason(0x02, 0x4770) == "breakpoint"               # FPB
    assert fault.halt_reason(0x03, 0x4770) == "breakpoint"               # ステップで踏んだブレークポイント
    assert fault.halt_reason(0x04, None) == "watchpoint"
    assert fault.halt_reason(0x08, None) == "vector_catch"
    assert fault.halt_reason(0x10, None) == "external"
    assert fault.halt_reason(0x00, None) is None


def test_all_dfsr_bits_are_reported_beside_the_one_word_reason():
    # 複数のビットが立っていても理由は 1 語のまま。立っていたビットは全部添える（優先の順で事実を潰さない）
    assert fault.dfsr_bits(0x03) == ["HALTED", "BKPT"]
    assert fault.dfsr_bits(0x1C) == ["DWTTRAP", "VCATCH", "EXTERNAL"]
    assert fault.dfsr_bits(0) == []


def test_probe_reads_the_halt_reason_from_dfsr_and_the_instruction_at_pc():
    # Probe._halt_reason: DFSR と（BKPT なら）PC の命令を読む。デバッガが置いたブレークポイントと区別する
    from types import SimpleNamespace
    from fprobe.mcp_server import Probe

    class Core:
        def __init__(self, dfsr, code):
            self.mem = {fault.DFSR: dfsr}
            self.code = code

        def read32(self, address):
            return self.mem[address]

        def read16(self, address):
            return self.code[address]

    panic = SimpleNamespace(target=Core(0x02, {0x1003B5E0: 0xBE00}), _dbg=None, _link=None)
    assert Probe._halt_reason(panic, 0x1003B5E0) == {"halt_reason": "bkpt_instruction", "halt_bits": ["BKPT"]}
    mine = SimpleNamespace(target=Core(0x03, {0x20000100: 0xBE00}), _dbg=SimpleNamespace(breakpoints={0x20000100: 1}), _link=None)
    assert Probe._halt_reason(mine, 0x20000100) == {"halt_reason": "breakpoint", "halt_bits": ["HALTED", "BKPT"]}
    assert Probe._halt_reason(SimpleNamespace(target=Core(0x01, {}), _dbg=None, _link=None), 0x100)["halt_reason"] == "request"
    assert Probe._halt_reason(SimpleNamespace(target=SimpleNamespace(), _dbg=None, _link=None), 0x100) == {}   # 読めない


def test_halt_reason_is_only_read_on_cortex_m():
    # DFSR（0xE000ED30）は Cortex-M のもの。Cortex-A・ARM11・RISC-V には読み書きしない
    from types import SimpleNamespace
    from fprobe.mcp_server import Probe
    touched = []
    core = SimpleNamespace(read32=lambda a: touched.append(a) or 0x01, write32=lambda a, v: touched.append(a))
    for target in ("bcm2836", "rp2350_riscv"):
        p = SimpleNamespace(target=core, _dbg=None, _link={"target": target})
        assert Probe._halt_reason(p, 0x100) == {}
        Probe._clear_halt_reason(p)
    assert touched == []
    p = SimpleNamespace(target=core, _dbg=None, _link={"target": "rp2040_core0"})
    assert Probe._halt_reason(p, 0x100)["halt_reason"] == "request"


def test_hints_are_architecture_facts_not_typical_causes():
    # 「NULL ポインタの参照が多い」「スタックのあふれ」等の経験的な原因の候補を書かない（CLAUDE.md）。
    # 残すのはアーキテクチャの定義から言えること（どのレジスタに何が入るか等）だけ
    from fprobe.fault import decode_cfsr, decode_dfsr, decode_hfsr
    causes = decode_cfsr(0xFFFF_FFFF) + decode_hfsr(0xFFFF_FFFF) + decode_dfsr(0xFFFF_FFFF)
    folk = ("多い", "あふれ", "壊れ", "取り違え", "パック", "推測", "疑")
    assert not [c for c in causes if any(w in c.get("hint", "") for w in folk)]
    nocp = next(c for c in causes if c["bit"] == "NOCP")
    assert "CP10" in nocp["hint"] and "CP11" in nocp["hint"]


def test_registers_and_stacked_words_give_where_they_were_read():
    """書く側（画面・AI）が target.memory.write で書けるよう、読んだ番地と大きさ、1 を書いて消すレジスタを返す。"""
    frame = [1, 2, 3, 4, 12, 0x20000101, 0x20000140, 0x01000000]
    core = FakeCore({"xpsr": 0x01000003, "lr": 0xFFFFFFF9, "msp": 0x20007F20, "psp": 0},
                    {fault.CFSR: 0x80000, fault.HFSR: 0x40000000, **stack(0x20007F20, frame)})
    r = fault.analyze(core)
    assert r["locations"]["CFSR"] == {"address": 0xE000ED28, "size": 4}
    assert r["locations"]["BFAR"] == {"address": 0xE000ED38, "size": 4}
    assert r["write_one_to_clear"] == ["CFSR", "HFSR", "DFSR"]
    assert r["frame"]["locations"]["r0"] == {"address": 0x20007F20, "size": 4}
    assert r["frame"]["locations"]["pc"] == {"address": 0x20007F20 + 24, "size": 4}
    v6m = fault.analyze(FakeCore({"xpsr": 0x01000003, "lr": 0xFFFFFFF1, "msp": 0x20000F00, "psp": 0},
                                 stack(0x20000F00, [0] * 8), partno=0xC60))
    assert "locations" not in v6m and v6m["frame"]["locations"]["xpsr"]["address"] == 0x20000F00 + 28


def test_each_cause_names_the_register_it_was_read_from():
    """原因のビットがどのレジスタ（CFSR の MMFSR・BFSR・UFSR の部分、HFSR、DFSR）のものかを返す（画面がビットの名前で
    レジスタを引かない）。"""
    from fprobe.fault import decode_cfsr, decode_dfsr, decode_hfsr
    reg = {c["bit"]: c["register"] for c in decode_cfsr(0xFFFF_FFFF) + decode_hfsr(0xFFFF_FFFF) + decode_dfsr(0xFFFF_FFFF)}
    assert reg["DACCVIOL"] == "MMFSR" and reg["MMARVALID"] == "MMFSR"
    assert reg["PRECISERR"] == "BFSR" and reg["BFARVALID"] == "BFSR"
    assert reg["NOCP"] == "UFSR" and reg["DIVBYZERO"] == "UFSR"
    assert reg["FORCED"] == "HFSR" and reg["BKPT"] == "DFSR"
