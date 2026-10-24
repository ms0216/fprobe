"""命令の流れの再構成: 共通の要素（elements）とプログラムの像（ELF か実機のメモリ）から、実行した命令の列を作る。

考え方（ETMv4・ETMv3.5 で共通。docs/reports/2026-10-19-etm-investigation.md §3.4）:
1. 同期点のアドレス（Address）から命令を順に読む（Thumb-2。capstone）。
2. 分岐（waypoint: b・bl・cbz・bx・blx・tbb・pop {pc}・ldr pc 等、PC を変えうる命令）に着くたびにアトムを 1 つ使う。
   E（成立）なら行き先へ: 直接の分岐は命令から計算、間接の分岐は次に来る Address が行き先（それまで後のアトムは
   待たせる）。N（不成立）なら次の命令へ。
3. 例外（ExceptionEntry）は戻り先の手前まで実行してから入る。入口（ハンドラの先頭）は次の Address。
4. Trace On・Overflow・解けないバイトは途切れ: 次の Address まで位置が分からない。

限界: M-profile の ETM は命令だけでデータの値が無い。ここで出すのは「どの命令を実行したか」で、レジスタ・メモリの
過去の値は含まない（推定するなら別に、スナップショットから前へ模擬する）。

    flow = Reconstructor(ElfImage("app.elf"), Symbols("app.elf")).run(Etmv4Decoder(Etmv4Config(commit_opt1=True, vmid_bytes=1, cid_bytes=4)).decode(data))
    flow.history(32)                       # 最後の 32 命令（事後の命令履歴）
"""

import bisect
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field

import capstone
from capstone import arm

from ..disasm import flow_of
from ..memview import MemoryReader, MemoryReadError
from ..symbols import Symbols
from . import elements as el

__all__ = ["WALK_LIMIT", "Block", "Flow", "Insn", "Marker", "Reconstructor"]

WALK_LIMIT = 1 << 16             # 分岐の無いまま進む命令の数の上限（越えたら止めて、上限に達したと書く。原因は推測しない）
_TABLE_BRANCHES = ("tbb", "tbh")


@dataclass(frozen=True)
class Insn:
    address: int
    size: int
    mnemonic: str
    operands: str
    waypoint: bool                   # 分岐（アトムを 1 つ使う）
    target: int | None = None        # 直接の分岐の行き先（間接なら None）


@dataclass
class Block:
    """続けて実行した命令の列（最後が分岐か、例外の手前か、読めなくなった所）。"""
    first_index: int                 # 流れ全体での、最初の命令の通し番号
    addresses: tuple[int, ...]
    exit: str = ""                   # "taken" / "not_taken" / "exception" / ""（途切れ）
    target: int | None = None        # 成立した分岐の行き先（間接は Address が来てから入る）


@dataclass(frozen=True)
class Marker:
    """命令の流れの中の出来事。index = その前に実行した命令の数（= 次の命令の通し番号）。
    kind: exception / exception_return / trace_on / overflow / timestamp / event / lost / decode_error。"""
    kind: str
    index: int
    info: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"kind": self.kind, "index": self.index, **self.info}


class Flow:
    """再構成した命令の流れ。命令は Block にまとめて持ち、一覧にするときに名前を付ける。"""

    def __init__(self, reconstructor: "Reconstructor", blocks: list[Block], markers: list[Marker], errors: list[str],
                 unresolved_atoms: int, next_address: int | None):
        self._r = reconstructor
        self.blocks, self.markers, self.errors = blocks, markers, errors
        self.unresolved_atoms = unresolved_atoms
        self.next_address = next_address              # 最後の命令の次に実行する番地（分からなければ None）
        self._starts = [b.first_index for b in blocks]

    @property
    def count(self) -> int:
        return self.blocks[-1].first_index + len(self.blocks[-1].addresses) if self.blocks else 0

    def addresses(self) -> list[int]:
        return [a for b in self.blocks for a in b.addresses]

    def _rows(self, start: int):
        """通し番号 start からの (通し番号, 番地, ブロック, ブロックの最後か)。"""
        k = max(0, bisect.bisect_right(self._starts, start) - 1)
        for block in self.blocks[k:]:
            last = len(block.addresses) - 1
            for n, address in enumerate(block.addresses):
                index = block.first_index + n
                if index >= start:
                    yield index, address, block, n == last

    def _row(self, index: int, address: int, block: Block, last: bool) -> dict:
        insn = self._r.insn(address)
        row = {"index": index, "address": address, "size": insn.size, "mnemonic": insn.mnemonic,
               "operands": insn.operands}
        if self._r.symbols is not None:
            where = self._r.symbols.describe(address)
            for key in ("function", "offset", "file", "line"):
                if getattr(where, key) is not None:
                    row[key] = getattr(where, key)
        if last and block.exit:
            row["branch"] = block.exit
            if block.exit == "taken" and block.target is not None:
                row["target"] = block.target
        return row

    def instructions(self, offset: int = 0, limit: int | None = None, function: str | None = None) -> list[dict]:
        """命令の一覧（offset 番目から limit 件）。function で関数を絞る（offset・limit は絞った後の並びで）。"""
        out: list[dict] = []
        if function is None:
            for index, address, block, last in self._rows(offset):
                if limit is not None and len(out) >= limit:
                    break
                out.append(self._row(index, address, block, last))
            return out
        matched = 0
        for index, address, block, last in self._rows(0):
            row = self._row(index, address, block, last)
            if row.get("function") != function:
                continue
            if matched >= offset and (limit is None or len(out) < limit):
                out.append(row)
            matched += 1
        return out

    def history(self, count: int) -> list[dict]:
        """最後の count 命令（古い順）。"""
        return self.instructions(offset=max(0, self.count - count))


class Reconstructor:
    def __init__(self, memory: MemoryReader, symbols: Symbols | None = None, walk_limit: int = WALK_LIMIT):
        """memory: 命令を読むもの（ElfImage・TargetMemory・BytesMemory）。symbols: 関数名・ファイル:行（任意）。"""
        self.memory, self.symbols, self.walk_limit = memory, symbols, walk_limit
        self._cs = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB | capstone.CS_MODE_MCLASS)
        self._cs.detail = True
        self._cache: dict[int, Insn] = {}

    def insn(self, address: int) -> Insn:
        """address の命令（Thumb-2）。読めなければ MemoryReadError、解けなければ ValueError。"""
        cached = self._cache.get(address)
        if cached is not None:
            return cached
        try:
            data = self.memory.read_memory(address, 4)
        except MemoryReadError:
            data = self.memory.read_memory(address, 2)                   # 区画の終わりの 16 ビット命令
        decoded = next(self._cs.disasm(data, address, 1), None)
        if decoded is None:
            raise ValueError(f"0x{address:08X} は命令として解けない（{data[:4].hex()}）")
        flow, _ = flow_of(decoded)
        base = decoded.mnemonic.split(".")[0]
        waypoint = bool(flow) or base in _TABLE_BRANCHES
        target = None
        if flow in ("call", "jump") and base not in _TABLE_BRANCHES:
            imms = [o.imm for o in decoded.operands if o.type == arm.ARM_OP_IMM]
            if imms:
                target = imms[-1] & 0xFFFF_FFFF
        insn = Insn(address, decoded.size, decoded.mnemonic, decoded.op_str, waypoint, target)
        self._cache[address] = insn
        return insn

    def run(self, elements: Iterable[el.Element]) -> Flow:
        return _Run(self).feed(elements)


class _Run:
    """1 回の再構成の状態。"""

    def __init__(self, r: Reconstructor):
        self.r = r
        self.pc: int | None = None
        self.waiting = False                      # 間接の分岐の行き先（Address）を待っている
        self.queue: deque[bool] = deque()         # 待つ間に来たアトム
        self.blocks: list[Block] = []
        self.markers: list[Marker] = []
        self.errors: list[str] = []
        self.count = 0
        self.unresolved = 0

    def feed(self, elements: Iterable[el.Element]) -> Flow:
        for e in elements:
            if isinstance(e, el.Address):
                self._address(e.address)
            elif isinstance(e, el.Atoms):
                for a in e.pattern:
                    self._atom(a == "E")
            elif isinstance(e, el.ExceptionEntry):
                self._exception(e)
            elif isinstance(e, el.ExceptionReturn):
                self._mark("exception_return")
            elif isinstance(e, (el.TraceOn, el.Overflow)):
                self._mark(e.kind)
                self._lose()
            elif isinstance(e, el.Timestamp):
                self._mark("timestamp", value=e.value, **({"cycles": e.cycles} if e.cycles is not None else {}))
            elif isinstance(e, el.Event):
                self._mark("event", events=e.events)
            elif isinstance(e, (el.DecodeError, el.Truncated)):
                message = e.message if isinstance(e, el.DecodeError) else "バイト列の終わりで切れたパケット"
                self._mark(e.kind, offset=e.offset, message=message)
                self.errors.append(f"トレースの {e.offset} バイト目: {message}")
                self._lose()
        if self.queue:
            self.unresolved += len(self.queue)
        next_address = None if self.waiting else self.pc
        return Flow(self.r, self.blocks, self.markers, self.errors, self.unresolved, next_address)

    # --- 要素 ---------------------------------------------------------------------------

    def _mark(self, kind: str, **info) -> None:
        self.markers.append(Marker(kind, self.count, info))

    def _lose(self) -> None:
        """位置が分からなくなる（次の Address まで）。待っていたアトムは解けない。"""
        self.unresolved += len(self.queue)
        self.queue.clear()
        self.pc, self.waiting = None, False

    def _address(self, address: int) -> None:
        if self.waiting:
            self.waiting = False
            if self.blocks and self.blocks[-1].exit == "taken" and self.blocks[-1].target is None:
                self.blocks[-1].target = address
            self.pc = address
            pending = list(self.queue)
            self.queue.clear()
            for taken in pending:
                self._atom(taken)
        else:
            self.pc = address

    def _atom(self, taken: bool) -> None:
        if self.waiting:
            self.queue.append(taken)
            return
        if self.pc is None:
            self.unresolved += 1
            return
        addresses, last = self._walk(self.pc, stop=None)
        if last is None:                                    # 読めない・分岐が無いまま上限: 途切れ
            self._block(addresses, "")
            self.unresolved += 1
            return
        if taken:
            if last.target is not None:
                self._block(addresses, "taken", last.target)
                self.pc = last.target
            else:
                self._block(addresses, "taken")
                self.pc, self.waiting = None, True
        else:
            self._block(addresses, "not_taken")
            self.pc = last.address + last.size

    def _exception(self, e: el.ExceptionEntry) -> None:
        ret = e.return_address
        if self.waiting and ret is not None:                # 間接の分岐の行き先 = 戻り先（そこはまだ実行していない）
            self._address(ret)
        elif self.pc is not None and ret is not None:
            addresses, last = self._walk(self.pc, stop=ret)
            if last is not None and last.address != ret:    # 戻り先の前に分岐: アトムと合わない
                self.errors.append(f"例外（{e.name}）の戻り先 0x{ret:08X} の手前に分岐 0x{last.address:08X} がある")
                self._mark("decode_error", offset=e.offset, message=self.errors[-1])
            if addresses:
                self._block(addresses, "exception")
        self._mark("exception", number=e.number, name=e.name, return_address=ret,
                   **({"fault_pending": True} if e.fault_pending else {}))
        self._lose()                                        # 入口は次の Address

    # --- 命令をたどる ---------------------------------------------------------------------

    def _walk(self, start: int, stop: int | None) -> tuple[list[int], Insn | None]:
        """start から命令をたどる。stop が None なら分岐まで（分岐を含む）、stop があれば stop の手前まで
        （途中の分岐で止まる。分岐は含まない）。(たどった番地, 最後に見た命令)。読めなければ印を付けて None。"""
        addresses: list[int] = []
        address = start
        for _ in range(self.r.walk_limit):
            if stop is not None and address == stop:
                return addresses, Insn(stop, 0, "", "", False)
            try:
                insn = self.r.insn(address)
            except (MemoryReadError, ValueError) as error:
                self.errors.append(f"0x{address:08X} の命令が読めない（{error}）")
                self._mark("lost", address=address, reason=str(error))
                self.pc, self.waiting = None, False
                return addresses, None
            if insn.waypoint:
                if stop is None:
                    addresses.append(address)
                return addresses, insn
            addresses.append(address)
            address += insn.size
        self.errors.append(f"0x{start:08X} から {self.r.walk_limit} 命令（たどる上限）たどっても分岐が無い。そこで止めた")
        self._mark("lost", address=address, reason=f"たどる上限 {self.r.walk_limit} 命令まで分岐が無い")
        self.pc, self.waiting = None, False
        return addresses, None

    def _block(self, addresses: list[int], exit: str, target: int | None = None) -> None:
        if not addresses:
            return
        self.blocks.append(Block(self.count, tuple(addresses), exit, target))
        self.count += len(addresses)
