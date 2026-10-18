"""fprobe.debug.Debugger の DWARF の機能（CFI の呼び出し履歴・式・ローカル変数）: 偽の pyOCD のターゲット。"""

from pathlib import Path

import pytest
from dwarf_stack import StackBuilder
from pyocd.core import exceptions as pyocd_errors

from fprobe.debug import Debugger, PyocdAccess
from fprobe.dwarf.access import AccessError, ImageAccess
from fprobe.symbols import Symbols

FIRMWARE = Path(__file__).resolve().parents[1] / "firmware"
SOURCE = (FIRMWARE / "dwarf_demo" / "dwarf_demo.c").read_text(encoding="utf-8").splitlines()


def line_of(text: str) -> int:
    return next(i for i, line in enumerate(SOURCE, start=1) if text in line)


class FakeTarget:
    """pyOCD の CortexM の、Debugger と PyocdAccess が使うところだけ（中身は ImageAccess）。"""

    def __init__(self, image: ImageAccess):
        self.image = image

    def read_memory_block8(self, address, length):
        try:
            return list(self.image.read_memory(address, length))
        except AccessError as e:
            raise pyocd_errors.TransferFaultError(str(e)) from None

    def read_memory_block32(self, address, count):
        data = self.image.read_memory(address, 4 * count)
        return [int.from_bytes(data[i:i + 4], "little") for i in range(0, 4 * count, 4)]

    def write_memory_block8(self, address, data):
        self.image.write_memory(address, bytes(data))

    def flush(self):
        pass

    def read_core_register_raw(self, name):
        try:
            return self.image.read_register(name)
        except AccessError as e:
            raise pyocd_errors.CoreRegisterAccessError(str(e)) from None

    def read_core_registers_raw(self, names):
        return [self.read_core_register_raw(n) for n in names]


@pytest.fixture
def halted():
    """-O0 の dwarf_demo: reset_handler → walk(1) → walk(0) → leaf_sum → probe_point で止まる。"""
    syms = Symbols(FIRMWARE / "dwarf_demo" / "dwarf_demo_O0.elf")
    sensors = syms.variable("sensors").address
    b = StackBuilder(syms)
    b.enter("reset_handler", lr=0xFFFFFFFF)
    b.call("reset_handler", "walk", sensors, 1)
    b.call("walk", "walk", sensors + 80, 0)
    b.call("walk", "leaf_sum", sensors + 80, 0)
    pc = b.call("leaf_sum", "probe_point", 77)
    return Debugger(FakeTarget(b.halt_at(pc)), syms), syms


def test_backtrace_uses_cfi_and_reports_call_lines(halted):
    dbg, syms = halted
    frames = dbg.backtrace()
    assert [f.where.function for f in frames] == ["probe_point", "leaf_sum", "walk", "walk", "reset_handler"]
    leaf = frames[1]
    site = syms.resolve(f"dwarf_demo.c:{line_of('probe_point(total + corner.x);')}")
    assert leaf.where.line == line_of("probe_point(total + corner.x);")
    assert leaf.where.address == leaf.pc and leaf.pc > site                # 戻り先の番地・呼び出し命令の行
    assert leaf.where.offset == leaf.pc - syms.symbol("leaf_sum").address
    assert all(f.cfa for f in frames[:-1]) and all(f.note == "" for f in frames)


def test_expressions_and_frame_variables(halted):
    dbg, _ = halted
    assert dbg.evaluate("head->next->name")["value"]["string"] == "beta"
    assert dbg.evaluate("depth", frame=3)["value"] == 1
    assert dbg.frame_variables(0) == {"function": "probe_point", "arguments": [
        {"name": "marker", "type": "int", "value": 77, "line": line_of("void probe_point"),
         "address": dbg.evaluate("&marker")["value"]["pointer"]}], "locals": []}
    dbg.write_expression("sensors[1].mode", "MODE_IDLE")
    assert dbg.evaluate("sensors[1].mode")["value"] == {"enum": "MODE_IDLE", "value": 0}
    assert dbg.type_info("union Word")["size"] == 4


def test_without_dwarf_the_simple_backtrace_remains():
    image = ImageAccess([(0x2000_1000, (0x1234).to_bytes(4, "little"))],
                        {"pc": 0x100, "lr": 0x201, "xpsr": 0, "msp": 0x2000_1000, "psp": 0})
    frames = Debugger(FakeTarget(image)).backtrace()
    assert [(f.pc, f.note) for f in frames] == [(0x100, ""), (0x200, "")]
    with pytest.raises(RuntimeError):
        Debugger(FakeTarget(image)).evaluate("x")


def test_pyocd_access_translates_errors_and_double_registers():
    image = ImageAccess([], {"s0": 1, "s1": 2})
    access = PyocdAccess(FakeTarget(image))
    assert access.read_register("d0") == 1 | 2 << 32
    with pytest.raises(AccessError):
        access.read_register("r0")
    with pytest.raises(AccessError):
        access.read_memory(0x4000, 4)


def test_dwarf_is_shared_between_debuggers_of_the_same_elf(halted):
    # Probe は接続し直すたびに Debugger を作り直す。同じ ELF の DWARF は読み直さない。ELF を替えれば替わる
    dbg, syms = halted
    again = Debugger(dbg.target, Symbols(syms.path))
    assert again.debug_info() is dbg.debug_info()
    other = Debugger(dbg.target, Symbols(FIRMWARE / "dwarf_demo" / "dwarf_demo_O1.elf"))
    assert other.debug_info() is not dbg.debug_info()
    assert Debugger(dbg.target).debug_info() is None


def test_backtrace_can_share_the_unwinding_with_an_inspector(halted):
    dbg, _ = halted
    inspector = dbg.inspector()
    frames = dbg.backtrace(inspector=inspector)
    assert inspector._frames is not None and len(inspector.frames()) == len(frames)      # 巻き戻しは 1 回


def test_step_out_finds_the_return_site_and_the_frame_with_cfi(halted):
    # 行単位のステップ（out・until）は CFI で戻り先と、いまのフレームの CFA（呼び出し元の sp）を知る
    dbg, _ = halted
    frames = dbg.backtrace()
    assert dbg._caller_site() == (frames[1].pc, frames[0].cfa)
    assert dbg._frame_cfa() == frames[0].cfa
    assert frames[1].where.function == "leaf_sum"
    assert Debugger(dbg.target)._caller_site() is None                  # ELF が無い: 1 命令ずつ出る
