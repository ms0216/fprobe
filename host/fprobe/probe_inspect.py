"""Probe の道具: 止まったターゲットをソースの言葉で調べる（C の式・フレームの変数・型・逆アセンブル・
周辺レジスタ（SVD）・メモリの表示）。

Probe（fprobe.probe。共通の部品の型は fprobe.probe_host.ProbeHost）に混ぜて使う mixin。Probe の _lock・target・dbg・_symbols・_debug・_target_type を使う。
結果はすべて JSON にできる形。仕様: docs/superpowers/specs/2026-10-17-f2-dwarf-design.md、
docs/superpowers/specs/2026-10-17-f2-disasm-svd-design.md、計画: docs/superpowers/plans/2026-10-17-api-integration-a-d.md
"""

from fnmatch import fnmatch
from pathlib import Path

from . import fault
from .debug import Debugger
from .dwarf.cexpr import UnknownNameError
from .dwarf.inspector import LOCAL_HINT, check_expression
from .disasm import SYNC_LIMIT, Disassembler, Listing
from .memview import ElfImage, TargetMemory, diff, format_hexdump, hexdump
from .svd import Device, Register, find_svd, load_device
from .symbols import is_network_path

__all__ = ["InspectTools", "SOURCE_LIMIT", "VIEW_LIMIT"]

SOURCE_LIMIT = 2 << 20               # 画面・混在表示に読むソースの大きさの上限
VIEW_LIMIT = 4096                    # 1 回に逆アセンブル・表示するメモリの上限（バイト）
VIEW_HISTORY = 16                    # compare のために覚えておく範囲の数
FUNCTION_LIMIT = 64 << 10           # 関数の逆アセンブルの上限（これより大きければ range で）


class _NeedsMemory(Exception):
    """式の番地を決めるのにメモリの値が要る（ポインタをたどる式）。"""


def _no_register(number: int) -> int:
    raise NotAvailable("走らせたまま・ELF だけではレジスタ（ローカル変数）は使えない")


class InspectTools:
    """止まったターゲットを調べる道具（Probe の mixin）。"""

    def _init_inspect(self) -> None:
        self._svd: Device | None = None
        self._svd_info: dict | None = None
        self._image: ElfImage | None = None                   # 逆アセンブル・メモリの名前に使う ELF の像
        self._view_previous: dict[tuple[int, int], bytes] = {}   # memory_view の前回の読み（compare 用）

    def _forget_elf(self) -> None:
        """ELF を読み替えた: ELF から作ったものを捨てる。"""
        self._image = None

    # --- ソースの言葉の変数（DWARF） ---------------------------------------------------------

    def evaluate(self, expression: str, frame: int = 0, max_elements: int = 64, live: bool = False) -> dict:
        """C の式（変数・メンバー・添字・ポインタ・& と *・キャスト・算術・比較・論理・ビット演算）を評価して読む。frame は backtrace の index
        （0 = いまの関数。そのフレームの引数・ローカル変数も使える）。live: 走らせたまま読む（ライブ ウォッチ。
        レジスタを使わず、大域・静的の変数とメモリだけ。コアを止めない）。"""
        with self._lock:
            try:
                return self.dbg.evaluate(expression, frame, max_elements, live)
            except UnknownNameError as e:
                if live:
                    raise UnknownNameError(f"{e}{LOCAL_HINT}") from None
                raise

    def expression_check(self, expression: str) -> dict:
        """C の式を ELF だけで調べる（ターゲットは使わない。止まっていても・走っていても・つないでいなくても同じ）:
        式の誤り・名前の無い変数・型の誤りは誤りにする。通れば {"expression", "type"（ポインタをたどる式は読むまで
        分からないので None）, "address"（番地の決まる式）, "live"（走らせたまま読める = 大域・静的の式）,
        "needs_memory"（番地がメモリの値で決まる）}。"""
        if self._symbols is None:
            raise RuntimeError("ELF が読み込まれていない（debug_elf_load を先に）")
        info = Debugger(None, self._symbols).debug_info()
        if info is None:
            raise RuntimeError("ELF に DWARF（-g）が無い: 式は調べられない")
        return check_expression(info, expression)

    def target_fault(self) -> dict:
        """止まったコアの故障の解析: 原因（CFSR・HFSR の各ビットの意味と手がかり）、故障の番地（MMFAR・BFAR）、
        故障した命令の場所と、その時のレジスタ（例外のフレーム）。故障していなければ faulted = False。"""
        with self._lock:
            return fault.analyze(self.target, self._symbols, self._handler_entry_sp())

    def _handler_entry_sp(self) -> int | None:
        """ハンドラに入った時の sp（DWARF の CFI。いまのフレームの CFA）。分からなければ None。"""
        try:
            inspector = self.dbg.inspector()
            return inspector.frames()[0].cfa if inspector is not None else None
        except Exception:                                         # 巻き戻せない（DWARF が無い・壊れたスタック）
            return None

    def frame_variables(self, frame: int = 0, max_elements: int = 64) -> dict:
        """フレームの引数とローカル変数（型付きの値。構造体・配列は入れ子）。"""
        with self._lock:
            return self.dbg.frame_variables(frame, max_elements)

    def write_expression(self, expression: str, value: int | float | str, frame: int = 0) -> dict:
        """C の式の指す値（メモリ上の基本型・列挙（名前でも）・ポインタ・ビットフィールド）に書き、読み直して返す。"""
        with self._lock:
            self.dbg.write_expression(expression, value, frame)
            return self.dbg.evaluate(expression, frame)

    def type_info(self, name: str) -> dict:
        """型の名前（"struct Sensor"・typedef 名等）の大きさ・メンバー・列挙子（ターゲットは使わない）。"""
        with self._lock:
            if self._symbols is None:
                raise RuntimeError("ELF が読み込まれていない（debug_elf_load）")
            return Debugger(None, self._symbols).type_info(name)

    def _backtrace(self, variables: bool, max_frames: int, max_elements: int, assume_leaf: bool = False) -> list[dict]:
        dbg = self.dbg
        inspector = dbg.inspector(max_frames, assume_leaf)
        cfi = inspector is not None and len(inspector.unwinder.table) > 0
        frames = dbg.backtrace(max_frames, inspector if cfi else None)
        out = []
        for index, f in enumerate(frames):
            entry = {"index": index, "pc": f.pc, "where": str(f.where), "note": f.note, "cfa": f.cfa}
            entry.update({k: getattr(f.where, k) for k in ("function", "offset", "file", "line")})
            if f.stop is not None:
                entry["stop"] = f.stop
            if variables and inspector is not None and cfi:
                variables_of = inspector.frame_variables(index, max_elements)
                entry.update(arguments=variables_of["arguments"], locals=variables_of["locals"])
            out.append(entry)
        return out

    # --- 逆アセンブル ----------------------------------------------------------------------

    def _elf_image(self) -> ElfImage | None:
        if self._symbols is None:
            return None
        if self._image is None or self._image.path != self._symbols.path:
            self._image = ElfImage(self._symbols.path)
        return self._image

    def _code_reader(self, from_elf: bool):
        """読むメモリ: 接続していればターゲット（from_elf なら ELF）、未接続でスナップショットを開いていればそれ、
        どちらでもなければ ELF。(読み手, "target" / "snapshot" / "elf")。"""
        if from_elf or self._debug is None and self._offline_target() is None:
            image = self._elf_image()
            if image is None:
                raise RuntimeError("ターゲット未接続で ELF も無い（target_connect か debug_elf_load）")
            return image, "elf"
        return TargetMemory(self.target), "target" if self._debug is not None else "snapshot"

    def _disassembler(self, from_elf: bool, branch_tables: list[dict] | None = None,
                      data_ranges: list[dict] | None = None) -> tuple[Disassembler, str]:
        """branch_tables: [{"address": tbb / tbh の命令の番地, "entries": 表の項目の数}]、data_ranges: [{"address",
        "size"}]（データとして区切る区間）。どちらも使う側が明示する。"""
        tables = {}
        for t in branch_tables or []:
            if not isinstance(t, dict) or set(t) != {"address", "entries"} or int(t["entries"]) < 1:
                raise ValueError(f"branch_tables の項目は {{address, entries（1 以上）}}: {t!r}")
            tables[int(t["address"])] = int(t["entries"])
        ranges = []
        for r in data_ranges or []:
            if not isinstance(r, dict) or set(r) != {"address", "size"} or int(r["size"]) < 1:
                raise ValueError(f"data_ranges の項目は {{address, size（1 以上）}}: {r!r}")
            ranges.append((int(r["address"]), int(r["size"])))
        memory, origin = self._code_reader(from_elf)
        return Disassembler(memory, self._symbols, self._elf_image(), tables, ranges), origin

    @staticmethod
    def _listing_dict(listing: Listing, origin: str, offset: int, limit: int) -> dict:
        items = listing.instructions[offset:offset + limit]
        pc = listing.pc_index
        return {"memory": origin, "sync": listing.sync, "sync_skipped": listing.sync_skipped,
                "total": len(listing.instructions), "offset": offset,
                "pc_index": pc - offset if pc is not None and offset <= pc < offset + limit else None,
                "instructions": [i.as_dict() for i in items]}

    def _function_listing(self, dis: Disassembler, location: str) -> Listing:
        start, size = dis.function_bounds(self._location(location))
        if size > FUNCTION_LIMIT:
            raise ValueError(f"{location} は {size} バイト（{FUNCTION_LIMIT} まで。debug_disasm_range で区切る）")
        return dis.function(start)

    def _location(self, location: str) -> str | int:
        return int(location, 16) if location.lower().startswith("0x") else location

    def _pc(self) -> int:
        return self.target.read_core_register("pc")

    def disasm_range(self, address: int, length: int, from_elf: bool = False, offset: int = 0,
                     limit: int = 256, branch_tables: list[dict] | None = None,
                     data_ranges: list[dict] | None = None) -> dict:
        """番地 address から length バイト（4096 まで）を逆アセンブルする。命令は offset 番目から limit 件。
        branch_tables: tbb / tbh の表の大きさ、data_ranges: データの区間（使う側が明示する。_disassembler）。"""
        with self._lock:
            dis, origin = self._disassembler(from_elf, branch_tables, data_ranges)
            return self._listing_dict(dis.range(address, address + length), origin, offset, limit)

    def disasm_function(self, location: str, from_elf: bool = False, offset: int = 0, limit: int = 256,
                        branch_tables: list[dict] | None = None, data_ranges: list[dict] | None = None) -> dict:
        """関数の全体（関数名か "0x..." の関数の中の番地）を逆アセンブルする。"""
        with self._lock:
            dis, origin = self._disassembler(from_elf, branch_tables, data_ranges)
            return self._listing_dict(self._function_listing(dis, location), origin, offset, limit)

    def disasm_around(self, address: int | None = None, before: int = 8, after: int = 8,
                      from_elf: bool = False, branch_tables: list[dict] | None = None,
                      sync_limit: int = SYNC_LIMIT, data_ranges: list[dict] | None = None) -> dict:
        """番地（省略時は PC）の前 before 命令・その命令・後ろ after 命令。前は関数の先頭から揃える（PC まで
        sync_limit バイトより遠ければ揃えない。揃えなかった理由は sync_skipped）。"""
        with self._lock:
            pc = self._pc() if address is None else address
            dis, origin = self._disassembler(from_elf, branch_tables, data_ranges)
            return {"pc": pc, **self._listing_dict(dis.around(pc, before, after, sync_limit), origin, 0,
                                                   before + after + 1)}

    def disasm_text(self, location: str | None = None, address: int | None = None, length: int | None = None,
                    before: int = 8, after: int = 8, source: bool = True, from_elf: bool = False,
                    branch_tables: list[dict] | None = None, sync_limit: int = SYNC_LIMIT,
                    data_ranges: list[dict] | None = None) -> dict:
        """ソースの行を挟んだ逆アセンブルの文字列（AI 向け）。location なら関数、address と length なら範囲、
        どちらも無ければ address（省略時は PC）の前後（sync・sync_skipped は disasm_around と同じ）。"""
        with self._lock:
            dis, origin = self._disassembler(from_elf, branch_tables, data_ranges)
            if location is not None:
                listing = self._function_listing(dis, location)
            elif length is not None:
                if address is None:
                    raise ValueError("length には address が要る")
                listing = dis.range(address, address + length)
            else:
                listing = dis.around(self._pc() if address is None else address, before, after, sync_limit)
            lines = self._source_lines() if source else None
            text = listing.text(source=(lambda f, n: lines(f, n)) if lines else None)
            return {"memory": origin, "sync": listing.sync, "sync_skipped": listing.sync_skipped, "text": text}

    def _source_lines(self):
        """(ファイル名, 行) → ソースの行。ELF が参照するファイルだけを読む（外の共有・大きすぎるものは読まない）。"""
        if self._symbols is None:
            return None
        sources, cache = self._symbols.sources(), {}

        def line(name: str | None, number: int | None) -> str | None:
            if name is None or number is None:
                return None
            if name not in cache:
                path = sources.get(name)
                ok = path is not None and not is_network_path(path) and path.is_file() \
                    and path.stat().st_size <= SOURCE_LIMIT
                cache[name] = path.read_text(encoding="utf-8", errors="replace").splitlines() if ok else None
            text = cache[name]
            return text[number - 1] if text is not None and 0 < number <= len(text) else None
        return line

    # --- 周辺レジスタ（SVD） ----------------------------------------------------------------

    def svd_load(self, device: str | None = None, path: str | None = None, from_target: bool = False) -> dict:
        """周辺レジスタの定義（CMSIS-SVD）を読む。path（ファイル）か device（SVD のファイル名。拡張子を除いて同じ
        名前のファイルを同梱・~/.fprobe/svd・pyOCD の内蔵から探す）か from_target（接続中のターゲットの pyOCD の定義が
        指す SVD = _svd_location のファイル。定義の事実で、名前から推さない）。ターゲット名から推して選ぶことはしない。"""
        with self._lock:
            if from_target:
                location = getattr(self.target, "_svd_location", None)
                member = getattr(getattr(location, "filename", None), "name", None)
                if not member:
                    raise RuntimeError("接続中のターゲットの定義に SVD が無い（device か path を渡す）")
                self._svd = Device.parse(_pyocd_builtin_svd(str(member)))         # 同名の別の置き場所のものは読まない
                self._svd_info = {"source": str(member), "origin": "target"}
                return self._svd_summary()
            if path is not None:
                if is_network_path(path):
                    raise ValueError(f"外の共有の SVD は開かない: {path}")
                self._svd = Device.load(path)
                self._svd_info = {"source": str(Path(path)), "origin": "file"}
            else:
                if not device:
                    raise RuntimeError("device（SVD のファイル名。例 STM32F401）か path を渡す")
                source = find_svd(device)
                self._svd = load_device(device)
                self._svd_info = {"source": source.name if source else device,
                                  "origin": source.origin if source else ""}
            return self._svd_summary()

    def _svd_summary(self) -> dict:
        return {"name": self._svd.name, **self._svd_info, "peripherals": len(self._svd.peripherals)}

    def _device(self) -> Device:
        """読み込んだ SVD（無ければ誤り。ターゲット名から推して読まない）。"""
        if self._svd is None:
            raise RuntimeError("SVD が読み込まれていない（target_svd_load で device か path を渡す）")
        return self._svd

    def svd_peripherals(self, filter: str | None = None) -> dict:
        """SVD の周辺の一覧。filter は名前・グループの一部（大文字小文字を区別しない）。"""
        with self._lock:
            dev = self._device()
            key = (filter or "").upper()
            return {"device": dev.name, "peripherals": [
                {"name": p.name, "base_address": p.base_address, "group": p.group, "description": p.description,
                 "registers": len(p.registers)}
                for p in dev.peripherals if key in p.name.upper() or key in (p.group or "").upper()]}

    def svd_registers(self, peripheral: str) -> dict:
        """周辺のレジスタの定義（番地・幅・アクセス・リセット値・フィールド・列挙）。"""
        with self._lock:
            p = self._device().peripheral(peripheral)
            return {"peripheral": p.name, "base_address": p.base_address,
                    "registers": [r.as_dict() for r in p.registers]}

    def peripheral_read(self, path: str, allow_side_effects: bool = False, skip: list[str] | None = None) -> dict:
        """周辺レジスタを読んでフィールドに解読する。path は "RCC.CR"（1 つ）か "GPIOA"（周辺の全部）。
        読むと状態が変わるもの（SVD の readAction）は allow_side_effects が無ければ読まず skipped に入れる。
        SVD に readAction の無いレジスタ（ST の SVD 等）はレジスタの名前から推さずに読む。周辺の全部を読むとき、
        ほかに読まないものは使う側が skip（"周辺.レジスタ" の glob。大文字小文字を区別しない）で渡す。"""
        if skip and "." in path:
            raise ValueError("skip は周辺の全部を読むときだけ（1 つを名指しで読むときは要らない）")
        with self._lock:
            dev, access = self._device(), TargetMemory(self.target)
            if "." in path:
                return {"registers": [dev.read_register(access, path, allow_side_effects=allow_side_effects).as_dict()],
                        "skipped": []}
            registers, skipped = [], []
            for r in dev.peripheral(path).registers:
                reason = self._bulk_skip_reason(r, allow_side_effects, tuple(skip or ()))
                if reason is None:
                    try:
                        registers.append(dev.read_register(access, r.path, allow_side_effects=True).as_dict())
                        continue
                    except Exception as e:                       # 時計の止まった周辺のバスエラー等: 他は読む
                        reason = f"read failed: {type(e).__name__}: {e}"
                skipped.append({"path": r.path, "reason": reason})
            return {"registers": registers, "skipped": skipped}

    @staticmethod
    def _bulk_skip_reason(r: Register, allow_side_effects: bool, skip: tuple[str, ...]) -> str | None:
        if r.access in ("write-only", "writeOnce"):
            return "write-only"
        for pattern in skip:
            if fnmatch(r.path.upper(), pattern.upper()):
                return f"skip {pattern}"
        if allow_side_effects:
            return None
        if r.has_read_side_effects:
            return "read side effects (SVD readAction)"
        return None

    def peripheral_write(self, path: str, fields: dict | None = None, value: int | None = None,
                         allow_side_effects: bool = False) -> dict:
        """周辺レジスタに書く。fields（{"HSEON": 1, "MODER5": "Output"}。読み・変更・書き込み）か value（全体）。
        書く前と後の値を返す（読むと状態が変わる・書くだけのレジスタは読まない）。"""
        if (fields is None) == (value is None):
            raise ValueError("fields か value のどちらか 1 つを指定する")
        with self._lock:
            dev, access = self._device(), TargetMemory(self.target)
            r = dev.register(path)
            readable = r.access not in ("write-only", "writeOnce") and (allow_side_effects or not r.has_read_side_effects)
            if fields is not None and not readable and r.access not in ("write-only", "writeOnce"):
                raise PermissionError(f"{r.path} は読むと状態が変わる（読み・変更・書き込みには allow_side_effects）")
            before = dev.read_register(access, path, allow_side_effects=True) if readable else None
            if fields is not None:
                value = r.encode(before.value if before is not None else r.reset_value, fields)
            access.write(r.address, r.size, value)
            after = dev.read_register(access, path, allow_side_effects=True) if readable else None
            return {"path": r.path, "written": value, "before": before.as_dict() if before else None,
                    "after": after.as_dict() if after else None}

    # --- メモリの表示 ----------------------------------------------------------------------

    def memory_view(self, address: int, length: int, unit: int = 8, width: int = 16, compare: bool = False,
                    from_elf: bool = False) -> dict:
        """メモリの 16 進表示（4096 バイトまで）。unit = 8 / 16 / 32 ビット、width = 1 行のバイト数。compare なら
        同じ範囲の前回の読みと比べ、変わった単位に印を付ける。番地にシンボル・周辺レジスタの名前を付ける。"""
        if not 0 < length <= VIEW_LIMIT:
            raise ValueError(f"length は 1〜{VIEW_LIMIT}")
        if not 0 <= address < 1 << 32:
            raise ValueError(f"番地 0x{address:X} は 32 ビットの番地の空間の外")
        length = min(length, (1 << 32) - address)          # 番地の空間の終わりで止める（折り返して 0 番地を読まない）
        with self._lock:
            memory, origin = self._code_reader(from_elf)
            data = memory.read_memory(address, length)
            key = (address, length)
            stored = self._view_previous.pop(key, None)
            previous = stored if compare else None
            self._view_previous[key] = data                    # 新しいものを後ろへ。古い範囲から忘れる
            while len(self._view_previous) > VIEW_HISTORY:
                del self._view_previous[next(iter(self._view_previous))]
            rows = hexdump(data, address, width=width, unit=unit, previous=previous)
            out = {"memory": origin, "address": address, "length": length, "unit": unit,
                   "rows": [{"address": r.address, "values": list(r.values), "text": r.text, "changed": list(r.changed)}
                            for r in rows],
                   "dump": format_hexdump(rows, unit), "names": self._names_in(address, length)}
            if compare:
                out["compared"] = previous is not None
                out["changes"] = [{"address": c.address, "before": c.before.hex(), "after": c.after.hex()}
                                  for c in (diff(previous, data, address) if previous is not None else [])]
            return out

    def _names_in(self, address: int, length: int) -> list[dict]:
        """範囲の中で始まるシンボル（ELF）・周辺レジスタ（読み込んだ SVD）の名前。"""
        names = []
        image = self._elf_image()
        for a in range(address, address + length):
            if image is not None and (s := image.symbol_at(a)) is not None and s.address == a:
                names.append({"address": a, "name": s.name, "kind": "symbol"})
            if self._svd is not None and (hit := self._svd.find(a)) is not None and hit.offset == 0:
                names.append({"address": a, "name": str(hit), "kind": "register"})
        return names


def _pyocd_builtin_svd(member: str) -> bytes:
    """pyOCD の内蔵の SVD（ターゲットの定義の _svd_location が指すもの）を名前で読む。"""
    import zipfile
    from importlib import resources
    from pyocd.debug.svd.loader import BUILTIN_SVD_DATA_PATH
    try:
        with resources.files("pyocd").joinpath(BUILTIN_SVD_DATA_PATH).open("rb") as f, zipfile.ZipFile(f) as z:
            return z.read(member)
    except KeyError:
        raise FileNotFoundError(f"ターゲットの定義が指す SVD {member} が pyOCD の内蔵に無い") from None
