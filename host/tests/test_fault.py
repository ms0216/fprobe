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
    assert fault.analyze(core, entry_sp=0x20007F20)["frame"]["pc"]["address"] == 0x20000140
