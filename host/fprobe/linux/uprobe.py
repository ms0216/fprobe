"""uprobes（tracefs の uprobe_events）: ELF の場所、uprobe_events の行、trace_pipe の行の解析。

32 ビットの ARM の uprobes は ARM 命令だけに対応する（Thumb の関数に付けると命令を読み違えてアプリを壊しうる）ので、付ける前に断る。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from elftools.elf.elffile import ELFFile

NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
GROUP = "fprobe"                                     # uprobe_events のグループ（fprobe が付けたものだけを外す）
_HEAD = re.compile(r"^\s*(?P<comm>.+?)-(?P<pid>\d+)\s+(?:\(\s*[\d-]+\)\s+)?\[(?P<cpu>\d+)\]\s+(?:\S+\s+)?"
                   r"(?P<sec>\d+)\.(?P<frac>\d+):\s+(?P<rest>.*)$")
_NAMED = re.compile(r"^(?P<event>\w+):\s+(?:\((?P<ip>0x[0-9a-fA-F]+)[^)]*\))?(?P<args>.*)$")      # event: (ip) k=v / k=v
_SYSCALL_ENTER = re.compile(r"^(?P<event>sys_\w+)\((?P<args>.*)\)$")                            # sys_openat(dfd: …)
_SYSCALL_EXIT = re.compile(r"^(?P<event>sys_\w+) -> (?P<ret>0x[0-9a-fA-F]+|-?\d+)$")              # sys_openat -> 0x3
_LOST = re.compile(r"^CPU:(?P<cpu>\d+) \[LOST (?P<lost>\d+) EVENTS\]$")
_ARG = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|\S+)')


class UprobeRefused(ValueError):
    """この場所には uprobe を付けない（理由は文に書く）"""


@dataclass(frozen=True)
class Site:
    path: str                                        # ターゲットの上のパス
    offset: int                                      # ファイルの中のオフセット（uprobe_events・/proc/<pid>/maps で使う）
    vaddr: int                                       # ELF の上の番地（Thumb の bit0 は外したもの）
    thumb: bool
    machine: str                                     # ELF の e_machine（EM_ARM / EM_AARCH64 / ...）


def site_of(elf: Path, remote_path: str, symbol: str | None = None, address: int | None = None,
            isa: str | None = None) -> Site:
    """symbol か address の一方で場所を決める。32 ビットの ARM の address は isa（"arm" / "thumb"）を明示する。"""
    if (symbol is None) == (address is None):
        raise ValueError("symbol と address のどちらか 1 つを渡す")
    with open(elf, "rb") as f:
        e = ELFFile(f)
        machine = e["e_machine"]
        if symbol is not None:
            tab = e.get_section_by_name(".symtab") or e.get_section_by_name(".dynsym")
            found = tab.get_symbol_by_name(symbol) if tab else None
            if not found:
                raise ValueError(f"シンボル {symbol} が {elf.name} に無い")
            value = found[0]["st_value"]
            thumb = machine == "EM_ARM" and bool(value & 1)
            vaddr = value & ~1 if machine == "EM_ARM" else value
        else:
            if machine == "EM_ARM" and isa not in ("arm", "thumb"):
                raise ValueError("32 ビットの ARM の番地には isa（arm / thumb）を明示する")
            thumb, vaddr = isa == "thumb", address
        for seg in e.iter_segments():
            if seg["p_type"] == "PT_LOAD" and seg["p_vaddr"] <= vaddr < seg["p_vaddr"] + seg["p_filesz"]:
                return Site(remote_path, vaddr - seg["p_vaddr"] + seg["p_offset"], vaddr, thumb, machine)
    raise ValueError(f"番地 0x{vaddr:x} は {elf.name} の読み込む区画に無い")


def uprobe_line(name: str, site: Site, fetchargs: list[str], ret: bool = False) -> str:
    """uprobe_events に書く 1 行。fetchargs は使う側が明示した文字列をそのまま使う（呼び出し規約を決めつけない）。"""
    if not NAME.match(name):
        raise ValueError(f"プローブの名前 {name!r} は英数字と _（先頭は数字以外、64 文字まで）")
    if site.machine == "EM_ARM" and site.thumb:
        raise UprobeRefused("32 ビットの ARM の uprobes は ARM 命令だけに対応する（この関数は Thumb）。"
                            "ptrace を使うか、アプリを -marm で作る")
    return " ".join([f"{'r' if ret else 'p'}:{GROUP}/{name} {site.path}:0x{site.offset:x}", *fetchargs])


def parse_lost(line: str) -> dict | None:
    """カーネルの「CPU:N [LOST n EVENTS]」（tracefs のバッファがあふれて捨てた数）"""
    m = _LOST.match(line.strip())
    return {"cpu": int(m["cpu"]), "lost": int(m["lost"])} if m else None


def parse_trace_line(line: str) -> dict | None:
    """trace_pipe の 1 行。形は 3 つ: 「event: (番地) k=v」（uprobe・kprobe）・「event: k=v」（tracepoint）・syscalls の
    「sys_x(引数)」「sys_x -> 戻り値」。args は k=v を拾ったもの、text は時刻の後ろの文字のまま（事実はこちら）。
    時刻は trace_clock の値（mono_raw なら CLOCK_MONOTONIC_RAW）で、文字の桁の分解能のまま ns にする。"""
    h = _HEAD.match(line)
    if not h:
        return None
    rest = h["rest"]
    ip = None
    if m := _NAMED.match(rest):
        event, ip, args = m["event"], m["ip"], dict(_ARG.findall(m["args"]))
    elif m := _SYSCALL_ENTER.match(rest):
        event, args = m["event"], {}
    elif m := _SYSCALL_EXIT.match(rest):
        event, args = m["event"], {"ret": m["ret"]}
    else:
        return None
    frac = h["frac"]
    return {"comm": h["comm"], "pid": int(h["pid"]), "cpu": int(h["cpu"]),
            "t_ns": int(h["sec"]) * 1_000_000_000 + int(frac) * 10 ** (9 - len(frac)),
            "event": event, "ip": ip, "args": args, "text": rest, "raw": line}
