# U18・U26: プロファイル・カバレッジの書き出しと集計、メモリ・スタックの使用量の解析

日付: 2026-10-18。単位と満たす競合の機能 ID は `docs/reports/2026-10-18-gap-analysis.md`（U18・U26）。
ホストだけ（ゲートウェアの変更なし）。

## 1. U18 プロファイル・カバレッジの書き出しと集計

### 1.1 入力

- PC サンプリング（`fprobe.profile`。方法 itm / pcsr）の `PcSamples` と `Profile`。
- テストポイント（`fprobe.testpoint`）の `Report`（関数の時間・呼び出しの木・C0 / C1）と最近の `Call` の列。

### 1.2 集計（`fprobe/profile/aggregate.py`）

| 機能 | 中身 | 競合 |
|---|---|---|
| 区間 `window(samples, t0, t1)` | 時刻 t0 ≤ t < t1 のサンプル。時刻の無いサンプル（itm で timestamps なし）は切れない（誤り） | IAR-14 |
| グループ `Grouping(by, groups)` | function / file / directory（行のファイル。インライン展開は展開先）/ prefix（名前の型: `HAL_x` → HAL、`ns::C::f` → ns::C）/ groups（関数名の glob・`file:` + ファイルの glob、上から最初に合ったもの、残り "(other)"） | CTX-04 |
| 推移 `timeline` | 区切りごとの load（眠り以外の割合）・sleep と、全体で多い top 個のグループの割合 | CTX-04 |
| テストポイントの区間 `call_stats` | 呼び出しを区間で切った含む時間（再帰は外側だけ）・自分の時間（区間の中の子を引く）・始まった回数・掛かった回数 | IAR-14・T32-06 |

時刻の単位: pcsr はタイムベース（記録の時間軸と同じ。API で t0 / t1 を受ける）、itm はローカルのタイムスタンプ
（コアのクロック）。API の t0_s / t1_s は最初のサンプル（テストポイントは最初の印）からの秒。

### 1.3 書き出し（`fprobe/export/profile_formats.py`）

| 形式 | PC サンプリング | テストポイント | 競合 |
|---|---|---|---|
| gmon.out（gprof） | 番地の度数（2 バイトの箱、32 ビット LE）。16 ビットを超えたら全体と prof_rate を同じ数で割る（OpenOCD は頭打ち）。率は時刻からの実測 | 呼び出しの組（arc）だけ。番地は ELF から | OOCD-08 |
| callgrind | 関数・行の自分のサンプル（呼び出しは書かない: PC サンプリングに呼び出し履歴は無い） | 関数の自分の時間・calls= 回数と含む時間（刻み） | ORB-06・ADS-04 |
| lcov | 近似のカバレッジ（回数 0 = サンプルが無い） | C0（DA）・関数（FN / FNDA）・C1（BRDA、判定まで来ない組は "-"） | KEIL-10・WIN-04・MPLB-07 |
| gcov テキスト | 同上（ソースがあれば行の中身） | 同上 + 関数の呼ばれた・戻った回数・塊の割合、枝の回数 | KEIL-10 |
| dot | 関数の割合 + 逆アセンブルの静的な呼び出し（破線） | 呼び出しの木（含む・自分の割合と回数） | ORB-05・ADS-04 |

カバレッジは `FileCoverage`（行 → 回数・関数・分岐）にまとめてから各形式に書く。

### 1.4 API（`probe_profile_export.py` の `ProfileExportTools`）

`profile.aggregate`・`profile.timeline`・`profile.export`・`testpoint.aggregate`・`testpoint.export`（すべて control・
observes: 取っている最中は SWO / 窓の新しい分を受け取るため）。

### 1.5 満たさないもの

- サンプリングの呼び出しの木（T32-06・ADS-04 の Call Paths のうち PC サンプリングのもの）: PC サンプルに呼び出し履歴が
  無い。テストポイントの木（実の呼び出し）と、逆アセンブルの静的な呼び出しで代える。
- ESP-03（.gcda の吸い出し）: セミホスティングのファイル操作で書ける見込み。`--coverage` の試験用ファームが無いので
  手順だけ（`gateware/tests/hil/test_profile_export_stack_hw.py` の skip の試験）。
- orbtop の gnuplot 出力: 推移は JSON（`profile.timeline`）で返す。

## 2. U26 メモリ・スタックの使用量の解析

### 2.1 ELF のメモリ使用量（`fprobe/elfsize.py`、Build Analyzer 相当、CUBE-06・SEG-72）

- 区画: SHF_ALLOC の区画の VMA・LMA（PT_LOAD の p_paddr から）・大きさ・中身の有無。.tbss は除く。
- 領域: リンカスクリプトの MEMORY（`parse_linker_regions`。数の足し引きと K / M）、GNU ld のマップの Memory Configuration
  （`parse_map_regions`）、接続中のターゲットのメモリマップ、直接の指定。ELF に領域は残らない。
- 使用量: 区画は VMA の領域を使い、中身があり LMA が違う区画（.data の初期値）は LMA の領域も使う。どの領域にも入らない
  区画は outside。
- シンボル: 大きさのある関数・変数を大きい順に。区画・領域・種類・名前（glob）で絞る。

### 2.2 静的な最大のスタック（`fprobe/callgraph.py`・`fprobe/stackusage.py`、IAR-28・CUBE-07・SEG-72）

- 関数の枠: コンパイラの .su（gcc は `file:line:col:name`、clang は `file:line:name`）があればその値、無ければ逆アセンブルの
  SP の動き。逆アセンブルは制御の流れに沿ってたどり（条件分岐は両方、tbb / tbh は表の先、IT の中の条件付きの戻りは続き
  も）、push / pop・vpush / vpop・sub / add sp・[sp, #-n]! / [sp], #n・mov sp, rX（rX が SP からの既知の値なら戻し、
  分からなければ可変長の確保）を数える。同じ場所をより深い SP で 8 回を超えて訪ねなおしたら解析を打ち切る（上限不明・深さは下限。「伸び続ける」とは断定しない）。表の先が
  分からない分岐があれば、たどれない命令の SP を下げる量を全部足す（多めに見積もる）。
- 呼び出し: bl / blx #imm（call）、関数の外への b（tail。枠を戻した後の深さ）、blx rX（indirect）、bx rX 等（indirect_tail）。
  各場所にその時点の深さ。.su の枠が逆アセンブルと違うときは呼び出しを .su の枠の深さとみなす。
- 最大 = max(枠, 場所の深さ + 呼び先の最大)。強連結成分（Tarjan）で呼び先から順に。上限不明（bounded = false、max は
  下限、unknown_from に元の関数）: 再帰（cycles）、関数ポインタ（indirect = {関数: [候補]} か assume_address_taken =
  番地がデータ・リテラル・movw / movt にある関数のどれか）、可変長の確保（.su の dynamic。dynamic,bounded は上限あり）、
  ELF に無い呼び先、解析できない関数。
- 根: リセット（ベクタ表の 1 か entry）、例外の処理（ベクタ表。同じ関数はまとめ、total に例外の枠を足す）、呼ばれない
  関数（uncalled / address_taken）、指定（roots）。例外の枠は 32 + 4（揃え）、FPU の命令があれば 104 + 4。
- MSP の見積もり: one_level = スレッドの最大 + 最も深い例外 1 つ、all_nested = すべての例外が入れ子（優先度がすべて
  違うときの最悪）。

### 2.3 MSP の塗りと実測（IAR-27）

- 範囲: 上端はリンカのシンボル（_estack・__StackTop 等）かベクタ表の最初の語。下端は __StackLimit 等、または
  上端 − 名目の大きさ（_Min_Stack_Size 等）。nominal=false なら上端の下の最後の RAM の区画の終わり（使い得る全部）。
- 塗り（`target.stack.paint`）: 止めて、いまの MSP − margin より下を fill（既定 0xCD）で。SP が上端（リセット直後）なら
  上端まで。
- 実測（`target.stack.usage`）: 底から塗りが残るバイト数（`rtos.stackcheck.untouched_bytes`）→ 使った最大・割合・
  threshold 以上で warning、底の 16 バイトが塗りでなければ overflow。走行中も読める（いまの SP は止まっているときだけ）。

### 2.4 API（`probe_stack.py` の `StackTools`）

`debug.elf.size`（read）・`debug.stack.static`（read）・`target.stack.paint`（control）・`target.stack.usage`
（control・observes）。

### 2.5 試験

- `host/firmware/stack_demo`（zig clang -O1 -fstack-usage。Flash + SRAM・.data の写し・再帰・関数ポインタ・alloca・
  末尾呼び出し・SysTick）。逆アセンブルの枠が .su と一致することを確かめる。
- 実機: `gateware/tests/hil/test_profile_export_stack_hw.py`（sram_demo。実測 ≦ 静的な最大）。

### 2.6 注意

- 同じ名前の static 関数が複数あると ELF のシンボル（名前で引く）では 1 つしか見えない。.su はファイルで見分ける。
- 塗りの値と同じ値を書いた所は使っていないと数える。下端より上にヒープが伸びると、その分も使用量に数える
  （nominal=false のとき）。
- RTOS のタスクのスタックは `rtos.tasks`（MSP は割り込みだけになる）。
