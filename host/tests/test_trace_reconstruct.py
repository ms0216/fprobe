"""fprobe.trace.reconstruct: ETM のパケットと ELF から命令の流れを再構成する。

etm_demo.elf（host/firmware/etm_demo、Cortex-M33、0x10000000〜）の上を走ったことにしたトレースを手で組む。
番地は objdump の出力で確かめたもの（ELF を作り直したら見直す）:
  reset_handler 0x00 movs / loop 0x02 bl leaf / 0x06 subs / 0x08 bne loop / 0x0a ldr r1 / 0x0c blx r1 / 0x0e movs /
  0x10 bl dispatch / 0x14 movs / 0x16 bl cond_ret / 0x1a b.w reset_handler / 0x20 リテラル
  leaf 0x24 adds / 0x26 bx lr、indirect 0x28 nop / 0x2a bx lr、dispatch 0x2c cmp / 0x2e bhi / 0x30 tbb / 0x34 表 /
  c0 0x38、c1 0x3c movs / 0x3e bx lr、c2 0x40、cond_ret 0x44 cmp / 0x46 it / 0x48 bxeq lr / 0x4a / 0x4c、
  irq_handler 0x4e push / 0x50 adds / 0x52 pop {r4, pc}（すべて + 0x10000000）
"""

from pathlib import Path

import pytest

import etm_encoder as enc
from fprobe.memview import BytesMemory, ElfImage
from fprobe.symbols import Symbols
from fprobe.trace.etmv4 import Etmv4Decoder
from fprobe.trace.reconstruct import Reconstructor

ELF = Path(__file__).resolve().parents[1] / "firmware" / "etm_demo" / "etm_demo.elf"
B = 0x1000_0000


@pytest.fixture(scope="module")
def recon():
    return Reconstructor(ElfImage(ELF), Symbols(ELF))


def run(recon, data: bytes):
    return recon.run(Etmv4Decoder().decode(data))


def addr(a: int) -> bytes:
    return enc.long_address_is1(B + a)


SCENARIO = (enc.async_() + enc.trace_info() + addr(0x00)
            + enc.atoms("EE") + addr(0x06)                 # bl leaf, bx lr → 0x06
            + enc.atoms("EEE") + addr(0x06)                # bne loop, bl leaf, bx lr → 0x06
            + enc.atoms("NE") + addr(0x28)                 # bne 不成立, blx r1 → indirect
            + enc.atoms("E") + addr(0x0E)                  # bx lr → 0x0e
            + enc.atoms("ENE") + addr(0x3C)                # bl dispatch, bhi 不成立, tbb → c1
            + enc.atoms("E") + addr(0x14)                  # bx lr → 0x14
            + enc.atoms("EE") + addr(0x1A)                 # bl cond_ret, bxeq lr 成立 → 0x1a
            + enc.atoms("E"))                              # b.w reset_handler
EXPECTED = [0x00, 0x02, 0x24, 0x26, 0x06, 0x08, 0x02, 0x24, 0x26, 0x06, 0x08, 0x0A, 0x0C, 0x28, 0x2A, 0x0E, 0x10,
            0x2C, 0x2E, 0x30, 0x3C, 0x3E, 0x14, 0x16, 0x44, 0x46, 0x48, 0x1A]


def test_the_flow_of_a_whole_scenario(recon):
    flow = run(recon, SCENARIO)
    assert flow.addresses() == [B + a for a in EXPECTED]
    assert flow.count == len(EXPECTED) and not flow.errors
    assert flow.next_address == B                                          # b.w の行き先（まだ実行していない）


def test_instructions_carry_names_and_branch_outcomes(recon):
    flow = run(recon, SCENARIO)
    items = flow.instructions()
    first = items[0]
    assert (first["index"], first["address"], first["mnemonic"], first["function"]) == (0, B, "movs", "reset_handler")
    bl = items[1]
    assert bl["mnemonic"] == "bl" and bl["branch"] == "taken" and bl["target"] == B + 0x24
    bne_not = items[10]
    assert bne_not["address"] == B + 0x08 and bne_not["branch"] == "not_taken"
    assert items[2]["function"] == "leaf" and items[2]["offset"] == 0
    assert "branch" not in items[0]
    assert flow.instructions(offset=26, limit=5) == items[26:]


def test_history_is_the_last_instructions(recon):
    flow = run(recon, SCENARIO)
    last = flow.history(3)
    assert [i["address"] for i in last] == [B + 0x46, B + 0x48, B + 0x1A]
    assert [i["function"] for i in last] == ["cond_ret", "cond_ret", "reset_handler"]
    assert [i["index"] for i in last] == [25, 26, 27]
    assert flow.history(1000) == flow.instructions()


def test_atoms_after_an_indirect_branch_wait_for_its_address(recon):
    # bx lr（間接）の E の後に、同じパケットの bne の E が来る。bne は 0x06 からたどる
    flow = run(recon, enc.async_() + enc.trace_info() + addr(0x24) + enc.atoms("EE") + addr(0x06))
    assert flow.addresses() == [B + a for a in (0x24, 0x26, 0x06, 0x08)]
    assert flow.next_address == B + 0x02


def test_exception_entry_and_return(recon):
    data = (enc.async_() + enc.trace_info() + addr(0x24)
            + enc.exception(15) + addr(0x26)              # adds を実行して SysTick、戻り先 0x26
            + addr(0x4E)                                  # ハンドラの先頭
            + enc.atoms("E") + enc.exception_return() + addr(0x26)    # pop {r4, pc} → 戻る
            + enc.atoms("E") + addr(0x06))                # bx lr
    flow = run(recon, data)
    assert flow.addresses() == [B + a for a in (0x24, 0x4E, 0x50, 0x52, 0x26)]
    marks = [(m.kind, m.index) for m in flow.markers]
    assert ("exception", 1) in marks and ("exception_return", 4) in marks
    ex = next(m for m in flow.markers if m.kind == "exception")
    assert ex.info["name"] == "SysTick" and ex.info["return_address"] == B + 0x26
    items = flow.instructions()
    assert items[0]["branch"] == "exception"                              # 例外の前の最後の命令


def test_exception_while_waiting_for_an_indirect_target(recon):
    # bx lr の行き先を待つ間に例外: 戻り先 = 行き先（そこの命令はまだ実行していない）
    data = (enc.async_() + enc.trace_info() + addr(0x24) + enc.atoms("E")
            + enc.exception(15) + addr(0x06) + addr(0x4E) + enc.atoms("E") + addr(0x06) + enc.atoms("E"))
    flow = run(recon, data)
    assert flow.addresses() == [B + a for a in (0x24, 0x26, 0x4E, 0x50, 0x52, 0x06, 0x08)]


def test_timestamps_and_gaps_are_markers(recon):
    data = (enc.async_() + enc.trace_info() + addr(0x24) + enc.timestamp(500) + enc.atoms("E") + addr(0x06)
            + enc.trace_on() + addr(0x28) + enc.overflow() + enc.async_() + enc.trace_info() + addr(0x2C)
            + enc.atoms("N"))
    flow = run(recon, data)
    assert flow.addresses() == [B + a for a in (0x24, 0x26, 0x2C, 0x2E)]
    marks = [(m.kind, m.index) for m in flow.markers]
    assert ("timestamp", 0) in marks and ("trace_on", 2) in marks and ("overflow", 2) in marks
    ts = next(m for m in flow.markers if m.kind == "timestamp")
    assert ts.info["value"] == 500


def test_code_that_cannot_be_read_is_reported(recon):
    flow = run(recon, enc.async_() + enc.trace_info() + enc.long_address_is1(0x2000_0000) + enc.atoms("E"))
    assert flow.count == 0
    assert flow.errors and "0x20000000" in flow.errors[0]
    assert any(m.kind == "lost" for m in flow.markers)


def test_atoms_without_a_known_address_are_counted_as_lost(recon):
    flow = run(recon, enc.async_() + enc.trace_info() + enc.atoms("EEN"))
    assert flow.count == 0 and flow.unresolved_atoms == 3


def test_target_memory_instead_of_elf():
    # 実機のメモリ（ここではバイト列）でも同じ: leaf の 2 命令
    image = ElfImage(ELF)
    memory = BytesMemory(B, image.read_memory(B, 0x54))
    flow = Reconstructor(memory).run(Etmv4Decoder().decode(enc.async_() + addr(0x24) + enc.atoms("E") + addr(0x06)))
    assert flow.addresses() == [B + 0x24, B + 0x26]
    assert flow.instructions()[0].get("function") is None                   # シンボルが無ければ名前は無い


def test_a_runaway_walk_is_stopped(recon):
    # pop {r4, pc}（0x52）が不成立 → 0x54 は ELF の終わりの外: 誤りとして止まる
    flow = run(recon, enc.async_() + addr(0x50) + enc.atoms("N") + enc.atoms("E"))
    assert flow.addresses() == [B + 0x50, B + 0x52]
    assert flow.errors and "0x10000054" in flow.errors[0] and flow.next_address is None
