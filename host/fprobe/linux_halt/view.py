"""Linux のプロセスの全コア停止デバッグ（HaltDebugger）を、マイコンのデバッガと同じ API の形で見せる層。

画面（host/ui/src/lib/debugger.ts）はマイコンのデバッガの API（target.status・debug.source.*・debug.breakpoint.*・
debug.backtrace・debug.frame_variables・target.registers.read 等）だけを読む。attach している間、各サービスはその
メソッドの入口でこの層に渡す（名前・引数・応答の形は同じ。仕様 docs/superpowers/specs/2026-11-03-linux-halt-ui-design.md）。
応答は事実だけ（判断・推定を入れない）。合わない欄は仕様の範囲で欠くか、理由の誤りにする。
"""
from __future__ import annotations

import dataclasses
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path, PurePosixPath

from ..disasm import SYNC_LIMIT, Disassembler, Instruction
from ..dwarf import Inspector
from ..dwarf.inspector import check_expression
from ..memview import MemoryReadError
from ..symbols import Location, Symbols, is_line_location, is_network_path
from .debugger import REG_NAMES, HaltDebugger, _ThreadAccess
from .engine import EngineError
from .mmu import Unmapped

__all__ = ["LinuxDebugView", "SOURCE_LIMIT", "prefetch_sources"]

SOURCE_LIMIT = 2 << 20               # 読むソースの大きさの上限（probe_inspect.SOURCE_LIMIT と同じ）

# 停止の種類 → target.status の halt_reason（同じ事実のときだけ。ほかは欠いて linux.stop_kind に元の種類）
HALT_REASON = {"swbreak": "breakpoint", "hwbreak": "breakpoint", "watch": "watchpoint", "int": "request",
               "step": "request", "attach": "request"}


class LinuxDebugView:
    """1 つの HaltDebugger をマイコンの API の形で見せる。"""

    def __init__(self, debugger: HaltDebugger, *, breakpoint_kind: str | None, sources: dict[str, dict] | None = None,
                 step_max_instructions: int | None = None, step_timeout_s: float | None = None):
        """sources: attach のときに対象から取り寄せたソース（prefetch_sources の結果）。breakpoint_kind・
        step_max_instructions・step_timeout_s: 画面の道（種類・上限の引数を持たない API）で使う値。attach で使う側が
        明示する（渡さなければその道は断る。既定を持たない）。"""
        if breakpoint_kind not in (None, "sw", "hw"):
            raise EngineError("breakpoint_kind must be 'sw' or 'hw' (got %r)" % (breakpoint_kind,))
        self.d = debugger
        self.breakpoint_kind = breakpoint_kind
        self.step_max_instructions, self.step_timeout_s = step_max_instructions, step_timeout_s
        self._disabled: dict[str, dict] = {}            # 無効にしたブレークポイント: location → 置いたときの事実
        self.sources = dict(sources or {})
        self.loaded_at = datetime.now().astimezone().isoformat(timespec="milliseconds")
        try:
            stat = debugger.image.path.stat()
            self._elf_stamp: tuple | None = (stat.st_size, stat.st_mtime_ns)
        except OSError:
            self._elf_stamp = None

    # ------------------------------------------------------------------ 状態
    def target_status(self) -> dict:
        """target.status の形（state・止まっていれば今のスレッドの pc と場所）と、linux 欄（事実）。走っていれば、対象の
        止まりがあったかを 1 回だけ確かめる（HaltDebugger.poll。待たない。target.resume の後に止まりを拾う道）。止めた
        ままの時間は数え直さない（デーモンの見張りが周期的に読む）。"""
        self.d.poll()                                    # 走っていれば当たりを 1 回だけ確かめる（待たない）
        return self._status_from(self.d.status_facts())

    def target_status_unqueued(self) -> dict | None:
        """作業スレッドを通さない読み（デーモンの見張り）: 鍵がすぐ取れれば target_status と同じ（poll もする）、取れなければ
        鍵を待たずに最後に分かった事実の写しから（行き詰まらない）。写しがまだ無ければ None。"""
        lock = self.d._cv
        if lock.acquire(blocking=False):
            try:
                return self.target_status()
            finally:
                lock.release()
        facts = self.d.snapshot()
        return None if facts is None else self._status_from(facts)

    def _status_from(self, f: dict) -> dict:
        stop = f["last_stop"]
        stop_kind = stop["kind"] if stop is not None else "attach"
        out: dict = {"state": "RUNNING" if f["running"] else "HALTED", "target": "linux"}
        if not f["running"] and f["pc"] is not None:
            where = f["where"]
            link = self.d._link(f["pc"])
            loc = self.d.symbols.describe(link)
            out["pc"] = f["pc"]
            out["where"] = str(Location(f["pc"], loc.function, loc.offset, loc.file, loc.line)) \
                if loc.function is not None or loc.file else f"0x{f['pc']:08X}"
            for key in ("function", "file", "line"):
                if where[key] is not None:
                    out[key] = where[key]
            if stop_kind in HALT_REASON:
                out["halt_reason"] = HALT_REASON[stop_kind]
        out["linux"] = {"pid": f["pid"], "comm": f["comm"], "tid": f["tid"], "tid_from": f["tid_from"],
                        "thread_comm": next((t["comm"] for t in f["threads"] if t["tid"] == f["tid"]), None),
                        "threads": f["threads"], "stop_kind": None if f["running"] else stop_kind,
                        "registers_from": f["registers_from"], "pc_error": f["pc_error"], "elf": f["elf"],
                        "load_bias": f["load_bias"]}
        return out

    def read_words(self, addresses: list[int]) -> dict[str, int]:
        """system.status の memory: プロセスの仮想メモリの 32 ビットの語（止まっているときだけ。置いた BKPT は元の命令で）。"""
        def go():
            out = {}
            for a in addresses:
                got = self.d.memory(a, 4)
                if got["data"] is None:
                    raise ValueError(f"0x{a:08X}: {got['reason']}")
                out[f"0x{a:08X}"] = int.from_bytes(bytes.fromhex(got["data"]), "little")
            return out
        return self.d.use(go, halted=True) if addresses else {}

    # ------------------------------------------------------------------ 実行（画面の続行・停止・ステップ）
    @staticmethod
    def _no_core(core: int | None) -> None:
        if core is not None:
            raise ValueError("core は複数ドロップの部品（RP2040）の引数: Linux のプロセスの全コア停止では使わない")

    def _limits(self, what: str) -> tuple[int, float]:
        if self.step_max_instructions is None or self.step_timeout_s is None:
            raise RuntimeError(f"{what}: linux.halt.debug.attach に step_max_instructions と step_timeout_s を渡していない"
                               "（上限の引数を持たない画面の道のステップには、使う側が明示した上限が要る）")
        return self.step_max_instructions, self.step_timeout_s

    def target_resume(self, core: int | None = None) -> dict:
        """全コアを再開して、待たずに返す（HaltDebugger.run(0)。すぐの当たりは拾う）。止まったことは target.status の
        poll（デーモンの見張り）が拾う。"""
        self._no_core(core)
        self.d.run(0)
        return self.target_status()

    def target_halt(self, core: int | None = None) -> dict:
        self._no_core(core)
        self.d.request_halt()
        self.d.halt()
        return self.target_status()

    def target_step(self, count: int = 1) -> dict:
        _, timeout_s = self._limits("target.step")
        r = self.d.step_instructions(self.d.current_tid(), count, timeout_s)
        return {**self.target_status(), "steps_done": r["steps_done"]}

    def target_reset(self, halt: bool = False, kind: str = "default") -> dict:
        raise RuntimeError("Linux のプロセスに attach している間はリセットしない（linux.halt.debug.close してから）")

    def run_until_halt(self, timeout_s: float = 5.0) -> dict:
        raise RuntimeError("Linux のプロセスでは debug.run_until_halt は使わない: 時間切れで止める既定を持つため。"
                           "linux.halt.debug.run（wait_s を明示。時間切れでも走らせたまま）か target.resume を使う")

    def step_line(self, mode: str = "over", location: str | None = None, timeout_s: float = 5.0,
                  mask_interrupts: bool = True) -> dict:
        """debug.step の形。over / into = 1 行、out = 呼び出し元へ戻るまで、until + location = そこまで（Run to Cursor）。
        上限は attach の step_max_instructions・step_timeout_s（debug.step の timeout_s・mask_interrupts はマイコンの
        引数で、Linux の道では使わない）。"""
        max_instructions, limit_s = self._limits("debug.step")
        tid = self.d.current_tid()
        t0 = time.monotonic()
        if mode in ("over", "into"):
            if location is not None:
                raise ValueError("location は until だけ")
            r = self.d.step(tid, mode, max_instructions, limit_s)
        elif mode == "out":
            r = self.d.step_out(tid, limit_s)
        elif mode == "until":
            if location is None:
                raise ValueError("Linux のプロセスの until には location（Run to Cursor の場所）が要る")
            r = self.d.run_to_location(tid, location, limit_s)
        else:
            raise ValueError(f"mode は over / into / out / until（{mode!r}）")
        return self._step_reply(r, time.monotonic() - t0)

    @staticmethod
    def _step_reply(r: dict, elapsed_s: float) -> dict:
        """HaltDebugger の止まり → debug.step の形。reason: step（終えた）/ timeout（時間の上限）/ max_instructions（命令の数の
        上限）/ breakpoint・watchpoint
        （途中の当たり）/ そのほかの種類の名前。終了したプロセスには pc が無いので、その事実を誤りにする。"""
        if r.get("address") is None:
            raise RuntimeError("プロセスが止まった場所が無い（%s%s）" % (r.get("kind"), "" if r.get("code") is None
                                                                       else f", code {r['code']}"))
        if r.get("timed_out"):
            reason = "timeout"
        elif r["kind"] == "step":                               # 着かずに命令の数の上限を使い切った: max_instructions
            reason = "step" if r.get("reached_new_line", True) else "max_instructions"
        else:
            reason = {"swbreak": "breakpoint", "hwbreak": "breakpoint", "watch": "watchpoint"}.get(r["kind"], r["kind"])
        pc = r["address"]
        out = {"reason": reason, "pc": pc, "where": _where_text(pc, r), "elapsed_s": elapsed_s, "time_source": "host"}
        for key in ("function", "file", "line"):
            if r.get(key) is not None:
                out[key] = r[key]
        if "instructions" in r:
            out["steps"] = r["instructions"]
        return out

    # ------------------------------------------------------------------ レジスタ・呼び出し履歴・変数
    def _regs(self) -> list[int]:
        return self.d.engine.get_regs(self.d.current_tid())

    def _inspector(self, max_frames: int = 64, assume_leaf: bool = False) -> Inspector:
        if self.d.info is None:
            raise RuntimeError("ELF に DWARF（-g）が無い: %s" % self.d.image.path)
        return Inspector(self.d.info, _ThreadAccess(self._regs(), self.d.proc.mem), symbols=self.d.symbols,
                         max_frames=max_frames, assume_leaf=assume_leaf, load_bias=self.d.load_bias)

    def read_registers(self, names: list[str] | None = None) -> dict[str, int]:
        def go():
            regs = dict(zip(REG_NAMES, self._regs()))
            if not names:
                return regs
            alias = {"r13": "sp", "r14": "lr", "r15": "pc"}
            missing = [n for n in names if alias.get(n.lower(), n.lower()) not in regs]
            if missing:
                raise KeyError(f"Linux のユーザのスレッドでは読まないレジスタ: {missing}（読めるのは {REG_NAMES}）")
            return {n: regs[alias.get(n.lower(), n.lower())] for n in names}
        return self.d.use(go, halted=True)

    def backtrace(self, variables: bool = False, max_frames: int = 64, max_elements: int = 64,
                  assume_leaf: bool = False) -> list[dict]:
        def go():
            insp = self._inspector(max_frames, assume_leaf)
            out = []
            for entry in insp.backtrace(variables, max_elements):
                pc = entry["pc"]
                where = (str(Location(pc, entry["function"], entry["offset"], entry["file"], entry["line"]))
                         if entry["function"] is not None or entry["file"] else f"0x{pc:08X}")
                out.append({"index": entry["index"], "pc": pc, "where": where, "note": entry["note"], "cfa": entry["cfa"],
                            "function": entry["function"], "offset": entry["offset"], "file": entry["file"],
                            "line": entry["line"],
                            **{k: entry[k] for k in ("stop", "arguments", "locals") if k in entry}})
            return out
        return self.d.use(go, halted=True)

    def frame_variables(self, frame: int = 0, max_elements: int = 64) -> dict:
        return self.d.use(lambda: self._inspector().frame_variables(frame, max_elements), halted=True)

    def evaluate(self, expression: str, frame: int = 0, max_elements: int = 64, live: bool = False) -> dict:
        if live:
            raise RuntimeError("Linux のプロセスは走らせたまま読めない（live）: 止めて読む")
        return self.d.use(lambda: self._inspector().read(expression, frame, max_elements), halted=True)

    def expression_check(self, expression: str) -> dict:
        """ELF の DWARF だけで調べる（番地は実行時 = リンク時 + load bias）。"""
        if self.d.info is None:
            raise RuntimeError("ELF に DWARF（-g）が無い: 式は調べられない")
        return check_expression(self.d.info, expression, load_bias=self.d.load_bias)

    def type_info(self, name: str) -> dict:
        if self.d.info is None:
            raise RuntimeError("ELF に DWARF（-g）が無い: 型は調べられない")
        return Inspector(self.d.info, None, symbols=self.d.symbols, load_bias=self.d.load_bias).type_info(name)

    def symbol_info(self, location: str) -> dict:
        """場所 ↔ 実行時の番地（リンク時 + load bias）。関数名は前置きの後ではなく関数の先頭（マイコンと同じ）。"""
        symbols, bias = self.d.symbols, self.d.load_bias
        text = location.strip()
        if text.lower().startswith("0x"):
            address = int(text, 16)
        else:
            address = (symbols.resolve(text) + bias) & 0xFFFFFFFF
        loc = symbols.describe((address - bias) & 0xFFFFFFFF)
        out = {"address": address, "where": str(Location(address, loc.function, loc.offset, loc.file, loc.line))
               if loc.function is not None or loc.file else f"0x{address:08X}",
               "function": loc.function, "offset": loc.offset, "file": loc.file, "line": loc.line}
        if not text.lower().startswith("0x") and not is_line_location(text):
            sym = symbols.symbol(text)
            out.update(kind=sym.kind, size=sym.size)
        return out

    def disasm_around(self, address: int | None = None, before: int = 8, after: int = 8, from_elf: bool = False,
                      branch_tables: list[dict] | None = None, sync_limit: int = SYNC_LIMIT,
                      data_ranges: list[dict] | None = None) -> dict:
        """プロセスの仮想メモリ（置いた BKPT は元の命令で）を、ELF の $a / $t / $d で区切って逆アセンブルする。番地は
        実行時（リンク時の番地で解いて load bias を足す）。命令の中の名前（target_name・literal_name）はリンク時の番地で
        引く。from_elf・branch_tables・data_ranges は使わない（渡したら断る）。"""
        if from_elf or branch_tables or data_ranges:
            raise ValueError("Linux のプロセスの逆アセンブルでは from_elf・branch_tables・data_ranges は使わない")

        def go():
            pc = self._regs()[15] if address is None else address
            bias = self.d.load_bias
            memory = _LinkMemory(self.d, bias)
            dis = Disassembler(memory, self.d.symbols)
            listing = dis.around((pc - bias) & 0xFFFFFFFF, before, after, sync_limit)
            # in_flash: Linux のプロセスの番地はマイコンの Flash の領域ではない（target.flash の領域が無い）
            items = [{**_shift(i, bias).as_dict(), "in_flash": False} for i in listing.instructions]
            skipped = listing.sync_skipped
            if skipped is not None:
                skipped = {**skipped, "from": (skipped["from"] + bias) & 0xFFFFFFFF}
            return {"pc": pc, "memory": "target", "sync": listing.sync, "sync_skipped": skipped, "total": len(items),
                    "offset": 0, "pc_index": listing.pc_index, "instructions": items}
        return self.d.use(go, halted=True)

    def memory_view(self, address: int, length: int, unit: int = 8, width: int = 16, compare: bool = False,
                    from_elf: bool = False) -> dict:
        raise RuntimeError("Linux のプロセスのメモリは linux.halt.debug.memory で読む（target.memory.view はマイコンの道）")

    # ------------------------------------------------------------------ 書く
    def write_register(self, name: str, value: int) -> None:
        self.d.write_register(self.d.current_tid(), name, value)

    def write_expression(self, expression: str, value, frame: int = 0) -> dict:
        tid = self.d.current_tid()
        self.d.write_expression(tid, expression, value, frame)
        return {**self.evaluate(expression, frame), "via": "halted"}           # 止まっているときだけ書く

    def watch_write(self, expression: str, value) -> dict:
        raise RuntimeError("Linux のプロセスは走らせたまま書けない: 止めて debug.write_expression で書く")

    def watchpoint_set(self, location: str, kind: str = "write", size: int | None = None) -> dict:
        raise RuntimeError("Linux のプロセスのウォッチポイントは画面の道に無い（linux.halt.gdb.serve の gdb の watch）")

    def watchpoint_clear(self, location: str) -> dict:
        raise RuntimeError("Linux のプロセスのウォッチポイントは画面の道に無い")

    # ------------------------------------------------------------------ ブレークポイント
    def _row(self, bp: dict, enabled: bool) -> dict:
        loc = self.d.symbols.describe(self.d._link(bp["address"]))
        row = {"address": bp["address"], "location": bp["location"], "enabled": enabled,
               "where": str(Location(bp["address"], loc.function, loc.offset, loc.file, loc.line))
               if loc.function is not None or loc.file else f"0x{bp['address']:08X}"}
        for key in ("function", "file", "line"):
            if bp.get(key) is not None:
                row[key] = bp[key]
        at = self.d.symbols.line_place(bp["location"])
        if at is not None:
            row["at"] = at
        if enabled:
            row["kind"] = bp["kind"]
        return row

    def breakpoint_list(self) -> dict:
        def go():
            rows = [self._row(b, True) for b in self.d.breakpoints.values()]
            rows += [self._row(b, False) for b in self._disabled.values()]
            return {"breakpoints": sorted(rows, key=lambda b: b["address"]), "watchpoints": []}
        return self.d.use(go, halted=False)

    def breakpoint_enable(self, location: str, enabled: bool) -> dict:
        """無効 = 外して場所と種類を覚える、有効 = 同じ場所・種類で置き直す（マイコンと同じ）。"""
        def go():
            if enabled:
                bp = self._disabled.get(location) or next(
                    (b for b in self._disabled.values() if b["address"] == self.d.resolve(location)[0]), None)
                if bp is None:
                    self._find(location)                         # 置いてあれば何もしない（無ければ KeyError）
                    return self.breakpoint_list()
                self.d.break_insert(bp["location"], bp["kind"])
                del self._disabled[bp["location"]]
            else:
                bp = self._find(location)
                self.d.break_remove(bp["id"])
                self._disabled[bp["location"]] = bp
            return self.breakpoint_list()
        return self.d.use(go, halted=True)

    def breakpoint_set(self, location: str, condition: str | None = None, ignore_count: int = 0,
                       log: list[str] | None = None, message: str | None = None) -> dict:
        if condition is not None or ignore_count or log is not None or message is not None:
            raise ValueError("Linux のプロセスのブレークポイントには condition・ignore_count・log・message は使えない")
        if self.breakpoint_kind is None:
            raise RuntimeError("linux.halt.debug.attach に breakpoint_kind（sw / hw）を渡していない: 種類を渡さない置き方"
                               "（画面の行のクリック等）には使う種類が要る（linux.halt.debug.break.insert なら kind を渡す）")
        bp = self.d.break_insert(location, self.breakpoint_kind)
        row = self._row(bp, True)
        return {k: row[k] for k in ("address", "where", "function", "file", "line") if k in row}

    def _find(self, location: str) -> dict:
        """置いたときの location の文字列か、同じ番地に解ける場所のブレークポイント。"""
        for b in self.d.breakpoints.values():
            if b["location"] == location:
                return b
        address = self.d.resolve(location)[0]
        for b in self.d.breakpoints.values():
            if b["address"] == address:
                return b
        raise KeyError(f"{location!r} にブレークポイントが無い")

    def breakpoint_clear(self, location: str | None = None) -> dict:
        def go():
            if location is None:
                self._disabled.clear()
                ids = list(self.d.breakpoints)
            elif location in self._disabled:
                del self._disabled[location]
                ids = []
            else:
                ids = [self._find(location)["id"]]
            for bp_id in ids:
                self.d.break_remove(bp_id)
            return self.breakpoint_list()
        return self.d.use(go, halted=True)

    # ------------------------------------------------------------------ ELF・ソース
    def elf_status(self) -> dict:
        """debug.elf.status の形: attach の ELF（読み込みの見張りは無い。プロセスの像と ELF の一致は
        linux.halt.debug.status の image の事実）。"""
        path = self.d.image.path
        try:
            stat = path.stat()
            changed = (stat.st_size, stat.st_mtime_ns) != self._elf_stamp
        except OSError:
            changed = True
        return {"path": str(path), "loaded_at": self.loaded_at, "watching": False, "auto_reload": False, "interval_s": 0,
                "file_changed": changed, "target_image": "unknown", "changes": 0, "reloads": 0, "last": None,
                "error": None}

    def source_list(self) -> dict:
        symbols = self.d.symbols
        relocated = symbols.relocated()
        files = []
        for name, path in sorted(symbols.sources().items()):
            item = {"name": name, "path": str(path), "exists": not is_network_path(path) and path.is_file(),
                    **({"built_path": str(relocated[name]["built"])} if name in relocated else {})}
            got = self.sources.get(name)
            if got is not None:
                item["fetched" if "error" not in got else "fetch_error"] = dict(got)
            files.append(item)
        return {"files": files, "same_name": {n: [str(p) for p in ps]
                                              for n, ps in sorted(symbols.source_conflicts().items())}}

    def source_read(self, name: str) -> dict:
        path = self.d.symbols.sources().get(name)
        if path is None:
            raise KeyError(f"{name!r} はこの ELF のソースではない")
        if is_network_path(path):
            raise ValueError(f"外の共有のソースは開かない: {path}")
        lines = self.d.symbols.lines_of(name)
        if path.is_file():
            return {"name": name, "path": str(path), "text": _text(path), "lines": lines}
        got = self.sources.get(name)
        if got is None:
            raise FileNotFoundError(
                f"{name} がこの PC に無い（{path}）。止まっている間は対象の ssh が答えないので、取り寄せは attach のとき"
                "（コアを止める前）だけ: linux.halt.debug.attach に fetch_from_target（linux.connect のターゲットの名前）を渡す")
        if "error" in got:
            raise FileNotFoundError(f"{name} は attach のときに {got['target']} の {got['path']} から取り寄せられなかった: "
                                    f"{got['error']}")
        return {"name": name, "path": got["local_path"], "text": _text(Path(got["local_path"])), "lines": lines,
                "fetched": dict(got)}


def _built(symbols, name: str, path: Path) -> str:
    """行表の場所（ビルドした場所。ELF の隣の同じ名前に置き換えたなら元の場所）を posix の文字で。"""
    built = symbols.relocated().get(name, {}).get("built", path)
    return Path(built).as_posix()


def prefetch_sources(elf: str, target: str, fetch: Callable[[str, str, str], dict]) -> dict[str, dict]:
    """attach のとき（コアを止める前。止まった対象の ssh は答えない）に、ELF の行表が指すソースのうちこの PC に無いものを
    対象から取り寄せ、ELF と同じ所の <ELF の名前>.sources/<対象の上の道> に写す。名前 → {target, path, local_path, bytes,
    sha256}、取り寄せられなければ {target, path, error}（1 つの失敗で他をやめない）。"""
    symbols = Symbols(elf)
    root = Path(elf).parent / f"{Path(elf).name}.sources"
    out: dict[str, dict] = {}
    for name, path in sorted(symbols.sources().items()):
        if not is_network_path(path) and path.is_file():
            continue
        remote = _built(symbols, name, path)
        parts = [p for p in PurePosixPath(remote).parts if p not in ("/", "..", ".")]
        local = root.joinpath(*parts) if parts else root / name
        try:
            got = fetch(target, remote, str(local))
            out[name] = {"target": target, "path": remote, "local_path": got["local_path"], "bytes": got["bytes"],
                         "sha256": got["sha256"]}
        except Exception as e:  # noqa: BLE001 - 取り寄せられなかった事実を残し、他のファイルは続ける
            out[name] = {"target": target, "path": remote, "error": f"{type(e).__name__}: {e}"}
    return out


def _text(path: Path) -> str:
    if path.stat().st_size > SOURCE_LIMIT:
        raise ValueError(f"{path.name} は大きすぎる（{SOURCE_LIMIT // (1 << 20)} MB まで）")
    return path.read_text(encoding="utf-8", errors="replace")


def _where_text(pc: int, r: dict) -> str:
    if r.get("function") is None and not r.get("file"):
        return f"0x{pc:08X}"
    head = r["function"] if r.get("function") is not None else f"0x{pc:08X}"
    return f"{head} ({r['file']}:{r['line']})" if r.get("file") else head


class _LinkMemory:
    """逆アセンブルの読み手: リンク時の番地 → プロセスの仮想メモリ（実行時 = + load bias）。置いた BKPT は元の命令で。"""

    def __init__(self, debugger: HaltDebugger, bias: int):
        self.d, self.bias = debugger, bias

    def read_memory(self, address: int, length: int) -> bytes:
        va = (address + self.bias) & 0xFFFFFFFF
        try:
            data = self.d.proc.mem.read(va, length)
        except Unmapped as e:
            raise MemoryReadError(str(e)) from None
        return self.d.engine.bps.shadow(self.d.proc.tgid, va, data)


def _shift(i: Instruction, bias: int) -> Instruction:
    """リンク時の番地の命令 → 実行時の番地（番地・分岐の先・PC 相対で読む番地に load bias を足す）。"""
    def add(v):
        return None if v is None else (v + bias) & 0xFFFFFFFF
    return dataclasses.replace(i, address=add(i.address), target=add(i.target), literal=add(i.literal))
