"""スナップショット: 止めたターゲットの状態を 1 つのファイルに保存し、プローブ無しで開いて調べる。

中身はコアレジスタ・メモリの区画（RAM・Flash（任意）・システム制御空間・周辺レジスタ）・説明（停止の理由・RTOS の
タスク・故障の解析・記録の参照等。JSON にできる dict）・ELF の指紋（と、任意で ELF そのもの）。
読みの裏 `SnapshotAccess` は fprobe.dwarf の TargetAccess の約束を満たすので、呼び出し履歴・C の式・RTOS の
読み手がターゲットの代わりにスナップショットを読む。`write_core` は ELF のコアファイル（GDB の `core-file` で読める
NT_PRSTATUS と PT_LOAD）に書き出す。

ファイル（.fsnap）は ZIP:
    manifest.json        版（FORMAT）・作った時刻・レジスタ・区画の表・説明・ELF の指紋
    memory/NNNN.bin      区画の中身（INLINE_LIMIT 以下の区画は manifest の中に 16 進で）
    elf/<名前>           ELF（埋め込んだときだけ）

機器（pyOCD）には依存しない。取る・書き戻すのは fprobe.probe_snapshot。
"""

from __future__ import annotations

import bisect
import hashlib
import io
import json
import struct
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from elftools.elf.elffile import ELFFile

from .dwarf.access import AccessError

__all__ = ["CORE_REGISTERS", "FORMAT", "SUFFIX", "ElfPrint", "Region", "Snapshot", "SnapshotAccess", "SnapshotError",
           "core_file", "merge_regions", "write_core"]

FORMAT = 1
SUFFIX = ".fsnap"
INLINE_LIMIT = 64                  # これ以下のバイト数の区画は manifest に 16 進で入れる
REGION_KINDS = ("ram", "flash", "system", "peripheral", "memory")
CORE_REGISTERS = [f"r{i}" for i in range(13)] + ["sp", "lr", "pc", "xpsr"]


class SnapshotError(Exception):
    """スナップショットのファイルが読めない・中身が矛盾する。"""


@dataclass(frozen=True)
class Region:
    """メモリの区画。kind: ram / flash / system（システム制御空間）/ peripheral（SVD の周辺レジスタ）/ memory。"""
    address: int
    data: bytes
    kind: str = "memory"
    name: str = ""

    @property
    def end(self) -> int:
        return self.address + len(self.data)


def merge_regions(regions: Iterable[Region]) -> list[Region]:
    """番地順に並べ、隙間なく続く同じ種類の区画を 1 つにまとめる（名前は最初の区画のもの）。"""
    out: list[Region] = []
    for r in sorted(regions, key=lambda r: r.address):
        if out and out[-1].end == r.address and out[-1].kind == r.kind:
            last = out.pop()
            r = Region(last.address, last.data + r.data, last.kind, last.name)
        out.append(r)
    return out


@dataclass(frozen=True)
class ElfPrint:
    """ELF の指紋: 名前・大きさ・SHA-256・GNU の build-id（あれば）。同じビルドかを確かめる。"""
    name: str
    size: int
    sha256: str
    build_id: str | None = None

    @classmethod
    def of(cls, path: str | Path) -> ElfPrint:
        data = Path(path).read_bytes()
        return cls(Path(path).name, len(data), hashlib.sha256(data).hexdigest(), _build_id(data))

    def matches(self, path: str | Path) -> bool:
        path = Path(path)
        if not path.is_file() or path.stat().st_size != self.size:
            return False
        return hashlib.sha256(path.read_bytes()).hexdigest() == self.sha256

    def as_dict(self) -> dict:
        return {"name": self.name, "size": self.size, "sha256": self.sha256, "build_id": self.build_id}


def _build_id(data: bytes) -> str | None:
    try:
        elf = ELFFile(io.BytesIO(data))
        for section in elf.iter_sections():
            if section["sh_type"] != "SHT_NOTE":
                continue
            for note in section.iter_notes():
                if note["n_type"] == "NT_GNU_BUILD_ID":
                    return note["n_desc"]
    except Exception:                                     # ELF でない・壊れた注記: 指紋は SHA-256 だけ
        return None
    return None


@dataclass
class Snapshot:
    registers: dict[str, int]
    regions: list[Region]
    info: dict = field(default_factory=dict)
    elf: ElfPrint | None = None
    elf_data: bytes | None = None                         # 埋め込んだ ELF
    created: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def __post_init__(self) -> None:
        self.regions = sorted(self.regions, key=lambda r: r.address)
        for a, b in zip(self.regions, self.regions[1:]):
            if b.address < a.end:
                raise SnapshotError(f"区画が重なる: {a.name or hex(a.address)} と {b.name or hex(b.address)}")
        for r in self.regions:
            if r.kind not in REGION_KINDS:
                raise SnapshotError(f"区画の種類 {r.kind!r} は知らない")

    # --- 読む -------------------------------------------------------------------------------

    def access(self) -> SnapshotAccess:
        return SnapshotAccess(self)

    def summary(self) -> dict:
        """JSON にできる要約（区画の表・レジスタの数・停止の様子・ELF の指紋・説明）。"""
        return {"created": self.created, "pc": self.registers.get("pc"),
                "registers": len(self.registers), "bytes": sum(len(r.data) for r in self.regions),
                "regions": [{"address": r.address, "length": len(r.data), "kind": r.kind, "name": r.name}
                            for r in self.regions if r.kind != "peripheral"],
                "peripheral_registers": sum(r.kind == "peripheral" for r in self.regions),
                "elf": {**self.elf.as_dict(), "embedded": self.elf_data is not None} if self.elf else None,
                **{k: v for k, v in self.info.items() if k not in ("rtos", "fault", "skipped")},
                "rtos": _rtos_summary(self.info.get("rtos")), "fault": _fault_summary(self.info.get("fault"))}

    # --- ファイル -----------------------------------------------------------------------------

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        regions, files = [], {}
        for i, r in enumerate(self.regions):
            entry = {"address": r.address, "length": len(r.data), "kind": r.kind, "name": r.name}
            if len(r.data) <= INLINE_LIMIT:
                entry["hex"] = r.data.hex()
            else:
                entry["file"] = f"memory/{i:04d}.bin"
                files[entry["file"]] = r.data
            regions.append(entry)
        elf = None
        if self.elf is not None:
            elf = {**self.elf.as_dict(), "embedded": f"elf/{self.elf.name}" if self.elf_data is not None else None}
            if self.elf_data is not None:
                files[elf["embedded"]] = self.elf_data
        manifest = {"format": FORMAT, "created": self.created, "registers": self.registers, "regions": regions,
                    "info": self.info, "elf": elf}
        temporary = path.with_name(path.name + ".tmp")
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
            for name, data in files.items():
                z.writestr(name, data)
        temporary.replace(path)                            # 途中で失敗しても、前のファイルを壊さない
        return path

    @classmethod
    def load(cls, path: str | Path) -> Snapshot:
        try:
            with zipfile.ZipFile(path) as z:
                manifest = json.loads(z.read("manifest.json"))
                if not isinstance(manifest, dict):
                    raise SnapshotError(f"{path}: manifest が JSON のオブジェクトでない")
                if manifest.get("format") != FORMAT:
                    raise SnapshotError(f"スナップショットの版 {manifest.get('format')} は読めない（この fprobe は {FORMAT}）")
                regions = [cls._region(z, entry) for entry in manifest["regions"]]
                elf = elf_data = None
                if manifest.get("elf"):
                    e = manifest["elf"]
                    elf = ElfPrint(e["name"], e["size"], e["sha256"], e.get("build_id"))
                    if e.get("embedded"):
                        elf_data = z.read(e["embedded"])
                        if hashlib.sha256(elf_data).hexdigest() != elf.sha256:
                            raise SnapshotError("埋め込んだ ELF が指紋と合わない（ファイルが壊れている）")
                registers = {str(k): int(v) for k, v in manifest["registers"].items()}
        except (zipfile.BadZipFile, KeyError, ValueError, TypeError, AttributeError) as e:
            raise SnapshotError(f"{path}: スナップショットのファイルではない・壊れている（{type(e).__name__}: {e}）") from None
        return cls(registers, regions, manifest.get("info") or {}, elf, elf_data, manifest.get("created", ""))

    @staticmethod
    def _region(z: zipfile.ZipFile, entry: dict) -> Region:
        data = bytes.fromhex(entry["hex"]) if "hex" in entry else z.read(entry["file"])
        if len(data) != entry["length"]:
            raise SnapshotError(f"区画 0x{entry['address']:08X} の長さが表と合わない")
        return Region(int(entry["address"]), data, entry["kind"], entry.get("name", ""))


def _rtos_summary(rtos: dict | None) -> dict | None:
    if not rtos:
        return None
    if "error" in rtos:
        return {"error": rtos["error"]}
    tasks = rtos.get("tasks") or []
    return {"rtos": rtos.get("rtos"), "tasks": len(tasks), "current": rtos.get("current")}


def _fault_summary(fault: dict | None) -> dict | None:
    if not fault:
        return None
    if "error" in fault:
        return {"error": fault["error"]}
    return {"faulted": fault.get("faulted", False), "causes": [c["bit"] for c in fault.get("causes", [])],
            "exception": (fault.get("exception") or {}).get("name"),
            "at": ((fault.get("frame") or {}).get("pc") or {}).get("where")}


class SnapshotAccess:
    """スナップショットの読み手（fprobe.dwarf.access.TargetAccess）。区画の外・取っていないレジスタは AccessError。"""

    def __init__(self, snapshot: Snapshot):
        self.snapshot = snapshot
        self._regions = snapshot.regions                   # 番地順・重なり無し（Snapshot が確かめる）
        self._starts = [r.address for r in self._regions]
        self._registers = {k.lower(): v for k, v in snapshot.registers.items()}

    def read_memory(self, address: int, length: int) -> bytes:
        out = bytearray()
        at, end = address, address + length
        while at < end:
            i = bisect.bisect_right(self._starts, at) - 1
            if i < 0 or at >= self._regions[i].end:
                raise AccessError(f"0x{at:08X} はスナップショットに無い")
            r = self._regions[i]
            take = min(end, r.end) - at
            out += r.data[at - r.address:at - r.address + take]
            at += take
        return bytes(out)

    def read_register(self, name: str) -> int:
        name = name.lower()
        if name == "r13":
            name = "sp"
        if name in self._registers:
            return self._registers[name]
        if name.startswith("d") and name[1:].isdigit():
            n = int(name[1:])
            return self.read_register(f"s{2 * n}") | self.read_register(f"s{2 * n + 1}") << 32
        raise AccessError(f"レジスタ {name} はスナップショットに無い")


# --- ELF のコアファイル -------------------------------------------------------------------------

ET_CORE, EM_ARM, PT_LOAD, PT_NOTE = 4, 40, 1, 4
NT_PRSTATUS, NT_ARM_VFP, NT_FPROBE = 1, 0x400, 0x46500001
PF_X, PF_W, PF_R = 1, 2, 4
EF_ARM_EABI_VER5 = 0x05000000
SIGTRAP = 5
_EHDR = struct.Struct("<16sHHIIIIIHHHHHH")
_PHDR = struct.Struct("<IIIIIIII")
# struct elf_prstatus（32 ビットの ARM、148 バイト）: pr_info（3 int）・pr_cursig（short + 詰め物）・pr_sigpend・
# pr_sighold・pr_pid・pr_ppid・pr_pgrp・pr_sid・時間 4 つ（timeval = 2 long）・pr_reg（r0〜r15・cpsr・orig_r0）・pr_fpvalid
_PRSTATUS = struct.Struct("<3ih2x2I4i8I18Ii")


def _note(name: str, kind: int, desc: bytes) -> bytes:
    raw = name.encode() + b"\0"
    pad = lambda b: b + b"\0" * (-len(b) % 4)               # noqa: E731
    return struct.pack("<III", len(raw), len(desc), kind) + pad(raw) + pad(desc)


def core_file(snapshot: Snapshot, signal: int = SIGTRAP) -> bytes:
    """ELF のコアファイル（ET_CORE・EM_ARM）。NT_PRSTATUS に r0〜pc と xPSR（cpsr の位置）、浮動小数点のレジスタが
    あれば NT_ARM_VFP（d0〜d31・fpscr）、fprobe の注記（"FPROBE"）に残りのレジスタ（msp・psp・control 等）と説明、
    メモリの区画は PT_LOAD（Flash は RX、ほかは RW）。GDB は `arm-none-eabi-gdb firmware.elf core.elf` で読む。"""
    regs = {k.lower(): v for k, v in snapshot.registers.items()}
    core = [regs.get(n, 0) & 0xFFFFFFFF for n in CORE_REGISTERS]
    prstatus = _PRSTATUS.pack(signal, 0, 0, signal, 0, 0, 1, 0, 1, 1, *([0] * 8), *core, core[0], 0)
    notes = [_note("CORE", NT_PRSTATUS, prstatus)]
    if any(f"s{i}" in regs for i in range(32)) or "fpscr" in regs:
        doubles = [regs.get(f"s{2 * n}", 0) | regs.get(f"s{2 * n + 1}", 0) << 32 for n in range(16)] + [0] * 16
        notes.append(_note("LINUX", NT_ARM_VFP, struct.pack("<32QI", *doubles, regs.get("fpscr", 0))))
    rest = {k: v for k, v in regs.items() if k not in CORE_REGISTERS and not (k[0] == "s" and k[1:].isdigit())}
    extra = {"registers": rest, "info": snapshot.info, "elf": snapshot.elf.as_dict() if snapshot.elf else None}
    notes.append(_note("FPROBE", NT_FPROBE, json.dumps(extra, ensure_ascii=False).encode()))
    note_blob = b"".join(notes)

    segments = merge_regions(Region(r.address, r.data, "flash" if r.kind == "flash" else "ram") for r in snapshot.regions)
    phnum = 1 + len(segments)
    offset = _EHDR.size + _PHDR.size * phnum
    headers = [_PHDR.pack(PT_NOTE, offset, 0, 0, len(note_blob), 0, 0, 4)]
    body = [note_blob]
    offset += len(note_blob)
    for seg in segments:
        pad = -offset % 4
        body.append(b"\0" * pad)
        offset += pad
        flags = PF_R | PF_X if seg.kind == "flash" else PF_R | PF_W
        headers.append(_PHDR.pack(PT_LOAD, offset, seg.address, seg.address, len(seg.data), len(seg.data), flags, 4))
        body.append(seg.data)
        offset += len(seg.data)
    ident = b"\x7fELF" + bytes([1, 1, 1, 0]) + bytes(8)       # 32 ビット・リトルエンディアン・版 1・System V
    header = _EHDR.pack(ident, ET_CORE, EM_ARM, 1, 0, _EHDR.size, 0, EF_ARM_EABI_VER5, _EHDR.size, _PHDR.size,
                        phnum, 0, 0, 0)
    return header + b"".join(headers) + b"".join(body)


def write_core(snapshot: Snapshot, path: str | Path, signal: int = SIGTRAP) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(core_file(snapshot, signal))
    return path
