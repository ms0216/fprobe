# 通信の復号・SWO/ITM/DWT/ETM の解読の不具合台帳（検出専用。修正はしていない）

領域: host/fprobe/decode/（uart・spi・i2c・swd ほか）、itm.py、swo_trace.py、probe_swo*.py、trace/（tpiu・etmv4・
reconstruct）、probe_etm.py、uart_flow.py。再現試験は host/tests/test_decode_bugs.py（strict の xfail。直ると XPASS で
失敗して気づく）。分類: A = 静かに誤った結果、B = 失敗が見える、C = 端の条件・まれ、D = 品質。

## 調べた手法と、不具合が見つからなかった範囲

別実装のエンコーダを試験側（スクラッチ）に書き、製品のデコーダへ往復させた。すべて一致（不具合なし）:
- UART: データ 5〜9 ビット × パリティ 5 種 × 停止 1/1.5/2 × 極性 × ビット順 × 1 ビットのサンプル数 2〜33（整数）と
  2.5〜40（小数・位相ずれ）、フレーム間の休止 0/1/3 ビット。各 3000〜4000 通り。
- SPI: モード 0〜3 × 語長 1〜64 × MSB/LSB × CS 極性 × CS あり/なし × MOSI のみ/MISO のみ/両方 × クロック幅 1〜5 サンプル。
- I2C: 7 ビット（ACK/NACK・データ NACK・繰り返しスタート・複数取引・クロック引き延ばし）、10 ビットの書き＋読み。
- SWD: ターンアラウンド 1〜4 × ORUNDETECT × WAIT/FAULT × AP/DP 読み書き × ライン リセット × drive 検査。
- ITM/DWT: 全パケット種別（同期・オーバーフロー・ローカル/グローバルのタイムスタンプ 1〜4 バイト・拡張・ソフト・
  イベントカウンタ・例外・PC サンプル・データトレース）を 1〜7 バイトのチャンクに分けて与えても一括と同じ結果
  （synced / resync の 4 通り、壊れた入力を混ぜても同じ。offset・consumed・unsynced_bytes まで一致）。
- TPIU: ID 切り替え・補助ビット・FSYNC 間隔・HSYNC 混在・先頭のごみ・任意のチャンク分割で、ID ごとの流れが一致。
- 壊れた入力（乱数のビット列・バイト列）で UART/SPI/I2C/SWD/ETMv4/TPIU/ITM が例外を出さず、注釈の範囲がサンプル長に
  収まる。

## 再現済み

### BUG-DEC-01 (A) 再構成が ldr pc,[sp],#4 の事後加算の即値を直接の分岐の行き先にする
- 再現: test_reconstruct_ldr_pc_post_index_is_not_a_direct_branch、test_reconstruct_atoms_after_ldr_pc_post_index_follow_the_address_packet
- 原因: trace/reconstruct.py Reconstructor.insn の `imms = [o.imm for o in decoded.operands if o.type == arm.ARM_OP_IMM]`
  （flow が "jump" の命令なら即値オペランドを行き先とみなす）。`ldr pc, [sp], #4`（pop {pc} の 32 ビット形。GCC が
  よく出す）は capstone で事後加算の 4 が IMM のオペランドになり、行き先 0x4 の直接の分岐になる。
- 直し方の案: 行き先を取るのは b・bl・cbz・cbnz などオペコードが直接の分岐と分かる命令だけにする（mov/ldr/add で PC を
  書く命令は常に間接）。
- 影響: 命令の一覧に `target: 4` が出る。E のアトムがまとまって来る（"EE"）と 2 つ目が 0x4 から読まれて「命令が読めない」
  になり、以降の流れが途切れる（行き先の Address が直後に来ていても）。戻りの関数が多いコードで履歴が欠ける。
- 確信度: 再現済み
- 修正済み: 行き先を取るのは b・bl・blx・cbz・cbnz（条件付き含む）だけ。PC を書く ldr/mov/add は常に間接（trace/reconstruct.py _DIRECT_BRANCH）。

### BUG-DEC-02 (A) 値一致（trace）の DataAccess.address が、比較の窓が変数より広くても変数の番地を言い切る
- 再現: test_value_watch_trace_does_not_claim_exact_address_when_window_is_wider
- 原因: swo_trace.py DataTraceAssembler._emit の `elif use.address is not None and use.mask is not None and
  (1 << use.mask) <= max(use.size, 1)`。値一致の ComparatorUse（role value_watch）の mask は常に 0 で、番地を絞る
  link の比較器の mask（窓の広さ）を見ていない。そのため `(1<<0) <= size` が常に真になる。同様に plan_trace の
  `_estimate` の「変数の外のアクセスも出る」警告も value_watch には出ない（mask 0 で判定）。
- 直し方の案: value_watch の窓の広さは link の mask から取り、窓が変数より広いときは address を None にして警告を出す。
- 影響: 4 バイトの変数が 4 バイト境界に無い（例 0x20000002）と窓は 8 バイトになり、窓の中の別の変数が同じ値を書いても
  「変数の番地への書き込み（complete=True）」として報告される。測っていない番地を事実のように返す（原則違反に近い）。
- 確信度: 再現済み
- 修正済み: 値一致の窓の広さは link の比較器（DATAVADDR0）の mask から取り、窓が変数より広く offset も無ければ address は None。plan_trace の「変数の外も出る」警告も value_watch に出る（swo_trace.py _window_mask）。

### BUG-DEC-03 (B) ETMv4 の解けないバイトの後に A-sync が来ないと、残りのバイトを数も示さず捨てる
- 再現: test_etmv4_bytes_dropped_after_error_without_async_are_accounted
- 原因: trace/etmv4.py Etmv4Decoder.decode。`_Bad` で synced=False にして次の A-sync まで読み飛ばすが、読み飛ばしの数は
  A-sync の要素の `skipped` にしか載らない。A-sync が来ないまま入力が終わると `_find_async` が None で break し、
  捨てたバイト数がどの要素にも出ない（DecodeError は 1 バイトだけ）。末尾の `0x00 0x00` も同様。
- 直し方の案: 入力の終わりで未同期ならば、捨てた区間（offset と長さ）の要素を 1 つ出す（ItmDecoder.unsynced_bytes
  と同じく事実として返す）。
- 影響: 55 バイトのうち 16 バイトしか説明されない。トレースが途中で壊れた事実は DecodeError で分かるが、後ろの 39 バイトが
  捨てられたことも、命令の流れが「ここで終わり」なのか「ここから先が読めていない」のかも分からない（欠けの明示の原則）。
- 確信度: 再現済み
- 修正済み: 同期が来ないまま入力が終わったら、読み飛ばしたバイト全部を載せた要素 Unsynced（kind unsynced）を出す。再構成は errors に載せる。

### BUG-DEC-04 (C) I2C: START を見ていない STOP（捕捉が取引の途中から始まる・迷い STOP）を何も出さない
- 再現: test_i2c_stop_without_a_preceding_start_is_reported
- 原因: decode/i2c.py decode_i2c の `if now & _SDA: # STOP / if cur is not None: ...`。cur が None なら注釈なし
  （ビットの組み立ても `if cur is not None` の中だけ）。
- 直し方の案: START 無しの STOP も kind "stop" で出し（fields に start_seen=False など事実）、途中のバイトは incomplete で出す。
- 影響: 取引の途中から始まった捕捉の、先頭のバイトと終わりの STOP が静かに消える。バス上で起きたことの欠け。
  （同じ捕捉の先頭に START が無いことは明示されない。）
- 確信度: 再現済み（STOP のみの捕捉で確認。途中から始まる取引のバイトの欠けも同根: 実測で注釈ゼロ）
- 修正済み: START の無い STOP も kind stop（error no_start、fields start_seen=False）で出す。途中から始まる取引のバイト（START 無しのビット）の組み立ては未対応。

### BUG-DEC-05 (C) UART: 捕捉の間ずっと低のままの線・先頭が低で途中で戻る線（ブレーク・地絡）を何も出さない
- 再現: test_uart_line_held_low_for_whole_capture_is_reported、test_uart_break_in_progress_at_capture_start_is_reported
- 原因: decode/uart.py _frames。開始ビットの立ち下がり（変化点）が無いと `j >= n` で終わる。先頭が低の線は
  変化点が無く（または戻る変化だけ）、何も出ない。
- 直し方の案: 先頭がすでに開始ビットの極性（アクティブ）の区間は kind "break"（または error "truncated"）として、
  始まりが捕捉の前であることを fields に事実で出す。
- 影響: 200 サンプル（25 ビット時間）低のままの線が「何も無い（休止）」と区別できない。AI が「通信が無い」と結論する。
- 確信度: 再現済み
- 修正済み: 捕捉の先頭からすでに開始ビットの極性の区間を kind break・error truncated（fields began_before_capture、ends_in_capture）で出す。

### BUG-DEC-06 (D) 例外番号の名前の付け方が 3 か所で食い違う
- 再現: test_exception_names_agree_for_reserved_and_secure_fault（一致する範囲の対照は test_exception_names_agree_across_modules）
- 原因: trace/elements.py exception_name（7 = SecureFault、他は "exception13"）、profile/exceptions.py exception_name
  （"Exception13"、0 = "Thread"）、probe_observe.py _exception_name と _EXCEPTIONS（7 が無く "exception 7"、"exception 13"）。
- 直し方の案: 1 か所（profile/exceptions.py）に統一して他は使う。
- 影響: 同じ例外が ITM の窓・ETM・プロファイルで別の名前になり、名前で突き合わせる人・AI が一致しない。
- 確信度: 再現済み
- 修正済み: 名前の表は profile/exceptions.py の 1 か所。trace/elements.py と probe_observe.py はそれを使う。

### BUG-DEC-07 (C) Timestamper が TC（ローカルのタイムスタンプの遅れの印）を捨てる
- 再現: test_timestamper_keeps_timestamp_relation
- 原因: itm.py Timestamper.feed は LocalTimestamp.relation（SYNC / TIMESTAMP_DELAYED / DATA_DELAYED / BOTH_DELAYED）を
  使わず、TimedPacket にも載せない。
- 直し方の案: TimedPacket に relation（時刻が正確でない印）を持たせる。
- 影響: FIFO が詰まってタイムスタンプが遅れたとき（高負荷の SWO ほど起きる）、時刻が不正確な出来事を正確なものと
  区別できず、µs の精度の議論が静かに誤る。
- 確信度: 再現済み
- 修正済み: TimedPacket.time_relation に TC（TimestampRelation）を載せる（時刻が未定なら None）。

### BUG-DEC-08 (C) ITM の拡張パケット（ステイミュラス・ポートのページ）を PortText が無視する
- 再現: test_port_text_honours_stimulus_port_page
- 原因: itm.py PortText.feed は Extension を見ず、Instrumentation.port（0〜31）をそのまま使う。Extension(source=0) の
  value がページ番号で、ポート番号は page × 32 + port。
- 直し方の案: 解読器か PortText がページを保持して通しのポート番号を付ける（または Instrumentation にページを持たせる）。
- 影響: ページ 1 以降のポートの printf がページ 0 の同じ番号のポートに混ざる。
- 確信度: 再現済み

## 疑い（再現試験なし。読みで見つけた・観測のみ）

### SUSPECT-DEC-09 (C) SwoTools._swo_pull が `t > run.last_t` でバイトを選び、前回と同じ時刻のバイトを捨てる
- probe_swo.py _swo_pull の `if t > run.last_t`。窓の読みが前回の最後のバイトと同じデバイス時刻のバイトを後から返すと
  （時刻の分解能より速いバイト列・同じ刻みに複数バイトが載る構成）、そのバイトは二度と読まれない。実機のタイムベースでの
  発生は未確認（モック不足で再現試験は書いていない）。確信度: 低〜中

### SUSPECT-DEC-10 (D) _swo_pull の run.times が増え続ける
- probe_swo.py: `run.times.update((id(p), t) for t, p in packets)` は LocalTimestamp・GlobalTimestamp も登録するが、
  Timestamper はそれらを吐き出さないので pop されず残る（長時間の SWO で辞書が増える。id の再利用では上書きされる）。
  確信度: 中（コード読み）

### SUSPECT-DEC-11 (C) SWD: 捕捉の終わりで 8 ビット未満の要求を何も出さない
- decode/swd.py _Parser.packet の `if i + 8 > n: return n`。8 ビット揃った要求は truncated で出るが、途中で切れた要求の
  先頭は注釈なし。確信度: 実測で確認（3 ビット・7 ビットで切ると無表示）。重さは小

### SUSPECT-DEC-12 (C) UART: 開始ビットの途中から始まる捕捉は、次のビットを開始ビットとして誤りなしの値を返す
- 実測: 8 サンプル/ビットの 0x55 の線を開始ビットの先頭から捕捉すると、2 ビット目の立ち下がりから 0xD5（error なし）を返す。
  先頭の線がすでにアクティブな捕捉の最初のフレームは信頼できないが、その印（truncated など）が付かない。
  UART は同期の手段が無いので設計上の限界とも言えるが、「先頭が休止でなかった」という事実を fields に出す余地はある。
  確信度: 実測で確認、重さは小

### SUSPECT-DEC-13 (D, 原則の確認) 経験則の閾値に近い箇所
- itm.py ItmDecoder(resync=True) の RESYNC_PACKETS = 6（「6 個続けて解ける位置」で再開する推定。明示 opt-in で
  Resync として知らせる）。trace/tpiu.py Deformatter と probe_etm.etm_decode の align="auto" 既定
  （FSYNC があるかで暗黙に切り替える。結果の aligned に出る）。いずれも出力に印は付くが、既定が暗黙の判断になっている
  （etm_decode の既定 align="auto"）。消してはいない。
- decode/uart.py の BREAK の定義（停止ビットが 0 で全データ・パリティも 0 を BREAK と名付ける）。長さの事実
  （end）は出るが、1 フレーム分に満たない低い区間も BREAK と呼ぶ。確信度: 低

### SUSPECT-DEC-14 (D) probe_etm._etm_markers が last+1 の印も含める
- `first <= m.index <= last + 1`。表示した行の外（次の命令の前）の出来事が含まれる。意図の可能性が高い。確信度: 低

### SUSPECT-DEC-15 (D) probe_swo._swo_record が address / value が None のアクセスを 0 として記録の列に入れる
- `a.address or 0, a.value or 0`（body には None が残る）。列だけで検索する側は 0 と区別できない。確信度: 中
