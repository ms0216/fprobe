"""fprobe.snapshot: スナップショットのファイル（保存・読み込み）、読みの裏（SnapshotAccess）、ELF の指紋、
ELF のコアファイル（GDB が読める形。pyelftools で中身を確かめる）。機器は使わない。"""

import json
import struct
import zipfile
from pathlib import Path

import pytest
from elftools.elf.elffile import ELFFile

from fprobe.dwarf.access import AccessError, TargetAccess
from fprobe.snapshot import (FORMAT, ElfPrint, Region, Snapshot, SnapshotError, core_file, merge_regions,
                             write_core)

FIRMWARE = Path(__file__).resolve().parents[1] / "firmware"
DWARF_O0 = FIRMWARE / "dwarf_demo" / "dwarf_demo_O0.elf"
REGS = {**{f"r{i}": 0x100 + i for i in range(13)}, "sp": 0x20007F00, "lr": 0x08000123, "pc": 0x08000200,
        "xpsr": 0x01000003, "msp": 0x20007F00, "psp": 0x20001000, "control": 0, "primask": 0}


def sample(**extra) -> Snapshot:
    return Snapshot(registers=dict(REGS),
                    regions=[Region(0x20000000, bytes(range(256)) * 4, "ram", "SRAM"),
                             Region(0xE000ED00, struct.pack("<4I", 0x410FC241, 0, 0x20000000, 0xFA050000), "system",
                                    "SCB"),
                             Region(0x40023800, struct.pack("<I", 0x0300AA83), "peripheral", "RCC.CR")],
                    info={"target": "stm32f401xe", "halt": {"reason": "breakpoint", "pc": 0x08000200}}, **extra)


# --- 読みの裏 ---------------------------------------------------------------------------

def test_access_reads_memory_and_registers_like_a_target():
    access = sample().access()
    assert isinstance(access, TargetAccess)
    assert access.read_memory(0x20000010, 4) == bytes([16, 17, 18, 19])
    assert access.read_memory(0x40023800, 4) == struct.pack("<I", 0x0300AA83)
    assert access.read_register("pc") == 0x08000200 and access.read_register("R13") == 0x20007F00
    with pytest.raises(AccessError):
        access.read_memory(0x200003FE, 4)                      # 区画の終わりをまたぐ
    with pytest.raises(AccessError):
        access.read_memory(0x30000000, 1)
    with pytest.raises(AccessError):
        access.read_register("s0")                             # 浮動小数点のレジスタは取っていない


def test_access_joins_adjacent_regions_and_builds_d_from_s():
    snap = Snapshot(registers={"s0": 0x11111111, "s1": 0x22222222},
                    regions=[Region(0x100, b"\x01\x02", "ram"), Region(0x102, b"\x03\x04", "ram")])
    access = snap.access()
    assert access.read_memory(0x101, 3) == b"\x02\x03\x04"
    assert access.read_register("d0") == 0x22222222_11111111


def test_overlapping_regions_are_refused():
    with pytest.raises(SnapshotError):
        Snapshot(registers={}, regions=[Region(0x100, bytes(16), "ram"), Region(0x108, bytes(4), "peripheral")])


def test_merge_regions_joins_only_touching_regions_of_one_kind():
    merged = merge_regions([Region(0x10, b"b", "ram"), Region(0x0, bytes(16), "ram"), Region(0x11, b"c", "flash")])
    assert [(r.address, len(r.data), r.kind) for r in merged] == [(0x0, 17, "ram"), (0x11, 1, "flash")]


# --- ファイル ------------------------------------------------------------------------------

def test_save_and_load_keep_everything(tmp_path):
    elf = ElfPrint.of(DWARF_O0)
    snap = sample(elf=elf, elf_data=DWARF_O0.read_bytes())
    path = snap.save(tmp_path / "a.fsnap")
    loaded = Snapshot.load(path)
    assert loaded.registers == REGS and loaded.info == snap.info and loaded.elf == elf
    assert [(r.address, r.data, r.kind, r.name) for r in loaded.regions] == \
        [(r.address, r.data, r.kind, r.name) for r in snap.regions]
    assert loaded.elf_data == DWARF_O0.read_bytes()
    with zipfile.ZipFile(path) as z:
        manifest = json.loads(z.read("manifest.json"))
    assert manifest["format"] == FORMAT and manifest["elf"]["sha256"] == elf.sha256
    small = next(r for r in manifest["regions"] if r["name"] == "RCC.CR")
    assert "hex" in small and "file" not in small                 # 小さい区画は manifest の中に
    assert "file" in next(r for r in manifest["regions"] if r["name"] == "SRAM")


def test_load_refuses_other_files_and_newer_formats(tmp_path):
    bad = tmp_path / "bad.fsnap"
    bad.write_bytes(b"not a zip")
    with pytest.raises(SnapshotError):
        Snapshot.load(bad)
    newer = tmp_path / "newer.fsnap"
    with zipfile.ZipFile(newer, "w") as z:
        z.writestr("manifest.json", json.dumps({"format": FORMAT + 1}))
    with pytest.raises(SnapshotError, match="版"):
        Snapshot.load(newer)


def test_load_refuses_a_region_whose_file_does_not_match_its_length(tmp_path):
    path = sample().save(tmp_path / "a.fsnap")
    with zipfile.ZipFile(path) as z:
        items = {n: z.read(n) for n in z.namelist()}
    manifest = json.loads(items["manifest.json"])
    manifest["regions"][0]["length"] += 1
    items["manifest.json"] = json.dumps(manifest).encode()
    broken = tmp_path / "broken.fsnap"
    with zipfile.ZipFile(broken, "w") as z:
        for n, data in items.items():
            z.writestr(n, data)
    with pytest.raises(SnapshotError):
        Snapshot.load(broken)


def test_elf_print_identifies_the_build(tmp_path):
    elf = ElfPrint.of(DWARF_O0)
    assert elf.name == DWARF_O0.name and elf.size == DWARF_O0.stat().st_size and len(elf.sha256) == 64
    assert elf.matches(DWARF_O0)
    other = tmp_path / "x.elf"
    other.write_bytes(DWARF_O0.read_bytes() + b"\0")
    assert not elf.matches(other) and not elf.matches(tmp_path / "missing.elf")


def test_summary_is_json_and_counts_bytes():
    s = sample().summary()
    json.dumps(s)
    assert s["regions"][0] == {"address": 0x20000000, "length": 1024, "kind": "ram", "name": "SRAM"}
    assert s["bytes"] == 1024 + 16 + 4 and s["pc"] == 0x08000200


# --- ELF のコアファイル -------------------------------------------------------------------------

def test_core_file_has_prstatus_registers_and_memory_segments(tmp_path):
    regs = {**REGS, **{f"s{i}": i for i in range(32)}, "fpscr": 0x03000000}
    snap = Snapshot(registers=regs, regions=sample().regions, info={})
    path = write_core(snap, tmp_path / "core.elf")
    with open(path, "rb") as f:
        elf = ELFFile(f)
        assert elf["e_type"] == "ET_CORE" and elf["e_machine"] == "EM_ARM" and elf.elfclass == 32
        loads = [s for s in elf.iter_segments() if s["p_type"] == "PT_LOAD"]
        assert [(s["p_vaddr"], s["p_filesz"]) for s in loads] == [(0x20000000, 1024), (0x40023800, 4),
                                                                 (0xE000ED00, 16)]
        assert loads[0].data() == bytes(range(256)) * 4
        assert loads[0]["p_flags"] == 6                                      # RW
        notes = [n for s in elf.iter_segments() if s["p_type"] == "PT_NOTE" for n in s.iter_notes()]
    by_name = {(n["n_name"], n["n_type"]): n for n in notes}
    prstatus = by_name[("CORE", "NT_PRSTATUS")]
    desc = prstatus["n_descdata"]
    assert len(desc) == 148
    pr_reg = struct.unpack_from("<18I", desc, 72)
    assert list(pr_reg[:16]) == [regs[f"r{i}"] for i in range(13)] + [regs["sp"], regs["lr"], regs["pc"]]
    assert pr_reg[16] == regs["xpsr"]
    vfp = next(n for (name, _), n in by_name.items() if name == "LINUX")
    vdesc = vfp["n_descdata"]
    assert len(vdesc) == 260
    d1 = struct.unpack_from("<Q", vdesc, 8)[0]
    assert d1 == 3 << 32 | 2 and struct.unpack_from("<I", vdesc, 256)[0] == 0x03000000
    fprobe_note = next(n for (name, _), n in by_name.items() if name == "FPROBE")
    extra = json.loads(fprobe_note["n_descdata"])
    assert extra["registers"]["msp"] == regs["msp"] and extra["registers"]["psp"] == regs["psp"]


def test_core_file_without_fpu_has_no_vfp_note():
    data = core_file(sample())
    assert b"LINUX\0" not in data and b"CORE\0" in data
