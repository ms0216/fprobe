"""Probe のスナップショットの道具（fprobe.probe_snapshot）: 止めた状態を保存し、切断してから開いて同じ道具
（呼び出し履歴・C の式・周辺レジスタ・故障・RTOS）で調べる。RAM・レジスタの書き戻し、ELF のコアファイル。
機器は使わない（dwarf_demo / freertos_demo の像の上に組み立てた偽のコア）。"""

import json
import shutil
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest
from dwarf_stack import StackBuilder
from elftools.elf.elffile import ELFFile
from probe_fakes import FakeDevice
from pyocd.core import exceptions as pyocd_errors
from pyocd.core.target import Target
from rtos_kernel import FreeRTOSBuilder, shared

from fprobe import fault
from fprobe.dwarf.access import AccessError, ImageAccess
from fprobe.mcp_server import Probe
from fprobe.probe_snapshot import SnapshotReadOnly
from fprobe.snapshot import Snapshot
from fprobe.symbols import Symbols

FIRMWARE = Path(__file__).resolve().parents[1] / "firmware"
DWARF_O0 = FIRMWARE / "dwarf_demo" / "dwarf_demo_O0.elf"
MINI_SVD = Path(__file__).resolve().parent / "data" / "mini.svd"
SRAM, SRAM_SIZE = 0x20000000, 0x8000
SCS, SCS_SIZE = 0xE000E000, 0x1000


class Cpu:
    """pyOCD の CortexM の偽物: SRAM（読んでいない番地は 0）・システム制御空間（読み書きできる語。既定 0）・
    周辺（words に置いた番地だけ。ほかはバスエラー）・コアレジスタ・止まる／走る。書き込みは writes に残す。"""

    def __init__(self, memory: ImageAccess, registers: dict[str, int], words: dict[int, int] | None = None,
                 fpu: bool = False):
        self.memory = memory
        self.registers = dict(registers)
        self.words = dict(words or {})                       # 周辺レジスタ
        self.scs: dict[int, int] = {fault.CPUID: 0x410FC241}  # Cortex-M4
        self.state, self.reason = Target.State.HALTED, Target.HaltReason.BREAKPOINT
        self.memory_map = [SimpleNamespace(start=SRAM, length=SRAM_SIZE, end=SRAM + SRAM_SIZE - 1, is_ram=True,
                                           is_flash=False, name="SRAM")]
        self.core_registers = SimpleNamespace(by_name=set(self.registers) | ({f"s{i}" for i in range(32)} | {"fpscr"}
                                                                             if fpu else set()))
        if fpu:
            self.registers.update({f"s{i}": 0x3F800000 + i for i in range(32)}, fpscr=0x03000000)
        self.writes: list[tuple] = []
        self.resumed = 0

    # メモリ
    def _byte(self, a: int) -> int:
        if SRAM <= a < SRAM + SRAM_SIZE:
            try:
                return self.memory.read_memory(a, 1)[0]
            except AccessError:
                return 0
        if SCS <= a < SCS + SCS_SIZE:
            return self.scs.get(a & ~3, 0) >> 8 * (a & 3) & 0xFF
        if a & ~3 in self.words:
            return self.words[a & ~3] >> 8 * (a & 3) & 0xFF
        raise pyocd_errors.TransferFaultError(f"bus fault at 0x{a:08X}")

    def read_memory_block8(self, address, length):
        return [self._byte(a) for a in range(address, address + length)]

    def read_memory_block32(self, address, count):
        data = bytes(self.read_memory_block8(address, 4 * count))
        return list(struct.unpack(f"<{count}I", data))

    def read_memory(self, address, transfer_size=32):
        return int.from_bytes(bytes(self.read_memory_block8(address, transfer_size // 8)), "little")

    def read32(self, address):
        return self.read_memory(address)

    def write_memory_block8(self, address, data):
        self.writes.append(("block", address, bytes(data)))
        if SRAM <= address < SRAM + SRAM_SIZE:
            self.memory.write_memory(address, bytes(data))

    def write_memory(self, address, value, transfer_size=32):
        self.writes.append(("word", address, value))
        if SCS <= address < SCS + SCS_SIZE:
            self.scs[address] = value
        else:
            self.words[address] = value

    def write32(self, address, value):
        self.write_memory(address, value)

    def flush(self):
        pass

    # コア
    def read_core_register_raw(self, name):
        try:
            return self.registers[name]
        except KeyError:
            raise pyocd_errors.CoreRegisterAccessError(name) from None

    read_core_register = read_core_register_raw

    def read_core_registers_raw(self, names):
        return [self.read_core_register_raw(n) for n in names]

    def write_core_register_raw(self, name, value):
        self.writes.append(("reg", name, value))
        self.registers[name] = value

    write_core_register = write_core_register_raw

    def get_state(self):
        return self.state

    def get_halt_reason(self):
        return self.reason

    def halt(self):
        self.state, self.reason = Target.State.HALTED, Target.HaltReason.USER

    def resume(self):
        self.resumed += 1
        self.state = Target.State.RUNNING


def connect(probe: Probe, cpu: Cpu, elf: Path | None = None, target_type: str = "cortex_m") -> Probe:
    probe._debug = SimpleNamespace(target=cpu, close=lambda: None)
    probe._target_type = target_type
    if elf is not None:
        probe._symbols = Symbols(elf)
    return probe


def halted_cpu() -> Cpu:
    """-O0 の dwarf_demo: reset_handler → walk(1) → walk(0) → leaf_sum → probe_point で止まる。"""
    syms = Symbols(DWARF_O0)
    sensors = syms.variable("sensors").address
    b = StackBuilder(syms)
    b.enter("reset_handler", lr=0xFFFFFFFF)
    b.call("reset_handler", "walk", sensors, 1)
    b.call("walk", "walk", sensors + 80, 0)
    b.call("walk", "leaf_sum", sensors + 80, 0)
    pc = b.call("leaf_sum", "probe_point", 77)
    image = b.halt_at(pc)
    regs = {name: image.read_register(name) for name in [f"r{i}" for i in range(13)] + ["sp", "lr", "pc"]}
    regs.update(xpsr=0x01000000, msp=regs["sp"], psp=0, control=0, primask=0)
    return Cpu(image, regs)


@pytest.fixture
def probe(tmp_path, monkeypatch):
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path))
    p = Probe("unused")
    p._session = SimpleNamespace(device=FakeDevice())
    yield p
    if p._recorder is not None:
        p.recording_stop()


@pytest.fixture
def live(probe):
    cpu = halted_cpu()
    return connect(probe, cpu, DWARF_O0), cpu


def disconnect(probe: Probe) -> None:
    probe._debug = None
    probe._symbols = None
    probe._dbg = None


# --- 保存 -------------------------------------------------------------------------------------

def test_save_keeps_registers_ram_system_registers_and_the_elf(live, tmp_path):
    probe, cpu = live
    cpu.scs[0xE000ED08] = SRAM                                                       # VTOR
    out = probe.snapshot_save(name="walk")
    path = Path(out["path"])
    assert path.parent == tmp_path / "snapshots" and path.suffix == ".fsnap" and "walk" in path.name
    snap = Snapshot.load(path)
    assert snap.registers["pc"] == cpu.registers["pc"] and snap.registers["msp"] == cpu.registers["msp"]
    access = snap.access()
    assert access.read_memory(SRAM, 64) == bytes(cpu.read_memory_block8(SRAM, 64))
    assert int.from_bytes(access.read_memory(0xE000ED08, 4), "little") == SRAM        # システム制御空間
    assert int.from_bytes(access.read_memory(fault.CFSR, 4), "little") == 0
    with pytest.raises(AccessError):
        access.read_memory(0xE000EDF0, 4)                    # DHCSR は読まない（読むと固定のビットが消える）
    assert snap.elf.matches(DWARF_O0) and snap.elf_data == DWARF_O0.read_bytes()
    assert snap.info["halt"]["reason"] == "breakpoint" and snap.info["halt"]["function"] == "probe_point"
    assert snap.info["target"] == "cortex_m" and snap.info["elf_path"] == str(DWARF_O0)
    assert out["regions"][0]["kind"] == "ram" and out["bytes"] > SRAM_SIZE
    assert not any(w[0] in ("block", "word", "reg") for w in cpu.writes)               # 何も書かない


def test_save_reads_only_side_effect_free_peripheral_registers(live):
    probe, cpu = live
    cpu.words = {0x40001000 + 4 * i: 0x100 + i for i in range(0x70 // 4)} | {0x40003000: 0x41, 0x40003004: 0x7}
    cpu.words[0x40001000] = 0x3405
    probe.svd_load(path=str(MINI_SVD))
    out = probe.snapshot_save()
    snap = Snapshot.load(out["path"])
    names = {r.name for r in snap.regions if r.kind == "peripheral"}
    assert "TIMER0.CTRL" in names and "TIMER0.STATUS" in names
    assert "TIMER0.DATA" not in names and "UART.DR" not in names                      # readAction・データの口
    assert "TIMER0.CTRL.START" not in names
    skipped = {s["path"]: s["reason"] for s in out["skipped"]}
    assert "readAction" in skipped["TIMER0.DATA"] and "UART.DR" in skipped
    assert any(p.startswith("TIMER1.") and "read failed" in r for p, r in skipped.items())   # バスエラーでも続ける
    assert snap.info["svd"]["origin"] == "file"
    out = probe.snapshot_save(skip_registers=["TIMER0.STATUS"])                  # 読まないものは使う側が明示する
    assert {s["path"]: s["reason"] for s in out["skipped"]}["TIMER0.STATUS"] == "skip TIMER0.STATUS"


def test_save_can_select_peripherals_or_none(live):
    probe, cpu = live
    cpu.words = {0x40001000 + 4 * i: 0 for i in range(0x70 // 4)} | {0x40003000: 0, 0x40003004: 0}
    probe.svd_load(path=str(MINI_SVD))
    only_uart = Snapshot.load(probe.snapshot_save(peripherals=["UART"])["path"])
    assert {r.name.split(".")[0] for r in only_uart.regions if r.kind == "peripheral"} == {"UART"}
    none = Snapshot.load(probe.snapshot_save(peripherals=[])["path"])
    assert not [r for r in none.regions if r.kind == "peripheral"]


def test_save_refuses_a_running_target_unless_asked_to_halt(live):
    probe, cpu = live
    cpu.state = Target.State.RUNNING
    with pytest.raises(RuntimeError, match="動いて"):
        probe.snapshot_save()
    out = probe.snapshot_save(halt=True)
    assert cpu.state == Target.State.HALTED and Snapshot.load(out["path"]).info["halt"]["reason"] == "user"


def test_save_takes_explicit_ranges_flash_and_fpu_registers(probe):
    cpu = halted_cpu()
    cpu.__init__(cpu.memory, cpu.registers, fpu=True)
    connect(probe, cpu, DWARF_O0)
    snap = Snapshot.load(probe.snapshot_save(ranges=[[SRAM, 0x400]])["path"])
    assert [(r.address, len(r.data)) for r in snap.regions if r.kind == "ram"] == [(SRAM, 0x400)]
    assert snap.registers["s3"] == 0x3F800003 and snap.registers["fpscr"] == 0x03000000
    assert any(r.address == 0xE000EF34 for r in snap.regions)                          # FPCCR（FPU があるとき）


def test_save_refers_to_the_recording_and_marks_it(live):
    probe, _ = live
    probe.recording_start(name="r")
    out = probe.snapshot_save()
    snap = Snapshot.load(out["path"])
    assert snap.info["recording"]["name"] == probe.recording_status()["name"]
    assert isinstance(snap.info["recording"]["time"], int)
    probe.recording_stop()
    events = probe.recording_find({"stream": "snapshot"})
    assert events["events"] and events["events"][0]["body"]["path"] == out["path"]


def test_save_includes_the_fault_analysis(live):
    probe, cpu = live
    cpu.scs[fault.CFSR] = 1 << 19                                                    # NOCP
    cpu.scs[fault.HFSR] = 1 << 30
    cpu.registers["xpsr"] = 0x01000003
    snap = Snapshot.load(probe.snapshot_save()["path"])
    assert snap.info["fault"]["faulted"] and [c["bit"] for c in snap.info["fault"]["causes"]] == ["FORCED", "NOCP"]


# --- 開く（プローブ無し） ---------------------------------------------------------------------------

def test_open_without_a_probe_answers_the_same_questions(live):
    probe, cpu = live
    before_bt = probe.backtrace(variables=True)
    before_expr = probe.evaluate("head->next->name")
    path = probe.snapshot_save()["path"]
    disconnect(probe)
    opened = probe.snapshot_open(path)
    assert opened["elf_path"] and opened["pc"] == cpu.registers["pc"]
    assert probe.backtrace(variables=True) == before_bt
    assert probe.evaluate("head->next->name") == before_expr
    assert probe.target_status()["state"] == "HALTED" and probe.target_status()["function"] == "probe_point"
    assert probe.read_registers(["pc"]) == {"pc": cpu.registers["pc"]}
    assert probe.memory_view(SRAM, 16)["memory"] == "snapshot"
    with pytest.raises(SnapshotReadOnly):
        probe.write_memory(SRAM, "00")
    with pytest.raises(SnapshotReadOnly):
        probe.target_resume()
    with pytest.raises(SnapshotReadOnly):
        probe.write_expression("sensors[1].mode", "MODE_IDLE")
    assert probe.snapshot_info()["open"] and probe.snapshot_close() == {"closed": True}
    with pytest.raises(Exception, match="未接続"):
        probe.target_status()


def test_open_refuses_while_connected_and_connecting_closes_it(live):
    probe, cpu = live
    path = probe.snapshot_save()["path"]
    with pytest.raises(RuntimeError, match="接続中"):
        probe.snapshot_open(path)
    disconnect(probe)
    probe.snapshot_open(path)
    connect(probe, cpu)
    assert probe.target is cpu                                     # 接続すれば実物を読む（スナップショットは影）
    probe._close_snapshot()
    assert not probe.snapshot_info()["open"]


def test_open_uses_the_embedded_elf_when_the_original_is_gone(probe, tmp_path):
    elf = tmp_path / "moved.elf"
    shutil.copy(DWARF_O0, elf)
    cpu = halted_cpu()
    connect(probe, cpu, elf)
    path = probe.snapshot_save()["path"]
    elf.unlink()
    disconnect(probe)
    opened = probe.snapshot_open(path)
    assert Path(opened["elf_path"]).read_bytes() == DWARF_O0.read_bytes()
    assert probe.backtrace()[0]["function"] == "probe_point"


def test_open_refuses_an_elf_of_another_build(live, tmp_path):
    probe, _ = live
    path = probe.snapshot_save(embed_elf=False)["path"]
    disconnect(probe)
    with pytest.raises(ValueError, match="違う"):
        probe.snapshot_open(path, elf=str(FIRMWARE / "dwarf_demo" / "dwarf_demo_O1.elf"))
    assert probe.snapshot_open(path, elf=str(DWARF_O0))["elf_path"] == str(DWARF_O0)


def test_open_without_any_elf_still_reads_by_address(live, tmp_path):
    probe, cpu = live
    elf = tmp_path / "gone.elf"
    shutil.copy(DWARF_O0, elf)
    probe.elf_load(str(elf))
    path = probe.snapshot_save(embed_elf=False)["path"]
    elf.unlink()
    disconnect(probe)
    opened = probe.snapshot_open(path)
    assert opened["elf_path"] is None and opened["warnings"]
    assert probe.read_memory(SRAM, 4) == bytes(cpu.read_memory_block8(SRAM, 4)).hex()


def test_offline_peripherals_and_fault_are_decoded_from_the_snapshot(live):
    probe, cpu = live
    cpu.words = {0x40001000 + 4 * i: 0 for i in range(0x70 // 4)} | {0x40003000: 0, 0x40003004: 0}
    cpu.words[0x40001000] = 0x3405
    cpu.scs[fault.CFSR] = 1 << 25                                                    # DIVBYZERO
    probe.svd_load(path=str(MINI_SVD))
    path = probe.snapshot_save()["path"]
    disconnect(probe)
    probe._svd = None
    probe.snapshot_open(path)
    ctrl = probe.peripheral_read("TIMER0.CTRL")["registers"][0]
    assert ctrl["value"] == 0x3405 and ctrl["fields"][0]["enum"] == "On"
    assert "TIMER0.DATA" in {s["path"] for s in probe.peripheral_read("TIMER0")["skipped"]}
    assert [c["bit"] for c in probe.target_fault()["causes"]] == ["DIVBYZERO"]


def test_offline_rtos_tasks_match_the_live_ones(probe):
    k = FreeRTOSBuilder()
    k.set_tick(42)
    h = {name: k.task(name, prio) for name, prio in [("ctrl", 3), ("sensor", 2), ("IDLE", 0)]}
    k.put_state(h["ctrl"], k.var("xSuspendedTaskList"))
    k.put_state(h["sensor"], k.ready(2))
    k.set_current(h["sensor"])
    k.put_state(h["IDLE"], k.ready(0))
    regs = {**{f"r{i}": 0 for i in range(13)}, "sp": SRAM + 0x7000, "lr": 0, "pc": 0x20000100, "xpsr": 0x01000000,
            "msp": SRAM + 0x7000, "psp": SRAM + 0x6000, "control": 0, "primask": 0}
    cpu = Cpu(k.memory, regs)
    probe._debug = SimpleNamespace(target=cpu, close=lambda: None)
    probe._symbols = shared()[1]
    live_tasks = probe.rtos_tasks()
    out = probe.snapshot_save()
    assert out["rtos"]["tasks"] == 3 and out["rtos"]["current"] == "sensor"
    disconnect(probe)
    probe.snapshot_open(out["path"])
    assert probe.rtos_tasks() == live_tasks
    assert probe.snapshot_info()["rtos"]["tasks"] == live_tasks["tasks"]              # 保存した時の一覧も残る


# --- 一覧・コアファイル ---------------------------------------------------------------------------

def test_list_shows_saved_snapshots_newest_first(live):
    probe, _ = live
    first = probe.snapshot_save(name="a")["path"]
    second = probe.snapshot_save(name="b")["path"]
    listed = probe.snapshot_list()["snapshots"]
    assert [s["path"] for s in listed[:2]] in ([second, first], [first, second])
    assert {s["path"] for s in listed} == {first, second} and listed[0]["pc"]


def test_export_core_writes_an_elf_core_file(live, tmp_path):
    probe, cpu = live
    path = probe.snapshot_save()["path"]
    out = probe.snapshot_export_core(path, str(tmp_path / "x.core"))
    with open(out["path"], "rb") as f:
        elf = ELFFile(f)
        assert elf["e_type"] == "ET_CORE"
        loads = [s for s in elf.iter_segments() if s["p_type"] == "PT_LOAD"]
        assert any(s["p_vaddr"] == SRAM and s["p_filesz"] == SRAM_SIZE for s in loads)
    assert out["gdb"].startswith("arm-none-eabi-gdb") and str(DWARF_O0.name) in out["gdb"]
    disconnect(probe)
    probe.snapshot_open(path)
    default = probe.snapshot_export_core()                                     # 開いているスナップショット
    assert Path(default["path"]).suffix == ".core" and Path(default["path"]).is_file()


# --- 書き戻し --------------------------------------------------------------------------------------

def test_restore_writes_ram_system_registers_and_core_registers(live):
    probe, cpu = live
    cpu.scs[0xE000ED08] = SRAM                                                        # VTOR
    cpu.scs[0xE000E100] = 0x5                                                         # NVIC ISER0
    cpu.scs[0xE000E010] = 0x7                                                         # SysTick: 有効・割り込み・コアの時計
    cpu.scs[0xE000E014] = 15999
    path = probe.snapshot_save()["path"]
    saved = dict(cpu.registers)
    ram = bytes(cpu.read_memory_block8(SRAM, SRAM_SIZE))
    # ターゲットが進んだ: RAM・レジスタ・VTOR・割り込みが変わる
    cpu.memory.write_memory(SRAM + 0x7F00, b"\xEE" * 16)
    cpu.registers.update(pc=0x20000040, sp=0x20001000, r0=0x1234)
    cpu.scs.update({0xE000ED08: 0, 0xE000E100: 0, 0xE000E010: 0})
    cpu.writes.clear()
    out = probe.snapshot_restore(path)
    assert bytes(cpu.read_memory_block8(SRAM, SRAM_SIZE)) == ram
    assert {k: cpu.registers[k] for k in saved if k != "sp"} == {k: v for k, v in saved.items() if k != "sp"}
    assert cpu.scs[0xE000ED08] == SRAM and cpu.scs[0xE000E100] == 0x5
    assert cpu.scs[0xE000E014] == 15999 and cpu.scs[0xE000E010] == 0x7
    order = [w[1] for w in cpu.writes if w[0] == "word"]
    assert order.index(0xE000E014) < order.index(0xE000E010)                         # SysTick は設定してから有効に
    assert not any(w[0] == "reg" and w[1] == "sp" for w in cpu.writes)              # sp は msp / psp で決まる
    assert out["ram_bytes"] == SRAM_SIZE and cpu.resumed == 0 and out["state"] == "HALTED"


def test_restore_can_resume(live):
    probe, cpu = live
    path = probe.snapshot_save()["path"]
    probe.snapshot_restore(path, resume=True)
    assert cpu.resumed == 1


def test_restore_refuses_when_the_code_outside_the_snapshot_differs(live):
    probe, cpu = live
    path = probe.snapshot_save(ranges=[[SRAM + 0x1000, 0x7000]])["path"]        # コード（ELF の区画）は取らない
    cpu.memory.write_memory(SRAM + 0x40, b"\x00\xBE")                              # コードが違う
    with pytest.raises(RuntimeError, match="ELF"):
        probe.snapshot_restore(path)
    assert probe.snapshot_restore(path, force=True)["code_checked"] is False


def test_restore_writes_peripherals_only_when_named(live):
    probe, cpu = live
    cpu.words = {0x40001000 + 4 * i: 0 for i in range(0x70 // 4)} | {0x40003000: 0, 0x40003004: 0}
    cpu.words[0x40001004] = 0xAB                                                     # TIMER0.CTRL2
    probe.svd_load(path=str(MINI_SVD))
    path = probe.snapshot_save()["path"]
    cpu.words[0x40001004] = 0
    probe.snapshot_restore(path)
    assert cpu.words[0x40001004] == 0                                                # 既定は周辺に書かない
    out = probe.snapshot_restore(path, peripherals=["TIMER0"])
    assert cpu.words[0x40001004] == 0xAB and "TIMER0.CTRL2" in out["peripherals"]
    assert "TIMER0.STATUS" not in out["peripherals"]                                 # 読むだけのレジスタは書かない


def test_restore_needs_a_live_target(live):
    probe, _ = live
    path = probe.snapshot_save()["path"]
    disconnect(probe)
    probe.snapshot_open(path)
    with pytest.raises(Exception, match="未接続"):
        probe.snapshot_restore(path)


def test_results_are_json(live):
    probe, _ = live
    out = probe.snapshot_save()
    json.dumps(out)
    json.dumps(probe.snapshot_info(out["path"]))
    json.dumps(probe.snapshot_list())
