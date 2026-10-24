# ヒューリスティックの総点検の台帳（2026-10-23）

6 担当の報告書（このフォルダの ui.md・fprobe-top-a.md・fprobe-top-b.md・fprobe-sub.md・rtos-profile-trace.md・gateware.md）の
H（ヒューリスティック）と ?（判断がつかない）をまとめ、扱いを決める。各報告書に、実行した検索・ヒット数・除外数と、
拾った全行の分類（F・E・O・D を含む）がある（漏れの無さの証拠）。

## 証拠の範囲と限界

- 全ファイル（約 11 万行、ui 133・fprobe 261・gateware 171 ファイル）に同じ 11〜12 本の検索を掛け、ヒットした全行を分類した
  （合わせて約 7,500 行）。
- 限界: 検索の語・形に当たらない書き方の判定は拾えない。全文を読んだのは一部（画面は 41 ファイル、残り 92 ファイルは
  検索のヒット行だけ）。「無い」と言える範囲は「この検索で拾える形のものは無い」まで。読み通しの第 2 の点検は §4。

## 1. H（ヒューリスティック）: 39 件

扱いの記号: **事** = 事実を返す形に直す（打ち切り・既定値を結果に出す、判定の文言を事実に）、**明** = 使う側が明示する
引数にする（暗黙の既定を外す）、**外** = 本体から外す（要ればプラグインへ）、**仕** = 仕様の情報（SVD・TRCIDR・ELF）に替える、
**移** = 画面からホストへ移す（UI は考えない層）。

| # | 場所 | 中身 | 扱い |
|---|---|---|---|
| G1 | gateware/rtl/debug/obs_jtag_engine.vhd:97 | ID_SCANS=16 で応答が無ければ stuck | 明（CSR で設定・申告） |
| G2 | gateware/rtl/debug/dap_sampler.vhd:40 | WAIT の再試行 255 が固定、errors に数えて飛ばす | 明（CSR の wait_retry を使う）＋事（WAIT と誤りを分けて数える） |
| G3 | gateware/rtl/debug/swd_injector.vhd:19 | 同じく 255 で失敗 | 明（同上） |
| G4 | gateware/fprobe_gw/caps.py:41 | SYNC_RX_PHASE_TICKS=0.5（一様分布の平均の仮定） | 事（0〜1 刻みの範囲として申告） |
| G5 | gateware/fprobe_gw/caps.py:160 | ロジアナの遅れ +0.5 刻み（同じ仮定） | 事（範囲として申告） |
| T1〜T8 | rtos/base.py:130・freertos.py:108・rtx5.py:122・threadx.py:155・zephyr.py:63・rtx5_objects.py:94・freertos_objects.py:383・nuttx.py:198 | 辿る上限に達しただけで「壊れている」 | 事（上限に達した、と壊れた（巡回・数の不一致）を分ける） |
| T9 | rtos/freertos_objects.py:148-153 | キューらしさの閾値で待ちリストの持ち主をキューと決める | 外（持ち主不明として返す） |
| T10 | trace/reconstruct.py:298-300 | 65536 命令分岐が無いと「像が違う？」で lost | 事（上限に達したと返す） |
| T11 | tracefmt/tracealyzer.py:136 | 大きさの閾値で拒否し「版が違う」と書く | 事（どの値が上限を超えたかを返す。原因は書かない） |
| S1 | decode/can.py:158 | サンプル点の既定 0.75 | 明 |
| S2 | decode/onewire.py:15-16 | 1-Wire の時間の区別を本体が固定 | 明（または規格の値なら出典を付けて F） |
| S3 | logfmt/trice.py:284-285 | 番号が 0xC0 に飛んだらリセットとみなす | 事（飛びを事実として数え、解釈しない） |
| S4 | logfmt/addresses.py:22 | 4〜8 桁の 16 進で実行区画内ならコードの番地とみなす | 外（プラグインへ）または明 |
| S5 | export/profile_formats.py:172 | 関数のファイルを行の最頻で選ぶ | 仕（DW_AT_decl_file） |
| S6 | testpoint/decode.py:96-100 | guard=1 が暗黙の既定 | 明 |
| A1 | inject.py:62-63 | timeout_s 2.0・observe_s 0.2 が暗黙で判定に効く | 明＋事（使った値を結果に） |
| A2 | probe_gdb_trace.py:140 | halt_risk の名前の一致で既定で断る | 明（断るかは使う側） |
| A3 | halt_risk.py:29-41 | 関数名の接頭辞で radio・usb・watchdog | 外（プラグインへ。台帳で済とされていたが残っていた） |
| A4 | disasm.py:344-375 | tbb/tbh の表の大きさを cmp から推定 | 事（推定である印を出すか、表の大きさを不明に） |
| A5 | connect_scripts.py:106-107 | 似ている度合い 0.8・接頭辞で警告 | 外（入力の検査の候補の提示はプラグインへ） |
| B1 | probe_inspect.py:29 | レジスタ名の型で「読むと消費する」 | 仕（SVD の readAction） |
| B2 | probe_observe.py:657 | 30 ms 何も来なければ尽きたとみなす | 明＋事 |
| B3 | rtt.py:36,163 | MAX_BUFFERS=64 を超えると不正 | 事（規格の上限が無ければ上限を外し、読んだ数を返す） |
| U1〜U9 | ui: flash.ts:133・sampler.ts:34・system.ts:34・105・209・231・eventlog.ts:175・state.ts:74・target.ts:24 | 残り時間の見積もり・使用者の推定・一周を前進とみなす・暗黙の窓・自分の watch の推定・経験の周期・印の主の推定・「AI 作業中」・クロックの境界 | 移・外（推定は事実の記録に替える: 誰が始めたかをデーモンが記録する等） |

## 0. 完了の基準（利用者と、2026-10-24）

検索で拾う 1 回目の点検は見落としが多かった（2 回目の全文を読む点検で H 約 35 件・? 約 100 件が新しく見つかった。
docs/reports/2026-10-24-heuristics-audit-2/）。完了は「直した後に、前の結果を見せない全文を読む点検を別のエージェントで
行い、新しく見つかる H が 0 件になる」まで繰り返す。0 件の回の報告書を証拠にする。それまでは「完了」と言わず、進みは
「見つかった件数のうち直した件数」で報告する。

進み（2026-10-24）: 3 回目の点検の H 16 件のうち 11 件を直した（残り 5 件は回路の R30）。画面の ? は 12 件のうち 12 件
（?4 は値に kind を付けて API の版 2）。**4 回目の点検は保留**（利用者の判断、トークンの都合。やめたのではない）。
再開するときは、全部を読むか、3 回目の後に変えたファイルだけを読むかを決める。

## 1.5 利用者の判断（2026-10-23）

- **名前の慣習での判定は H**（経験則。利用者が名前を変えられる）。出所を結果に出す・上書きできるでも同じ。前回の台帳で
  「残す」とした Ruling（idle のタスク名・liveness の関数名・リンカのシンボル名・SVD のファイル名 等）も取り消して直す。
  ただし規格が決める名前の変数を探すだけで、無ければ無いと返すもの（CMSIS の SystemCoreClock・_SEGGER_RTT）は事実の読み取り。
- **打ち切りは、切ったことが見えないのが NG**。表示のための件数そのものは許す。結果に総数と切った印を必ず入れ、件数は
  引数で渡せて、使った値も結果に出す。

## 進み（直したもの）

- T1〜T8・T10・T11（52f5a18）: 辿る上限と壊れているを分けて事実で書く・トレースの原因の推測を外す
- B3（6880f58）: RTT の制御ブロックの検めを事実（範囲に収まるか）に。候補が複数なら選ばない
- A1（eb1de91）: 故障注入の判定を決める待ち timeout_s・observe_s を必須にし、結果に出す
- S1・S2・?1（b9477a3）: 1-Wire の区別を規格の値（出典つき、timing で明示して変えられる）、CAN のサンプル点を必須
- S6（08cc0b9）: テストポイントの 2 線の guard を必須に（guard_samples）
- T9（このブランチ）: FreeRTOS の待ちリストの持ち主の推定を外す
- A2・A3・Q6・PC_SAMPLES（8e0787d）: halt_risk・liveness の名前の表を本体から外しプラグインへ。警告・拒否は使う側が
  条件を明示したときだけ（debug.halt_risk.set・target.liveness.set）。残り: (1) pc_samples の既定 4（条件を渡したときだけ
  使われるが暗黙の既定）、(2) 画面にこの条件を入れる場所が無い（人と AI の一対一に反する。画面の作り直しで足す）
- 名前の慣習 12 件（担当 2、main に merge）: idle のタスク名（idle_tasks で明示）・prefix の区切り（separator 必須）・スタックの
  シンボルの表（明示）・SVD のファイル名の前方一致（同名だけ）・bitstreams の正規表現（jtag_chain か ARM_A_PARTS の完全一致）・
  B1 読むと消費（SVD の readAction）・S4 番地とみなす（値が実行区画に入るの事実だけ）・trice の重要度の表と 0xC0（外す）・
  A5（プラグインへ）・S5（DW_AT_decl_file）・A4 tbb（明示 / プラグイン）・画面の rp2040（target.status の rescue_dp）
- 打ち切り 10 件（担当 3、main に merge）: correlate・durations・heap の呼び出し元（WRAPPERS は外して skip_functions）・ETM の誤り・
  testpoint の top と木の深さ（合流をやめ cut に数える）・RTOS と tracefmt の数え止め・timeline の carry（carry_complete）・
  run の 10 秒・probe.pins.write の待ち・同名の static（選ばず誤り）・画面の swotrace の比較器 4 つ
- 打ち切りの残り 4 件（済、main に merge）: recording.transactions の rows（rows_total・rows_truncated・rows_limit）・
  SCVD の文字列（string_limit、値の印と notes）・heap.scan の changes（changes_limit・new/gone_truncated）・時間軸の画面に
  carry_complete=false を「不明」の斜線で描く
- 画面の H U1〜U9（ブランチ fix/ui-heuristics）: 書き込みの進みは段と段の進み・経過をそのまま（残り時間の見積もりと
  段の等分の合成を外す）・サンプラ / watch を始めた者と印を置いた者はデーモンが呼び出し元を事実として残す（probe.sampler.status・
  watch.status の client、印の本文の client。仕様の caller）・メモリの毎秒を外して読んだ値と時刻・条件の回数はデーモンの
  fpga_obs.count（最後の last 件を使う側が明示）・記録の読みの速さは欄で明示（既定なし）・「AI 作業中」を「AI 接続中」の
  事実に・SystemCoreClock の境界を外す
- G2・G3（ブランチ fix/gw-wait-retry、ビルドの確かめ中）: サンプラ・割り込み書き込みの WAIT の再試行を固定 255 でなく
  ホストの DAP_TransferConfigure の wait_retry に
- まだ残っているもの（未）:
  - （済 1569e24）SVD を接続中のターゲットの pyOCD の定義（_svd_location）から読む from_target
  - （済 166f186）Event Recorder を trace の API で読むときの string_limit
  - （済 7af3bed）画面: Call Graph がベクタ表の番地を渡せない（MSP の見積もり・例外の根が出ない。欄か VTOR を読む
    形が要る）、halt_risk・liveness の条件を入れる場所が無い（一対一）→ Call Graph にベクタ表の欄と「VTOR を読む」
    （欄に入れるだけ）、Breakpoints に停止前の警告の条件・実行中の監視の条件の欄（今の条件は API から読んで描く）。
    残り: halt_risk の条件は debug.halt_risk（ELF が要る）でしか読めないため、ELF が無いと欄を出せない
    （ELF に依らない debug.halt_risk.get が無い）
  - （済 6ae5dd7）liveness の pc_samples の既定 4 を外す（条件を渡すときは必須）
  - B2（SWO）・G1（観測の ID_SCANS=16）・G4・G5（平均 0.5 刻みの仮定）: ゲートウェアと設計が要る
  - （済、ブランチ fix/ui-heuristics）画面の H 9 件 U1〜U9（下の「画面の H」）
  - ? の残り（解読器の既定の扱いは利用者の判断待ち）
- 新たに見つかった残り（済）:
  - callgraph.py の _VECTOR_SYMBOLS（名前の表）と「最も低い区画の先頭の変数」: 外した。ベクタ表は使う側が番地を渡す
    （CallGraph.build・debug.stack.static の vector_table。大きさは vector_table_size か ELF のその番地の変数の大きさ）。
    渡さなければ例外の根は無く msp は null、表の語はデータの語として数える（処理の関数は address_taken）
  - probe_formats の _priority_order（OS 名の部分一致）: 外した。形式が 1 つの RTOS のもの（ctf = Zephyr・tracex =
    ThreadX）だけその向き、ほかは不明（None）で逆転を判定しない。時間軸の結果に priority_order。_TimelineRun.order の
    既定 1 と inversions_from_ops・inversion_episodes の order=1 の既定も外した（必須）
  - Disassembler の「リテラルは推定する」: 推定の印を出す（data の data_basis = mapping / branch_table / pc_load /
    undecodable。pc_load は文字列の表示にも「推定」）。$t・$d の印がある所は推定で上書きしない
  - tbb の表を API から渡す口: debug.disasm.range / function / around / text に branch_tables
    （[{address, entries}]）。プラグインの tbb_table_sizes の結果に同じ形の branch_tables
- B2（未）: SWO の受信の残りを 30 ms の沈黙で尽きたとみなす。事実で止めるには、ゲートウェアの DAP_SWO_Status が
  バッファのバイト数（今は常に 0）を答える必要がある → ゲートウェアの件と一緒に直す

## 2. ?（判断がつかない）: 70 件の扱いの方針

各報告書の ? を、次の 4 つに分けて 1 件ずつ決める。
- **黙った打ち切り**（上位 20 に切って総数を返さない、LIST_LIMIT・MAX_MESSAGES 等で黙って止める）: 事（総数と「切った」を返す）。
- **暗黙のプロトコルの既定**（UART 8N1・SPI mode 0・EEPROM 0x50 等）: 明（必須の引数にするか、使った値を結果に出す）。
- **名前の慣習**（idle のタスク名・liveness の関数名・リンカのシンボル名・SVD のファイル名の前方一致・aggregate の接頭辞・
  WRAPPERS・bitstreams の正規表現）: 仕（規格・ELF・設定の情報）か明か外。台帳で Ruling 済みのものも、利用者の指摘
  （名前が利用者に変えられる）を受けて見直す。
- **ホストの good・bad・warn の色分け**（timeline_rows.py）: 事（色ではなく事実の種類を返し、色は画面の見せ方）。

## 3. 進め方

範囲ごとにブランチを分け、各件に「その H を見つける試験」を書いてから直す（ヒューリスティックの除去と同じやり方）。
順: 本体（host/fprobe）の H → 画面の H（画面の作り直しの §5 と合わせる）→ ゲートウェアの H（ビルドと実機が要る）→ ? の 70 件。

## 4. 第 2 の点検（読み通し）

検索で拾えない書き方を見つけるため、直した後に、検索ではなく全文を読む点検を別のエージェントに頼む（今回の検索の
結果を見せずに）。2 つの点検の差が、検索の漏れの事実になる。
