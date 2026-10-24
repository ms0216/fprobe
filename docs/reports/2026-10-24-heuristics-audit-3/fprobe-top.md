# ヒューリスティックの点検（3 回目）: host/fprobe 直下の .py

対象: `host/fprobe/*.py`（下位フォルダ・plugins・tests は対象外）。検索で候補を拾わず、各ファイルを頭から最後まで読んだ。行番号は点検の時点（main, 0e677cfb）のもの。コードは変更していない。

分類: **H** = ヒューリスティック（原則に反する）。**?** = 判断が要る（理由を添える）。

## H（3 件）

### H1. coresight_id.py:173-205 — ROM テーブルのたどりを黙って打ち切る

```python
# coresight_id.py:173-185
def walk_rom_table(read: Read, base: int, depth: int = 4, limit: int = 128) -> Component:
    """base の部品を読み、ROM テーブルなら入口をたどる（深さ depth、全体で limit 個まで）。"""
    seen: set[int] = set()

    def visit(address: int, level: int) -> Component:
        seen.add(address)
        comp = read_component(read, address)
        if comp.error or not comp.is_rom_table or level >= depth:
            return comp
        entries = 960 if comp.cls == CLASS_ROM else 512
        for i in range(entries):
            if len(seen) >= limit:
                break
```

深さ depth=4・部品の総数 limit=128 の既定の上限で探索をやめるが、戻り値の Component には打ち切ったこと・使った上限が何も残らない。上限で止めた ROM テーブルの下の部品は「無い」のと区別できない。原則「切り詰めたら切り詰めたことと上限を返す」に反する。

### H2. mcp_server.py:1377-1393 — recording_events が 1000 件で黙って切る

```python
# mcp_server.py:1377-1378
    def recording_events(self, t0: int | None = None, t1: int | None = None, streams: list[str] | None = None,
                         kinds: list[str] | None = None, limit: int = 1000) -> dict:
...
# mcp_server.py:1385-1393
        out = []
        for e in view.events(t0, t1, streams, kinds, limit):
            item = asdict(e)
            if e.stream == "sample" and e.a in names:
                item["name"] = names[e.a]
            if e.stream == "fpga" and e.kind == "value":
                item["fields"] = signals.fields(e.a, e.b)
            out.append(item)
        return {"events": out}
```

limit=1000 を既定にして view.events に渡し、返すのは {"events": out} だけ。切ったか（範囲の総数）も上限も返さないので、AI は範囲の出来事がそれで全部だと読める。

### H3. run.py:461-464, 491-493 — BKPT（即値を問わず）で止まれば正常終了とみなす

```python
# run.py:461-464
        if self._at_bkpt(status):
            if self.log.failures() or self.log.verdict == "failed":
                return RunResult("bkpt", EXIT_FAILED, self._failures_text() or "試験の失敗", 0, stop=status)
            return RunResult("bkpt", EXIT_OK, "BKPT 命令で止まった（終わり）", 0, stop=status)
...
# run.py:491-493
    def _at_bkpt(self, status: dict) -> bool:
        half = self._instruction(status)
        return half is not None and half & 0xFF00 == 0xBE00
```

0xBExx のどの BKPT でも、試験の行に失敗が無ければ EXIT_OK・「BKPT 命令で止まった（終わり）」とする。BKPT を終わりの合図とするのはprobe-rs 流の慣習（ファームとの約束）で、測った事実ではない。assert・panic の BKPT も合格になりうる（fault.halt_reason は同じ停止をpanic 等として扱う）。終わりの合図（即値）は使う側が明示して渡す形にすべき。

## ?（75 件）

### ?1. __main__.py:39, 44, 141, 146 — CLI の暗黙の既定（pretrigger 1024・timeout 10 s・SWD 4 MHz・timeout 60 s）

```python
# __main__.py:39-39
    la.add_argument("--pretrigger", type=int, default=1024)
...
# __main__.py:44-44
    la.add_argument("--timeout", type=float, default=10.0)
...
# __main__.py:141-141
    run.add_argument("--frequency", type=float, default=4e6, help="SWD の周波数 [Hz]")
...
# __main__.py:146-146
    run.add_argument("--timeout", type=float, default=60.0, help="時間切れ [秒]")
```

測り方・操作の既定値。道具の設定としての既定が許されるか、値を出力に添えるべきかは判断が要る。

### ?2. arm_debug.py:213-226 — ステップの時間切れで停止要求で止めても停止の理由が step のまま

```python
# arm_debug.py:215-225
            deadline = time.monotonic() + self.timeout_s
            while not self.port.dscr() & DSCR_HALTED:
                if time.monotonic() >= deadline:              # WFI 等で次の命令に進まない: 止めて終える
                    self.port.halt_request()
                    self._wait(DSCR_HALTED, "ステップの後に止まらない")
                    break
                time.sleep(0.001)
        finally:
            self.port.write_breakpoint(slot, 0, 0)
        self._ensure_entered(self.port.dscr())
        self._stepped = True
```

時間切れ（既定 timeout_s 0.5 s）で halt_request して止めた場合も _stepped=True で、halt_cause は step になる。測った事実（要求で止めた）と違う名前。

### ?3. armv7a_debug.py:29-39, 64 — 最初の APB-AP を選ぶ・番地順をコア番号とみなす

```python
# armv7a_debug.py:29-39
def find_apb_ap(dap: RawDap, max_aps: int = 16) -> int | None:
    """ADIv5 の AP を順に見て、最初の APB の MEM-AP（APB2/3・APB4）の番号。"""
    for ap in range(max_aps):
        try:
            idr = decode_ap_idr(dap.read_ap(ap, AP_IDR))
        except DapError:
            dap.clear_errors()
            continue
        if idr["kind"] == "mem-ap" and idr["type"] in (2, 6):
            return ap
    return None
...
# armv7a_debug.py:62-64
        except DapError:
            continue
    return sorted(found)
```

複数の APB-AP があっても最初の 1 つを使う。デバッグの部品を番地で並べた順をコアの番号とするのは配置の仮定。

### ?4. aux_mcu.py:49 — live の判定の待ち時間 settle_s=0.01 の暗黙の既定

```python
# aux_mcu.py:49-50
    def read(self, settle_s: float = 0.01) -> AuxReading:
        """最新値。settle_s 待って samples が増えたか（live）を、待った時間と前後の数とともに返す。既定の 10 ms は
```

待った時間は返すが、10 ms という既定の値で「生きている」を決めている。

### ?5. callgraph.py:39, 238-243 — _REVISITS=8 の打ち切りを dynamic（上限が無い）の印に混ぜる

```python
# callgraph.py:39-39
_REVISITS = 8                       # 同じ場所をより深い SP で訪ねなおす回数の上限（解析の打ち切り。判定ではない）
...
# callgraph.py:236-243
            if n in seen:
                revisits[n] = revisits.get(n, 0) + 1
                if revisits[n] > _REVISITS:
                    if not flow.dynamic:
                        flow.dynamic = True
                        flow.reasons.append(f"0x{code[n].address:08X}: より深い SP で {_REVISITS} 回を超えて訪ねなおしたので"
                                            "解析を打ち切った（深さはここまでの下限）")
                    break
```

打ち切った事実は reasons の文にあるが、印は「動的に SP を変える = 上限が無い」と同じ dynamic。打ち切りと本当の無限を機械的に区別できない。

### ?6. callgraph.py:206-216 — tbb/tbh の表の長さを「直後に続くデータの所すべて」とみなす

```python
# callgraph.py:206-216
def _table_targets(code: Sequence[Instruction], index: int) -> list[int] | None:
    """tbb / tbh の表（直後のデータ）の分かれ先。表が読めなければ None。"""
    insn = code[index]
    entries = []
    for item in code[index + 1:]:
        if item.kind != "data" or item.literal_value is None:
            break
        entries.append(item.literal_value)                         # 逆アセンブラは表の幅（1 / 2 バイト）で区切る
    if not entries:
        return None
    return [insn.address + 4 + 2 * e for e in entries]
```

表の本当の長さは直前の cmp 等の範囲の検査で決まる。続くリテラルまで分かれ先にする推し。

### ?7. callgraph.py:304-316 — たどれない命令の SP の下げを全部足して frame に加える

```python
# callgraph.py:304-316
def _unreached(code: Sequence[Instruction], seen: dict[int, int], flow: FunctionFlow,
               sites: dict[int, CallSite], resolve: Callable[[int], str | None]) -> None:
    """たどれなかった命令: SP を下げる量を全部足して枠を多めにし、呼び出しは枠の深さで加える。"""
    flow.uncertain = True
    flow.reasons.append("分岐の先が分からない所がある（表の分岐など）: たどれない命令の分を多めに足した")
    extra = 0
    probe = FunctionFlow(flow.name, flow.address, flow.size)
    for n, insn in enumerate(code):
        if n in seen or insn.kind != "code":
            continue
        after = _step(insn, _State(0, {}), probe)
        extra += max(after.depth, 0)
    flow.frame += extra
```

uncertain と reasons は付くが、frame の値そのものに見積もりが混ざり、事実の部分と分けて返していない。

### ?8. calling.py:46 — SCRATCH_LIMIT 64 KB の暗黙の上限

```python
# calling.py:46-46
SCRATCH_LIMIT = 64 << 10                # 引数・戻り値の置き場所に使う sp の下の大きさの上限
```

安全のための上限だが、使う側が渡せず、誤りの文に値が出ない。

### ?9. calling.py:318 — 時間切れ + IPSR が故障なら理由を fault に書き換える

```python
# calling.py:318-318
                    out["reason"] = "fault" if halt.reason == "timeout" and halt.regs.ipsr in FAULTS else halt.reason
```

測った事実は「時間切れのとき IPSR が故障の番号」。それを fault と名付け、時間切れの事実を消す。

### ?10. coresight_id.py:291-313 — 決まった番地の値が表に当たれば部品とみなす

```python
# coresight_id.py:291-304
def identify_part(read: Read, designers: set[tuple[int, int]] = frozenset()) -> Part | None:
    """部品を判別する。designers は ROM テーブル・TARGETID で見えた設計者（読む順の手がかり。空でもすべて試す）。"""
    probes = [_identify_stm32, _identify_nrf, _identify_rp]
    preferred = {ST: _identify_stm32, NORDIC: _identify_nrf, RASPBERRY_PI: _identify_rp}
    order = [preferred[d] for d in designers if d in preferred]
    for probe in order + [p for p in probes if p not in order]:
        part = probe(read)
        if part is not None:
            return part
    return None


def _identify_stm32(read: Read) -> Part | None:
    for address in _DBGMCU:
```

STM32 は設計者（JEP106）を確かめずに DBGMCU の番地の値で引く。誤判別の余地がある（source は返す）。

### ?11. correlate.py:525-540 — 同じ時刻の候補から代表を「項が多い・短い」で選ぶ

```python
# correlate.py:525-538
def _merge_same(causes: list[dict]) -> list[dict]:
    """成立する時刻（_times）がすべて同じ候補を 1 つに: 項の多いもの（説明が多い。同じなら短いもの）を残し、ほかを
    same に（自動の候補の重複。例 "restart changed and state=BUSY" と "restart changed and addr=CTRL"・"restart rose"
    がこのデータでは同じ出来事）。順は保つ。"""
    groups: dict[tuple, list[dict]] = {}
    for c in causes:
        groups.setdefault(c["_times"], []).append(c)
    out = []
    for group in groups.values():
        keep = min(group, key=lambda c: (-len(parse_condition(c["condition"]).terms), len(c["condition"])))
        rest = [c["condition"] for c in group if c is not keep]
        item = {k: v for k, v in keep.items() if k != "_times"}
        if rest:
            item["same"] = rest
```

代表の選び方は決めごと。ほかは same に残すので情報は欠けないが、先頭が選び方で決まる。

### ?12. debug.py:170-172, 362-369 — ウォッチポイントの大きさ既定 4・read_variable の添字を 4 バイト要素と仮定

```python
# debug.py:170-172
        if size is None:
            size = self.symbols.variable(location).size if self.symbols and isinstance(location, str) \
                and not location.startswith("0x") else 4
...
# debug.py:362-369
    def read_variable(self, name: str, index: int | None = None) -> int | bytes:
        """変数を読む。大きさ 1 / 2 / 4 / 8 なら符号なし整数、それ以外（配列・構造体）はバイト列。
        index を指定すると 4 バイトの要素の配列として index 番目を読む。"""
        var = self.symbols.variable(name)
        if index is not None:
            if not 0 <= index * 4 < var.size:
                raise IndexError(f"{name} は {var.size // 4} 要素")
            return self.target.read32(var.address + 4 * index)
```

番地指定のときの size=4 は暗黙の既定。添字の計算で要素の大きさを 4 と仮定。

### ?13. diagnose.py:67-78, 104-109, 154-162, 196, 259 — 診断の判定・推奨・試す速度の暗黙の値

```python
# diagnose.py:67-78
    def verdict(self) -> str:
        fails = [s for s in self.steps if s.status == "fail"]
        if not self.ok:                                  # 失敗した段があれば「接続できる」とは書かない（ok と食い違わない）
            head = fails[0].message if fails else "ターゲットのデバッグポートに届かなかった"
            volts = next((s.data["volts"] for s in self.steps if s.name == "voltage" and s.status == "ok"), None)
            return f"接続できない: {head}" + ("" if volts is None else f"（Vtarget = {volts:.2f} V）")
        what = (self.part.name if self.part else self.core["core"] if self.core else "ターゲット")
        text = f"接続できる（{what}、{self.protocol.upper()} {self.frequency_hz:,} Hz）"
        warns = [s for s in self.steps if s.status == "warn"]
        if warns:
            text += "。ただし: " + " / ".join(s.message for s in warns)
        return text
...
# diagnose.py:104-109
    d.connect = {"target": (d.part.suggested_target if d.part and d.part.suggested_target else "cortex_m"),
                 "frequency_hz": d.frequency_hz}
    if d.protocol == "jtag" and d.jtag is not None and d.jtag.chain:
        d.connect["jtag_chain"] = d.jtag.chain
    if d.core and d.core.get("lockup"):              # 眠っている（WFI）はそのまま接続できる: リセットは勧めない
        d.connect["connect_mode"] = "under-reset"
...
# diagnose.py:154-162
def _check_pins(d: Diagnosis, pins) -> None:
    if pins is None:
        d.add("pins", "skip", "ピンを読めない")
        return
    value = pins()
    state = {"swclk": value & 1, "swdio": (value >> 1) & 1, "nreset": (value >> 7) & 1}
    low = [name for key, name in (("nreset", "nRESET"), ("swdio", "SWDIO")) if not state[key]]
    text = " / ".join(f"{name} = Low" for name in low) or "nRESET = High、SWDIO = High"
    d.add("pins", "warn" if low else "ok", text, **state)          # 原因の候補は並べない（事実だけ）
...
# diagnose.py:196-196
    speeds = [frequency_hz] + [f for f in (frequency_hz // 10, SLOW_HZ) if SLOW_HZ <= f < frequency_hz]
...
# diagnose.py:259-259
    hz = min(frequency_hz, 1_000_000)
```

nRESET・SWDIO が Low を warn にする、lockup なら under-reset を勧める、速度を [f, f//10, 100 kHz] と試す、JTAG を 1 MHz に制限する、段の status を集めて結論の文（verdict）を返す。診断という機能の性格上どこまで許すかは判断が要る。

### ?14. disasm.py:52-54, 396-397 — $t/$d の無い所で ldr [pc] の先をデータとみなす

```python
# disasm.py:52-54
    #   "mapping" = ELF の $d（ARM ELF の規格の印）/ "branch_table" = 使う側が渡した tbb / tbh の表の大きさ /
    #   "pc_load" = 前の ldr [pc, #n] が読む先なのでデータとみなした（推定。$t・$d の無い所だけ）/
    #   "undecodable" = 命令として解読できない
...
# disasm.py:396-397
                    if not self._marked(literal):                    # 印のある所は印に従う（推定で上書きしない）
                        literals[literal] = (literal + size, 4, "pc_load")
```

本体での推定。data_basis="pc_load" と「推定」で示してはいる。

### ?15. elfsize.py:346-348 — 下端を渡さなければ最後の RAM 区画の終わりを下端にする

```python
# elfsize.py:346-348
    elif free_bottom is not None:
        bottom, source = free_bottom, "end_of_ram_sections"
        notes.append("下端は上端の下の最後の区画の終わり（ヒープが伸びてくると、その分も使用量に数える）")
```

暗黙の代わりの値（source と notes で示す）。

### ?16. fault.py:100-110 — DFSR の複数ビットから 1 つの停止の理由を選ぶ

```python
# fault.py:100-110
def halt_reason(dfsr: int, instruction: int | None, breakpoint: bool = False) -> str | None:
    """止まった理由を 1 語で（DFSR は pyOCD が再開・ステップのたびにクリアするので、その停止の分だけ）。
    instruction: PC の 16 ビットの命令（BKPT 0xBExx なら、デバッガが置いたもの（breakpoint=True）でなければファームウェア
    自身の BKPT = panic・assert・セミホスティング等）。複数のビットなら具体的なほうを優先（ステップで踏んだブレークポイント等）。"""
    if dfsr & 0x02:
        own = instruction is not None and instruction & 0xFF00 == 0xBE00 and not breakpoint
        return "bkpt_instruction" if own else "breakpoint"
    for bit, name in ((0x04, "watchpoint"), (0x08, "vector_catch"), (0x10, "external"), (0x01, "request")):
        if dfsr & bit:
            return name
    return None
```

優先の順は決めごと。複数立っていた事実が理由の 1 語に潰れる。

### ?17. fault.py:169-170 — entry_sp が無ければ今の MSP を例外のフレームとみなす

```python
# fault.py:169-170
    else:
        sp = entry_sp if entry_sp is not None else target.read_core_register("msp")
```

ハンドラが積んだ後なら違う。仮定したことが出力に印として残らない。

### ?18. flash.py:52-55 — STM32F2/F4/F7 の守りを part_number の前置きで当てる

```python
# flash.py:52-55
    @staticmethod
    def applies(target) -> bool:
        part = str(getattr(target, "part_number", "") or "").upper()
        return part.startswith(("STM32F2", "STM32F4", "STM32F7"))
```

機器固有の処理を名前の前置きで当てる。

### ?19. flash.py:173-186 — 形式を拡張子（名前の慣習）→ 先頭のバイト → 既定 bin で推す

```python
# flash.py:173-186
def _guess_format(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in (".elf", ".axf", ".out"):
        return "elf"
    if suffix in (".hex", ".ihex", ".ihx"):
        return "hex"
    if suffix == ".bin":
        return "bin"
    head = path.read_bytes()[:4]
    if head == b"\x7fELF":
        return "elf"
    if head[:1] == b":":
        return "hex"
    return "bin"
```

format で明示はできるが、省略時は名前の慣習で判定する。

### ?20. flash.py:662-671 — 16 バイトの行を全チップに仮定

```python
# flash.py:662-671
    def _fetch_elsewhere(self, scratch: int, pc: int) -> None:
        """CPU に pc と別の Flash の行を 1 度読ませる（RAM の scratch で ldr pc, [pc, #0] を 1 命令）。Flash の読みの
        行の保持（STM32F4 の Flash の取り込みの 128 ビット等）は書き換えでは捨てられず、止めた場所（pc）に置いたまま
        再開すると古い命令を実行する（2026-10-18 に実機で、戻した BKPT を実行し続けた。別の行を通れば読み直す。ART の
        キャッシュのリセット・待ちの切り替えでは捨てられなかった）。レジスタと scratch は呼び手が戻す。"""
        region = next(iter(self.memory_map.iter_matching_regions(is_flash=True)))
        other = region.start if (region.start ^ pc) >> 4 else region.start + 16
        self.target.write_memory_block8(scratch, list(bytes.fromhex("dff800f0") + (other | 1).to_bytes(4, "little")))
        self.target.write_core_register("pc", scratch)
        self.target.step(disable_interrupts=True)
```

特定のチップ由来の値を一般に使う。

### ?21. flash_bp.py:48, 50, 111, 291, 480-499 — DORMANT_LIMIT=3・周り 32 バイトの照合で同じファームとみなす

```python
# flash_bp.py:48-50
WINDOW = 32                              # 記録で確かめる周りの大きさ
DFSR, DFSR_BKPT, DFSR_HALTED = 0xE000ED30, 0x2, 0x1
DORMANT_LIMIT = 3
...
# flash_bp.py:111-111
                 enabled: bool = True, dormant_limit: int = DORMANT_LIMIT):
...
# flash_bp.py:291-291
        if self._hits[pc] >= self.dormant_limit:
...
# flash_bp.py:480-493
    def recover(self) -> dict:
        """前のプロセスが残した BKPT（記録）を戻す。周りの中身が記録と違う（別のファームウェアを書いた）番地は触らない。
        {"restored": [番地], "skipped": [番地]}。"""
        entries = self.journal.load() if self.journal is not None else None
        if entries is None:
            return {"restored": [], "skipped": []}
        bkpts = {w.address + i: b for w in entries for i, b in enumerate(BKPT_CODE)}
        changes, restored, skipped = [], [], []
        for window in sorted({w.window for w in entries}):
            members = [w for w in entries if w.window == window]
            current = bytes(self.target.read_memory_block8(window, WINDOW))
            original = members[0].window_original
            intact = all(c == o or bkpts.get(window + i) == c for i, (c, o) in enumerate(zip(current, original)))
            for w in members:
```

休眠の BKPT に 3 回当たったらセクタを戻す閾値、recover の 32 バイトの窓は経験の値（status に値は出る）。

### ?22. fpga_obs.py:115, 239-243 — 印が 1 つの鎖は前の鎖の傾き（無ければ申告の比）で時刻を出す・SYNC_WINDOW=256

```python
# fpga_obs.py:115-115
    SYNC_WINDOW = 256
...
# fpga_obs.py:239-243
            chain.pairs = (chain.pairs + [(instant - self.sync_delay, float(f))])[-self.SYNC_WINDOW:]
            segs = fit_sync(chain.pairs, self._ratio or self.nominal_ratio)
            chain.segment = segs[0] if segs else None
            if chain.segment is not None and len(chain.pairs) >= 2:
                self._ratio = chain.segment.ratio
```

仮定に頼る時刻の推定で、ObsSample にその印が無い。直近 256 組の窓も暗黙。

### ?23. heap.py:556 — 記録に無い free を「記録を始める前の確保」と数える

```python
# heap.py:556-556
                self.untracked_frees += 1                           # 記録を始める前の確保
```

原因の解釈を名前に当てる。

### ?24. heap.py:595-613 — live_counts を 5 時点・使用量の推移を添字で等間隔に間引く

```python
# heap.py:595-608
    def _live_counts(self, caller: str) -> list[tuple[float, int]]:
        history = self.history.get(caller, [])
        if self.first is None:
            return []
        marks = [self.first + (self.last - self.first) * k / 4 for k in range(5)]
        out, i, n = [], 0, 0
        for m in marks:
            while i < len(history) and history[i][0] <= m:
                n = history[i][1]
                i += 1
            out.append((m, n))
        return out

    def _downsampled(self, points: int) -> list[tuple[float, int]]:
```

点の数が暗黙。等間隔の間引きは山を落としうる（series_downsampled と peak_bytes は返す）。

### ?25. inject.py:284 — 規則の判定が warn までなら ok

```python
# inject.py:284-284
        return {"check": "rules", "ok": RANK[result["verdict"]] <= RANK["warn"], "value": result}
```

warn を合格に数える決め。

### ?26. la.py:585 — post_beats の既定 = 容量の半分

```python
# la.py:585-585
        post = self.beats // 2 if post_beats is None else post_beats
```

測り方の暗黙の既定。

### ?27. la_measure.py:34, 262 — 違反の一覧を 100 件で切り、上限を返さない

```python
# la_measure.py:34-34
VIOLATION_LIMIT = 100
...
# la_measure.py:262-262
                if len(r.violations) < VIOLATION_LIMIT:
```

violation_count との差で切ったことは分かるが、上限の値は返さない。

### ?28. liveness.py:27, 59 — PCSR の 0 を「読めない」とみなす

```python
# liveness.py:27-27
_INVALID = (0, 0xFFFFFFFF)           # PCSR が読めない（止まっている・眠っている・DWT が無い）
...
# liveness.py:59-59
        valid = [pc for pc in pcs if pc is not None and pc not in _INVALID]
```

0 番地のコードもありうる（規格の読めない値は 0xFFFFFFFF）。

### ?29. mcp_server.py:101, 1470 — TRIGGER_LAG_S=2.0（遅れて届いた出来事は調べない）

```python
# mcp_server.py:101-101
TRIGGER_LAG_S = 2.0                    # recording_trigger: 今からこれだけ前までを調べる（流れ・取り込みが記録に届くまで）
...
# mcp_server.py:1470-1470
            now = self.session.device.time() - int(TRIGGER_LAG_S * hz)
```

暗黙の仮定で、落ちたことが分からない。

### ?30. mcp_server.py:332-346 — 常に 0 のチャンネルを省く・first_edges を先頭 16 件

```python
# mcp_server.py:332-345
def _channel_summary(cap) -> dict:
    period_ns = cap.sample_period_s * 1e9
    out = {}
    for ch in range(cap.channels):
        bits = cap.channel(ch)
        edges = [i for i in range(1, len(bits)) if bits[i] != bits[i - 1]]
        if not edges and not bits[0]:
            continue                                       # 常に 0 のチャンネルは省く
        out[ch] = {
            "initial": bits[0],
            "edges": len(edges),
            # トリガ（0 ns）からの相対時刻。多いときは先頭だけ
            "first_edges_ns": [round((i - cap.trigger) * period_ns) for i in edges[:16]],
        }
```

切ったことの印が無い（edges の数はある）。

### ?31. mcp_server.py:965, 1182, 1416 — pretrigger 1024・before/after 4096・post_s 1.0 の暗黙の既定

```python
# mcp_server.py:965-965
    def la_arm(self, rate_hz: float | None = None, pretrigger: int = 1024, mask: int = 0, value: int = 0,
...
# mcp_server.py:1182-1182
                        fall: int = 0, rle: bool = True, before_beats: int = 4096, after_beats: int = 4096,
...
# mcp_server.py:1416-1416
    def recording_trigger_set(self, condition: str, action: str = "mark", post_s: float = 1.0) -> dict:
```

測り方の既定。

### ?32. mcp_server.py:2045, 2057 — UART のボーレートの既定 115200

```python
# mcp_server.py:2045-2045
            want = baud or link.uart_baud or 115200
...
# mcp_server.py:2057-2057
            self._uart = Serial(ports[0], baud or 115200, timeout=0)
```

暗黙の既定。

### ?33. mcp_server.py:2232-2246 — 通知を最新 20 件だけ残す

```python
# mcp_server.py:2232-2245
NOTICE_LIMIT = 20


class Notices:
    """デーモンの通知のうち AI に知らせるもの。MCP には押し出しが無いので、次に返す道具の結果（辞書）に
    notices = [{"topic", "message", ...}] として添える（添えたら消す。溜めるのは最新 NOTICE_LIMIT 件）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._pending: list[dict] = []

    def add(self, topic: str, params: dict) -> None:
        with self._lock:
            self._pending.append({"topic": topic, **params})
```

古いものを黙って捨てる。

### ?34. merge.py:76-78 — 子の出来事の不確かさに内挿の誤りを入れない

```python
# merge.py:76-78
        rows.append((f"{child_name}.{e.stream}", e.kind, at(fit, e.t), e.a, e.b, body,
                     None if e.t_end is None else at(fit, e.t_end),
                     QUANTIZATION_TICKS + (e.uncertainty or 0)))
```

組の間の直線が正しいという仮定（nearest と residual_max は別に返す）。

### ?35. monitor.py:191-193 — PC に置けないとき近くで raw.halt し、それを返さない

```python
# monitor.py:191-193
    if not (existing or placed) or not _wait(lambda: raw.get_state() == Target.State.HALTED,
                                              monitor.timeout_s, monitor.poll_s):
        raw.halt()                                             # 置けなかった・戻らない: すぐ近くで止める
```

黙った代わりの動作。

### ?36. obs_condition.py:37-38 — 時刻の決まらない値を数えから外し、外した数を返さない

```python
# obs_condition.py:37-38
    values = [it for it in items if it.get("kind") == "value" and it.get("fields") is not None
              and it.get("time_us") is not None]
```

欠けの明示が無い。

### ?37. probe_breakpoints.py:22, 48, 110-113 — ログを最新 10000 件だけ持つ

```python
# probe_breakpoints.py:22-22
LOG_LIMIT = 10_000
...
# probe_breakpoints.py:48-48
        self._break_log: collections.deque = collections.deque(maxlen=LOG_LIMIT)
...
# probe_breakpoints.py:110-113
    def breakpoint_log(self, since: int = 0, limit: int = 200) -> dict:
        """ログ・ブレークポイントの記録（seq が since より後のものを古い順に limit 件）。next を次の since に渡す。"""
        items = [e for e in self._break_log if e["seq"] > since][:limit]
        return {"entries": items, "next": items[-1]["seq"] if items else since}
```

落ちたことを返さない。

### ?38. probe_breakpoints.py:39-42 — 条件の値が value・string の無い辞書なら真

```python
# probe_breakpoints.py:39-42
def _truth(value: object) -> bool:
    if isinstance(value, dict):
        value = value.get("value", value.get("string", True))     # 列挙・文字の配列
    return bool(value)
```

暗黙の決め。

### ?39. probe_formats.py:297 — RTT の時刻の不確かさに poll_s（無ければ 0.01 s）を足す

```python
# probe_formats.py:297-297
        poll = getattr(self, "poll_s", 0.01) * (feed.timebase_hz or 0)
```

暗黙の 10 ms。

### ?40. probe_formats.py:523-524 — 流れのトレースの時刻合わせの近似を warnings に出さない

```python
# probe_formats.py:523-524
        if feed.clock is None and trace.ts_hz and t is not None and feed.timebase_hz:
            feed.clock = TargetClock(trace.ts_hz, feed.timebase_hz, events[-1].ts, t, unc or 0.0)
```

仮定の時刻合わせの明示が欠ける。

### ?41. probe_heap.py:315 — 記録に無い使用中のブロックを「(記録の前の確保)」と名付ける

```python
# probe_heap.py:315-315
        caller = live.caller if live is not None else "(記録の前の確保)"
```

解釈のラベル（記録中の見えない確保もありうる）。

### ?42. probe_longrec.py:32-34 — segment_s 3600・DEFAULT_DECIMATE の既定

```python
# probe_longrec.py:32-34
        policy = Policy(segment_s=3600.0 if segment_s is None else segment_s, segment_bytes=segment_bytes,
                        max_bytes=max_bytes, decimate_after_s=decimate_after_s,
                        decimate_streams=DEFAULT_DECIMATE if decimate_streams is None else tuple(decimate_streams))
```

暗黙の既定。

### ?43. probe_observe.py:662-676 — SWO を 30 ms 静かなら出し切ったとみなす（最長 0.5 s）

```python
# probe_observe.py:662-675
    def _swo_drain(self, quiet_s: float = 0.03, limit_s: float = 0.5) -> int:
        """SWO の受信に残っている古いバイトを捨てる（quiet_s の間なにも来なくなるまで、最長 limit_s）。捨てたバイト数。
        受信を始めた直後は、前の受信の残り（機器の FIFO）が先に届き、新しい流れとの継ぎ目で解読がずれる
        （2026-10-18 に実機で）。ターゲットが出力を止めている間に呼ぶ。"""
        dropped, last, deadline = 0, time.monotonic(), time.monotonic() + limit_s
        while time.monotonic() < deadline:
            chunk = self._collect("swo", 0)
            now = time.monotonic()
            if chunk:
                dropped, last = dropped + len(chunk), now
            elif now - last >= quiet_s:
                break
            time.sleep(0.005)
        self._swo_reset()
```

暗黙の閾値（捨てた数は返す）。

### ?44. probe_profile.py:292-293 — exceptions を top で切り総数を返さない

```python
# probe_profile.py:292-293
                out["exceptions"] = [{k: e[k] for k in ("exception", "name", "count", "total_self", "load")}
                                     for e in traced[:top]]
```

rows には rows_total があるのに揃っていない。

### ?45. probe_profile_export.py:205-213 — testpoint_aggregate を top で切り総数を返さない

```python
# probe_profile_export.py:205-213
        if by == "function":
            out["rows"] = [{"function": r["function"], "inclusive_s": r["inclusive"] / hz,
                            "exclusive_s": r["exclusive"] / hz, "started": r["started"], "active": r["active"]}
                           for r in rows[:top]]
        else:
            files, _, _ = self._tp_names(run.live.table)
            grouped = group_rows(rows, Grouping(by, groups, separator), files.get)
            out["rows"] = [{"group": g["group"], "exclusive_s": g["exclusive"] / hz, "started": g["started"],
                            "functions": g["functions"]} for g in grouped[:top]]
```

小さい。

### ?46. probe_rtos_analysis.py:87 — window_us 既定 10 ms

```python
# probe_rtos_analysis.py:87-87
    def rtos_timeline_load(self, window_us: float = 10_000.0, limit: int = WINDOWS_LIMIT) -> dict:
```

集計の窓の暗黙の既定。

### ?47. probe_safety.py:109-129（mcp_server.py:666-668） — tick_stopped 等を "fatal" の欄に入れる

```python
# probe_safety.py:109-122
    def _fatal_state(self, state: str) -> dict | None:
        """走っているコアが致命的エラーで回り続けていれば、その様子（_lock の中で。target_status から）。読めなければ
        None（見張りは状態の読みを妨げない）。"""
        if state not in ("RUNNING", "LOCKUP") or getattr(self, "_monitor", None) is not None \
                or getattr(self.target, "debug_architecture", None) in NOT_CORTEX_M:     # DHCSR・DWT は Cortex-M だけ
            self._liveness.reset()
            return None
        try:
            target = self.target
            lockup = state == "LOCKUP" or bool(target.read_memory(DHCSR) & S_LOCKUP)
            pcs: list[int | None] = []
            if not lockup and self._liveness_settings["pc_samples"]:
                demcr = target.read_memory(DEMCR)
                if not demcr & DEMCR_TRCENA:
...
# mcp_server.py:666-668
            fatal = self._fatal_state(state)
            if fatal is not None:
                status["fatal"] = fatal
```

liveness は「正常かは決めない」が、置き場所の名前が判定の言葉。

### ?48. probe_stack.py:124-125, 168 — .su を ELF の下で探す既定・margin 64 を返さない

```python
# probe_stack.py:124-125
        if su_paths is None:
            su_paths = [str(self._symbols.path.parent)]
...
# probe_stack.py:168-168
                    margin: int = 64, top_symbol: str | None = None, vector_table: int | None = None,
```

置き場所の慣習での既定、暗黙の余白。

### ?49. probe_swo.py:192 — value_watch の size 既定 4

```python
# probe_swo.py:192-192
        size = d.get("size") or (w.size if w is not None else 4)
```

暗黙の既定。

### ?50. probe_swo.py:36, 307-308, 361 — 例外の出来事を 65536 件で黙って持つのをやめる

```python
# probe_swo.py:36-36
EXCEPTION_LIMIT = 1 << 16               # ためる例外トレースの上限
...
# probe_swo.py:307-308
                if len(run.exceptions) < EXCEPTION_LIMIT:
                    run.exceptions.append((t, p.number, p.action.name.lower()))
...
# probe_swo.py:361-361
               "exceptions": {"counts": dict(run.exception_counts), "events": len(run.exceptions)},
```

切ったことを返さない。

### ?51. probe_testpoint.py:339-343 — 最初の Hit より前を数えずに捨てる

```python
# probe_testpoint.py:339-343
        if not run.synced:                                        # 受け始めの途中の記録は取りこぼしではない
            first = next((k for k, i in enumerate(items) if isinstance(i, Hit)), None)
            if first is None:
                return
            items, run.synced = items[first:], True
```

捨てた数を返さない。

### ?52. probe_watch.py:162-163 — 大きさ 0 のシンボルを 4 バイトとみなす（s.size or 4）

```python
# probe_watch.py:162-163
            s = self._symbols.variable(expression)                        # DWARF が無い: シンボルの番地と大きさ
            return _Watched(expression, s.address, s.size or 4)
```

暗黙の代わり。

### ?53. probe_watch.py:44, 132, 570-571 — 吸い出した要素を最新 200000 件だけ持つ

```python
# probe_watch.py:44-44
BUFFER_LIMIT = 200_000                 # 吸い出した要素を残す数
...
# probe_watch.py:132-132
    samples: collections.deque = field(default_factory=lambda: collections.deque(maxlen=BUFFER_LIMIT))
...
# probe_watch.py:570-571
            items = [s for s in buf.samples if s[0] >= first][:limit]
            nxt = items[-1][0] + 1 if items else max(first, buf.samples[0][0] if buf.samples else first)
```

古いものを黙って飛ばす。

### ?54. pyocd_targets.py:1-24 — STM32F401xE を F412 の Flash のアルゴリズムで定義

```python
# pyocd_targets.py:1-14
"""pyOCD に無いターゲットの定義（pyOCD をライブラリとして使うときに register() する）。

STM32F401xE: pyOCD の内蔵ターゲットにも、現在の CMSIS パック索引にも無い。Flash コントローラと
セクタ構成（16 KB × 4 + 64 KB + 128 KB × 3 = 512 KB）は STM32F412xE と同じなので、その書き込み
アルゴリズムを使い、RAM だけ F401xE の 96 KB にする（アルゴリズムは 0x20000000 から約 12 KB を使う）。
"""

from pyocd.core.memory_map import MemoryMap, RamRegion
from pyocd.target import TARGET
from pyocd.target.builtin.target_STM32F412xx import STM32F412xE

__all__ = ["STM32F401xE", "register"]


```

機器固有の定義を本体に置き、同じとみなす仮定。

### ?55. raw_dap.py:56-63 — 知らない TransferError を NO_ACK とする

```python
# raw_dap.py:56-63
def ack_of(error: BaseException) -> Ack:
    if isinstance(error, DAPAccessIntf.TransferFaultError):
        return Ack.FAULT
    if isinstance(error, DAPAccessIntf.TransferTimeoutError):
        return Ack.WAIT
    if isinstance(error, DAPAccessIntf.TransferProtocolError):
        return Ack.PROTOCOL
    return Ack.NO_ACK
```

分からないものを特定の原因の名前にする。

### ?56. recording.py:66-78 — ClockMap が公称の速さで写したことを返さない・不確かさは往復の半分だけ

```python
# recording.py:66-78
    def to_ticks(self, host_s: float) -> tuple[int, float]:
        """(タイムベース, 不確かさ（タイムベースの単位）)。"""
        if not self._points:
            raise RuntimeError("時計の対応付けの点が無い（記録を始める前のホストの出来事は置けない）")
        xs, ys = [p[0] for p in self._points], [p[1] for p in self._points]
        if len(xs) == 1 or max(xs) - min(xs) < 1e-3:
            slope, x0, y0 = self.hz, xs[-1], ys[-1]                  # 点が足りない: 公称の速さ
        else:
            mx, my = statistics.fmean(xs), statistics.fmean(ys)
            slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)
            x0, y0 = mx, my
        uncertainty = max(p[2] for p in self._points) / 2 * self.hz
        return round(y0 + (host_s - x0) * slope), uncertainty
```

仮定に頼る不確かさ。

### ?57. recording_long.py:43, 72, 76 — Policy の既定（segment_s・間引く stream の表）

```python
# recording_long.py:43-43
DEFAULT_DECIMATE = ("dap", "sample", "swo", "clock", "waveform")    # 間引くときに消す既定（"waveform" = 波形のファイル）
...
# recording_long.py:72-72
    segment_s: float = 3600.0
...
# recording_long.py:76-76
    decimate_streams: tuple[str, ...] = DEFAULT_DECIMATE
```

暗黙の既定。

### ?58. recording_long.py:710-713 — 間引いたかたまりの 'x' が 2 つの意味

```python
# recording_long.py:710-713
                if (c.t0 >= p0 and c.t1 <= p1) or not kept:
                    highs[px] |= first | toggles
                    lows[px] |= (~first | toggles) & full
                    continue
```

「画素の中で変化」と「かたまりのどこかで変化」が同じ文字。

### ?59. recording_stream.py:34, 71 — トリガのサンプル 0 の不確かさを 4 サンプルとする

```python
# recording_stream.py:34-34
TRIGGER_SLACK_SAMPLES = 4          # 符号器がトリガを見るまでの遅れ（数サンプル）を不確かさに入れる
...
# recording_stream.py:71-71
            return stream, StreamAnchor(t, TRIGGER_SLACK_SAMPLES * stream.sample_period_s * timebase_hz, "trigger")
```

見込みの値。

### ?60. riscv_target.py:95-100 — PC がブレークポイントかでトリガの種類を推す

```python
# riscv_target.py:95-100
    def get_halt_reason(self):
        cause = self.dm.halt_cause()
        if cause == "trigger":
            pc = self.dm.read_csr(CSR["dpc"])
            return Target.HaltReason.BREAKPOINT if pc in self.breakpoints else Target.HaltReason.WATCHPOINT
        return _REASONS.get(cause)                                # dcsr.cause が表に無ければ None（分からない）
```

どのトリガかの推定。

### ?61. sampler.py:19-71 — 巡回の速さの見込み（転送 × 48 SWCLK のモデル）を本体が出す

```python
# sampler.py:19-32
# 速さの見積もり（B9・B17。docs/superpowers/specs/2026-10-18-b17-sampler-lazy-restore-design.md の 1・4 節）:
# 1 転送 = ヘッダ 8 + ターン 1 + ACK 3 + データ・パリティ 33 + ターン 1 = 46 SWCLK + idle 2（pyOCD）= 48 SWCLK。
# B17（遅延の戻し）: ホストが線を使っていない間は SELECT・CSW・TAR を戻さず線を持ったまま読むので、定常の 1 か所 n 語は
# TAR・DRW × n・RDBUFF・CTRL/STAT = 3 + n 転送。有効なか所が 1 つで 1 語なら CSW を自動増加なしにして TAR も書かない
# （DRW・RDBUFF・CTRL/STAT の 3 転送）。ホストの命令が来るたびに、戻し 3 転送と借り直し（SELECT・CSW・TAR）が加わる。
# B17 の前（毎回戻す）は 1 か所 8 + n 転送で、実機（SWD 3.857 MHz、1 か所 1 語）は見積もり 112 µs に対し約 120 µs
# （調停の分）だった。実際の速さは巡回の数で測る。
BITS_PER_TRANSFER = 48
TRANSFERS_PER_SLOT = 3                         # TAR・RDBUFF・CTRL/STAT（+ DRW × 語数）


def round_transfers(slots: list[tuple[int, int]]) -> int:
    """か所 [(番地, 語数)] の定常の 1 巡回の転送の数（線を持ったまま。ホストの命令が無い間）。"""
    if len(slots) == 1 and slots[0][1] == 1:
```

見込みの計算（実測 achieved と並べる箇所もある）。

### ?62. stackusage.py:106-111 — .su の同名が見分けられなければ大きい方

```python
# stackusage.py:106-111
        if len(found) > 1 and file_of is not None and (file := file_of(name)):
            same = [e for e in found if Path(e.file).name == Path(file).name]
            found = same or found
        if len(found) > 1:
            notes.append(f"{name}: .su に同じ名前が {len(found)} 個（static 関数）。大きい方を使った")
        out[name] = max(found, key=lambda e: e.size)
```

選び方の決め（notes に出す）。

### ?63. stackusage.py:236-237 — FPU の命令の有無で例外の枠 108 / 36 を決める

```python
# stackusage.py:236-237
    frame_size = exception_frame if exception_frame is not None else (
        EXTENDED_FRAME if graph.uses_fpu() else BASIC_FRAME)
```

推して決める既定（exception_frame で明示可）。

### ?64. stackusage.py:251-256 — 呼び出しの深さを .su の枠とみなす

```python
# stackusage.py:251-256
def _site_depth(site: CallSite, frame: int, source: str, disasm_frame: int) -> int:
    """呼び出しの場所の深さ。.su の枠が逆アセンブルと違うときは、呼び出しは .su の枠の深さとみなす
    （末尾の呼び出しは逆アセンブルの深さ = 枠を戻した後）。"""
    if source == "su" and frame != disasm_frame and site.kind in ("call", "indirect"):
        return frame
    return min(site.depth, frame) if source == "su" and site.kind in ("tail", "indirect_tail") else site.depth
```

仮定。

### ?65. stepping.py:43-57 — DFSR の複数ビットを優先の順で 1 つに

```python
# stepping.py:43-56
def dfsr_reason(dfsr: int, stepping: bool) -> str | None:
    """DFSR → 止まった理由。1 命令の実行（HALTED だけ）は stepping なら None、走らせた後なら外からの停止 "user"
    （DFSR.HALTED = 停止の要求かステップ。ARMv7-M ARM C1.6.1）。走らせた後にビットが 1 つも無ければ "unknown"
    （外からの停止とみなさない）。複数のビットが立っていれば vector_catch・watchpoint・breakpoint・external の順の 1 つ。"""
    if dfsr & _VCATCH:
        return "vector_catch"
    if dfsr & _DWTTRAP:
        return "watchpoint"
    if dfsr & _BKPT:
        return "breakpoint"
    if dfsr & _EXTERNAL:
        return "external"
    if stepping:
        return None
```

fault.halt_reason と同類。

### ?66. stepping.py:195-198 — BL/BLX を LR と PC の変化から推す

```python
# stepping.py:195-198
def _is_call(before: Regs, after: Regs) -> bool:
    """BL / BLX を実行した: LR が直前の命令の次の番地（Thumb のビット付き）になり、順に進んでいない。"""
    return (after.lr != before.lr and after.lr & 1 == 1 and (after.lr & ~1) - before.pc in (2, 4)
            and not 0 < after.pc - before.pc <= 4)
```

推定。

### ?67. stepping.py:274-279, 332-333 — CFI が無ければ関数が変わった = 戻ったとみなす

```python
# stepping.py:274-279
    def _returned(self, function, cfa0: int | None, now: Regs) -> bool:
        if cfa0 is not None:
            cfa = self.runner.locked(self.frame_cfa)
            if cfa is not None:
                return cfa > cfa0
        return self.symbols.function_at(now.pc) != function
...
# stepping.py:332-333
            elif self.symbols.function_at(now.pc) != function:
                return Halt("arrived", now)
```

末尾の呼び出し等と区別できない推定。

### ?68. symbols.py:243-253 — ビルドした場所に無いソースを、ELF の隣の同じ名前のファイルとみなす

```python
# symbols.py:243-253
    def _relocate_sources(self) -> None:
        """ビルドした場所に無いソースが ELF の隣にあれば、そちらを使う（ELF とソースを一緒に別の場所へ移したとき。
        例: リポジトリを別の PC・場所に置いた同梱のデモ）。"""
        moved = {}
        for name, paths in self._source_paths.items():
            here = self.path.parent / name
            if len(paths) == 1 and not paths[0].exists() and here.is_file():   # 同じ名前が複数ならどれとも決めない
                moved[paths[0]] = here
        if moved:
            self._set_lines([(a, f, line, moved.get(p, p)) for a, f, line, p in self._lines],
                            {name: [moved.get(p, p) for p in paths] for name, paths in self._source_paths.items()})
```

ファイル名が同じなら同じソースという仮定。置き換えたことを印として返さない（sources() の場所が変わるだけ）。

### ?69. symbols.py:313-318 — "ファイル:行" の番地を、その行の複数の番地の最小にする

```python
# symbols.py:313-318
    def _line_address(self, file: str, line: int) -> int:
        path = self._source_for(file)
        hits = [address for address, _, n, p in self._lines if n == line and p == path]
        if not hits:
            raise KeyError(f"{file}:{line} に対応する命令が無い（最適化で消えたか、行が違う）")
        return min(hits)
```

行が複数の所（ループ・インライン展開）に分かれていても 1 つを選び、ほかの候補を返さない。GDB の慣習に近い決めごと。

### ?70. timeline_fpga.py:29-34 — 壊れた信号の定義を黙って空として扱う

```python
# timeline_fpga.py:29-34
def signals_of(rec: Any) -> FpgaSignals:
    """記録の情報の信号の定義（無い・壊れていれば空 = すべての組を組の値の段 1 本で）。"""
    try:
        return FpgaSignals.from_dict(rec.meta.get("fpga.signals"))
    except ValueError:
        return FpgaSignals({})
```

ValueError を握りつぶし、組の値の段 1 本で見せる。定義が壊れていたことがタイルに出ない。

### ?71. timeline_rows.py:170, 208-210 — 状態に良し悪しの色の意味を付ける

```python
# timeline_rows.py:169-170
        Rule("dap", "ap", "points", ("ap_read", "ap_write", "inject"), label=_ap_label, address=lambda e: e.a, jump="memory",
             tone=lambda e: "bad" if _body(e, "ack", 1) != 1 else "warn" if _dhcsr_action(e) == "halt" else "normal"),
...
# timeline_rows.py:208-210
    def state_tone(e: Event) -> str:
        s = _body(e, "state")
        return {"RUNNING": "good", "HALTED": "warn", "DISCONNECTED": "bad", "LOCKUP": "bad"}.get(s, "normal")
```

RUNNING=good・HALTED=warn・DHCSR の halt の書き込み=warn 等。画面の材料だが、本体（共通タイムラインの規則）が良し悪しを決めている。

### ?72. timesync.py:16-17 — 非同期の辺を取り込む位相を平均の定数で引く

```python
# timesync.py:16-17
# 子の時刻 → 線の辺: 回路の遅れ（周期）と、非同期の辺を取り込む位相の平均
_RX = g.SYNC_RX_LATENCY_TICKS + g.SYNC_RX_PHASE_TICKS
```

SYNC_RX_PHASE_TICKS は「位相の平均」で、個々の組の実際の位相ではない。量子化 1 刻みは別に返す。

### ?73. usertrace.py:290-308 — 対の無い stop を「窓の始めで切れた」「並びの崩れ」に分ける

```python
# usertrace.py:290-292
    unmatched_stops: dict[str, int] = field(default_factory=dict)     # 対の無い stop の数（並びの崩れ）
    open: dict[str, int] = field(default_factory=dict)                # 終わりの無い start の数（窓の終わりで切れた）
    started_before: dict[str, int] = field(default_factory=dict)      # 最初の start より前の stop（窓の始めで切れた）
...
# usertrace.py:304-308
        elif r.kind == "stop":
            stack = stacks.get(r.name)
            if not stack:
                counts = out.unmatched_stops if r.name in started else out.started_before
                counts[r.name] = counts.get(r.name, 0) + 1
```

事実は「同じ名前の start が前に来ていたか」だけで、欄の名前・注釈が原因の解釈になっている。

### ?74. wiring.py:88-94 — DPIDR が読めた最初のピンの組を返す

```python
# wiring.py:88-94
def find_swd_pins(dev: Device, pins: int = PINS) -> SwdPins | None:
    """全ての (SWCLK, SWDIO) の組を試し、DPIDR が読めた組を返す。"""
    for swclk, swdio in permutations(range(pins), 2):
        dpidr = read_dpidr(dev, swclk, swdio)
        if dpidr is not None:
            return SwdPins(swclk, swdio, dpidr)
    return None
```

ほかの組でも読めるか（複数のターゲット等）を調べず、1 つ目で決める。

### ?75. xtrig.py:321 — 読み戻した未定義のモード値を WHILE に丸める

```python
# xtrig.py:321-321
                mode=OutputMode(min(_field(ctrl, g.XTRIG_OUT_CTRL0_MODE), 2)),
```

min(..., 2) でレジスタの値 3 を WHILE と見せる。読んだ事実と違う値を返す。

## 読んだファイル（132 個）

以下のすべてを頭から最後まで全部読んだ。上に挙げたもの以外に H・? は無かった。

- __init__.py
- __main__.py
- arm11_debug.py
- arm_a_target.py
- arm_debug.py
- armv7a_debug.py
- aux_flash.py
- aux_mcu.py
- bitstreams.py
- callgraph.py
- calling.py
- caps.py
- code_at.py
- collector.py
- connect_scripts.py
- control.py
- coresight_id.py
- correlate.py
- debug.py
- device.py
- diagnose.py
- disasm.py
- elfsize.py
- eth_link.py
- events.py
- fault.py
- flash.py
- flash_bp.py
- fpga_obs.py
- fpga_signals.py
- frame_link.py
- generated.py
- gpio.py
- halt_risk.py
- heap.py
- hooks.py
- inject.py
- itm.py
- jtag_chain.py
- la.py
- la_measure.py
- liveness.py
- mcp_server.py
- memory.py
- memview.py
- merge.py
- monitor.py
- multidrop.py
- obs_condition.py
- packs.py
- probe_analysis.py
- probe_breakpoints.py
- probe_connect.py
- probe_cycles.py
- probe_decode.py
- probe_design.py
- probe_elf_files.py
- probe_elf_watch.py
- probe_etm.py
- probe_execution.py
- probe_flash.py
- probe_flash_bp.py
- probe_formats.py
- probe_gdb.py
- probe_gdb_trace.py
- probe_gpio.py
- probe_heap.py
- probe_hooks.py
- probe_inspect.py
- probe_linux.py
- probe_longrec.py
- probe_lowlevel.py
- probe_monitor.py
- probe_multicore.py
- probe_names.py
- probe_observe.py
- probe_profile.py
- probe_profile_export.py
- probe_rtos.py
- probe_rtos_analysis.py
- probe_rules.py
- probe_safety.py
- probe_snapshot.py
- probe_stack.py
- probe_streams.py
- probe_swo.py
- probe_terminal.py
- probe_testpoint.py
- probe_timeline.py
- probe_ui_state.py
- probe_user.py
- probe_watch.py
- probe_window.py
- probe_xtrig_out.py
- pyocd_targets.py
- raw_dap.py
- recording.py
- recording_long.py
- recording_sql.py
- recording_stream.py
- recording_summary.py
- riscv_dm.py
- riscv_target.py
- rtt.py
- rules.py
- run.py
- sampler.py
- semihosting.py
- serial_link.py
- session.py
- sigrok.py
- snapshot.py
- spi.py
- stackusage.py
- stepping.py
- stm32_option_bytes.py
- streams.py
- svd.py
- swo_trace.py
- symbols.py
- thumb_step.py
- timeline.py
- timeline_fpga.py
- timeline_rows.py
- timesync.py
- transport.py
- uart_flow.py
- usertrace.py
- watch_buffer.py
- watch_trigger.py
- wiring.py
- xtrig.py
