# ヒューリスティックの 3 回目の点検: host/fprobe の下位フォルダ

- 範囲: host/fprobe の api・data・decode・dwarf・export・gdb・linux・logfmt・testpoint・rtos・profile・trace・tracefmt の
  .py すべて（plugins・テスト・tests/fake_* は除く）。131 ファイル。
- 方法: 検索で候補を拾わず、各ファイルを先頭から最後まで読んだ。前の点検の報告・台帳は読んでいない。
- 基準: CLAUDE.md の「fprobe 本体にヒューリスティックを入れない」と、課題の文の補足（境界・既定は使う側が明示、
  規格の値は出典つきなら既定可、名前の慣習の判定も経験則、打ち切りは切ったことと上限を返す、推定はプラグインだけ）。
- 分類: **H** = ヒューリスティック（原則に反する）、**?** = 判断がつかない（理由を書く）。
- コードは変えていない。

## H（ヒューリスティック）

### H1. PC サンプリングの割合に、独立・ランダムな抽出を仮定した信頼区間を付ける（既定の信頼度 0.95）

- profile/stats.py:19 `def wilson_interval(hits: int, total: int, confidence: float = 0.95)`
- profile/pcprofile.py:78 `def profile(self, samples: PcSamples, *, confidence: float = 0.95, ...)`、
  :118 `confidence: float = 0.95`、:145-148 `_stat` が各行に `"ci": [lo, hi]` を付ける
- profile/aggregate.py:123 `lo, hi = wilson_interval(n, profile.total, profile.confidence)`（group_profile の行の ci）
- なぜ: Wilson の区間は「各サンプルが独立な二項の試行」という仮定の上の推定。PC サンプリングは周期の抽出で、この仮定は
  成り立たない（notes にエイリアシングは含まないと書いてはいるが、区間そのものは結果の欄として本体が出している）。
  CLAUDE.md が名指しで禁じる「仮定に頼る推定（ランダムな時刻なら等）」に当たる。信頼度 0.95 も使う側が明示しない
  暗黙の既定（API の既定値として本体に入っている）。

### H2. 例外トレースの負荷が「信頼区間の外」かで注記を出す（判定）

- profile/aliasing.py:14-25
  ```python
  def aliasing_notes(sampled, traced, total, confidence: float = 0.95) -> list[str]:
      ...
      low, high = wilson_interval(hits, total, confidence)
      if not low <= load <= high:
          notes.append(f"{name}: 例外トレースで測った負荷 ... の {confidence:.0%} 信頼区間 ... の外")
  ```
- なぜ: H1 の仮定に頼る区間を境に「外か」を判定して文を出す。判定の境（信頼度）も既定 0.95 で暗黙。原因は断定しないと
  書いてあるが、注記を出す・出さないの判定そのものが仮定に頼っている。

## ?（判断がつかない）

### ?1. SWD のビット列の上で、要求の形（start=1・stop=0・park=1）を 1 ビットずつずらして探す。パリティの誤った要求は ACK の相が 1 1 1 のときだけ要求と認める

- decode/swd.py:159-174
  ```python
  if stop != 0 or park != 1:
      return i + 1
  ...
  if par != (ap ^ rnw ^ a2 ^ a3):
      # パリティの誤った要求: ターゲットは応答しない（ACK の相がプルアップの 1）ときだけ要求と認める
      a = i + 8 + trn
      if a + 3 <= n and bits[a] and bits[a + 1] and bits[a + 2]:
          self.emit(i, 8, "request", "REQUEST (parity)", error="request_parity")
  ```
- 理由: 規格の形での照合だが、どこを要求の始まりとみなすかは形の一致による推測（データの中に同じ並びがあれば要求と
  読みうる）。「パリティ誤りでも ACK が 111 なら要求」は規格の振る舞いから導いた規則だが、雑音との区別に頼っている。

### ?2. SPI Flash の命令の表が出典の無い「一般的な」表で、名前・アドレス長・ダミー長・書き込みの許可の判定を決める

- decode/spiflash.py:13-32（`COMMANDS`・`_WRITES`）、:72-76（READ_STATUS の bit の割り当て `wip/wel/bp/srp`）、
  :80-83（`error = "write_disabled"`）
- 理由: 0x35・0x15・0x31・0x11・0x4B・0x75/0x7A 等は製造元で意味が違う（JEDEC で決まるものと混ざっている）。出典を
  書いておらず、表が合わない機器では名前・データの切り分け・write_disabled の誤りが誤って付く。規格の表か、使う側が
  渡す表かがはっきりしない。

### ?3. DWARF の型の欠けを暗黙の値で埋める

- dwarf/typesys.py:259 `encoding = _ENCODINGS.get(_int_attr(die, "DW_AT_encoding") or 0, "unsigned")`
- dwarf/typesys.py:265-267 基底の型の無い列挙は `any(v < 0 for _, v in values)` で符号を、大きさは `or 4` で決める
- dwarf/typesys.py:305 `storage = _int_attr(die, "DW_AT_byte_size") or t.size or 4`（DWARF 2/3 のビットフィールド）
- dwarf/info.py:459 `size = t.size or 4`（DW_AT_const_value の値の大きさ）
- 理由: DWARF に無い値を「unsigned」「4 バイト」「値に負があれば符号付き」で補う。値の見た目からの推定と暗黙の既定。
  ただし多くのコンパイラの実際と一致し、ここで補わなければ読めない場面もある。補ったことは結果に出ない。

### ?4. PC サンプルの近似カバレッジを lcov・gcov に書くと、サンプルの無い行が「実行されなかった」（DA:0・#####）になる

- export/profile_formats.py:179-196（`coverage_from_profile`。docstring は「通らなかったとは限らない」）、
  :254-273（`render_gcov` の `mark = "-" if count is None else "#####" if count == 0 else str(count)`）
- 理由: 本体の結果（FileCoverage）は事実だが、書き出しの形式の上では 0 が「未実行」の判定として読まれる。形式に
  「分からない」が無いことによる写し方の問題で、ヒューリスティックかどうか判断がつかない。

### ?5. GDB のメモリマップで Flash の消去の単位が分からないとき 1 KB とする

- gdb/description.py:101 `out.append(f'{head}><property name="blocksize">0x{r.blocksize or 0x400:x}</property></memory>')`
- 理由: 機器の事実の無い所を出典の無い 0x400 で埋める暗黙の既定。GDB はこの値で書き込みの区切りを決める。

### ?6. 番地のログ（compact）で、同期を取り直すとき「ELF の文字列を指す語」を書式とみなす

- logfmt/compact.py:91-114
  ```python
  fmt = self.strings.get(address) if self.strings.has_string(address) else None
  if fmt is None:
      self._skipped += self._buf[:4]
  ```
- 理由: ELF の中身だけで決めると明記してあるが、引数の語がたまたま文字列の番地なら書式として読む推測の同期。読み飛ばした
  ことは undecoded の行で出るが、取り直した位置が正しいかは結果から分からない。

### ?7. CPU クロックの推定に、最小二乗の傾きと独立な誤差を仮定した標準誤差を付ける

- profile/cycles.py:45-69（`estimate_clock`。`stderr = (sum(r * r ...) / (n - 2) / sxx) ** 0.5`）
- 理由: 傾きは計算だが、stderr（と ppm）は残差の独立・等分散の仮定の上の推定。名前も「推定（estimate）」。

### ?8. SWO の帯域の見積もりでサンプルの間隔を自動で延ばす

- profile/dwt.py:54 `_PC_PACKET = 5  # 眠りのときは 2 バイトだが、多い方で見積もる`、:106-117（タイムスタンプの大きさを
  間隔から仮定）、:150-160（`while usage(index)[1] > headroom ...: index += 1; fitted = True`）
- 理由: headroom は使う側の明示だが、使用率は仮定（最大のパケット・タイムスタンプの差 = 間隔）の見積もりで、それで設定を
  自動で変える。警告は出る。設定の計画なので測った結果ではないが、判断を本体がしている。

### ?9. FreeRTOS のメッセージバッファの長さの語を 4 バイトと仮定して数える

- rtos/freertos_objects.py:39 `MESSAGE_LENGTH_BYTES = 4  # configMESSAGE_BUFFER_LENGTH_TYPE の既定（size_t）`、:278-293
- 理由: notes に「と仮定」と書いて返すが、使う側が明示できない暗黙の既定。設定が違えば数が誤る（一貫しないときの注記はある）。

### ?10. RTX5 でスタックの底のマジックワードが無いと overflow = True にする

- rtos/rtx5.py:254-261
  ```python
  if magic != MAGIC_WORD:
      info = check_stack(self.access.read_memory, base, end, pointer, FILL_BYTE)
      info.overflow = True
  ```
- 理由: RTX5 自身の印による判定だが、スタックの検査を有効にしていない設定（マジックワードを書かない）でも「あふれ」と
  判定する。設定の事実を確かめずに RTOS の慣習で判定している可能性がある。

### ?11. スタックの塗りが「残っているか」を底の 16 バイトで決める

- rtos/stackcheck.py:18 `GUARD = 16  # 底のこのバイト数が塗りのままかを見る（FreeRTOS の方法 2 と同じ幅）`、:53-58
- 理由: 出典（FreeRTOS の方法）はあるが、intact / none を分ける境の幅が固定値で使う側が渡せない。none の側は事実
  （fill_run_above）を添えている。

### ?12. 方法 A の時間軸の分解能をサンプルの間隔の中央値にし、gap_factor の基準にする

- rtos/timeline.py:129-131 `achieved = statistics.median(diffs) ...; resolution = max(period, achieved) ...`、:140
  `if gap_factor is not None and resolution and dt > gap_factor * resolution:`
- 理由: gap_factor は明示の引数だが、掛ける基準（中央値を分解能とする）は本体が選んだ代表値。中央値を選ぶこと自体が
  暗黙の決め。既定では抜けにしないので結果には影響しない。

### ?13. 待ちの結果（ok / failed）を「起きたタスクの次の操作」で対応付ける

- rtos/flow.py:150-172（`blocking`。`w = awaiting.pop(op.actor)` → 同じオブジェクトへの成功なら ok、失敗なら failed）
- 理由: カーネルの振る舞い（待ちから戻ると続きの操作の出来事が出る）を前提にした対応付け。docstring に前提を書いているが、
  前提が外れる版・ポートでは誤る。

### ?14. スタック・ヒープの推移の窓に、前のサンプルの値が続いたとみなして入れる

- rtos/analysis.py:363-393（`memory_series`。`values = [] if current is None else [current]`、docstring「値は次の
  サンプルまで続く」）
- 理由: サンプルの無い窓を前の値で埋める仮定（sample and hold）。points の既定 200 と窓の幅は結果に返している。

### ?15. 前処理をしない C の解析で、マクロの塊・宣言を形で推測する（テストポイントの挿入）

- testpoint/cparse.py:399-407（`名前 (...) {` を Opaque = マクロのループとみなす）、:315（戻り値の型が空なら `"int"`）
- testpoint/instrument.py:315-331（`_lvalue_before`。`*p = …` と `int *p = …` を直前の字句の形で見分ける）
- 理由: 測定の結果ではなく挿入の道具だが、名前・字句の形による判定。誤ると挿入が壊れる（多くは Unsupported で断る）。

### ?16. RTT のテストポイントの CYCCNT の回り込みを「記録の間が 2^32 クロックより短い」前提で戻し、同期を印で取り直す

- testpoint/decode.py:213-250（`RttDecoder`。`if self._last is not None and cyc < self._last: high += 1 << 32`、
  `if flags & 0xFFFE != RTT_MARK or probe_id == 0: ... del self._buf[0]`）
- 理由: 前提は docstring にあるが結果には出ない。前提が外れると時刻が黙ってずれる。

### ?17. 32 ビットの時刻の桁あふれを「前の出来事から 1 周未満」とみなして戻す

- tracefmt/model.py:188-192 `unwrap32`（ctf.py:289・eventrecorder.py:133・tracealyzer.py:185 が使う）、
  tracefmt/tracex.py:122 も同じ考え
- 理由: 32 ビットしか送られない形式では避けられない処理だが、1 周（CTF の ns で約 4.29 秒）を超える間があると黙って
  時刻がずれる。前提は結果の notes に出ない。

### ?18. Event Recorder の作成・削除を、その handle が切り替え（task_in）に出たときだけタスクのものとする

- tracefmt/eventrecorder.py:190-200（`_task_lifecycle`）
- 理由: 成分の名前では決めないが、「同じ handle が切り替えに出た」ことからタスクだと推論する。読みの範囲に切り替えが
  無ければ同じ出来事が object になり、読み方で分類が変わる。

### ?19. SCVD の重要度の名前の写しと、知らない型を uint32 とみなす

- tracefmt/scvd.py:34 `LEVELS = {"Error": "error", "API": "info", "Op": "info", "Detail": "debug"}`
- tracefmt/scvd.py:53-54 `return _SCALARS.get(t, (4, "I"))`、:305 `size, code = _SCALARS.get(kind, (4, "I"))`、
  :567・:569 型の欄が無いメンバを `"uint32_t"` とする
- 理由: 重要度の写しは fprobe の決め（出典の無い対応）。知らない型の 4 バイト符号なしは暗黙の既定。

### ?20. Tracealyzer のエントリの表の妥当な範囲と、コードの範囲での種類分け

- tracefmt/tracealyzer.py:136-138
  `if states > 16 or symbol_size > 256 or count > 4096: raise TracealyzerError(...)`
- tracefmt/tracealyzer.py:270 `kind = "object" if code in _CODES and code >= 0x11 else "other"`
- 理由: 範囲は出典の無い固定値で、超えると読まない（誤りの文に範囲を書くので黙ってはいない）。0x11 以上を object と
  するのは番号の範囲での分類で、NEW_TIME・MALLOC・LOWPOWER・UNUSED_STACK 等もカーネルオブジェクトの操作になる。

### ?21. ETMv4 の解読の構成の既定（commit_opt1 = True・vmid 1 バイト・cid 4 バイト）

- trace/etmv4.py:28-35（`Etmv4Config`）、trace/__init__.py:24 `Etmv4Decoder(config or Etmv4Config())`
- 理由: 本来は TRCIDRn から決まる値（coresight.Etmv4Info.decoder_config で渡す道もある）。渡さないと暗黙の値で解読し、
  機器と違えば黙って誤った要素になる。

## 判断に入れなかったもの（参考）

- decode/synth.py・testpoint/synth.py の既定（sample_point 0.75・1-Wire の時間・store_s 等）は試験・デモの波形の発生器で、
  測った結果ではないので対象外とした。
- 規格・RTOS のソースに出典のある値（CAN の休止 11 ビット・Modbus の t3.5・1-Wire の時間・JTAG の TMS 5 回・SWD の
  line reset 50 ビット・各 RTOS の状態の値・TraceX / SystemView / CTF の表）は既定でよい扱い。
- 打ち切り（MAX_CHAIN・MAX_LIST_ITEMS・name_limit・max_frames・calls_limit・top 等）は、切ったことと上限を結果か
  warnings に返していることを確かめた。dwarf/values.py の配列の max_elements は {"length", "head"} で切ったことが分かる。
- 操作の既定（api/arbiter.py の hold_s 300・gdb の frequency 4 MHz・linux の watchdog の 1/3 の ping 等）は測定の
  判断ではないので入れていない。

## 読んだファイル（すべて先頭から最後まで読んだ）

api/__init__.py（26 行）、api/__main__.py（11 行）、api/activity.py（52 行）、api/arbiter.py（123 行）、api/binary.py（102 行）、
api/client.py（126 行）、api/daemon.py（766 行）、api/gen.py（72 行）、api/spec.py（55 行）、
data/connect_scripts/reset_with_nreset.py（17 行）、data/connect_scripts/stm32_debug_low_power.py（24 行）、
decode/__init__.py（65 行）、decode/can.py（309 行）、decode/core.py（149 行）、decode/eeprom.py（85 行）、decode/i2c.py（187 行）、
decode/i2s.py（72 行）、decode/jtag.py（199 行）、decode/manchester.py（107 行）、decode/modbus.py（194 行）、
decode/onewire.py（173 行）、decode/parallel.py（84 行）、decode/registry.py（250 行）、decode/spi.py（123 行）、
decode/spiflash.py（105 行）、decode/swd.py（256 行）、decode/synth.py（589 行）、decode/uart.py（107 行）、
dwarf/__init__.py（20 行）、dwarf/access.py（100 行）、dwarf/cexpr.py（542 行）、dwarf/info.py（497 行）、
dwarf/inspector.py（242 行）、dwarf/location.py（281 行）、dwarf/typesys.py（307 行）、dwarf/unwind.py（333 行）、
dwarf/values.py（207 行）、export/__init__.py（9 行）、export/perfetto.py（271 行）、export/profile_formats.py（360 行）、
gdb/__init__.py（1 行）、gdb/agent.py（339 行）、gdb/client.py（92 行）、gdb/description.py（114 行）、gdb/rsp.py（107 行）、
gdb/server.py（61 行）、gdb/session.py（673 行）、gdb/trace.py（576 行）、linux/__init__.py（1 行）、linux/agent.py（179 行）、
linux/caps.py（85 行）、linux/clock.py（28 行）、linux/gdb_relay.py（134 行）、linux/memap.py（111 行）、linux/perf.py（48 行）、
linux/ptrace_plan.py（41 行）、linux/ring.py（97 行）、linux/ssh.py（67 行）、linux/uprobe.py（98 行）、logfmt/__init__.py（1 行）、
logfmt/addresses.py（85 行）、logfmt/compact.py（348 行）、logfmt/defmt.py（696 行）、logfmt/framing.py（194 行）、
logfmt/printf.py（248 行）、logfmt/record.py（81 行）、logfmt/stream.py（112 行）、logfmt/trice.py（346 行）、
testpoint/__init__.py（29 行）、testpoint/__main__.py（65 行）、testpoint/analysis.py（501 行）、testpoint/clex.py（217 行）、
testpoint/cparse.py（466 行）、testpoint/decode.py（282 行）、testpoint/instrument.py（507 行）、testpoint/live.py（255 行）、
testpoint/probes.py（127 行）、testpoint/record.py（68 行）、testpoint/synth.py（105 行）、testpoint/trace.py（38 行）、
rtos/__init__.py（44 行）、rtos/analysis.py（562 行）、rtos/base.py（209 行）、rtos/contention.py（74 行）、rtos/cortexm.py（175 行）、
rtos/flow.py（458 行）、rtos/frames.py（35 行）、rtos/freertos.py（371 行）、rtos/freertos_objects.py（379 行）、
rtos/kernel_trace.py（183 行）、rtos/model.py（183 行）、rtos/nuttx.py（367 行）、rtos/nuttx_objects.py（114 行）、
rtos/objects.py（168 行）、rtos/rtx5.py（313 行）、rtos/rtx5_objects.py（183 行）、rtos/stackcheck.py（89 行）、
rtos/structs.py（108 行）、rtos/threadx.py（318 行）、rtos/threadx_objects.py（167 行）、rtos/timeline.py（280 行）、
rtos/ucos3.py（328 行）、rtos/ucos3_objects.py（201 行）、rtos/zephyr.py（211 行）、rtos/zephyr_objects.py（236 行）、
profile/__init__.py（26 行）、profile/aggregate.py（220 行）、profile/aliasing.py（27 行）、profile/coverage.py（120 行）、
profile/cycles.py（108 行）、profile/dwt.py（266 行）、profile/exceptions.py（185 行）、profile/pcprofile.py（223 行）、
profile/samples.py（78 行）、profile/stats.py（106 行）、profile/timing.py（89 行）、trace/__init__.py（29 行）、
trace/coresight.py（198 行）、trace/elements.py（153 行）、trace/etmv4.py（316 行）、trace/reconstruct.py（307 行）、
trace/tpiu.py（151 行）、tracefmt/__init__.py（2 行）、tracefmt/ctf.py（322 行）、tracefmt/eventrecorder.py（242 行）、
tracefmt/model.py（193 行）、tracefmt/scvd.py（887 行）、tracefmt/stream.py（156 行）、tracefmt/systemview.py（461 行）、
tracefmt/tracealyzer.py（284 行）、tracefmt/tracex.py（157 行）

計 131 ファイル。いずれも先頭から最後の行まで全部読んだ（パスは host/fprobe/ からの相対）。
