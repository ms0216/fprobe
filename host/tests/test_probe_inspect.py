"""Probe の止まったターゲットを調べる道具（fprobe.probe_inspect）: 式・フレームの変数・呼び出し履歴・型・
逆アセンブル・SVD・周辺レジスタ・メモリの表示。機器は使わない（偽のターゲットと試験用ファームの ELF）。"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from dwarf_stack import StackBuilder
from test_debug_dwarf import FakeTarget as DwarfTarget

from fprobe.mcp_server import Probe
from fprobe.svd import Device
from fprobe.symbols import Symbols

FIRMWARE = Path(__file__).resolve().parents[1] / "firmware"
DWARF_O0 = FIRMWARE / "dwarf_demo" / "dwarf_demo_O0.elf"
SRAM_DEMO = FIRMWARE / "sram_demo" / "sram_demo.elf"
MINI_SVD = Path(__file__).resolve().parent / "data" / "mini.svd"


def connect(probe: Probe, target, symbols_path: Path | None = None, target_type: str = "cortex_m") -> Probe:
    """機器を開かずに、偽のターゲットへ接続した状態にする。"""
    probe._debug = SimpleNamespace(target=target, close=lambda: None)
    probe._target_type = target_type
    if symbols_path is not None:
        probe._symbols = Symbols(symbols_path)
    return probe


class BusTarget:
    """pyOCD の target の、メモリ・周辺レジスタの読み書きと PC だけ。中身はバイト列（番地 → 値）。"""

    def __init__(self, memory: dict[int, int] | None = None, pc: int = 0):
        self.memory = dict(memory or {})
        self.pc = pc
        self.log: list[tuple] = []

    @classmethod
    def from_elf(cls, path: Path, pc: int = 0) -> "BusTarget":
        target = cls(pc=pc)
        for seg in Symbols(path).segments:
            for i, b in enumerate(seg.data):
                target.memory[seg.address + i] = b
        return target

    def read_memory_block8(self, address, length):
        missing = [a for a in range(address, address + length) if a not in self.memory]
        if missing:
            raise RuntimeError(f"bus fault at 0x{missing[0]:08X}")
        return [self.memory[a] for a in range(address, address + length)]

    def read_memory(self, address, transfer_size=32):
        self.log.append(("read", address, transfer_size))
        n = transfer_size // 8
        return int.from_bytes(bytes(self.read_memory_block8(address, n)), "little")

    def write_memory(self, address, value, transfer_size=32):
        self.log.append(("write", address, transfer_size, value))
        for i, b in enumerate(value.to_bytes(transfer_size // 8, "little")):
            self.memory[address + i] = b

    def flush(self):
        pass

    def read_core_register(self, name):
        assert name == "pc"
        return self.pc


@pytest.fixture
def probe():
    return Probe("unused")                                     # 機器は使う時まで開かない


@pytest.fixture
def halted(probe):
    """-O0 の dwarf_demo: reset_handler → walk(1) → walk(0) → leaf_sum → probe_point で止まる。"""
    syms = Symbols(DWARF_O0)
    sensors = syms.variable("sensors").address
    b = StackBuilder(syms)
    b.enter("reset_handler", lr=0xFFFFFFFF)
    b.call("reset_handler", "walk", sensors, 1)
    b.call("walk", "walk", sensors + 80, 0)
    b.call("walk", "leaf_sum", sensors + 80, 0)
    pc = b.call("leaf_sum", "probe_point", 77)
    return connect(probe, DwarfTarget(b.halt_at(pc)), DWARF_O0)


# --- ソースの言葉の変数 ---------------------------------------------------------------------

def test_backtrace_keeps_its_fields_and_adds_function_line_and_variables(halted):
    plain = halted.backtrace()
    assert [f["function"] for f in plain] == ["probe_point", "leaf_sum", "walk", "walk", "reset_handler"]
    first = plain[0]
    assert {"pc", "where", "note"} <= set(first) and first["where"].startswith("probe_point")   # 既存の欄は同じ意味
    assert first["index"] == 0 and first["file"] == "dwarf_demo.c" and isinstance(first["line"], int)
    assert "arguments" not in first
    rich = halted.backtrace(variables=True)
    assert [a["name"] for a in rich[0]["arguments"]] == ["marker"] and rich[0]["arguments"][0]["value"] == 77
    depth = next(a for a in rich[3]["arguments"] if a["name"] == "depth")
    assert depth["value"] == 1                                                  # 外側の walk(1)
    json.dumps(rich)                                                            # JSON にできる


def test_evaluate_frame_variables_and_write_expression(halted):
    assert halted.evaluate("head->next->name")["value"]["string"] == "beta"
    assert halted.evaluate("depth", frame=3)["value"] == 1
    assert halted.frame_variables(0)["function"] == "probe_point"
    after = halted.write_expression("sensors[1].mode", "MODE_IDLE")
    assert after["value"] == {"enum": "MODE_IDLE", "value": 0}


def test_type_info_needs_only_the_elf(probe):
    with pytest.raises(RuntimeError):
        probe.type_info("union Word")
    probe.elf_load(str(DWARF_O0))                                               # ターゲット未接続
    info = probe.type_info("struct Sensor")
    assert info["kind"] == "struct" and any(m["name"] == "next" for m in info["members"])


# --- 逆アセンブル ---------------------------------------------------------------------------

def test_disassembly_from_the_elf_when_not_connected(probe):
    probe.elf_load(str(SRAM_DEMO))
    listing = probe.disasm_function("tick")
    assert listing["memory"] == "elf" and listing["total"] == 14 and listing["sync"] == "function"
    first = listing["instructions"][0]
    assert (first["address"], first["mnemonic"], first["function"]) == (0x20000050, "push", "tick")
    page = probe.disasm_function("tick", offset=10, limit=2)
    assert [i["address"] for i in page["instructions"]] == [i["address"] for i in listing["instructions"][10:12]]
    assert probe.disasm_range(0x20000050, 8)["instructions"][0]["mnemonic"] == "push"
    json.dumps(listing)


def test_disassembly_reads_the_target_around_the_pc(probe):
    target = BusTarget.from_elf(SRAM_DEMO, pc=0x20000058)
    connect(probe, target, SRAM_DEMO)
    around = probe.disasm_around(before=2, after=1)
    assert around["memory"] == "target" and around["pc"] == 0x20000058
    assert around["instructions"][around["pc_index"]]["address"] == 0x20000058
    text = probe.disasm_text(location="tick")["text"]
    assert "tick:" in text and "counter++;" in text                            # ソースの行を挟む
    assert "=> " in probe.disasm_text()["text"]                                 # PC の前後は印付き
    with pytest.raises(ValueError):
        probe.disasm_text(length=8)                                            # 範囲には番地が要る


def test_without_target_or_elf_disassembly_is_an_error(probe):
    with pytest.raises(RuntimeError):
        probe.disasm_range(0x20000000, 4)


# --- 周辺レジスタ ---------------------------------------------------------------------------

@pytest.fixture
def mini(probe):
    target = BusTarget({a: 0 for a in range(0x40001000, 0x40001070)} | {a: 0 for a in range(0x40003000, 0x40003008)})
    target.write_memory(0x40001000, 0x3405)
    target.log.clear()
    connect(probe, target)
    probe.svd_load(path=str(MINI_SVD))
    return probe, target


def test_svd_load_lists_peripherals_and_registers(mini):
    probe, _ = mini
    assert probe.svd_load(path=str(MINI_SVD))["peripherals"] == 3
    names = [p["name"] for p in probe.svd_peripherals()["peripherals"]]
    assert names == ["TIMER0", "TIMER1", "UART"]
    assert [p["name"] for p in probe.svd_peripherals(filter="timer")["peripherals"]] == ["TIMER0", "TIMER1"]
    regs = probe.svd_registers("TIMER0")["registers"]
    assert regs[0]["path"] == "TIMER0.CTRL" and regs[0]["fields"][0]["name"] == "EN"


def test_svd_is_found_from_the_target_name(probe):
    connect(probe, BusTarget(), target_type="stm32f401xe")
    assert probe.svd_peripherals(filter="RCC")["device"].startswith("STM32F401")   # 自動で読む
    with pytest.raises(RuntimeError):
        connect(Probe("unused"), BusTarget()).svd_peripherals()                    # cortex_m では探せない


def test_peripheral_read_decodes_fields_and_skips_side_effects(mini):
    probe, target = mini
    one = probe.peripheral_read("TIMER0.CTRL")["registers"][0]
    assert one["value"] == 0x3405 and one["fields"][0] == {"name": "EN", "bits": "[0]", "value": 1, "enum": "On"}
    whole = probe.peripheral_read("TIMER0")
    skipped = {s["path"]: s["reason"] for s in whole["skipped"]}
    assert "TIMER0.DATA" in skipped                                             # readAction
    assert all(("read", 0x4000100C, 32) != entry for entry in target.log)       # 読んでいない
    assert "TIMER0.CTRL" in [r["path"] for r in whole["registers"]]
    uart = probe.peripheral_read("UART")
    assert "UART.DR" in {s["path"] for s in uart["skipped"]}
    assert {r["path"] for r in probe.peripheral_read("UART", allow_side_effects=True)["registers"]} >= {"UART.DR"}


def test_peripheral_read_reports_bus_errors_per_register(probe):
    connect(probe, BusTarget({a: 0 for a in range(0x40001000, 0x40001004)}))
    probe.svd_load(path=str(MINI_SVD))
    out = probe.peripheral_read("TIMER0")
    assert [r["path"] for r in out["registers"]] == ["TIMER0.CTRL"]
    assert any(s["reason"].startswith("read failed") for s in out["skipped"])


def test_peripheral_write_by_fields_returns_before_and_after(mini):
    probe, target = mini
    out = probe.peripheral_write("TIMER0.CTRL", fields={"EN": "Off"})
    assert out["before"]["value"] == 0x3405 and out["after"]["value"] == 0x3404 and out["written"] == 0x3404
    assert probe.peripheral_write("TIMER0.CTRL2", value=7)["after"]["value"] == 7
    with pytest.raises(ValueError):
        probe.peripheral_write("TIMER0.CTRL")
    with pytest.raises(PermissionError):
        probe.peripheral_write("TIMER0.DATA", fields={})                        # 読むと消える: 読み・変更・書きをしない


# --- メモリの表示 ---------------------------------------------------------------------------

def test_memory_view_marks_changes_and_names(probe):
    syms = Symbols(SRAM_DEMO)
    counter = syms.variable("counter").address
    target = BusTarget.from_elf(SRAM_DEMO)
    for a in range(counter, counter + 16):
        target.memory.setdefault(a, 0)
    connect(probe, target, SRAM_DEMO)
    first = probe.memory_view(counter, 16, unit=32, compare=True)
    assert first["compared"] is False and first["changes"] == []
    assert {"address": counter, "name": "counter", "kind": "symbol"} in first["names"]
    target.write_memory(counter, 5)
    second = probe.memory_view(counter, 16, unit=32, compare=True)
    assert second["rows"][0]["values"][0] == 5 and second["rows"][0]["changed"][0]
    assert second["changes"] == [{"address": counter, "before": "00", "after": "05"}]
    assert "*" in second["dump"]
    with pytest.raises(ValueError):
        probe.memory_view(counter, 5000)


def test_memory_view_stops_at_the_end_of_the_32_bit_address_space(probe):
    # 0xFFFFFFF0 から 256 バイト: 番地の空間の終わりで止める（折り返して 0x100000000 の行に番地 0 の中身を出さない）
    target = BusTarget({a: a & 0xFF for a in range(0xFFFFFFF0, 0x1_0000_0000)} | {a: 0xEE for a in range(0, 0x100)})
    connect(probe, target)
    view = probe.memory_view(0xFFFFFFF0, 256)
    assert view["length"] == 16 and [r["address"] for r in view["rows"]] == [0xFFFFFFF0]
    assert view["rows"][0]["values"] == list(range(0xF0, 0x100))
    with pytest.raises(ValueError):
        probe.memory_view(0x1_0000_0000, 16)


def test_memory_view_names_registers_of_the_loaded_svd(mini):
    probe, _ = mini
    names = probe.memory_view(0x40001000, 16, unit=32)["names"]
    assert {"address": 0x40001000, "name": "TIMER0.CTRL", "kind": "register"} in names


def test_device_of_the_svd_survives_an_elf_reload(mini):
    probe, _ = mini
    assert isinstance(probe._svd, Device)
    probe.elf_load(str(SRAM_DEMO))
    assert probe._svd is not None and probe._image is None
