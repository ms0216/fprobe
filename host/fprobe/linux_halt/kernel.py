"""Linux カーネルの構造体（タスク一覧・スレッドグループ・pt_regs）を、停止中のメモリから読む。

オフセットは利用者が渡す vmlinux の DWARF から取り（`load_layout`）、アタッチのときに `init_task` の読みで照合する（`verify_attach`）。
合わなければ `KernelLayoutError` で断る。黙って別の並びで読まない。読みの口は `read_virt(va, n) -> bytes`（カーネルの仮想アドレス。
線形マップを通して物理を読む部分は呼び出し側）。

コスト: 1 回の JTAG の読みは大きさによらず十数 ms かかるので、task_struct は必要な範囲を 1 回で読み、プロセスは pid の IDR（xarray）
で探して、見つからなければタスク一覧を歩く。
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Callable, Mapping

XA_CHUNK_SHIFT = 6               # CONFIG_BASE_SMALL=0: xarray のノードは 64 スロット（マクロで DWARF に無い）


def xa_is_node(entry: int) -> bool:
    """カーネルの xa_is_node（include/linux/xarray.h）そのもの: 内部の値（下位 2 ビットが 2）で、4096 より大きい。4096 は経験則でなく
    カーネルの定義（4096 以下の内部の値は XA_RETRY_ENTRY・XA_ZERO_ENTRY・兄弟の印などで、ノードを指さない）。"""
    return entry & 3 == 2 and entry > 4096

ReadVirt = Callable[[int, int], bytes]

REQUIRED = {
    "tasks": ("task_struct", "tasks"),
    "pid": ("task_struct", "pid"),
    "tgid": ("task_struct", "tgid"),
    "comm": ("task_struct", "comm"),
    "mm": ("task_struct", "mm"),
    "stack": ("task_struct", "stack"),
    "thread_info": ("task_struct", "thread_info"),
    "pgd": ("mm_struct", "pgd"),
    "asid": ("mm_struct", "context.id.counter"),
    "saved_auxv": ("mm_struct", "saved_auxv"),
    "signal": ("task_struct", "signal"),
    "live": ("signal_struct", "live"),
    "pid_idr_head": ("pid_namespace", "idr.idr_rt.xa_head"),
    "xa_node_shift": ("xa_node", "shift"),
    "xa_node_slots": ("xa_node", "slots"),
    "pid_tasks": ("pid", "tasks"),
    "pid_links": ("task_struct", "pid_links"),
    "signal_flags": ("signal_struct", "flags"),
    "group_exit_code": ("signal_struct", "group_exit_code"),
}
# スレッドの連結はカーネルの版で違う: task_struct.thread_group（リーダー自身のメンバーが先頭。〜6.12）か、
# signal_struct.thread_head + task_struct.thread_node（新しい版）。
THREAD_LISTS = (
    {"thread_group": ("task_struct", "thread_group")},
    {"thread_node": ("task_struct", "thread_node"), "thread_head": ("signal_struct", "thread_head")},
)
SYMBOLS = ("init_task", "init_pid_ns", "do_exit")


class KernelLayoutError(RuntimeError):
    """カーネルのメモリが、渡されたオフセットの言う姿をしていない（別のカーネルの vmlinux、または破損）。"""


class ProcessNotFound(LookupError):
    pass


@dataclass
class KernelLayout:
    o: dict[str, int]            # オフセット・シンボルのアドレス・page_offset 等（offsets.json と同じ名前）


@dataclass
class Task:
    addr: int
    pid: int                     # スレッド ID（カーネルの "pid"）
    tgid: int                    # プロセス ID
    mm: int
    comm: str = ""
    tasks_next: int = 0
    group_next: int = 0
    signal: int = 0


def build_layout(offset_of: Callable[[str, str], int | None], *, sizeof_pt_regs: int, symbols: Mapping[str, int],
                 page_offset: int, phys_offset: int, thread_size: int, signal_group_exit: int,
                 linear_end: int) -> KernelLayout:
    """offset_of(構造体名, "a.b.c") -> バイト位置（このカーネルに無ければ None）。足りないものは全部まとめて報告する。
    page_offset・phys_offset・thread_size・signal_group_exit は DWARF に無い設定値で、呼び出し側が明示する（既定なし）。"""
    o: dict[str, int] = {}
    missing = []
    for key, (s, p) in REQUIRED.items():
        v = offset_of(s, p)
        if v is None:
            missing.append("%s.%s is not in this kernel's debug information" % (s, p))
        else:
            o[key] = v
    for layout in THREAD_LISTS:
        got = {k: offset_of(s, p) for k, (s, p) in layout.items()}
        if all(v is not None for v in got.values()):
            o.update(got)
            break
    else:
        missing.append("no known thread list layout (tried %s)" % [list(x) for x in THREAD_LISTS])
    for name in SYMBOLS:
        if name not in symbols:
            missing.append("symbol %s is not in the symbol table given" % name)
        else:
            o[name] = symbols[name]
    if missing:
        raise KernelLayoutError("unsupported kernel layout:\n  " + "\n  ".join(missing))
    o.update(sizeof_pt_regs=sizeof_pt_regs, page_offset=page_offset, phys_offset=phys_offset,
             thread_size=thread_size, signal_group_exit=signal_group_exit, linear_end=linear_end)
    return KernelLayout(o)


def load_layout(vmlinux_path: str, *, symbols: Mapping[str, int], page_offset: int, phys_offset: int,
                thread_size: int, signal_group_exit: int, linear_end: int) -> KernelLayout:
    """vmlinux の DWARF（fprobe.dwarf）から構造体のオフセットを取る。symbols は実際に動いているカーネルのシンボル
    （/proc/kallsyms か System.map）。コンパイラが違うと vmlinux のアドレスは動いているカーネルと違う。構造体の並びは DWARF、
    アドレスは動いている側から取る。"""
    from fprobe.dwarf.info import DebugInfo
    from fprobe.dwarf.typesys import StructType, strip
    info = DebugInfo(vmlinux_path)

    def offset_of(struct_name: str, path: str) -> int | None:
        try:
            t = strip(info.type_named("struct " + struct_name))
            total = 0
            for name in path.split("."):
                m, extra = t.member(name)
                total += m.offset + extra
                t = strip(m.type)
            return total
        except KeyError:
            return None

    try:
        size = strip(info.type_named("struct pt_regs")).size
    except KeyError as e:
        raise KernelLayoutError("struct pt_regs is not in the DWARF of %s" % vmlinux_path) from e
    return build_layout(offset_of, sizeof_pt_regs=size, symbols=symbols, page_offset=page_offset,
                        phys_offset=phys_offset, thread_size=thread_size, signal_group_exit=signal_group_exit,
                        linear_end=linear_end)


def load_layout_json(path: str, *, page_offset: int, phys_offset: int, linear_end: int) -> KernelLayout:
    """プロトタイプ（tools/gen_offsets.py）が書く offsets.json を読む。必要なキーが足りなければ全部まとめて報告する。
    page_offset・phys_offset・linear_end は、ファイルには無い設定値なので引数で渡す（既定なし。プロトタイプは page_offset・phys_offset を
    黙って補っていた）。linear_end = 線形マップの上端の仮想アドレス（これ以上は vmalloc・highmem。VMAP_STACK のスタックもここ）。"""
    import json
    with open(path, encoding="utf-8") as f:
        o = json.load(f)
    need = list(REQUIRED) + list(SYMBOLS) + ["sizeof_pt_regs", "thread_size", "signal_group_exit"]
    missing = [k for k in need if k not in o]
    if "thread_group" not in o and not ("thread_node" in o and "thread_head" in o):
        missing.append("thread_group (or thread_node and thread_head)")
    if missing:
        raise KernelLayoutError("%s lacks: %s" % (path, ", ".join(missing)))
    o = dict(o)
    o.update(page_offset=page_offset, phys_offset=phys_offset, linear_end=linear_end)
    return KernelLayout(o)


class Kernel:
    def __init__(self, read_virt: ReadVirt, layout, *, max_tasks: int, max_threads: int):
        self.read, self.o = read_virt, layout.o
        self.max_tasks, self.max_threads = max_tasks, max_threads
        o = self.o
        self._link = o["thread_group"] if "thread_group" in o else o["thread_node"]
        fields = [(o["tasks"], 4), (o["pid"], 4), (o["tgid"], 4), (o["mm"], 4), (self._link, 4), (o["signal"], 4),
                  (o["comm"], 16)]
        self._lo = min(a for a, _ in fields)
        self._hi = max(a + n for a, n in fields)
        self._stack: dict[int, tuple[int, int]] = {}
        self.found_by: str | None = None          # "idr" か "walk"（プロセスをどう見つけたか。診断用の事実）

    def u32(self, addr: int) -> int:
        return struct.unpack("<I", self.read(addr, 4))[0]

    def v2p(self, va: int) -> int:
        """カーネルの線形マップの仮想アドレス → 物理アドレス。線形マップの外（vmalloc・VMAP_STACK のスタック・highmem）は、
        間違った物理アドレスを黙って返さず KernelLayoutError。"""
        o = self.o
        if va < o["page_offset"] or va >= o["linear_end"]:
            raise KernelLayoutError("0x%08x is outside the kernel linear map 0x%08x..0x%08x: no physical address from the offset "
                                    "alone" % (va, o["page_offset"], o["linear_end"]))
        return va - o["page_offset"] + o["phys_offset"]

    def owner_of_stack_address(self, tasks, address: int) -> Task | None:
        """`tasks` のうち、カーネルスタックが `address` を含むタスク。無ければ None。"""
        size = self.o["thread_size"]
        for t in tasks:
            base = self.stack_base(t)
            if base <= address < base + size:
                return t
        return None

    def read_task(self, addr: int) -> Task:
        o, lo = self.o, self._lo
        b = self.read(addr + lo, self._hi - lo)

        def u(off: int) -> int:
            return struct.unpack_from("<I", b, off - lo)[0]
        return Task(addr, u(o["pid"]), u(o["tgid"]), u(o["mm"]),
                    b[o["comm"] - lo:o["comm"] - lo + 16].split(b"\0")[0].decode(errors="replace"),
                    u(o["tasks"]), u(self._link), u(o["signal"]))

    def verify(self) -> Task:
        """init_task（pid 0、tgid 0、comm が swapper）の読みで、渡されたオフセットが動いているカーネルのものか確かめる。"""
        t = self.read_task(self.o["init_task"])
        if (t.pid, t.tgid) != (0, 0) or not t.comm.startswith("swapper"):
            raise KernelLayoutError(
                "the layout does not match the running kernel: init_task at 0x%08x reads as pid=%d tgid=%d comm=%r "
                "(expected pid 0, tgid 0, comm 'swapper/0'). Wrong vmlinux for this kernel, or wrong init_task address."
                % (self.o["init_task"], t.pid, t.tgid, t.comm))
        return t

    def leaders(self):
        """スレッドグループのリーダー（`tasks` の連結）を init_task から順に。1 タスク 1 回の読み。"""
        head = self.o["init_task"]
        addr, n = head, 0
        while n < self.max_tasks:
            t = self.read_task(addr)
            yield t
            addr, n = t.tasks_next - self.o["tasks"], n + 1
            if addr == head:
                return
        raise KernelLayoutError("the task list does not return to init_task after %d entries: corrupt, or the layout "
                                "does not match this kernel" % self.max_tasks)

    def find_by_pid(self, pid: int) -> Task | None:
        """init_pid_ns の IDR（xarray）から pid のタスクを探す（読みは 6 回ほど）。見つからなければ None（呼び出し側が検証する）。"""
        o = self.o
        if "init_pid_ns" not in o or "pid_idr_head" not in o:
            return None
        entry = self.u32(o["init_pid_ns"] + o["pid_idr_head"])
        if xa_is_node(entry):
            node = entry - 2
            shift = self.read(node + o["xa_node_shift"], 1)[0]
            if pid >> (shift + XA_CHUNK_SHIFT):
                return None
            while True:
                shift = self.read(node + o["xa_node_shift"], 1)[0]
                slot = (pid >> shift) & ((1 << XA_CHUNK_SHIFT) - 1)
                entry = self.u32(node + o["xa_node_slots"] + 4 * slot)
                if xa_is_node(entry):
                    node = entry - 2
                    continue
                break
        elif pid != 0:
            return None
        if not entry:
            return None
        first = self.u32(entry + o["pid_tasks"])
        if not first:
            return None
        return self.read_task(first - o["pid_links"])

    def find_leader(self, tgid: int) -> Task | None:
        t = self.find_by_pid(tgid)
        if t is not None and t.pid == tgid and t.tgid == tgid:
            self.found_by = "idr"
            return t
        self.found_by = "walk"
        for t in self.leaders():
            if t.tgid == tgid and t.pid == tgid:
                return t
        return None

    def threads(self, leader_addr: int, leader: Task | None = None) -> list[Task]:
        """グループの全スレッド（リーダーが先頭）。先頭に戻らない連結は、ゴミを返さず KernelLayoutError。"""
        first = leader if leader is not None else self.read_task(leader_addr)
        if "thread_group" in self.o:
            node, head, out, q = self.o["thread_group"], leader_addr + self.o["thread_group"], [first], first.group_next
        else:
            node, head, out = self.o["thread_node"], first.signal + self.o["thread_head"], []
            q = self.u32(head)
        while q != head:
            if len(out) > self.max_threads:
                raise KernelLayoutError("the thread list of task 0x%08x does not end after %d entries: corrupt, or the "
                                        "layout does not match this kernel" % (leader_addr, self.max_threads))
            t = first if q - node == leader_addr else self.read_task(q - node)
            out.append(t)
            q = t.group_next
        out.sort(key=lambda t: t.addr != leader_addr)
        return out

    def stack_base(self, task: Task) -> int:
        cached = self._stack.get(task.addr)
        if cached is None or cached[0] != task.pid:
            cached = (task.pid, self.u32(task.addr + self.o["stack"]))
            self._stack[task.addr] = cached
        return cached[1]

    def pt_regs_addr(self, task: Task) -> int:
        """task_pt_regs(): カーネルスタックの上端から、先頭の 8 バイトと sizeof(pt_regs) を引いた所。"""
        return self.stack_base(task) + self.o["thread_size"] - 8 - self.o["sizeof_pt_regs"]



def verify_attach(layout, read_virt: ReadVirt) -> None:
    Kernel(read_virt, layout, max_tasks=1, max_threads=1).verify()


def list_threads(layout, read_virt: ReadVirt, pid: int, *, max_tasks: int, max_threads: int) -> list[Task]:
    """pid のプロセスの全スレッド（リーダーが先頭）。max_tasks・max_threads は必須（超えたら KernelLayoutError。黙って打ち切らない。既定値なし）。"""
    k = Kernel(read_virt, layout, max_tasks=max_tasks, max_threads=max_threads)
    k.verify()
    leader = k.find_leader(pid)
    if leader is None:
        raise ProcessNotFound("pid %d not found in the kernel task list (the process has exited, or the pid is wrong)" % pid)
    if leader.mm == 0:
        raise ProcessNotFound("pid %d is a kernel thread (no address space)" % pid)
    return k.threads(leader.addr, leader)
