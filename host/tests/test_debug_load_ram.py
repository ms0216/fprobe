"""fprobe.debug.Debugger の RAM への読み込み（target.program_ram）と、CFI の無い呼び出し履歴の印。

ヒューリスティックの点検 2 回目（docs/reports/2026-10-24-heuristics-audit-2/fprobe-top.md の H1・H2）:
- ベクタ表を「最も低い番地の区画」と仮定しない。使う側が vector_table を渡せばその表から、渡さなければ ELF の
  ヘッダの入口（e_entry。リンカが書いた事実）から起動する（GDB の load と同じ。SP・VTOR は変えない）。
- CFI が無いときの LR からの呼び出し元は、確かめていない推定なので note を付ける（test_debug_dwarf）。
"""

from pathlib import Path

from fprobe.debug import VTOR, Debugger
from fprobe.symbols import Symbols

ELF = Path(__file__).resolve().parents[1] / "firmware" / "dwarf_demo" / "dwarf_demo_O0.elf"


class LoadTarget:
    """load_to_ram が使う pyOCD の口だけ（メモリは辞書、レジスタも辞書）。"""

    def __init__(self):
        self.memory: dict[int, int] = {}
        self.registers = {"pc": 0, "msp": 0, "sp": 0, "xpsr": 0}
        self.words: dict[int, int] = {}

    def reset_and_halt(self):
        pass

    def write_memory_block8(self, address, data):
        for i, b in enumerate(data):
            self.memory[address + i] = b

    def read_memory_block32(self, address, count):
        return [int.from_bytes(bytes(self.memory.get(address + 4 * n + i, 0) for i in range(4)), "little")
                for n in range(count)]

    def write32(self, address, value):
        self.words[address] = value

    def write_core_register(self, name, value):
        self.registers[name] = value

    def read_core_register(self, name):
        return self.registers[name]

    def flush(self):
        pass


def test_without_a_vector_table_it_starts_at_the_elf_entry_and_leaves_sp_and_vtor():
    syms = Symbols(ELF)
    target = LoadTarget()
    stop, start = Debugger(target, syms).load_to_ram()
    assert stop.pc == syms.entry and target.registers["pc"] == syms.entry
    assert target.registers["msp"] == 0 and VTOR not in target.words          # 表を仮定しない: SP・VTOR は触らない
    assert start == {"from": "entry", "entry": syms.entry, "vector_table": None}


def test_with_an_explicit_vector_table_it_starts_from_that_table():
    syms = Symbols(ELF)
    table = syms.symbol("vectors").address
    target = LoadTarget()
    stop, start = Debugger(target, syms).load_to_ram(vector_table=table)
    sp, reset = target.read_memory_block32(table, 2)
    assert target.words[VTOR] == table and target.registers["msp"] == sp
    assert stop.pc == reset & ~1
    assert start == {"from": "vector_table", "entry": syms.entry, "vector_table": table}

