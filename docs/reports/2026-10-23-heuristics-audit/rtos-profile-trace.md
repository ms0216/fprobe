# ヒューリスティックの監査: rtos・profile・trace・tracefmt（2026-10-23）

対象: `host/fprobe/rtos`・`host/fprobe/profile`・`host/fprobe/trace`・`host/fprobe/tracefmt` の下の `.py` すべて。
コードは変更していない。基準は CLAUDE.md の「fprobe 本体にヒューリスティックを入れない」。
過去の台帳 `.superpowers/sdd/heuristics-ledger.md` で済・Ruling とされたものも今のコードで見直した（該当する所に書く）。

## 0. 結論の要約

検索で拾った行（重複を除く）: **901 行**。分類の内訳: H 21・? 39・F 643・E 35・O 83・D 80。

件（同じ原因の行をまとめた数）で見ると:

- **H 11 件**: (1)〜(8) 辿る数の暗黙の上限に達したら「壊れている／大きすぎる」と断定する（8 か所）、(9) FreeRTOS の待ちリストの持ち主をキューらしさの閾値で決める、(10) ETM の再構成で分岐の無い命令 65536 個で「像が違う」とみなす、(11) Tracealyzer の表の形を閾値で拒否し「版が違う」と原因を書く。
- **? 15 件**: 黙った打ち切り（欠けの表示なし）5 件、優先度の向きの既定 order=1、名前による idle の判定、prefix の名前の規則、aliasing の注記の信頼度の既定、例外の頻度の既定 0、スタックの fill_above の暗黙の範囲、ETMv4 の構成の既定、台帳の Ruling で残したもの 3 件（counter_delta・メッセージ長・TraceX の切り替え）。

## 1. 実行した検索

`rg -n -t py <パターン> rtos profile trace tracefmt`（`host/fprobe` で実行）。件数は行の数。機械的に除外できる件数も数えたが、**除外した行も含めて全 901 行を 1 行ずつ分類した**（3 章）。

| # | パターン | ヒット | 機械的に除外できる数と理由 |
|---|---|---|---|
| 1a | `[<>]=?\s*-?\d*\.?\d+` | 137 | 113（ビットシフト `<<`・`>>` だけ 55、0・1 との比較だけ 58）。長さの 0 比較 0。残り 24 |
| 1b | `-?\d*\.?\d+\s*[<>]=?` | 79 | 70（ビットシフトだけ 42、0・1 だけ 28）。長さの 0 比較 0。残り 9 |
| 2a | `\b\d+\.\d+\b` | 47 | 24（0.0・1.0・0.5 だけ）。うち説明文の版数・節番号 17 程度 |
| 2b | `\*\s*\d+(\.\d+)?\b` | 9 | 1（`* 100` の百分率）。単位換算 1e3/1e6 は 0 |
| 3a | `:\s*\w+[^,)]*=\s*-?\d` | 67 | 0（すべて分類） |
| 3b | `=\s*-?\d+(\.\d+)?\s*[,)]` | 52 | 0 |
| 3c | `^[A-Z_]+\s*=\s*` | 199 | 0 |
| 4 | `` in \(| in \{| in \[|startswith|endswith|re\.(match|search)`` | 112 | 0 |
| 5 | `sorted\(|\.sort\(|max\(|min\(|\[:\d+\]|most_common` | 152 | 0（内訳: min/max を含む 80・sorted/sort 64・`[:n]` 7・most_common 1） |
| 6 | `推定|経験|目安|典型|typical|guess|heuristic|likely|probably|assume|みなす|とみなし|らしい|疑い|suspect|verdict|healthy|risk|warn|idle`（-i） | 152 | 0（内訳: idle 80・warn 51（ほぼ warnings）・推定 14・経験 3・みなす 2・とみなし 1・目安 1、他 0） |
| 7 | `timeout|retry|retries|sleep\(`（-i） | 21 | 0（内訳: timeout 20（ほぼ RTOS・形式の欄名）・sleep( 1、retry 0） |

重複を除いた行: 901（複数の検索に当たった行は 1 行として分類）。

## 2. H と ? の全件

### 2.1 H（ヒューリスティック）

| ファイル:行 | コード | 何をしているか・なぜ H か |
|---|---|---|
| rtos/base.py:19 | `MAX_CHAIN = 4096                  # 辿る列の長さの上限（壊れたリストでも止まる）` | MAX_CHAIN=4096: 上限に達したら列を『壊れている』と判定（130 行） |
| rtos/base.py:130 | `self.warnings.append(f"{what} の列が壊れている（{len(seen)} 個で止めた）")` | 上限で止めたときも『壊れている』と断定する文言（MAX_CHAIN） |
| rtos/freertos.py:29 | `MAX_LIST_ITEMS = 256              # 壊れたリストでも止まる` | MAX_LIST_ITEMS=256: 項目が 256 を超えるリストを『壊れている』と判定（108-109 行） |
| rtos/freertos.py:108 | `if item in seen or len(out) >= min(count, MAX_LIST_ITEMS) or item == 0:` | min(count, MAX_LIST_ITEMS) で止め、上限でも『壊れている』と書く |
| rtos/freertos.py:109 | `self.warnings.append(f"リスト 0x{lst:08X} が壊れている（{len(out)} 項目で止めた。数は {count}）")` | 上限で止めたときも『壊れている』と断定する文言 |
| rtos/freertos_objects.py:40 | `MAX_FREE_BLOCKS = 4096` | MAX_FREE_BLOCKS=4096: 上限で『空きのリストが壊れている』と判定（383-384 行） |
| rtos/freertos_objects.py:148 | `kind_ok = not q.has("ucQueueType") or self.mem.u8(h + q.offset("ucQueueType")) <= 4` | _plausible_queue: ucQueueType <= 4 でキューらしいと判定（持ち主の分からない待ちリストをキューと決める根拠の 1 つ） |
| rtos/freertos_objects.py:149 | `lists_ok = all(self.mem.u32(h + q.offset(f"{p}.uxNumberOfItems")) < 256` | _plausible_queue: 待ちリストの項目数 < 256 でキューらしいと判定 |
| rtos/freertos_objects.py:153 | `return 0 < length <= 0x10000 and messages <= length and item_size <= 0x10000 and kind_ok and lists_ok` | _plausible_queue: 0 < 長さ <= 0x10000・項目の大きさ <= 0x10000 でキューらしいと判定 |
| rtos/nuttx.py:33 | `MAX_PIDHASH = 4096` | MAX_PIDHASH=4096: これを超える g_npidhash を『大きすぎる』と判定して切り詰める（197-199 行） |
| rtos/nuttx.py:198 | `self.warnings.append(f"g_npidhash が大きすぎる（{n}。{MAX_PIDHASH} まで読む）")` | 『大きすぎる』の判定の文言（暗黙の上限 4096 による） |
| rtos/rtx5.py:33 | `MAX_THREADS = 256` | MAX_THREADS=256: 上限で『リストが壊れている』と判定（121-122 行）。221 行は黙って None |
| rtos/rtx5.py:122 | `self.warnings.append(f"{what} のリストが壊れている（{len(out)} 個で止めた）")` | 上限で止めたときも『壊れている』と断定する文言 |
| rtos/rtx5_objects.py:26 | `MAX_ITEMS = 4096` | MAX_ITEMS=4096: 上限で『列が壊れている』と判定（93-94 行） |
| rtos/rtx5_objects.py:94 | `self.rtos.warnings.append(f"{what} の列が壊れている（{len(out)} 個で止めた）")` | 上限で止めたときも『壊れている』と断定する文言 |
| rtos/threadx.py:33 | `MAX_ITEMS = 4096` | MAX_ITEMS=4096: 上限で『環が壊れている』と判定（154-155 行） |
| rtos/threadx.py:155 | `self.warnings.append(f"{what} の環が壊れている（{len(seen)} 個で止めた）")` | 上限で止めたときも『壊れている』と断定する文言 |
| rtos/zephyr.py:24 | `MAX_THREADS = 256` | MAX_THREADS=256: 上限で『スレッドのリストが壊れている』と判定（62-63 行） |
| rtos/zephyr.py:63 | `self.warnings.append(f"スレッドのリストが壊れている（{len(out)} 個で止めた）")` | 上限で止めたときも『壊れている』と断定する文言 |
| trace/reconstruct.py:33 | `WALK_LIMIT = 1 << 16             # 分岐の無いまま進む命令の数の上限（越えたら像が違うとみなして止める）` | WALK_LIMIT=65536: 分岐の無い命令がこれを超えたら『像が違う』とみなして lost にする（298-300 行） |
| tracefmt/tracealyzer.py:136 | `if states > 16 or symbol_size > 256 or count > 4096:` | states>16・symbol_size>256・count>4096 で表を拒否し『見出しの版が違う』と原因を書く |

補足（検索に当たらなかった関連の行）:

- 上限で止めて「壊れている」と書く行の本体: `rtos/base.py:129-130`・`rtos/rtx5.py:121`・`rtos/threadx.py:154`・`rtos/zephyr.py:62`・`rtos/rtx5_objects.py:93`・`rtos/freertos_objects.py:383-384`・`rtos/nuttx.py:197-199`。巡回（同じ節を 2 度見た）・数の不一致での「壊れている」は事実なので F。問題は**上限に達しただけ**でも同じ文言になる点（長い正当なリストを壊れていると誤って伝える）。上限で止めたことを事実として（「N 個で読み止めた」）書き分ければ H でなくなる。
- `rtos/freertos_objects.py:121-135`（`_from_wait_list`）: 上の `_plausible_queue` が真なら待ちリストの番地からキューと決め、`source = "waiter"` で返す。キューと確かめていない旨の note は無い（キューでもイベントグループでもないときだけ note がある）。`_plausible_group`（`:155-166`）は vListInitialise の印（portMAX_DELAY）で照合するので F 寄り。
- `trace/reconstruct.py:282-300`: `walk_limit` は引数で変えられるが既定は WALK_LIMIT。超えると `lost` の印と「プログラムの像が違う？」の原因の推測の文言。

### 2.2 ?（判断がつかない・使う側の判断に関わるかもしれない）

| ファイル:行 | コード | 何をしているか・理由 |
|---|---|---|
| profile/aggregate.py:7 | `timeline(samples, profiler, bucket=1000, grouping=Grouping("prefix"))   # 区切りごとの負荷とグループの割合` | 例の prefix の分け方（prefix_of の名前の規則。下の 30 行と同じ件） |
| profile/aggregate.py:30 | `BY = ("function", "file", "directory", "prefix", "groups")` | BY の "prefix": prefix_of（78-86 行）が名前の最初の _ までを『名前の型』とする命名の慣習の仮定 |
| profile/aliasing.py:14 | `def aliasing_notes(sampled: list[dict], traced: list[dict], total: int, confidence: float = 0.95) -> list[str]…` | confidence=0.95 の既定で『信頼区間の外』の注記を出すか決める（既定の暗黙の境界値） |
| profile/cycles.py:85 | `def counter_delta(start: int, end: int, overflows: int \| None = None, bits: int = 8) -> int:` | counter_delta: 溢れを数えないとき 1 周未満とみなす（台帳の Ruling で残す。counters_exact で明示） |
| profile/cycles.py:87 | `無ければ 1 周未満とみなす。数えた溢れと値が合わない（パケットが欠けた）ときも 1 周未満の分だけ。"""` | 同上の説明文（1 周未満とみなす） |
| profile/dwt.py:121 | `exception_trace: bool = False, exception_rate_hz: float = 0, timestamps: bool = False,` | exception_rate_hz=0 の既定: 例外トレースを有効にしても帯域の見積もりは例外 0 回とし、fits・警告を決める |
| rtos/analysis.py:467 | `priorities: Mapping[int, int], order: int = 1, names: Mapping[int, str] \| None = None,` | order=1 の既定（優先度の向き）。同上 |
| rtos/contention.py:17 | `def contention(tasks: list[Task], objects: list[KernelObject], order: int = 1) -> dict:` | order=1 の既定（優先度の向き）。呼び出し側は渡すが、省くと FreeRTOS の向きを仮定 |
| rtos/flow.py:313 | `def inversions_from_ops(tl: Timeline, trace: KernelTrace, priorities: Mapping[int, int], order: int = 1,` | order=1 の既定（優先度の向き）。同上 |
| rtos/freertos_objects.py:38 | `MESSAGE_LENGTH_BYTES = 4                                    # configMESSAGE_BUFFER_LENGTH_TYPE の既定（size_t）` | メッセージの長さの語 = size_t 4 バイトの仮定（台帳の Ruling で残す。note で明示） |
| rtos/freertos_objects.py:39 | `MAX_MESSAGES = 1024` | MAX_MESSAGES=1024: メッセージを 1024 個で黙って数え止める（欠けの表示なし） |
| rtos/objects.py:24 | `MAX_DEPTH = 4                    # 構造体・配列の入れ子をこの深さまで` | MAX_DEPTH=4: 入れ子 4 段より深いオブジェクトは黙って見落とす（欠けの表示なし） |
| rtos/objects.py:25 | `MAX_ELEMENTS = 64                # 配列の要素をこの数まで` | MAX_ELEMENTS=64: 配列の 65 番目以降のオブジェクトを黙って見落とす |
| rtos/objects.py:26 | `MAX_POINTERS = 4096              # 読むハンドル（ポインタ）の数` | MAX_POINTERS=4096: ハンドルを 4096 個で黙って読み止める |
| rtos/objects.py:104 | `for i in range(min(s.length, max_elements)):` | max_elements で配列を黙って切る（上の MAX_ELEMENTS） |
| rtos/stackcheck.py:17 | `CHUNK = 256                       # 1 回に読むバイト数` | CHUNK=256: 読みの単位だが、fill_above を探す範囲（底 +16〜256 B）の上端も決める（_sample 69 行） |
| rtos/stackcheck.py:18 | `GUARD = 16                        # 底のこのバイト数が塗りのままかを見る（FreeRTOS の方法 2 と同じ幅）` | GUARD=16: 底の 16 B が塗りかで fill=intact/none を決める（FreeRTOS の方法 2 の幅）。幅は結果に出ない |
| rtos/stackcheck.py:54 | `needle = (pattern * 4)[:max(4, len(pattern))]` | fill_above は塗りが 4 バイト以上続くかで決める（暗黙の 4 バイト。GUARD・CHUNK と同じ件） |
| rtos/stackcheck.py:69 | `hi = min(pointer, base + CHUNK)` | fill_above を探す範囲の上端 base+CHUNK（暗黙の 256 B） |
| rtos/zephyr_objects.py:22 | `MAX_ITEMS = 4096` | MAX_ITEMS=4096: k_queue の items を 4096 で黙って数え止める（155 行）。142 行は None を返す（O） |
| trace/etmv4.py:34 | `vmid_bytes: int = 1` | Etmv4Config の既定 vmid_bytes=1（TRCIDR から渡さないと解読の前提が暗黙） |
| trace/etmv4.py:35 | `cid_bytes: int = 4` | Etmv4Config の既定 cid_bytes=4（同上） |
| tracefmt/ctf.py:267 | `if is_rtos_idle_name(info.name):` | 名前で idle を決める利用箇所（tracefmt/model.py:28 と同じ件） |
| tracefmt/ctf.py:268 | `self.trace.idle.add(task)` | 名前で idle を決める利用箇所（tracefmt/model.py:28 と同じ件） |
| tracefmt/eventrecorder.py:223 | `if is_rtos_idle_name(info.name):` | 名前で idle を決める利用箇所（tracefmt/model.py:28 と同じ件） |
| tracefmt/eventrecorder.py:224 | `trace.idle.add(handle)` | 名前で idle を決める利用箇所（tracefmt/model.py:28 と同じ件） |
| tracefmt/model.py:28 | `_IDLE_NAME = re.compile(r"IDLE\d*\|idle( \d+)?\|osRtxIdleThread")` | アイドルの判定をタスク名（IDLE\d*・idle( \d+)?・osRtxIdleThread の完全一致）でする。台帳 f8b00c3 で RTOS の既定の名前として残したが、configIDLE_TASK_NAME は変えられ、同名の利用者のタスクも idle になる |
| tracefmt/model.py:31 | `def is_rtos_idle_name(name: str) -> bool:` | 名前で idle を決める利用箇所（tracefmt/model.py:28 と同じ件） |
| tracefmt/model.py:33 | `return bool(_IDLE_NAME.fullmatch(name))` | 名前で idle を決める（tracefmt/model.py:28 と同じ件） |
| tracefmt/scvd.py:29 | `LIST_LIMIT = 1024                         # readlist・list の回数の上限（壊れたリストで止まらないように）` | LIST_LIMIT=1024: readlist・list・loop を 1024 回で黙って止める（欠けの表示なし） |
| tracefmt/scvd.py:808 | `addresses = [base + i * size for i in range(min(count if count is not None else 1, LIST_LIMIT))]` | count を LIST_LIMIT で黙って切る |
| tracefmt/stream.py:36 | `SNAPSHOT_LIMIT = 4 << 20` | SNAPSHOT_LIMIT=4 MiB: ctf の ram_tracing を黙って切り詰め（121 行）、TraceX の大きさを『合わない』と判定（134 行） |
| tracefmt/stream.py:121 | `data = read(start, min(length, SNAPSHOT_LIMIT))` | ram_tracing を SNAPSHOT_LIMIT で黙って切り詰める（info に元の長さが無い） |
| tracefmt/stream.py:134 | `if not 0 < size <= SNAPSHOT_LIMIT:` | 4 MiB を超える TraceX のバッファを『大きさが合わない』と拒否 |
| tracefmt/systemview.py:276 | `if is_rtos_idle_name(args[2]):` | 名前で idle を決める利用箇所（tracefmt/model.py:28 と同じ件） |
| tracefmt/systemview.py:277 | `self.trace.idle.add(task)` | 名前で idle を決める利用箇所（tracefmt/model.py:28 と同じ件） |
| tracefmt/tracealyzer.py:197 | `if info.name and is_rtos_idle_name(info.name):` | 名前で idle を決める利用箇所（tracefmt/model.py:28 と同じ件） |
| tracefmt/tracealyzer.py:198 | `self.trace.idle.add(task)` | 名前で idle を決める利用箇所（tracefmt/model.py:28 と同じ件） |
| tracefmt/tracex.py:128 | `if ctx not in (ISR_CONTEXT, INIT_CONTEXT) and ctx != running and isr_depth == 0:` | 文脈が変わった所をスレッドの切り替えとみなす（inferred: true・note あり。台帳の Ruling で残す） |

補足:

- **黙った打ち切り**（`rtos/objects.py` の MAX_*・`rtos/zephyr_objects.py:155`・`tracefmt/scvd.py` の LIST_LIMIT・`rtos/freertos_objects.py:318` の MAX_MESSAGES・`tracefmt/stream.py:121`）は閾値による判定ではないが、原則の「欠けている所」を出していない。上限に達したことを結果に書けば O になる。
- **order=1 の既定**: 本体の呼び出し（`probe_rtos.py:210,284`・`probe_rtos_analysis.py:171`）は `kernel.priority_order` を渡しているので今は正しく動く。省いたときに FreeRTOS の向き（大きい方が高い）を黙って仮定する。ThreadX・uC/OS-III・Zephyr は逆向き。既定を外して必須にすれば E。
- **idle の名前**（`tracefmt/model.py:28`）: 台帳 f8b00c3 で「RTOS が定める名前の完全一致」として残した。今のコードでも完全一致（`fullmatch`）だが、FreeRTOS の `configIDLE_TASK_NAME` は設定で変えられ、利用者が `idle`・`IDLE2` という名前のタスクを作ればアイドルにされる。結果に「名前で決めた」ことが出ているかは呼び出し側（範囲外）で要確認。
- **台帳の Ruling で残したもの**（`profile/cycles.py:85`・`rtos/freertos_objects.py:38`・`tracefmt/tracex.py:128`）: いずれも結果に明示（counters_exact（probe_cycles.py:133）・note（freertos_objects.py:327）・inferred: true と note（tracex.py:129,160））されていることを今のコードで確かめた。原則の「仮定に頼る推定」ではあるので ? に残し、判断は台帳の Ruling に従う。
- **ETMv4 の構成の既定**（`trace/etmv4.py:33-35`。33 行の commit_opt1=True は検索に当たらなかった）: `Etmv4Info.decoder_config`（TRCIDR から）を渡せば事実になる。渡さないと黙って既定で解読する。

### 2.3 台帳で「済」とされたものの再確認

- `rtos/stackcheck.py`（a853e97）: overflow=True の断定は消えている（`overflow` は SP < base の事実だけ）。ただし `fill_above` の範囲（底 +16〜256 B、4 バイト以上の塗り）は暗黙の値のまま（2.2 の ?）。
- `rtos/timeline.py`（gap の倍率）: 既定は抜けにしない（`gap_factor=None`）。確認済み。
- `rtos/analysis.py`（ジョブの推定）: `find_jobs` は出来事のあるタスクだけ、`run_summary` は followed_by の事実だけ。確認済み。
- `rtos/flow.py`（失敗を timeout と推定・SWO の 8 割の注記・cpu_share）: `outcome` は ok / failed だけ、`overhead` は語数と帯域の使用率だけ（CPU の負荷は推定しない note）。確認済み。
- `profile/dwt.py`（headroom 0.75）: 既定 1.0（帯域の上限そのもの）。確認済み。
- `profile/aliasing.py`（4.6 回・係数 2）: 消えて、信頼区間の外かどうかだけ。ただし信頼度の既定 0.95（2.2 の ?）。
- `profile/pcprofile.py`（WFI の次を眠り）: `sleep` はコアの印（PC の無いサンプル）だけ、PC が WFI/WFE の上は `wfi` に別に数える。確認済み。
- `profile/stats.py`（1.5 IQR）: 外れ値の判定は無い（分位・ヒストグラムだけ）。確認済み。

## 3. F・E・O・D の列挙（ファイル:行 — 一言）

分類の意味: F = 事実・仕様で決まる規則、E = 使う側が明示して渡す引数、O = 操作の既定、D = 見せ方。説明文（docstring・コメント）は判定を含まないので F に入れ「説明文」と書いた。

### 3.1 F（事実・仕様） — 643 行

- profile/aggregate.py:53 — 形式・RTOS の定める種類・コードでの分岐
- profile/aggregate.py:68 — 形式の構文の解析（接頭辞・正規表現）
- profile/aggregate.py:124 — 割合（0 除算を避ける）
- profile/aggregate.py:125 — 割合（0 除算を避ける）
- profile/aggregate.py:137 — 引数・値の 0 との比較（正であることの検査・符号）
- profile/aggregate.py:159 — 割合（0 除算を避ける）
- profile/aggregate.py:160 — 割合（0 除算を避ける）
- profile/aggregate.py:179 — 0・1 で切る／0 除算を避ける算術（区間の重なり・個数）
- profile/aggregate.py:186 — 呼び出しの区間の入れ子・時間の窓の比較（区間の算術）
- profile/aggregate.py:197 — 呼び出しの区間の入れ子・時間の窓の比較（区間の算術）
- profile/aliasing.py:4 — 説明文・コメント（判定を含まない）
- profile/aliasing.py:6 — 説明文・コメント（判定を含まない）
- profile/aliasing.py:25 — 測った値の最大・最小・区間の端（事実）
- profile/coverage.py:21 — 形式・RTOS・規格の定数（名前・コードの表）
- profile/coverage.py:37 — 割合（0 除算を避ける）
- profile/coverage.py:41 — 割合（0 除算を避ける）
- profile/coverage.py:69 — 割合（0 除算を避ける）
- profile/coverage.py:82 — 引数・値の 0 との比較（正であることの検査・符号）
- profile/cycles.py:1 — 説明文（docstring・文言）
- profile/cycles.py:7 — 説明文（規格・出典・推定しないことの記述）
- profile/cycles.py:19 — CYCCNT は 32 ビット（ARMv7-M）
- profile/cycles.py:23 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- profile/cycles.py:33 — 説明文（規格・出典・推定しないことの記述）
- profile/cycles.py:48 — 長さの検査（形式の欄の数・バイト数が足りるか）
- profile/cycles.py:49 — 最小二乗の傾きと標準誤差（統計の定義。3 点要るのは自由度 n-2 のため）
- profile/cycles.py:56 — 最小二乗の傾きと標準誤差（統計の定義。3 点要るのは自由度 n-2 のため）
- profile/cycles.py:63 — 最小二乗の傾きと標準誤差（統計の定義。3 点要るのは自由度 n-2 のため）
- profile/cycles.py:67 — 読んだ値の事実を warnings に書く（下の H に挙げたもの以外）
- profile/cycles.py:77 — 引数・値の 0 との比較（正であることの検査・符号）
- profile/cycles.py:79 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- profile/cycles.py:88 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- profile/cycles.py:91 — 引数・値の 0 との比較（正であることの検査・符号）
- profile/cycles.py:106 — 形式・RTOS の定める種類・コードでの分岐
- profile/dwt.py:19 — レジスタの番地・ビット・形式の定数（規格・RTOS のソース）
- profile/dwt.py:20 — レジスタの番地・ビット・形式の定数（規格・RTOS のソース）
- profile/dwt.py:21 — レジスタの番地・ビット・形式の定数（規格・RTOS のソース）
- profile/dwt.py:22 — レジスタの番地・ビット・形式の定数（規格・RTOS のソース）
- profile/dwt.py:23 — レジスタの番地・ビット・形式の定数（規格・RTOS のソース）
- profile/dwt.py:24 — レジスタの番地・ビット・形式の定数（規格・RTOS のソース）
- profile/dwt.py:25 — レジスタの番地・ビット・形式の定数（規格・RTOS のソース）
- profile/dwt.py:26 — レジスタの番地・ビット・形式の定数（規格・RTOS のソース）
- profile/dwt.py:27 — レジスタの番地・ビット・形式の定数（規格・RTOS のソース）
- profile/dwt.py:28 — レジスタの番地・ビット・形式の定数（規格・RTOS のソース）
- profile/dwt.py:29 — レジスタの番地・ビット・形式の定数（規格・RTOS のソース）
- profile/dwt.py:30 — レジスタの番地・ビット・形式の定数（規格・RTOS のソース）
- profile/dwt.py:32 — レジスタの番地・ビット・形式の定数（規格・RTOS のソース）
- profile/dwt.py:35 — レジスタの番地・ビット・形式の定数（規格・RTOS のソース）
- profile/dwt.py:36 — 形式の決まったバイト数・語数の切り出し
- profile/dwt.py:37 — 形式の決まったバイト数・語数の切り出し
- profile/dwt.py:38 — レジスタの番地・ビット・形式の定数（規格・RTOS のソース）
- profile/dwt.py:39 — 形式の決まったバイト数・語数の切り出し
- profile/dwt.py:40 — レジスタの番地・ビット・形式の定数（規格・RTOS のソース）
- profile/dwt.py:41 — レジスタの番地・ビット・形式の定数（規格・RTOS のソース）
- profile/dwt.py:42 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- profile/dwt.py:43 — 形式・RTOS・規格の定数（名前・コードの表）
- profile/dwt.py:46 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- profile/dwt.py:47 — レジスタの番地・ビット・形式の定数（規格・RTOS のソース）
- profile/dwt.py:49 — レジスタの番地・ビット・形式の定数（規格・RTOS のソース）
- profile/dwt.py:50 — 形式・RTOS・規格の定数（名前・コードの表）
- profile/dwt.py:53 — 形式・RTOS・規格の定数（名前・コードの表）
- profile/dwt.py:54 — 形式・RTOS・規格の定数（名前・コードの表）
- profile/dwt.py:55 — 形式・RTOS・規格の定数（名前・コードの表）
- profile/dwt.py:56 — 形式・RTOS・規格の定数（名前・コードの表）
- profile/dwt.py:64 — DWT_PCSR の比較のマスク（全ビット）
- profile/dwt.py:108 — ローカルのタイムスタンプのパケットの大きさ（差 ≤ 6 は 1 バイト。ARMv7-M D4.2.4）
- profile/dwt.py:110 — 測った値の最大・最小・区間の端（事実）
- profile/dwt.py:123 — 説明文（docstring・文言）
- profile/dwt.py:126 — 引数・値の 0 との比較（正であることの検査・符号）
- profile/dwt.py:134 — 読んだ値の事実を warnings に書く（下の H に挙げたもの以外）
- profile/dwt.py:136 — 読んだ値の事実を warnings に書く（下の H に挙げたもの以外）
- profile/dwt.py:157 — 読んだ値の事実を warnings に書く（下の H に挙げたもの以外）
- profile/dwt.py:159 — 読んだ値の事実を warnings に書く（下の H に挙げたもの以外）
- profile/dwt.py:191 — 引数・値の 0 との比較（正であることの検査・符号）
- profile/dwt.py:195 — 引数・値の範囲の検査（規格・形式の範囲）
- profile/dwt.py:196 — 0・1 で切る／0 除算を避ける算術（区間の重なり・個数）
- profile/dwt.py:197 — 読んだ値の事実を warnings に書く（下の H に挙げたもの以外）
- profile/dwt.py:200 — 引数・値の 0 との比較（正であることの検査・符号）
- profile/dwt.py:201 — 読んだ値の事実を warnings に書く（下の H に挙げたもの以外）
- profile/dwt.py:214 — 引数・値の範囲の検査（規格・形式の範囲）
- profile/dwt.py:220 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- profile/dwt.py:223 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- profile/dwt.py:224 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- profile/dwt.py:228 — 説明文・コメント（判定を含まない）
- profile/dwt.py:249 — 引数・値の 0 との比較（正であることの検査・符号）
- profile/dwt.py:263 — 読んだ値の事実を warnings に書く（下の H に挙げたもの以外）
- profile/exceptions.py:17 — 形式・RTOS・規格の定数（名前・コードの表）
- profile/exceptions.py:27 — 例外の番号 16 以上 = IRQ（ARMv7-M の規格）
- profile/exceptions.py:146 — 測った値の最大・最小・区間の端（事実）
- profile/pcprofile.py:25 — 形式・RTOS・規格の定数（名前・コードの表）
- profile/pcprofile.py:26 — 形式・RTOS・規格の定数（名前・コードの表）
- profile/pcprofile.py:27 — 形式・RTOS・規格の定数（名前・コードの表）
- profile/pcprofile.py:67 — 引数・値の 0 との比較（正であることの検査・符号）
- profile/pcprofile.py:148 — 割合（0 除算を避ける）
- profile/pcprofile.py:151 — 割合（0 除算を避ける）
- profile/pcprofile.py:199 — 説明文（規格・出典・推定しないことの記述）
- profile/stats.py:14 — 引数・値の範囲の検査（規格・形式の範囲）
- profile/stats.py:16 — 信頼区間の z 値・端（Wilson 区間の定義）
- profile/stats.py:22 — 引数・値の範囲の検査（規格・形式の範囲）
- profile/stats.py:25 — 信頼区間の z 値・端（Wilson 区間の定義）
- profile/stats.py:31 — 0・1 で切る／0 除算を避ける算術（区間の重なり・個数）
- profile/stats.py:32 — 0・1 で切る／0 除算を避ける算術（区間の重なり・個数）
- profile/stats.py:48 — 測った値の最大・最小・区間の端（事実）
- profile/stats.py:52 — 測った値の最大・最小・区間の端（事実）
- profile/stats.py:65 — 中央値 = 0.5 分位（定義）
- profile/stats.py:71 — 引数・値の範囲の検査（規格・形式の範囲）
- profile/stats.py:75 — 測った値の最大・最小・区間の端（事実）
- profile/stats.py:81 — 引数・値の 0 との比較（正であることの検査・符号）
- profile/stats.py:91 — 測った値の最大・最小・区間の端（事実）
- profile/stats.py:99 — 分位（p90・p99）の事実（判定に使わない）
- profile/timing.py:23 — 呼び出しの区間の入れ子・時間の窓の比較（区間の算術）
- profile/timing.py:60 — 形式・RTOS の定める種類・コードでの分岐
- profile/timing.py:68 — 引数・値の 0 との比較（正であることの検査・符号）
- rtos/__init__.py:30 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/analysis.py:33 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/analysis.py:44 — 呼び出しの区間の入れ子・時間の窓の比較（区間の算術）
- rtos/analysis.py:45 — 測った値の最大・最小・区間の端（事実）
- rtos/analysis.py:55 — 呼び出しの区間の入れ子・時間の窓の比較（区間の算術）
- rtos/analysis.py:57 — 0・1 で切る／0 除算を避ける算術（区間の重なり・個数）
- rtos/analysis.py:63 — 0・1 で切る／0 除算を避ける算術（区間の重なり・個数）
- rtos/analysis.py:65 — 0・1 で切る／0 除算を避ける算術（区間の重なり・個数）
- rtos/analysis.py:114 — 引数・値の 0 との比較（正であることの検査・符号）
- rtos/analysis.py:116 — 呼び出しの区間の入れ子・時間の窓の比較（区間の算術）
- rtos/analysis.py:122 — 0・1 で切る／0 除算を避ける算術（区間の重なり・個数）
- rtos/analysis.py:125 — 0・1 で切る／0 除算を避ける算術（区間の重なり・個数）
- rtos/analysis.py:128 — 呼び出しの区間の入れ子・時間の窓の比較（区間の算術）
- rtos/analysis.py:130 — 測った値の最大・最小・区間の端（事実）
- rtos/analysis.py:138 — 測った値の最大・最小・区間の端（事実）
- rtos/analysis.py:141 — 引数・値の 0 との比較（正であることの検査・符号）
- rtos/analysis.py:150 — 測った値の最大・最小・区間の端（事実）
- rtos/analysis.py:202 — 説明文・コメント（判定を含まない）
- rtos/analysis.py:210 — 形式・RTOS の定める種類・コードでの分岐
- rtos/analysis.py:219 — 説明文（規格・出典・推定しないことの記述）
- rtos/analysis.py:228 — 呼び出しの区間の入れ子・時間の窓の比較（区間の算術）
- rtos/analysis.py:231 — 抜け（記録の gap）をまたぐ切り替えを "(gap)" にする（記録の事実）
- rtos/analysis.py:243 — 測った値の最大・最小・区間の端（事実）
- rtos/analysis.py:247 — 形式・RTOS の定める種類・コードでの分岐
- rtos/analysis.py:263 — 呼び出しの区間の入れ子・時間の窓の比較（区間の算術）
- rtos/analysis.py:265 — 測った値の最大・最小・区間の端（事実）
- rtos/analysis.py:267 — 測った値の最大・最小・区間の端（事実）
- rtos/analysis.py:270 — 呼び出しの区間の入れ子・時間の窓の比較（区間の算術）
- rtos/analysis.py:273 — 測った値の最大・最小・区間の端（事実）
- rtos/analysis.py:296 — 測った値の最大・最小・区間の端（事実）
- rtos/analysis.py:298 — 測った値の最大・最小・区間の端（事実）
- rtos/analysis.py:302 — 測った値の最大・最小・区間の端（事実）
- rtos/analysis.py:304 — 形式・RTOS の定める種類・コードでの分岐
- rtos/analysis.py:315 — 呼び出しの区間の入れ子・時間の窓の比較（区間の算術）
- rtos/analysis.py:334 — 測った値の最大・最小・区間の端（事実）
- rtos/analysis.py:372 — 0・1 で切る／0 除算を避ける算術（区間の重なり・個数）
- rtos/analysis.py:399 — 測った値の最大・最小・区間の端（事実）
- rtos/analysis.py:401 — 測った値の最大・最小・区間の端（事実）
- rtos/analysis.py:404 — 測った値の最大・最小・区間の端（事実）
- rtos/analysis.py:494 — 形式・RTOS の定める種類・コードでの分岐
- rtos/analysis.py:522 — 測った値の最大・最小・区間の端（事実）
- rtos/analysis.py:539 — 形式・RTOS の定める種類・コードでの分岐
- rtos/base.py:72 — カーネルの変数が指すアイドルのタスク（RTOS のソース）を返す関数
- rtos/base.py:106 — 形式の構文の解析（接頭辞・正規表現）
- rtos/base.py:124 — 説明文・コメント（判定を含まない）
- rtos/base.py:136 — 読んだ値の事実を warnings に書く（下の H に挙げたもの以外）
- rtos/contention.py:42 — 形式・RTOS の定める種類・コードでの分岐
- rtos/cortexm.py:7 — 説明文（規格・出典・推定しないことの記述）
- rtos/cortexm.py:28 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/cortexm.py:29 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/cortexm.py:30 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/cortexm.py:31 — レジスタの番地・ビット・形式の定数（規格・RTOS のソース）
- rtos/cortexm.py:32 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/cortexm.py:33 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/cortexm.py:34 — レジスタの番地・ビット・形式の定数（規格・RTOS のソース）
- rtos/cortexm.py:61 — 形式の決まったバイト数・語数の切り出し
- rtos/cortexm.py:87 — 形式の決まったバイト数・語数の切り出し
- rtos/cortexm.py:172 — 形式の構文の解析（接頭辞・正規表現）
- rtos/cortexm.py:174 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- rtos/flow.py:29 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/flow.py:30 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/flow.py:31 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/flow.py:51 — 形式・RTOS の定める種類・コードでの分岐
- rtos/flow.py:93 — 形式・RTOS の定める種類・コードでの分岐
- rtos/flow.py:95 — 形式・RTOS の定める種類・コードでの分岐
- rtos/flow.py:151 — 説明文（規格・出典・推定しないことの記述）
- rtos/flow.py:191 — 測った値の最大・最小・区間の端（事実）
- rtos/flow.py:210 — 測った値の最大・最小・区間の端（事実）
- rtos/flow.py:249 — 形式・RTOS の定める種類・コードでの分岐
- rtos/flow.py:255 — 0・1 で切る／0 除算を避ける算術（区間の重なり・個数）
- rtos/flow.py:257 — 測った値の最大・最小・区間の端（事実）
- rtos/flow.py:272 — 0・1 で切る／0 除算を避ける算術（区間の重なり・個数）
- rtos/flow.py:273 — 0・1 で切る／0 除算を避ける算術（区間の重なり・個数）
- rtos/flow.py:274 — 測った値の最大・最小・区間の端（事実）
- rtos/flow.py:291 — 引数・値の 0 との比較（正であることの検査・符号）
- rtos/flow.py:294 — 0・1 で切る／0 除算を避ける算術（区間の重なり・個数）
- rtos/flow.py:338 — 測った値の最大・最小・区間の端（事実）
- rtos/flow.py:369 — 形式・RTOS の定める種類・コードでの分岐
- rtos/flow.py:412 — 形式・RTOS の定める種類・コードでの分岐
- rtos/flow.py:425 — 説明文（規格・出典・推定しないことの記述）
- rtos/flow.py:434 — 説明文（CPU の負荷を推定しないことの note）
- rtos/flow.py:437 — SWO のバイト数 = ITM の 32 ビットのパケット 5 バイト × 語数、1 バイト 10 ビット（ARMv7-M の ITM・NRZ の UART の規格）。Manchester では 10 ビットでない点は呼び出し側の baud の意味次第
- rtos/frames.py:25 — 引数・値の 0 との比較（正であることの検査・符号）
- rtos/freertos.py:8 — 説明文（規格・出典・推定しないことの記述）
- rtos/freertos.py:28 — レジスタの番地・ビット・形式の定数（規格・RTOS のソース）
- rtos/freertos.py:30 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/freertos.py:31 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/freertos.py:32 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/freertos.py:33 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/freertos.py:34 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/freertos.py:54 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- rtos/freertos.py:101 — 説明文（docstring・文言）
- rtos/freertos.py:139 — 形式・RTOS の定める種類・コードでの分岐
- rtos/freertos.py:173 — 形式・RTOS の定める種類・コードでの分岐
- rtos/freertos.py:238 — 形式・RTOS の定める種類・コードでの分岐
- rtos/freertos.py:243 — 形式・RTOS の定める種類・コードでの分岐
- rtos/freertos.py:316 — カーネルの変数が指すアイドルのタスク（RTOS のソース）を返す関数
- rtos/freertos.py:317 — 説明文・コメント（判定を含まない）
- rtos/freertos.py:318 — カーネルの変数が指すアイドルのタスク（RTOS のソース）
- rtos/freertos.py:324 — 形式・RTOS の定める種類・コードでの分岐
- rtos/freertos_objects.py:27 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/freertos_objects.py:33 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/freertos_objects.py:41 — レジスタの番地・ビット・形式の定数（規格・RTOS のソース）
- rtos/freertos_objects.py:42 — portBYTE_ALIGNMENT = 8（FreeRTOS の Cortex-M の port の値。heap の total の計算）
- rtos/freertos_objects.py:89 — 形式・RTOS の定める種類・コードでの分岐
- rtos/freertos_objects.py:125 — 形式・RTOS の定める種類・コードでの分岐
- rtos/freertos_objects.py:150 — FreeRTOS のキューの 2 つの待ちリストの名前（_plausible_queue の中。閾値は 149 行）
- rtos/freertos_objects.py:209 — 形式・RTOS の定める種類・コードでの分岐
- rtos/freertos_objects.py:210 — RTOS の構造体の値からの事実（持ち主 0 = 空き等）
- rtos/freertos_objects.py:217 — 形式の構文の解析（接頭辞・正規表現）
- rtos/freertos_objects.py:298 — 0・1 で切る／0 除算を避ける算術（区間の重なり・個数）
- rtos/freertos_objects.py:306 — 形式・RTOS の定める種類・コードでの分岐
- rtos/freertos_objects.py:315 — 測った値の最大・最小・区間の端（事実）
- rtos/freertos_objects.py:355 — 形式・RTOS の定める種類・コードでの分岐
- rtos/freertos_objects.py:388 — 形式・RTOS の定める種類・コードでの分岐
- rtos/freertos_objects.py:391 — 測った値の最大・最小・区間の端（事実）
- rtos/kernel_trace.py:26 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/kernel_trace.py:27 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/kernel_trace.py:28 — レジスタの番地・ビット・形式の定数（規格・RTOS のソース）
- rtos/kernel_trace.py:29 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/kernel_trace.py:32 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/kernel_trace.py:33 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/kernel_trace.py:34 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/kernel_trace.py:35 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/kernel_trace.py:36 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/kernel_trace.py:64 — 長さの検査（形式の欄の数・バイト数が足りるか）
- rtos/kernel_trace.py:86 — 形式・RTOS の定める種類・コードでの分岐
- rtos/kernel_trace.py:118 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- rtos/kernel_trace.py:125 — 形式の構文の解析（接頭辞・正規表現）
- rtos/kernel_trace.py:153 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- rtos/kernel_trace.py:165 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- rtos/kernel_trace.py:176 — 形式・RTOS の定める種類・コードでの分岐
- rtos/model.py:28 — 説明文（規格・出典・推定しないことの記述）
- rtos/model.py:144 — 説明文・コメント（判定を含まない）
- rtos/nuttx.py:31 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/nuttx.py:35 — RTOS の定める値（優先度の向き・状態の番号・属性のビット）
- rtos/nuttx.py:36 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/nuttx.py:37 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/nuttx.py:41 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/nuttx.py:43 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/nuttx.py:46 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/nuttx.py:174 — 引数・値の 0 との比較（正であることの検査・符号）
- rtos/nuttx.py:187 — 引数・値の 0 との比較（正であることの検査・符号）
- rtos/nuttx.py:195 — 引数・値の 0 との比較（正であることの検査・符号）
- rtos/nuttx.py:206 — 読んだ値の事実を warnings に書く（下の H に挙げたもの以外）
- rtos/nuttx.py:260 — 0・1 で切る／0 除算を避ける算術（区間の重なり・個数）
- rtos/nuttx.py:263 — 形式・RTOS の定める種類・コードでの分岐
- rtos/nuttx.py:285 — 形式・RTOS の定める種類・コードでの分岐
- rtos/nuttx.py:303 — 形式・RTOS の定める種類・コードでの分岐
- rtos/nuttx.py:326 — カーネルの変数が指すアイドルのタスク（RTOS のソース）を返す関数
- rtos/nuttx.py:327 — カーネルの変数が指すアイドルのタスク（RTOS のソース）
- rtos/nuttx.py:330 — カーネルの変数が指すアイドルのタスク（RTOS のソース）
- rtos/nuttx_objects.py:22 — RTOS の定める値（優先度の向き・状態の番号・属性のビット）
- rtos/nuttx_objects.py:25 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/nuttx_objects.py:47 — 形式・RTOS の定める種類・コードでの分岐
- rtos/nuttx_objects.py:84 — 0・1 で切る／0 除算を避ける算術（区間の重なり・個数）
- rtos/objects.py:86 — 引数・値の 0 との比較（正であることの検査・符号）
- rtos/rtx5.py:31 — レジスタの番地・ビット・形式の定数（規格・RTOS のソース）
- rtos/rtx5.py:32 — レジスタの番地・ビット・形式の定数（規格・RTOS のソース）
- rtos/rtx5.py:35 — レジスタの番地・ビット・形式の定数（規格・RTOS のソース）
- rtos/rtx5.py:36 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/rtx5.py:39 — RTOS の定める値（優先度の向き・状態の番号・属性のビット）
- rtos/rtx5.py:41 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/rtx5.py:43 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/rtx5.py:46 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/rtx5.py:51 — RTOS の定める値（優先度の向き・状態の番号・属性のビット）
- rtos/rtx5.py:118 — 説明文・コメント（判定を含まない）
- rtos/rtx5.py:143 — カーネルの変数が指すアイドルのタスク（RTOS のソース）
- rtos/rtx5.py:151 — 読んだ値の事実を warnings に書く（下の H に挙げたもの以外）
- rtos/rtx5.py:203 — 形式・RTOS の定める種類・コードでの分岐
- rtos/rtx5.py:291 — カーネルの変数が指すアイドルのタスク（RTOS のソース）を返す関数
- rtos/rtx5.py:292 — カーネルの変数が指すアイドルのタスク（RTOS のソース）
- rtos/rtx5.py:293 — カーネルの変数が指すアイドルのタスク（RTOS のソース）
- rtos/rtx5_objects.py:22 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/rtx5_objects.py:129 — RTOS の構造体の値からの事実（持ち主 0 = 空き等）
- rtos/rtx5_objects.py:148 — 形式・RTOS の定める種類・コードでの分岐
- rtos/rtx5_objects.py:152 — 形式・RTOS の定める種類・コードでの分岐
- rtos/stackcheck.py:31 — 測った値の最大・最小・区間の端（事実）
- rtos/stackcheck.py:43 — 説明文・コメント（判定を含まない）
- rtos/stackcheck.py:61 — 0・1 で切る／0 除算を避ける算術（区間の重なり・個数）
- rtos/threadx.py:32 — レジスタの番地・ビット・形式の定数（規格・RTOS のソース）
- rtos/threadx.py:36 — レジスタの番地・ビット・形式の定数（規格・RTOS のソース）
- rtos/threadx.py:37 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/threadx.py:42 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/threadx.py:150 — 説明文・コメント（判定を含まない）
- rtos/threadx.py:159 — 読んだ値の事実を warnings に書く（下の H に挙げたもの以外）
- rtos/threadx.py:166 — 読んだ値の事実を warnings に書く（下の H に挙げたもの以外）
- rtos/threadx.py:219 — 形式・RTOS の定める種類・コードでの分岐
- rtos/threadx_objects.py:19 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/threadx_objects.py:24 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/threadx_objects.py:26 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/threadx_objects.py:42 — 形式の構文の解析（接頭辞・正規表現）
- rtos/threadx_objects.py:102 — RTOS の構造体の値からの事実（持ち主 0 = 空き等）
- rtos/threadx_objects.py:117 — 形式・RTOS の定める種類・コードでの分岐
- rtos/threadx_objects.py:142 — RTOS の構造体の値からの事実（持ち主 0 = 空き等）
- rtos/threadx_objects.py:145 — RTOS・形式の欄名 timeout（時間切れの定数ではない）
- rtos/threadx_objects.py:146 — RTOS・形式の欄名 timeout（時間切れの定数ではない）
- rtos/timeline.py:32 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/timeline.py:33 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/timeline.py:34 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/timeline.py:101 — 割合（0 除算を避ける）
- rtos/timeline.py:102 — 測った値の最大・最小・区間の端（事実）
- rtos/timeline.py:104 — サンプル 1 回だけの区間の数（数えた事実）
- rtos/timeline.py:123 — 説明文・コメント（判定を含まない）
- rtos/timeline.py:131 — 測った値の最大・最小・区間の端（事実）
- rtos/timeline.py:137 — 測った値の最大・最小・区間の端（事実）
- rtos/timeline.py:178 — 測った値の最大・最小・区間の端（事実）
- rtos/timeline.py:206 — 測った値の最大・最小・区間の端（事実）
- rtos/timeline.py:231 — 形式・RTOS の定める種類・コードでの分岐
- rtos/timeline.py:257 — 説明文（docstring・文言）
- rtos/ucos3.py:36 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/ucos3.py:39 — RTOS の定める値（優先度の向き・状態の番号・属性のビット）
- rtos/ucos3.py:40 — レジスタの番地・ビット・形式の定数（規格・RTOS のソース）
- rtos/ucos3.py:42 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/ucos3.py:44 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/ucos3.py:145 — 説明文・コメント（判定を含まない）
- rtos/ucos3.py:172 — 読んだ値の事実を warnings に書く（下の H に挙げたもの以外）
- rtos/ucos3.py:180 — 測った値の最大・最小・区間の端（事実）
- rtos/ucos3.py:193 — 形式の構文の解析（接頭辞・正規表現）
- rtos/ucos3.py:291 — カーネルの変数が指すアイドルのタスク（RTOS のソース）を返す関数
- rtos/ucos3.py:292 — カーネルの変数が指すアイドルのタスク（RTOS のソース）
- rtos/ucos3.py:293 — カーネルの変数が指すアイドルのタスク（RTOS のソース）
- rtos/ucos3_objects.py:23 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/ucos3_objects.py:28 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/ucos3_objects.py:30 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/ucos3_objects.py:32 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/ucos3_objects.py:37 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/ucos3_objects.py:38 — RTOS・形式の欄名 timeout（時間切れの定数ではない）
- rtos/ucos3_objects.py:39 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/ucos3_objects.py:63 — 読んだ値の事実を warnings に書く（下の H に挙げたもの以外）
- rtos/ucos3_objects.py:137 — RTOS の構造体の値からの事実（持ち主 0 = 空き等）
- rtos/ucos3_objects.py:192 — 0・1 で切る／0 除算を避ける算術（区間の重なり・個数）
- rtos/zephyr.py:23 — レジスタの番地・ビット・形式の定数（規格・RTOS のソース）
- rtos/zephyr.py:26 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- rtos/zephyr.py:179 — カーネルの変数が指すアイドルのタスク（RTOS のソース）を返す関数
- rtos/zephyr.py:180 — 説明文（docstring・文言）
- rtos/zephyr.py:181 — カーネルの変数が指すアイドルのタスク（RTOS のソース）
- rtos/zephyr.py:185 — カーネルの変数が指すアイドルのタスク（RTOS のソース）
- rtos/zephyr.py:186 — 形式・RTOS の定める種類・コードでの分岐
- rtos/zephyr_objects.py:19 — Zephyr のカーネルのオブジェクトの構造体の名前（Zephyr のソース）
- rtos/zephyr_objects.py:21 — 形式・RTOS・規格の定数（名前・コードの表）
- rtos/zephyr_objects.py:37 — RTOS・形式の欄名 timeout（時間切れの定数ではない）
- rtos/zephyr_objects.py:93 — RTOS の構造体の値からの事実（持ち主 0 = 空き等）
- rtos/zephyr_objects.py:117 — RTOS・形式の欄名 timeout（時間切れの定数ではない）
- rtos/zephyr_objects.py:136 — 説明文・コメント（判定を含まない）
- rtos/zephyr_objects.py:137 — RTOS・形式の欄名 timeout（時間切れの定数ではない）
- rtos/zephyr_objects.py:138 — RTOS・形式の欄名 timeout（時間切れの定数ではない）
- rtos/zephyr_objects.py:140 — RTOS・形式の欄名 timeout（時間切れの定数ではない）
- rtos/zephyr_objects.py:143 — RTOS・形式の欄名 timeout（時間切れの定数ではない）
- rtos/zephyr_objects.py:144 — RTOS・形式の欄名 timeout（時間切れの定数ではない）
- rtos/zephyr_objects.py:166 — 形式・RTOS の定める種類・コードでの分岐
- rtos/zephyr_objects.py:168 — 形式・RTOS の定める種類・コードでの分岐
- rtos/zephyr_objects.py:172 — 形式・RTOS の定める種類・コードでの分岐
- rtos/zephyr_objects.py:173 — 形式・RTOS の定める種類・コードでの分岐
- rtos/zephyr_objects.py:181 — 形式・RTOS の定める種類・コードでの分岐
- trace/__init__.py:1 — 説明文（docstring・文言）
- trace/__init__.py:18 — 形式・RTOS・規格の定数（名前・コードの表）
- trace/__init__.py:25 — 形式・RTOS の定める種類・コードでの分岐
- trace/coresight.py:22 — レジスタの番地・ビット・形式の定数（規格・RTOS のソース）
- trace/coresight.py:23 — レジスタの番地・ビット・形式の定数（規格・RTOS のソース）
- trace/coresight.py:24 — レジスタの番地・ビット・形式の定数（規格・RTOS のソース）
- trace/coresight.py:25 — レジスタの番地・ビット・形式の定数（規格・RTOS のソース）
- trace/coresight.py:27 — レジスタの番地・ビット・形式の定数（規格・RTOS のソース）
- trace/coresight.py:35 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- trace/coresight.py:36 — 形式・RTOS・規格の定数（名前・コードの表）
- trace/coresight.py:40 — レジスタの番地・ビット・形式の定数（規格・RTOS のソース）
- trace/coresight.py:44 — 形式・RTOS・規格の定数（名前・コードの表）
- trace/coresight.py:45 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- trace/coresight.py:107 — 引数・値の範囲の検査（規格・形式の範囲）
- trace/coresight.py:109 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- trace/coresight.py:111 — 引数・値の範囲の検査（規格・形式の範囲）
- trace/coresight.py:127 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- trace/coresight.py:128 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- trace/coresight.py:129 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- trace/coresight.py:130 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- trace/coresight.py:131 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- trace/coresight.py:132 — TRCIDR0 のビット（ETMv4 の規格）
- trace/coresight.py:134 — TRCSTATR.IDLE のビットを読む（ETMv4 の規格。名前が idle）
- trace/coresight.py:138 — 説明文・コメント（判定を含まない）
- trace/coresight.py:169 — 引数・値の範囲の検査（規格・形式の範囲）
- trace/coresight.py:184 — 引数・値の範囲の検査（規格・形式の範囲）
- trace/coresight.py:187 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- trace/elements.py:14 — 形式・RTOS・規格の定数（名前・コードの表）
- trace/elements.py:20 — 例外の番号 16 以上 = IRQ（ARMv7-M の規格）
- trace/etmv4.py:25 — 形式・RTOS・規格の定数（名前・コードの表）
- trace/etmv4.py:31 — 説明文（TRCIDR0.COMMOPT の意味）
- trace/etmv4.py:57 — 続きの印の付いた値は 5 バイトまで（32 ビット値。ETMv4 の仕様）
- trace/etmv4.py:79 — ETMv4 のアドレスの履歴 3 段（ETMv4 の仕様）
- trace/etmv4.py:92 — ETMv4 のアドレスの履歴 3 段（ETMv4 の仕様）
- trace/etmv4.py:94 — ETMv4 の A-sync（0x00 の並び）の検出（ETMv4 の仕様）
- trace/etmv4.py:145 — 形式・RTOS の定める種類・コードでの分岐
- trace/etmv4.py:149 — 形式・RTOS の定める種類・コードでの分岐
- trace/etmv4.py:150 — 形式・RTOS の定める種類・コードでの分岐
- trace/etmv4.py:166 — 形式・RTOS の定める種類・コードでの分岐
- trace/etmv4.py:176 — 形式・RTOS の定める種類・コードでの分岐
- trace/etmv4.py:181 — 形式・RTOS の定める種類・コードでの分岐
- trace/etmv4.py:184 — ETMv4 のヘッダの割り当て（ETMv4 の仕様）
- trace/etmv4.py:188 — ETMv4 のヘッダの割り当て（ETMv4 の仕様）
- trace/etmv4.py:190 — 形式・RTOS の定める種類・コードでの分岐
- trace/etmv4.py:192 — ETMv4 のヘッダの割り当て（ETMv4 の仕様）
- trace/etmv4.py:196 — 形式・RTOS の定める種類・コードでの分岐
- trace/etmv4.py:199 — 形式・RTOS の定める種類・コードでの分岐
- trace/etmv4.py:200 — 形式・RTOS の定める種類・コードでの分岐
- trace/etmv4.py:201 — 形式・RTOS の定める種類・コードでの分岐
- trace/etmv4.py:203 — ETMv4 のヘッダの割り当て（ETMv4 の仕様）
- trace/etmv4.py:218 — ETMv4 のアドレスの履歴 3 段（ETMv4 の仕様）
- trace/etmv4.py:226 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- trace/etmv4.py:233 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- trace/etmv4.py:240 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- trace/etmv4.py:244 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- trace/etmv4.py:246 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- trace/etmv4.py:267 — ETMv4 のアドレスの履歴 3 段（ETMv4 の仕様）
- trace/etmv4.py:276 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- trace/etmv4.py:284 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- trace/etmv4.py:286 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- trace/etmv4.py:299 — 形式・RTOS の定める種類・コードでの分岐
- trace/etmv4.py:301 — ETMv4 のヘッダの割り当て（ETMv4 の仕様）
- trace/etmv4.py:303 — ETMv4 のヘッダの割り当て（ETMv4 の仕様）
- trace/etmv4.py:305 — ETMv4 のヘッダの割り当て（ETMv4 の仕様）
- trace/etmv4.py:307 — 形式・RTOS の定める種類・コードでの分岐
- trace/etmv4.py:309 — ETMv4 のヘッダの割り当て（ETMv4 の仕様）
- trace/reconstruct.py:3 — 説明文（規格・出典・推定しないことの記述）
- trace/reconstruct.py:12 — 説明文・コメント（判定を含まない）
- trace/reconstruct.py:34 — 形式・RTOS・規格の定数（名前・コードの表）
- trace/reconstruct.py:88 — 0・1 で切る／0 除算を避ける算術（区間の重なり・個数）
- trace/reconstruct.py:102 — 形式・RTOS の定める種類・コードでの分岐
- trace/reconstruct.py:132 — 0・1 で切る／0 除算を避ける算術（区間の重なり・個数）
- trace/reconstruct.py:154 — 形式の決まったバイト数・語数の切り出し
- trace/reconstruct.py:159 — 形式・RTOS の定める種類・コードでの分岐
- trace/tpiu.py:4 — 説明文（規格・出典・推定しないことの記述）
- trace/tpiu.py:20 — 形式・RTOS・規格の定数（名前・コードの表）
- trace/tpiu.py:21 — 形式・RTOS・規格の定数（名前・コードの表）
- trace/tpiu.py:22 — 形式・RTOS・規格の定数（名前・コードの表）
- trace/tpiu.py:64 — 0・1 で切る／0 除算を避ける算術（区間の重なり・個数）
- trace/tpiu.py:67 — 引数・値の 0 との比較（正であることの検査・符号）
- trace/tpiu.py:81 — TPIU の FSYNC・HSYNC の長さ（CoreSight の仕様）
- trace/tpiu.py:87 — TPIU の FSYNC・HSYNC の長さ（CoreSight の仕様）
- trace/tpiu.py:117 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- tracefmt/ctf.py:8 — 説明文・コメント（判定を含まない）
- tracefmt/ctf.py:9 — 説明文・コメント（判定を含まない）
- tracefmt/ctf.py:15 — 説明文・コメント（判定を含まない）
- tracefmt/ctf.py:31 — 形式・RTOS・規格の定数（名前・コードの表）
- tracefmt/ctf.py:32 — 形式・RTOS・規格の定数（名前・コードの表）
- tracefmt/ctf.py:66 — 形式・RTOS・規格の定数（名前・コードの表）
- tracefmt/ctf.py:71 — 説明文（docstring・文言）
- tracefmt/ctf.py:78 — 形式・RTOS の定める種類・コードでの分岐
- tracefmt/ctf.py:89 — 形式の構文の解析（接頭辞・正規表現）
- tracefmt/ctf.py:91 — 形式の構文の解析（接頭辞・正規表現）
- tracefmt/ctf.py:92 — 形式・RTOS の定める種類・コードでの分岐
- tracefmt/ctf.py:94 — 形式の構文の解析（接頭辞・正規表現）
- tracefmt/ctf.py:95 — 形式の構文の解析（接頭辞・正規表現）
- tracefmt/ctf.py:96 — 形式の構文の解析（接頭辞・正規表現）
- tracefmt/ctf.py:126 — 説明文・コメント（判定を含まない）
- tracefmt/ctf.py:128 — 形式・RTOS・規格の定数（名前・コードの表）
- tracefmt/ctf.py:134 — RTOS・形式が出す idle の出来事（Zephyr の 0x1E・SystemView の IDLE。記録の事実）
- tracefmt/ctf.py:136 — RTOS・形式の欄名 timeout（時間切れの定数ではない）
- tracefmt/ctf.py:137 — RTOS・形式の欄名 timeout（時間切れの定数ではない）
- tracefmt/ctf.py:138 — RTOS・形式の欄名 timeout（時間切れの定数ではない）
- tracefmt/ctf.py:139 — RTOS・形式の欄名 timeout（時間切れの定数ではない）
- tracefmt/ctf.py:140 — RTOS・形式の欄名 timeout（時間切れの定数ではない）
- tracefmt/ctf.py:141 — RTOS・形式の欄名 timeout（時間切れの定数ではない）
- tracefmt/ctf.py:146 — RTOS・形式の欄名 timeout（時間切れの定数ではない）
- tracefmt/ctf.py:160 — 形式・RTOS・規格の定数（名前・コードの表）
- tracefmt/ctf.py:167 — RTOS・形式が出す idle の出来事（Zephyr の 0x1E・SystemView の IDLE。記録の事実）
- tracefmt/ctf.py:168 — 形式・RTOS・規格の定数（名前・コードの表）
- tracefmt/ctf.py:262 — 形式の構文の解析（接頭辞・正規表現）
- tracefmt/ctf.py:276 — 形式・RTOS の定める種類・コードでの分岐
- tracefmt/eventrecorder.py:2 — 説明文（規格・出典・推定しないことの記述）
- tracefmt/eventrecorder.py:12 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- tracefmt/eventrecorder.py:28 — 形式・RTOS・規格の定数（名前・コードの表）
- tracefmt/eventrecorder.py:29 — 形式の定数（構造体の大きさ・コマンドの番号・数の語の法。仕様）
- tracefmt/eventrecorder.py:30 — レジスタの番地・ビット・形式の定数（規格・RTOS のソース）
- tracefmt/eventrecorder.py:31 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- tracefmt/eventrecorder.py:32 — 形式・RTOS・規格の定数（名前・コードの表）
- tracefmt/eventrecorder.py:33 — レジスタの番地・ビット・形式の定数（規格・RTOS のソース）
- tracefmt/eventrecorder.py:34 — 形式・RTOS・規格の定数（名前・コードの表）
- tracefmt/eventrecorder.py:106 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- tracefmt/eventrecorder.py:108 — 0・1 で切る／0 除算を避ける算術（区間の重なり・個数）
- tracefmt/eventrecorder.py:113 — 測った値の最大・最小・区間の端（事実）
- tracefmt/eventrecorder.py:122 — Event Recorder のリングの位置（CMSIS-View の EventRecorder.c）
- tracefmt/eventrecorder.py:123 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- tracefmt/eventrecorder.py:126 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- tracefmt/eventrecorder.py:127 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- tracefmt/eventrecorder.py:130 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- tracefmt/eventrecorder.py:131 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- tracefmt/eventrecorder.py:132 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- tracefmt/eventrecorder.py:134 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- tracefmt/eventrecorder.py:143 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- tracefmt/eventrecorder.py:150 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- tracefmt/eventrecorder.py:151 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- tracefmt/eventrecorder.py:158 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- tracefmt/eventrecorder.py:191 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- tracefmt/eventrecorder.py:203 — 長さの検査（形式の欄の数・バイト数が足りるか）
- tracefmt/eventrecorder.py:204 — 形式の決まったバイト数・語数の切り出し
- tracefmt/eventrecorder.py:219 — 形式・RTOS の定める種類・コードでの分岐
- tracefmt/model.py:25 — 形式・RTOS・規格の定数（名前・コードの表）
- tracefmt/model.py:26 — 説明文・コメント（判定を含まない）
- tracefmt/model.py:27 — 説明文・コメント（判定を含まない）
- tracefmt/model.py:34 — 形式・RTOS・規格の定数（名前・コードの表）
- tracefmt/model.py:44 — 説明文・コメント（判定を含まない）
- tracefmt/model.py:117 — 形式・RTOS の定める種類・コードでの分岐
- tracefmt/model.py:139 — 説明文・コメント（判定を含まない）
- tracefmt/model.py:140 — 説明文・コメント（判定を含まない）
- tracefmt/model.py:151 — RTOS・形式が出す idle の出来事（Zephyr の 0x1E・SystemView の IDLE。記録の事実）
- tracefmt/model.py:189 — 形式・RTOS の定める種類・コードでの分岐
- tracefmt/scvd.py:30 — 形式・RTOS・規格の定数（名前・コードの表）
- tracefmt/scvd.py:33 — 形式・RTOS・規格の定数（名前・コードの表）
- tracefmt/scvd.py:52 — 形式の構文の解析（接頭辞・正規表現）
- tracefmt/scvd.py:88 — 形式・RTOS・規格の定数（名前・コードの表）
- tracefmt/scvd.py:89 — C の数値リテラルの字句（SCVD の式の構文）
- tracefmt/scvd.py:94 — 形式・RTOS・規格の定数（名前・コードの表）
- tracefmt/scvd.py:96 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- tracefmt/scvd.py:107 — 形式・RTOS の定める種類・コードでの分岐
- tracefmt/scvd.py:120 — 説明文（規格・出典・推定しないことの記述）
- tracefmt/scvd.py:173 — 形式・RTOS の定める種類・コードでの分岐
- tracefmt/scvd.py:186 — 形式の構文の解析（接頭辞・正規表現）
- tracefmt/scvd.py:238 — 形式・RTOS の定める種類・コードでの分岐
- tracefmt/scvd.py:313 — SCVD の仕様の値の解釈（配列の範囲・メンバーの大きさ）
- tracefmt/scvd.py:316 — 引数・値の 0 との比較（正であることの検査・符号）
- tracefmt/scvd.py:364 — 引数・値の範囲の検査（規格・形式の範囲）
- tracefmt/scvd.py:370 — SCVD の仕様の値の解釈（配列の範囲・メンバーの大きさ）
- tracefmt/scvd.py:381 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- tracefmt/scvd.py:383 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- tracefmt/scvd.py:441 — 引数・値の 0 との比較（正であることの検査・符号）
- tracefmt/scvd.py:452 — SCVD の %S（スタックの使用率）の書式の定義（CMSIS の scvd の仕様）
- tracefmt/scvd.py:453 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- tracefmt/scvd.py:469 — 引数・値の 0 との比較（正であることの検査・符号）
- tracefmt/scvd.py:496 — 引数・値の 0 との比較（正であることの検査・符号）
- tracefmt/scvd.py:513 — 形式の構文の解析（接頭辞・正規表現）
- tracefmt/scvd.py:617 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- tracefmt/scvd.py:619 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- tracefmt/scvd.py:630 — 形式の決まったバイト数・語数の切り出し
- tracefmt/scvd.py:635 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- tracefmt/scvd.py:637 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- tracefmt/scvd.py:639 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- tracefmt/scvd.py:642 — 形式・RTOS の定める種類・コードでの分岐
- tracefmt/scvd.py:652 — 形式・RTOS の定める種類・コードでの分岐
- tracefmt/scvd.py:683 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- tracefmt/scvd.py:709 — 形式・RTOS の定める種類・コードでの分岐
- tracefmt/scvd.py:778 — 引数・値の 0 との比較（正であることの検査・符号）
- tracefmt/scvd.py:847 — 形式・RTOS の定める種類・コードでの分岐
- tracefmt/stream.py:30 — 形式・RTOS・規格の定数（名前・コードの表）
- tracefmt/stream.py:31 — 形式・RTOS・規格の定数（名前・コードの表）
- tracefmt/stream.py:32 — 形式・RTOS・規格の定数（名前・コードの表）
- tracefmt/stream.py:33 — 形式・RTOS・規格の定数（名前・コードの表）
- tracefmt/stream.py:34 — 形式・RTOS・規格の定数（名前・コードの表）
- tracefmt/stream.py:35 — 形式・RTOS・規格の定数（名前・コードの表）
- tracefmt/systemview.py:2 — 説明文（規格・出典・推定しないことの記述）
- tracefmt/systemview.py:9 — 説明文（規格・出典・推定しないことの記述）
- tracefmt/systemview.py:32 — 形式・RTOS・規格の定数（名前・コードの表）
- tracefmt/systemview.py:33 — 形式の定数（構造体の大きさ・コマンドの番号・数の語の法。仕様）
- tracefmt/systemview.py:34 — 形式・RTOS・規格の定数（名前・コードの表）
- tracefmt/systemview.py:35 — 形式・RTOS・規格の定数（名前・コードの表）
- tracefmt/systemview.py:37 — 形式・RTOS・規格の定数（名前・コードの表）
- tracefmt/systemview.py:40 — SystemView の出来事の番号の表・可変長の符号（SEGGER_SYSVIEW.c）
- tracefmt/systemview.py:44 — 形式・RTOS・規格の定数（名前・コードの表）
- tracefmt/systemview.py:47 — 形式・RTOS・規格の定数（名前・コードの表）
- tracefmt/systemview.py:49 — 形式・RTOS・規格の定数（名前・コードの表）
- tracefmt/systemview.py:66 — 形式の構文の解析（接頭辞・正規表現）
- tracefmt/systemview.py:68 — 引数・値の 0 との比較（正であることの検査・符号）
- tracefmt/systemview.py:88 — 形式の構文の解析（接頭辞・正規表現）
- tracefmt/systemview.py:90 — 長さの検査（形式の欄の数・バイト数が足りるか）
- tracefmt/systemview.py:101 — 形式の構文の解析（接頭辞・正規表現）
- tracefmt/systemview.py:132 — SystemView の出来事の番号の表・可変長の符号（SEGGER_SYSVIEW.c）
- tracefmt/systemview.py:138 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- tracefmt/systemview.py:177 — 引数・値の 0 との比較（正であることの検査・符号）
- tracefmt/systemview.py:179 — 0・1 で切る／0 除算を避ける算術（区間の重なり・個数）
- tracefmt/systemview.py:180 — 0・1 で切る／0 除算を避ける算術（区間の重なり・個数）
- tracefmt/systemview.py:213 — SystemView の出来事の番号の表・可変長の符号（SEGGER_SYSVIEW.c）
- tracefmt/systemview.py:226 — 形式・RTOS の定める種類・コードでの分岐
- tracefmt/systemview.py:250 — 測った値の最大・最小・区間の端（事実）
- tracefmt/systemview.py:256 — 形式・RTOS の定める種類・コードでの分岐
- tracefmt/systemview.py:257 — 0・1 で切る／0 除算を避ける算術（区間の重なり・個数）
- tracefmt/systemview.py:258 — SystemView の出来事の番号の表・可変長の符号（SEGGER_SYSVIEW.c）
- tracefmt/systemview.py:279 — 形式・RTOS の定める種類・コードでの分岐
- tracefmt/systemview.py:282 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- tracefmt/systemview.py:289 — 形式・RTOS の定める種類・コードでの分岐
- tracefmt/systemview.py:295 — RTOS・形式が出す idle の出来事（Zephyr の 0x1E・SystemView の IDLE。記録の事実）
- tracefmt/systemview.py:340 — 長さの検査（形式の欄の数・バイト数が足りるか）
- tracefmt/systemview.py:361 — 形式・RTOS の定める種類・コードでの分岐
- tracefmt/systemview.py:362 — 形式・RTOS の定める種類・コードでの分岐
- tracefmt/systemview.py:368 — 形式・RTOS の定める種類・コードでの分岐
- tracefmt/systemview.py:417 — 形式・RTOS の定める種類・コードでの分岐
- tracefmt/systemview.py:419 — 形式・RTOS の定める種類・コードでの分岐
- tracefmt/systemview.py:420 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- tracefmt/systemview.py:421 — 形式・RTOS の定める種類・コードでの分岐
- tracefmt/systemview.py:432 — 形式・RTOS の定める種類・コードでの分岐
- tracefmt/systemview.py:442 — 測った値の最大・最小・区間の端（事実）
- tracefmt/systemview.py:443 — 形式の構文の解析（接頭辞・正規表現）
- tracefmt/tracealyzer.py:1 — 説明文・コメント（判定を含まない）
- tracefmt/tracealyzer.py:18 — 説明文・コメント（判定を含まない）
- tracefmt/tracealyzer.py:33 — レジスタの番地・ビット・形式の定数（規格・RTOS のソース）
- tracefmt/tracealyzer.py:34 — 形式・RTOS・規格の定数（名前・コードの表）
- tracefmt/tracealyzer.py:35 — 形式・RTOS・規格の定数（名前・コードの表）
- tracefmt/tracealyzer.py:36 — レジスタの番地・ビット・形式の定数（規格・RTOS のソース）
- tracefmt/tracealyzer.py:37 — 形式・RTOS・規格の定数（名前・コードの表）
- tracefmt/tracealyzer.py:56 — 形式・RTOS・規格の定数（名前・コードの表）
- tracefmt/tracealyzer.py:93 — 長さの検査（形式の欄の数・バイト数が足りるか）
- tracefmt/tracealyzer.py:104 — 0・1 で切る／0 除算を避ける算術（区間の重なり・個数）
- tracefmt/tracealyzer.py:110 — 形式の決まったバイト数・語数の切り出し
- tracefmt/tracealyzer.py:124 — 形式・RTOS の定める種類・コードでの分岐
- tracefmt/tracealyzer.py:132 — 長さの検査（形式の欄の数・バイト数が足りるか）
- tracefmt/tracealyzer.py:162 — 長さの検査（形式の欄の数・バイト数が足りるか）
- tracefmt/tracealyzer.py:165 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- tracefmt/tracealyzer.py:173 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- tracefmt/tracealyzer.py:174 — PSF の出来事の数の語（コア数で 12/16 ビット。TraceRecorder の仕様）
- tracefmt/tracealyzer.py:175 — 形式の定数（構造体の大きさ・コマンドの番号・数の語の法。仕様）
- tracefmt/tracealyzer.py:220 — 長さの検査（形式の欄の数・バイト数が足りるか）
- tracefmt/tracealyzer.py:223 — 長さの検査（形式の欄の数・バイト数が足りるか）
- tracefmt/tracealyzer.py:228 — 長さの検査（形式の欄の数・バイト数が足りるか）
- tracefmt/tracealyzer.py:230 — 長さの検査（形式の欄の数・バイト数が足りるか）
- tracefmt/tracealyzer.py:240 — 入れ子の割り込みの出口（TraceRecorder の出来事の意味）
- tracefmt/tracealyzer.py:253 — 長さの検査（形式の欄の数・バイト数が足りるか）
- tracefmt/tracealyzer.py:261 — 長さの検査（形式の欄の数・バイト数が足りるか）
- tracefmt/tracealyzer.py:271 — コード 0x11 以上 = オブジェクトの操作（TraceRecorder のコード表）
- tracefmt/tracex.py:25 — レジスタの番地・ビット・形式の定数（規格・RTOS のソース）
- tracefmt/tracex.py:26 — 形式・RTOS・規格の定数（名前・コードの表）
- tracefmt/tracex.py:27 — 形式・RTOS・規格の定数（名前・コードの表）
- tracefmt/tracex.py:29 — 形式・RTOS・規格の定数（名前・コードの表）
- tracefmt/tracex.py:30 — 形式・RTOS・規格の定数（名前・コードの表）
- tracefmt/tracex.py:32 — 形式・RTOS・規格の定数（名前・コードの表）
- tracefmt/tracex.py:40 — 形式・RTOS・規格の定数（名前・コードの表）
- tracefmt/tracex.py:42 — 形式・RTOS・規格の定数（名前・コードの表）
- tracefmt/tracex.py:43 — 形式・RTOS・規格の定数（名前・コードの表）
- tracefmt/tracex.py:52 — 形式・RTOS・規格の定数（名前・コードの表）
- tracefmt/tracex.py:103 — ビットの取り出し・組み立て（形式・レジスタの仕様）
- tracefmt/tracex.py:108 — 引数・値の範囲の検査（規格・形式の範囲）
- tracefmt/tracex.py:136 — 0・1 で切る／0 除算を避ける算術（区間の重なり・個数）
- tracefmt/tracex.py:147 — 形式・RTOS の定める種類・コードでの分岐
- tracefmt/tracex.py:151 — 形式・RTOS の定める種類・コードでの分岐
- tracefmt/tracex.py:157 — 形式・RTOS の定める種類・コードでの分岐

### 3.2 E（使う側が渡す引数） — 35 行

- profile/dwt.py:122 — headroom の既定 1.0 = 帯域の上限そのもの（余裕は使う側が明示。台帳 7f98b11）
- profile/dwt.py:150 — headroom（使う側が明示する余裕。既定 1.0 = 帯域の上限そのもの）で間隔を延ばす
- profile/pcprofile.py:78 — 信頼区間の信頼度 confidence（既定 0.95。結果に confidence として出す区間の定義。判定には使わない）
- profile/pcprofile.py:118 — 信頼区間の信頼度 confidence（既定 0.95。結果に confidence として出す区間の定義。判定には使わない）
- profile/stats.py:19 — 信頼区間の信頼度 confidence（既定 0.95。結果に confidence として出す区間の定義。判定には使わない）
- rtos/analysis.py:6 — 使う側が渡す idle（アイドルのタスク）の集合を使う／「アイドル」という出来事・欄
- rtos/analysis.py:7 — 使う側が渡す idle（アイドルのタスク）の集合を使う／「アイドル」という出来事・欄
- rtos/analysis.py:8 — 使う側が渡す idle（アイドルのタスク）の集合を使う／「アイドル」という出来事・欄
- rtos/analysis.py:11 — 使う側が渡す idle（アイドルのタスク）の集合を使う／「アイドル」という出来事・欄
- rtos/analysis.py:110 — 関数の引数（既定は判断に関わらない）
- rtos/analysis.py:118 — 使う側が渡す idle（アイドルのタスク）の集合を使う／「アイドル」という出来事・欄
- rtos/analysis.py:144 — 使う側が渡す idle（アイドルのタスク）の集合を使う／「アイドル」という出来事・欄
- rtos/analysis.py:145 — 使う側が渡す idle（アイドルのタスク）の集合を使う／「アイドル」という出来事・欄
- rtos/analysis.py:155 — 使う側が渡す idle（アイドルのタスク）の集合を使う／「アイドル」という出来事・欄
- rtos/analysis.py:199 — 関数の引数（既定は判断に関わらない）
- rtos/analysis.py:203 — 使う側が渡す idle（アイドルのタスク）の集合を使う／「アイドル」という出来事・欄
- rtos/analysis.py:216 — 関数の引数（既定は判断に関わらない）
- rtos/analysis.py:220 — 使う側が渡す idle（アイドルのタスク）の集合を使う／「アイドル」という出来事・欄
- rtos/analysis.py:226 — 使う側が渡す idle（アイドルのタスク）の集合を使う／「アイドル」という出来事・欄
- rtos/analysis.py:413 — 関数の引数（既定は判断に関わらない）
- rtos/analysis.py:417 — 使う側が渡す idle（アイドルのタスク）の集合を使う／「アイドル」という出来事・欄
- rtos/analysis.py:422 — 使う側が渡す idle（アイドルのタスク）の集合を使う／「アイドル」という出来事・欄
- rtos/analysis.py:425 — 使う側が渡す idle（アイドルのタスク）の集合を使う／「アイドル」という出来事・欄
- rtos/analysis.py:427 — 使う側が渡す idle（アイドルのタスク）の集合を使う／「アイドル」という出来事・欄
- rtos/analysis.py:428 — 使う側が渡す idle（アイドルのタスク）の集合を使う／「アイドル」という出来事・欄
- rtos/analysis.py:446 — 使う側が渡す idle（アイドルのタスク）の集合を使う／「アイドル」という出来事・欄
- rtos/analysis.py:447 — 使う側が渡す idle（アイドルのタスク）の集合を使う／「アイドル」という出来事・欄
- rtos/timeline.py:196 — 関数の引数（既定は判断に関わらない）
- rtos/timeline.py:256 — 関数の引数（既定は判断に関わらない）
- rtos/timeline.py:260 — 使う側が渡す idle（アイドルのタスク）の集合を使う／「アイドル」という出来事・欄
- rtos/timeline.py:264 — 使う側が渡す idle（アイドルのタスク）の集合を使う／「アイドル」という出来事・欄
- rtos/timeline.py:265 — 使う側が渡す idle（アイドルのタスク）の集合を使う／「アイドル」という出来事・欄
- trace/reconstruct.py:111 — 関数の引数（既定は判断に関わらない）
- tracefmt/model.py:98 — 記録が申告したアイドルのタスクの集合の欄
- tracefmt/model.py:129 — 関数の引数（既定は判断に関わらない）

### 3.3 O（操作の既定） — 83 行

- profile/cycles.py:36 — データクラスの欄の初期値（数える欄の 0 等。判定に関わらない）
- profile/cycles.py:37 — warnings の欄の用意・受け渡し
- profile/cycles.py:42 — warnings の欄の用意・受け渡し
- profile/cycles.py:64 — warnings の欄の用意・受け渡し
- profile/cycles.py:69 — warnings の欄の用意・受け渡し
- profile/dwt.py:73 — warnings の欄の用意・受け渡し
- profile/dwt.py:128 — warnings の欄の用意・受け渡し
- profile/dwt.py:139 — warnings の欄の用意・受け渡し
- profile/dwt.py:173 — warnings の欄の用意・受け渡し
- profile/dwt.py:185 — warnings の欄の用意・受け渡し
- profile/dwt.py:193 — warnings の欄の用意・受け渡し
- profile/dwt.py:208 — warnings の欄の用意・受け渡し
- profile/exceptions.py:46 — データクラスの欄の初期値（数える欄の 0 等。判定に関わらない）
- profile/exceptions.py:133 — データクラスの欄の初期値（数える欄の 0 等。判定に関わらない）
- profile/exceptions.py:134 — データクラスの欄の初期値（数える欄の 0 等。判定に関わらない）
- profile/pcprofile.py:119 — データクラスの欄の初期値（数える欄の 0 等。判定に関わらない）
- profile/pcprofile.py:120 — データクラスの欄の初期値（数える欄の 0 等。判定に関わらない）
- profile/pcprofile.py:123 — データクラスの欄の初期値（数える欄の 0 等。判定に関わらない）
- profile/samples.py:30 — データクラスの欄の初期値（数える欄の 0 等。判定に関わらない）
- profile/samples.py:31 — データクラスの欄の初期値（数える欄の 0 等。判定に関わらない）
- profile/stats.py:79 — ヒストグラムの区切りの数の既定 20（見せ方）
- profile/timing.py:36 — ヒストグラムの区切りの数の既定 20（分布の見せ方。値は変えない）
- profile/timing.py:65 — ヒストグラムの区切りの数の既定 20（見せ方）
- profile/timing.py:73 — ヒストグラムの区切りの数の既定 20（見せ方）
- rtos/analysis.py:363 — 推移の点の数の既定 200（窓ごとの最悪値で間引く。points_width を出す）
- rtos/base.py:31 — warnings の欄の用意・受け渡し
- rtos/base.py:173 — 変数・引数の 0 の初期値（判定に関わらない）
- rtos/flow.py:32 — キューの推移の点の数の既定 500（窓ごとの最大で間引く。points_total を出す。見せ方）
- rtos/flow.py:160 — 変数・引数の 0 の初期値（判定に関わらない）
- rtos/flow.py:281 — 値の無いときの欄の初期化
- rtos/freertos.py:119 — warnings の欄の用意・受け渡し
- rtos/freertos.py:349 — warnings の欄の用意・受け渡し
- rtos/freertos_objects.py:317 — 変数の初期化（0 から数える）
- rtos/freertos_objects.py:381 — 変数の初期化（0 から数える）
- rtos/kernel_trace.py:48 — データクラスの欄の初期値（数える欄の 0 等。判定に関わらない）
- rtos/kernel_trace.py:50 — データクラスの欄の初期値（数える欄の 0 等。判定に関わらない）
- rtos/kernel_trace.py:51 — データクラスの欄の初期値（数える欄の 0 等。判定に関わらない）
- rtos/kernel_trace.py:53 — データクラスの欄の初期値（数える欄の 0 等。判定に関わらない）
- rtos/kernel_trace.py:100 — データクラスの欄の初期値（数える欄の 0 等。判定に関わらない）
- rtos/nuttx.py:212 — warnings の欄の用意・受け渡し
- rtos/nuttx.py:356 — warnings の欄の用意・受け渡し
- rtos/rtx5.py:159 — warnings の欄の用意・受け渡し
- rtos/rtx5.py:311 — warnings の欄の用意・受け渡し
- rtos/structs.py:42 — 変数の初期化（0 から数える）
- rtos/threadx.py:199 — warnings の欄の用意・受け渡し
- rtos/threadx.py:315 — warnings の欄の用意・受け渡し
- rtos/timeline.py:52 — データクラスの欄の初期値（数える欄の 0 等。判定に関わらない）
- rtos/timeline.py:65 — データクラスの欄の初期値（数える欄の 0 等。判定に関わらない）
- rtos/timeline.py:133 — 変数の初期化（0 から数える）
- rtos/timeline.py:143 — 変数の初期化（0 から数える）
- rtos/timeline.py:162 — データクラスの欄の初期値（数える欄の 0 等。判定に関わらない）
- rtos/ucos3.py:198 — warnings の欄の用意・受け渡し
- rtos/ucos3.py:323 — warnings の欄の用意・受け渡し
- rtos/zephyr.py:70 — warnings の欄の用意・受け渡し
- rtos/zephyr.py:128 — 変数の初期化（0 から数える）
- rtos/zephyr.py:205 — warnings の欄の用意・受け渡し
- rtos/zephyr_objects.py:141 — 変数の初期化（0 から数える）
- rtos/zephyr_objects.py:154 — 変数の初期化（0 から数える）
- trace/coresight.py:47 — ETM の止まる・動き出すを待つ時間切れ 0.2 s（操作。超えたら RuntimeError で止める）
- trace/coresight.py:50 — ETM の止まる・動き出すを待つ時間切れ（操作。結果の判定ではない）
- trace/coresight.py:51 — ETM の止まる・動き出すを待つ時間切れ 0.2 s（操作。超えたら RuntimeError で止める）
- trace/coresight.py:57 — ETM の止まる・動き出すを待つ時間切れ（操作。結果の判定ではない）
- trace/coresight.py:100 — ETM の構成の既定（トレース ID・サイクル数の閾値・同期の間隔。解読の結果の判断に関わらない）
- trace/coresight.py:102 — ETM の構成の既定（トレース ID・サイクル数の閾値・同期の間隔。解読の結果の判断に関わらない）
- trace/coresight.py:104 — ETM の構成の既定（トレース ID・サイクル数の閾値・同期の間隔。解読の結果の判断に関わらない）
- trace/coresight.py:140 — ETM の止まる・動き出すを待つ時間切れ（操作。結果の判定ではない）
- trace/coresight.py:146 — ETM の止まる・動き出すを待つ時間切れ（操作。結果の判定ではない）
- trace/coresight.py:157 — ETM の止まる・動き出すを待つ時間切れ（操作。結果の判定ではない）
- trace/coresight.py:158 — ETM の止まる・動き出すを待つ時間切れ（操作。結果の判定ではない）
- trace/elements.py:41 — データクラスの欄の初期値（数える欄の 0 等。判定に関わらない）
- trace/elements.py:93 — データクラスの欄の初期値（数える欄の 0 等。判定に関わらない）
- trace/etmv4.py:222 — 変数の初期化（0 から数える）
- tracefmt/ctf.py:27 — import（判定は model.py:28）
- tracefmt/ctf.py:110 — 変数の初期化（0 から数える）
- tracefmt/eventrecorder.py:22 — import（判定は model.py:28）
- tracefmt/model.py:23 — __all__（公開名の列）
- tracefmt/model.py:95 — データクラスの欄の初期値（数える欄の 0 等。判定に関わらない）
- tracefmt/model.py:96 — データクラスの欄の初期値（数える欄の 0 等。判定に関わらない）
- tracefmt/scvd.py:122 — 変数・引数の 0 の初期値（判定に関わらない）
- tracefmt/scvd.py:507 — 変数の初期化（0 から数える）
- tracefmt/systemview.py:27 — import（判定は model.py:28）
- tracefmt/systemview.py:113 — 変数・引数の 0 の初期値（判定に関わらない）
- tracefmt/tracealyzer.py:27 — import（判定は model.py:28）

### 3.4 D（見せ方） — 80 行

- profile/aggregate.py:31 — 表示の名前（"(other)"・"(unknown)"）
- profile/aggregate.py:32 — 表示の名前（"(other)"・"(unknown)"）
- profile/aggregate.py:118 — 表の上位 n 行（多い順。総数 groups_total・total は別に出す）の既定
- profile/aggregate.py:126 — グループの中の多い関数 members 個（既定 5。数の多い順の表示。グループの合計は別に出す）
- profile/aggregate.py:127 — 並べ替え（時刻・名前・数の順。値は変えない）
- profile/aggregate.py:133 — timeline の top 個のグループの既定 8（残りは (other) の割合）
- profile/aggregate.py:144 — timeline の top 個のグループ（サンプル数の多い順。残りは "(other)" として割合を出す。暗黙に捨てない）
- profile/aggregate.py:150 — 並べ替え（時刻・名前・数の順。値は変えない）
- profile/aggregate.py:171 — 並べ替え（時刻・名前・数の順。値は変えない）
- profile/aggregate.py:205 — 並べ替え（時刻・名前・数の順。値は変えない）
- profile/aggregate.py:217 — 並べ替え（時刻・名前・数の順。値は変えない）
- profile/coverage.py:84 — 並べ替え（時刻・名前・数の順。値は変えない）
- profile/coverage.py:108 — 並べ替え（時刻・名前・数の順。値は変えない）
- profile/coverage.py:114 — 並べ替え（時刻・名前・数の順。値は変えない）
- profile/coverage.py:116 — 並べ替え（時刻・名前・数の順。値は変えない）
- profile/cycles.py:50 — 並べ替え（時刻・名前・数の順。値は変えない）
- profile/dwt.py:103 — 並べ替え（時刻・名前・数の順。値は変えない）
- profile/exceptions.py:158 — 並べ替え（時刻・名前・数の順。値は変えない）
- profile/pcprofile.py:158 — 並べ替え（時刻・名前・数の順。値は変えない）
- profile/pcprofile.py:163 — 並べ替え（時刻・名前・数の順。値は変えない）
- profile/pcprofile.py:172 — 並べ替え（時刻・名前・数の順。値は変えない）
- profile/pcprofile.py:179 — 並べ替え（時刻・名前・数の順。値は変えない）
- profile/pcprofile.py:184 — 表の上位 n 行（多い順。総数 groups_total・total は別に出す）の既定
- profile/stats.py:41 — 並べ替え（時刻・名前・数の順。値は変えない）
- profile/timing.py:20 — 並べ替え（時刻・名前・数の順。値は変えない）
- profile/timing.py:37 — 長い順に返す数の既定 10（並べるだけ。全体の数・分布は別に出す）
- profile/timing.py:45 — 並べ替え（時刻・名前・数の順。値は変えない）
- profile/timing.py:66 — 長い順に返す数の既定 10（並べるだけ。全体の数・分布は別に出す）
- rtos/analysis.py:43 — 並べ替え（時刻・名前・数の順。値は変えない）
- rtos/analysis.py:147 — 並べ替え（時刻・名前・数の順。値は変えない）
- rtos/analysis.py:205 — 並べ替え（時刻・名前・数の順。値は変えない）
- rtos/analysis.py:212 — 並べ替え（時刻・名前・数の順。値は変えない）
- rtos/analysis.py:213 — 並べ替え（時刻・名前・数の順。値は変えない）
- rtos/analysis.py:222 — 並べ替え（時刻・名前・数の順。値は変えない）
- rtos/analysis.py:223 — 並べ替え（時刻・名前・数の順。値は変えない）
- rtos/analysis.py:285 — 並べ替え（時刻・名前・数の順。値は変えない）
- rtos/analysis.py:367 — 並べ替え（時刻・名前・数の順。値は変えない）
- rtos/analysis.py:394 — 並べ替え（時刻・名前・数の順。値は変えない）
- rtos/analysis.py:484 — 並べ替え（時刻・名前・数の順。値は変えない）
- rtos/analysis.py:518 — 並べ替え（時刻・名前・数の順。値は変えない）
- rtos/base.py:107 — 並べ替え（時刻・名前・数の順。値は変えない）
- rtos/flow.py:108 — 並べ替え（時刻・名前・数の順。値は変えない）
- rtos/flow.py:109 — 並べ替え（時刻・名前・数の順。値は変えない）
- rtos/flow.py:193 — 並べ替え（時刻・名前・数の順。値は変えない）
- rtos/flow.py:300 — 並べ替え（時刻・名前・数の順。値は変えない）
- rtos/flow.py:417 — 並べ替え（時刻・名前・数の順。値は変えない）
- rtos/freertos.py:150 — 並べ替え（時刻・名前・数の順。値は変えない）
- rtos/freertos_objects.py:34 — 表示名の出どころの優先の順（登録簿 > カーネル > 変数 > 待ち手）。他の名前は aliases に残す
- rtos/freertos_objects.py:62 — 表示名の出どころの優先の順（登録簿 > カーネル > 変数 > 待ち手）。他の名前は aliases に残す
- rtos/freertos_objects.py:98 — 並べ替え（時刻・名前・数の順。値は変えない）
- rtos/freertos_objects.py:114 — 並べ替え（時刻・名前・数の順。値は変えない）
- rtos/freertos_objects.py:118 — 並べ替え（時刻・名前・数の順。値は変えない）
- rtos/nuttx.py:32 — 名前を読む長さの上限（文字列の切り詰め。見せ方）
- rtos/nuttx.py:147 — 名前を読む長さの上限（文字列の切り詰め。見せ方）
- rtos/nuttx.py:209 — 並べ替え（時刻・名前・数の順。値は変えない）
- rtos/nuttx_objects.py:41 — 並べ替え（時刻・名前・数の順。値は変えない）
- rtos/nuttx_objects.py:68 — 並べ替え（時刻・名前・数の順。値は変えない）
- rtos/rtx5.py:34 — 名前を読む長さの上限（文字列の切り詰め。見せ方）
- rtos/rtx5_objects.py:49 — 並べ替え（時刻・名前・数の順。値は変えない）
- rtos/rtx5_objects.py:70 — 並べ替え（時刻・名前・数の順。値は変えない）
- rtos/rtx5_objects.py:72 — 並べ替え（時刻・名前・数の順。値は変えない）
- rtos/threadx.py:34 — 名前を読む長さの上限（文字列の切り詰め。見せ方）
- rtos/threadx.py:35 — 名前を読む長さの上限（文字列の切り詰め。見せ方）
- rtos/threadx_objects.py:43 — 並べ替え（時刻・名前・数の順。値は変えない）
- rtos/threadx_objects.py:64 — 並べ替え（時刻・名前・数の順。値は変えない）
- rtos/threadx_objects.py:66 — 並べ替え（時刻・名前・数の順。値は変えない）
- rtos/timeline.py:126 — 並べ替え（時刻・名前・数の順。値は変えない）
- rtos/timeline.py:203 — 並べ替え（時刻・名前・数の順。値は変えない）
- rtos/ucos3.py:35 — 名前を読む長さの上限（文字列の切り詰め。見せ方）
- rtos/ucos3.py:188 — 並べ替え（時刻・名前・数の順。値は変えない）
- rtos/ucos3_objects.py:69 — 並べ替え（時刻・名前・数の順。値は変えない）
- rtos/ucos3_objects.py:96 — 並べ替え（時刻・名前・数の順。値は変えない）
- rtos/ucos3_objects.py:98 — 並べ替え（時刻・名前・数の順。値は変えない）
- rtos/zephyr_objects.py:45 — 並べ替え（時刻・名前・数の順。値は変えない）
- rtos/zephyr_objects.py:54 — 並べ替え（時刻・名前・数の順。値は変えない）
- rtos/zephyr_objects.py:57 — 並べ替え（時刻・名前・数の順。値は変えない）
- trace/tpiu.py:37 — 並べ替え（時刻・名前・数の順。値は変えない）
- tracefmt/eventrecorder.py:185 — 並べ替え（時刻・名前・数の順。値は変えない）
- tracefmt/model.py:111 — 並べ替え（時刻・名前・数の順。値は変えない）
- tracefmt/scvd.py:856 — 文字列を読む長さの上限 128（切り詰め。見せ方）

## 4. 対象のファイル

`rg --files -t py rtos profile trace tracefmt` で **51 ファイル・10,744 行**（rtos 25・profile 11・trace 6・tracefmt 9）。7 種類（11 本）の検索はすべて 4 つのディレクトリ全体に対して実行した。49 ファイルに 1 件以上当たった。当たらなかった 2 ファイル（`profile/__init__.py`・`tracefmt/__init__.py`）は再公開だけで、目で読んで判定の無いことを確かめた。

分類の手順: 全ヒットを (ファイル, 行) で重複を除き、ソースの行を読み直して分類した。定型（レジスタの定数・ビット演算・形式のコードの分岐・並べ替え・0 除算の回避・データクラスの欄の初期値）は規則で仮に分類し、規則に当たらない行と H・? の候補はソースの前後を読んで手で決めた。
