# F7 テストポイント方式（DT+ 方式）の設計

日付: 2026-10-17 / 担当 H / 状態: 実装済み（ホストのライブラリ・試験用ファーム、API・MCP は U17 で。7.1 節）

## 1. 目的と範囲

DT10 / DT+（ハートランド・データ）の考え方 —— ソースに短い出力のコード（テストポイント）を自動で入れ、外のハードで
時刻付きで集め、関数の遷移・関数ごとの時間・変数の値・カバレッジ・長時間の記録を解析する —— を fprobe で行う。
fprobe ならではの点:

- 受け手のハードはロジアナ（8 本・100 MS/s・DDR3 の深いバッファ・PC へ流し続ける捕捉）、SWO（ITM）、RTT の
  どれでもよい。どれで受けても **同じ ID の列（`Hit`）** になり、解析は 1 つ。
- 結果は共通の時間軸の記録（F3 の `Recorder`）に stream `"testpoint"` として書け、ロジアナの波形・DAP の出来事・
  UART の解読と同じ時間軸で見られる（DT+ の「解析の一体化」に相当）。
- DT+ の不満（データが重い・見づらい・共有しにくい）への答え: ID は 16 ビット（GPIO なら数百 ns、ITM なら 3 バイト）、
  解析は逐次（長時間でもメモリが一定）で、要約（時間ごとの件数・異常の印・関数の統計）を JSON にして AI・人に渡す。
  記録は 1 ディレクトリ（SQLite）で共有しやすい。

範囲:

| 部分 | 内容 | 置き場所 |
|---|---|---|
| 挿入 | C のソース → 挿入した写し + ID の対応表（JSON）。差分の再挿入（ID を保つ） | `host/fprobe/testpoint/{clex,cparse,instrument,probes}.py` |
| ターゲット側 | 1 行のマクロ `FPROBE_TP(id)` / `FPROBE_TP_VALUE(id, v)` と 5 つの出力方式 | `host/fprobe/testpoint/fprobe_tp.h` |
| 解読 | GPIO（並列・2 線）・ITM・RTT → `Hit` の列 | `host/fprobe/testpoint/decode.py` |
| 合成 | 試験・デモ用に `Hit` から波形・ITM のバイト列・RTT のデータを作る | `host/fprobe/testpoint/synth.py` |
| 解析 | 遷移・統計・2 点間・カバレッジ・長時間の要約・異常 | `host/fprobe/testpoint/analysis.py` |
| 記録 | 解析を `Recorder` の stream `"testpoint"` に書く | `host/fprobe/testpoint/record.py` |
| 試験用ファーム | STM32F401（SRAM）、2 線 GPIO・並列 GPIO・ITM の 3 種の ELF | `host/firmware/testpoint_demo/` |

範囲外（将来）: MC/DC（`&&`・`||` の条件ごとのカバレッジ）、式の中の分岐（`?:`）、ポインタ経由の変数の書き込み、
C++、`longjmp` / `exit` による関数の出口、FPGA 側での ID の切り出し（ホストの解読をゲートウェアに移すのは帯域が
足りなくなってから）。

## 2. 挿入

### 2.1 C の解析の方式（pycparser を使わない理由）

pycparser（BSD、.venv に既にある）を検討したが採らない:

1. **プリプロセス済みのソースが要る。** ターゲットのヘッダ（CMSIS・HAL）をホストの前処理器に通す必要があり、
   コンパイラ固有の拡張（`__attribute__`、`__asm`、組み込み関数）で止まる。偽の libc ヘッダを用意する手間も大きい。
2. **写しが元のソースの形を失う。** AST から C を作り直すとマクロ・コメント・書式・`#if` が消え、写しを人が読めず、
   ビルドの設定（どの `#if` が有効か）が挿入の時点で固定される。
3. 欲しいのは **文の構造** だけ（関数・複文・if/else・switch/case・ループ・return・ラベル・式文）。
   式の中は「括弧の釣り合った字句の並び」として飛ばせばよい。

そこで、純 Python の **字句解析（`clex`）+ 文の水準の構文解析（`cparse`）** を元のソースに直接かけ、
**元の文字の位置に文字列を差し込む**（置き換えるのは `return` の 2 字句だけ）。差し込む文字列は改行を含まないので
**写しの行番号は元と同じ**。写しの先頭に

```c
#include "../fprobe_tp.h"   /* 写しの置き場所からの相対パス */
#line 1 "C:/path/to/original/demo.c"
```

の 2 行を足し、`#line` で行番号とファイル名を元に戻す（`line_root` を渡すと `#line` は相対パス。ELF に作った場所の
絶対パスが入らない。試験用ファームはこれを使う）。よって写しで作った ELF の DWARF は **元のソースの行** を指す
（デバッガ・F2 の DWARF・`symbols.py` がそのまま使える）。

字句解析が扱うもの: `//`・`/* */` コメント、文字列・文字（接頭辞 `L` `u` `U` `u8`、エスケープ）、行の継続（`\` + 改行）、
前処理の指令（行頭の `#` から論理行の終わりまで。中のコメント・文字列も）、pp-number、最長一致の区切り記号。

### 2.2 何を「関数の定義」と見るか

ファイルの最上位で、`;` か `}` から次の `{` までの字句の並び（宣言の並び）について、

- 末尾（`__attribute__((…))`・`__asm__(…)` を除く）が `)` で、
- 最上位に `=` が無い（初期化子の `{` ではない）

なら関数の定義。名前は末尾の括弧の組の直前の識別子。直前が `)` なら（関数ポインタを返す複雑な宣言子）その組の中を探す。
`extern "C" {` は透過する。`struct`・`enum`・初期化子の `{}` は丸ごと飛ばす。K&R 形式の定義は対象外（挿入しない）。

戻り値の型は「名前より前の字句」から記憶域・関数の指定子（`static` `inline` `extern` `_Noreturn` `__attribute__((…))`
`[[…]]` など）を除いたもの。`void` だけなら void 関数。

### 2.3 文の水準の構文

関数の本体を再帰下降で読む。節点: 複文 / if（else 付き）/ switch / while・for・do / return / ラベル（case・default・
名前）/ 単純文（式文・宣言・goto・break・continue・空文）/ マクロの塊（`FOREACH(x) { … }`・`LOOP { … }` のように
識別子（と括弧）の直後に `{` が来る文。中は再帰して挿入するが、それ自身には印を付けない）。

式の中は括弧（`()` `[]` `{}`）の釣り合いだけを見て `;` まで飛ばすので、文の式・複合リテラル・GCC の文の式
`({ … })`・初期化子の `{}` を含んでも崩れない。

**前処理の条件（`#if` `#ifdef` `#elif` `#else` `#endif`）** は、複文の **項目の境目** にあるときだけ許す。さらに
1 つの `#if` 群の `#elif` / `#else` / `#endif` が同じ複文の中にあることを確かめる。そうでない関数（文の途中や枝ごとに
括弧の数が違う書き方）は **挿入せずに残し、理由を報告** する（間違った写しを作らない）。条件でない指令（`#pragma`
`#define` など）はどこにあってもよい。

### 2.4 挿入する場所と形

| 種類（`kind`） | 場所 | 挿入の形 |
|---|---|---|
| `entry` | 関数の本体の `{` の直後 | `{ FPROBE_TP(1);` |
| `exit` | すべての `return` と、本体の最後の `}` の前（最後の文が return なら置かない。届かない関数もあるので「足した印」として C0 から除く） | 下記 |
| `then` / `else` | if の各枝の先頭。else が無ければ `else { FPROBE_TP(n); }` を足す（暗黙の枝） | |
| `case` / `default` | 各ラベルの `:` の直後。default が無ければ本体の末尾に `break; default: FPROBE_TP(n); break;` を足す | |
| `loop_body` / `loop_exit` | ループの本体の先頭 / ループの文の直後 | |
| `var` | 指定した変数に書く文（`=`・複合代入・`++`・`--`・初期化子付きの宣言）の直後 | `FPROBE_TP_VALUE(n, (左辺));` |

- 括弧の無い子の文（`if (c) x();`）は必ず `{ … }` で包む。ぶら下がり else は構文解析が C の規則（近い if に付く）で
  決めた後で包むので意味は変わらない。
- **`return 式;` は式を先に評価してから出口を記録** する（式の中の呼び出しが子として正しく入れ子になる）:
  `{ int fprobe_tp_r = (式); FPROBE_TP(7); return fprobe_tp_r; }`。型は関数の戻り値の型。型の字句に大文字だけの
  識別子（マクロの疑い）が混じる・宣言子が複雑なときは `__auto_type`（GCC / Clang）。void 関数の `return;` は
  `{ FPROBE_TP(7); return; }`。
- 変数の値は左辺の式をそのまま読み直す（`table[i] = x;` → `FPROBE_TP_VALUE(n, (table[i]))`）。左辺に副作用
  （`++` `--` 代入・呼び出し）があれば値なしの `FPROBE_TP` にする。構造体など整数にできない変数は `name:novalue` で
  値なしを指定する。値は `uint32_t` に変換する。
- 対象外の印: `naked` 属性の関数（中に C を足すと壊れる）、除外の指定に当たる関数、条件の位置が許せない関数。

### 2.5 ID と対応表

ID は 1〜65535（0 は使わない）。対応表 `testpoints.json`:

```json
{"format": 1, "next_id": 42, "header": "fprobe_tp.h",
 "probes": [{"id": 1, "file": "src/demo.c", "line": 30, "function": "fib", "kind": "entry",
             "decision": null, "label": "", "variable": null, "implicit": false}, ...],
 "functions": [{"file": "src/demo.c", "name": "fib", "index": 0, "line": 30, "end_line": 36,
                "fingerprint": "…", "instrumented": true, "ids": [1, 2, 3]}],
 "skipped": [{"file": "…", "function": "…", "line": 0, "reason": "…"}]}
```

`decision` は分岐の組（if の then/else、switch の各ラベル、ループの本体/出口）を、その組の最初の ID で表す。
`implicit` はソースに無い枝（足した else・default）。

### 2.6 差分の再挿入（ID を保つ）

関数ごとに **指紋**（本体を含む字句の並び（空白・コメントを除く）と挿入の設定の SHA-1）を持つ。前回の対応表を
渡すと、(ファイル, 関数名, 同名の何番目) が同じで指紋も同じ関数は **前回の ID をそのまま使う**（行が動いても）。
変わった・新しい関数は `next_id` から新しい ID を取り、消えた ID は使い回さない（古い記録の ID と混ざらない）。
写しは中身が変わったときだけ書く（ビルドの差分が最小になる）。

`only_changed=True`（DT+ の Advisor に相当する「変更箇所に絞る」）: 前回から指紋の変わった・新しい関数にだけ
入れる（負荷とデータ量を変更の確認に絞る）。指紋はすべての関数について保つので、次回の比較もできる。
結果（`InstrumentResult`）は変わった・新しい・消えた・挿入しなかった（理由）関数を返す。

## 3. 出力の方式

ターゲットは `fprobe_tp.h` を（写しが自動で）取り込み、`FPROBE_TP_BACKEND` で方式を選ぶ。どの方式でも
1 か所の挿入は **1 回の関数呼び出し**（`movs r0, #id; …; bl fprobe_tp_<方式>_send`）で、記録の本体は `noinline` の関数 1 つ。
複数の書き込みからなる記録は **割り込みを止めて**（PRIMASK）出す（割り込みのテストポイントが途中に混ざらない）。

### 3.1 記録の中身

`ID`（16 ビット）、`VALUE` 付きなら加えて値（32 ビット）。上位のビットから送る。

### 3.2 GPIO 並列（`FPROBE_TP_GPIO_PARALLEL`）

同じポートの連続した W 本（D0..D(W-1)）とストローブ 1 本。D(W-1) は **先頭の印**（記録の最初の記号で 1）、
残りの P = W − 1 本が中身。記録を P ビットずつの記号に分け（末尾を 0 で埋める）、記号ごとに BSRR に 2 回書く:

1. データ（W 本）を置き、ストローブを 0 に（1 回の書き込み）
2. ストローブを 1 に（**立ち上がりで受け手がデータを取る**）

データはストローブの立ち上がりの 1 書き込み前に置かれ、次の記号まで変わらないので、受け手は立ち上がりのサンプルの値を
そのまま使える。記号の数で長さが分かる（ID だけ: ⌈16/P⌉、値付き: ⌈48/P⌉）。先頭の印があるので、捕捉が記録の途中から
始まっても次の記録で同期する。設定: `FPROBE_TP_GPIO_BSRR`（BSRR のアドレス）、`FPROBE_TP_PAR_SHIFT`（D0 のビット）、
`FPROBE_TP_PAR_WIDTH`（W）、`FPROBE_TP_PAR_STROBE`（ストローブのビット）。

### 3.3 GPIO 2 線（`FPROBE_TP_GPIO_SERIAL`）

クロック（CLK）とデータ（DAT）。アイドルは両方 1。I2C に似た開始・終了:

- 開始: CLK = 1 のまま DAT を 0 に。間（`FPROBE_TP_GPIO_GAP`）を置く。
- ビットごと: (CLK = 0 と DAT = b を 1 回で) → (CLK = 1)。**受け手は CLK の立ち上がりで DAT を取る。**
- 記録の後に 0 のビットを 1 つ（終わりの印。受け手は長さとこのビットで記録を検める）。
- 終了: CLK = 1 のまま DAT を 1 に（間を置いてから）。

CLK の立ち下がりと DAT の変化は同じ書き込みなので、ピンの間のずれで DAT が 1 サンプル先に変わることがある。受け手は
「CLK = 1 の間の DAT の変化」を、その後 `guard` サンプル（既定 1）CLK が 1 のままのときだけ開始・終了とみなす。
ファームは開始の後・終了の前に `FPROBE_TP_GPIO_GAP`（既定: nop 4 つ）を置き、この間を 2 サンプル以上にする。

### 3.4 ITM（`FPROBE_TP_ITM`）

スティミュラス・ポート `FPROBE_TP_ITM_PORT`（既定 1）に、値付きならまず値を 4 バイトで、次に ID を **2 バイト** で
書く（大きさで区別するので 1 本のポートで足りる）。受け手は 4 バイトを預かり、次の 2 バイトの ID に付ける
（ID が来た時点で記録が完結する。流しながら遅れずに出せる）。ITM が無効・ポートが無効なら捨てる（待ち続けない）。
時刻は ITM のローカルのタイムスタンプ（`itm.Timestamper`）か、FPGA が SWO のバイトに付けた時刻。

### 3.5 RTT（`FPROBE_TP_RTT`）

上りのチャンネルに固定長の記録を書く（リトル・エンディアン）: `u32 CYCCNT, u16 ID, u16 flags`（flags の上位 8 ビットは
印 0xA5（壊れたときに同期を取り直す手がかり）、bit0 = 値付き）、
値付きならさらに `u32 値`。書き込みは利用者の関数 `FPROBE_TP_RTT_WRITE(ptr, len)`（例: SEGGER の
`SEGGER_RTT_WriteSkipNoLock(1, p, n)`。全部書けなければ捨てる方式なら記録が途中で切れない）。時刻は DWT の
CYCCNT（`fprobe_tp_rtt_init()` が有効にする）。受け手は 32 ビットの回り込みを戻す（記録の間が 2^32 クロック
（84 MHz で 51 秒）より短い前提）。

### 3.6 その他

`FPROBE_TP_NONE`（既定。何も出さない。写しのまま試しにビルドできる）、`FPROBE_TP_CUSTOM`（利用者の
`fprobe_tp_user_emit(id)` / `fprobe_tp_user_emit_value(id, v)` を呼ぶ。UART・SPI・独自の出力に使う。試験は
これで ID をホストの配列に記録して実行する）。

### 3.7 負荷の見積もり

Cortex-M4。命令数は zig cc（clang）-O1 で作った試験用ファーム（`testpoint_demo_*.elf`）の `fprobe_tp_*_send` を
`fprobe.disasm` で逆アセンブルし、実際に通る命令を数えた（呼び出し側の引数の設定と `bl` の 3〜4 命令を含む）。
コンパイラは記号・ビットのループを展開し、シフトはすべて定数になる（64 ビットの可変シフトを避ける書き方にした）。
時間は 16 MHz（HSI、試験用ファーム）と 84 MHz で、1 命令 ≈ 1.3 クロック（GPIO の書き込みは AHB1 で 2 クロック）。

| 方式 | 記録 | 通る命令 | 16 MHz | 84 MHz | 受け手側の制約 |
|---|---|---|---|---|---|
| GPIO 並列 W=3（P=2、試験用ファーム） | ID（8 記号） | 約 60 | 約 5 µs | 約 0.9 µs | 記号の 2 回の書き込みの間が 1 サンプル以上（100 MS/s で 10 ns） |
| GPIO 並列 W=3 | ID + 値（24 記号） | 約 140 | 約 11 µs | 約 2.2 µs | 同上 |
| GPIO 並列 W=7（P=6） | ID（3 記号） | 約 30 | 約 2.5 µs | 約 0.5 µs | 同上（ロジアナの 7 本 + ストローブ） |
| GPIO 2 線 | ID（17 ビット） | 約 100 | 約 8 µs | 約 1.6 µs | 開始・終了の間（nop 4 つ + 書き込み）が 2 サンプル以上 |
| GPIO 2 線 | ID + 値（49 ビット） | 約 260 | 約 21 µs | 約 4 µs | 同上 |
| ITM | ID | 約 24 | 約 2 µs | 約 0.4 µs | **SWO の帯域**: 3 バイト（+ タイムスタンプ 1〜2 バイト）≈ 2 Mbaud で 20〜25 µs。これより頻繁だと FIFO 待ちで CPU が止まる |
| ITM | ID + 値 | 約 32 | 約 2.6 µs | 約 0.5 µs | 8 バイト（+ TS）≈ 45 µs |
| RTT | ID | 約 25 + 書き込み関数（SEGGER で約 60） | 約 6 µs | 約 1 µs | PC の読み出しが追いつく限り（数 MB/s） |

コードの大きさ: 1 か所の挿入は 6〜10 バイト（引数の設定と `bl`）。出力の関数は 1 つ（並列 W=3 で 200 バイト、2 線は
展開で 440 バイト、ITM で 110 バイト）。数値は 8 節の手順で実機で確かめて置き換える（ロジアナで 1 記録の長さを計る）。
帯域で選ぶと: 頻度の高い経路は GPIO 並列、ピンが無ければ 2 線、ピンを使えないなら ITM / RTT。

## 4. 解読

すべての解読器は **逐次**（`feed` を何度呼んでもよい・捕捉のかたまりの境目で記録が切れてよい）で、
`Hit(t, id, value)`（t は整数のクロック・サンプル番号）と `Loss(t, reason)`（取りこぼし・壊れた記録）の列を返す。
`Decoded(items, hz)` に時刻の単位（Hz）を持たせる。

| 解読器 | 入力 | 時刻 |
|---|---|---|
| `ParallelDecoder(data_channels, strobe_channel)` | `la.Transitions`（`feed(tr, base_sample)`） | サンプル番号（hz = サンプル周波数） |
| `SerialDecoder(clock_channel, data_channel, guard=1)` | 同上 | 開始のサンプル |
| `ItmDecoder(port)` | `itm.Packet` / `itm.TimedPacket` / `(t, Packet)` | ローカルのタイムスタンプの累積、または与えた時刻 |
| `RttDecoder()` | 上りのチャンネルのバイト列 | 回り込みを戻した CYCCNT |

ITM のオーバーフロー・壊れた GPIO の記録・RTT の不正な flags は `Loss` になる（RTT は 1 バイトずつずらして同期を取り直す）。

## 5. 解析

`Analyzer(table, hz, …)` は `Hit` / `Loss` を逐次に受け、メモリを一定に保つ（呼び出しの一覧は `keep_calls=True` の
ときだけ持つ）。

- **関数の遷移**: 入口で積み、出口で降ろす。出口が一番上の関数と違えば、下を探してその上の枠を「出口が無い」
  （`missing_exit`）として閉じる。見つからなければ `exit_without_entry`。`Loss` が来たら積んだ枠を捨てる。
  割り込みのテストポイントはそのときの関数の子として入る（時間は子を含む方に入る）。完了した呼び出しは
  `Call(function, t0, t1, depth, self)` として返し（コールバック）、`render_calls` で字下げした時間軸の文字列にできる。
- **呼び出しの木**: 呼び出しの経路（深さ 64 まで）ごとの回数・時間の集計（長時間でも経路の数しか増えない）。
- **関数ごとの統計**: 回数、子を含む時間と自分だけの時間の合計・最小・最大・平均・標準偏差（Welford）。
  再帰は一番外側の呼び出しだけを合計に入れる（二重に数えない）。
- **2 点間の時間** `intervals(items, start_id, end_id, hz)`: 開始の後で最初の終了までの時間の列と統計
  （次の開始が先に来たら、その開始からやり直す）。
- **カバレッジ**: 関数（入った関数 / 入れた関数）、C0（ソースにある塊の印 = entry・then・else・case・default・
  loop_body・loop_exit・exit のうち、足した枝を除く）、C1（分岐の組の結果のうち通ったもの。足した else・default を
  含む）。通らなかった印の一覧（ファイル・行）。
- **長時間の要約**: 時間の区切り（既定 1 秒）ごとの件数・呼び出しの数・異常の数・関数ごとの呼び出しの数。
- **異常の印**: `long_call`（それまでの 30 回以上の平均 + 6σ を超え、かつ平均の 1.2 倍を超える。基準は関数ごと
  なので、引数で仕事が大きく変わる関数（試験用ファームの `fib`）には付きやすい — その関数は除くか閾値を上げる）、`missing_exit`、
  `exit_without_entry`、`unknown_id`、`silence`（指定した時間より長く何も来ない）、`no_exit`（終わりの時点で
  指定した時間より長く開いたままの入口）、`loss`。
- `Report.to_dict()` は JSON にでき、MCP の答えにそのまま使える。

記録（`record.py`）: `RecorderSink(recorder, table, hz, offset_ticks)` を解析のコールバックにつなぐと、流しながら
stream `"testpoint"` に書く — `call`（t, t_end, a = 入口の ID, b = 深さ, body = {function, self}）、`hit`
（分岐・変数の印。a = ID, b = 値, body = {kind, function, label, line}）、`anomaly`（a = ID, body = {type, function,
detail}）。時刻は `offset + round(t × 記録の Hz / hz)`。`record_testpoints(recorder, items, table, hz)` はまとめて行う。

## 6. 試験用ファーム（`host/firmware/testpoint_demo/`）

`src/demo.c`: 再帰（`fib`）、早期 return（`validate`）、switch と fallthrough（`classify`）、ループと break / continue
（`sum_until`）、goto のエラー処理（`parse`）、void 関数の末尾、変数 `state` の書き込み。SRAM（`link.ld` は sram_demo と
同じ）で動き、`run_count` を増やしながら無限に回る。`build.py` が挿入ツールで `build/instrumented/` に写しを作り、
`testpoints.json` を（前回の ID を保って）更新し、zig cc で 3 つの ELF を作る:

| ELF | 方式 | Nucleo のピン | Primer（J12 = pmod1）のロジアナ |
|---|---|---|---|
| `testpoint_demo_serial.elf` | GPIO 2 線 | CLK = PA5（CN10-11）、DAT = PA9（CN10-21） | ch4（今の配線のまま）、ch5（今の配線のまま） |
| `testpoint_demo_parallel.elf` | GPIO 並列 W=3 | D0 = PC0、D1 = PC1、D2（印）= PC2、STB = PC3 | ch1 ← PC0（CN7-38）、ch5 ← PC1（CN7-36）、ch6 ← PC2（CN7-35）、ch4 ← PC3（CN7-37） |
| `testpoint_demo_itm.elf` | ITM ポート 1 | SWO = PB3（CN10-31） | ch6（SWO、今の配線のまま） |

2 線は今の配線（PA5 = LD2 の線、PA9 = UART RX の線）のまま使える。並列は J12 の ch1・ch4・ch5・ch6 の 4 本の線を
PC0〜PC3 につなぎ替える（ch0 = SWCLK・ch2 = nRESET・ch3 = SWDIO はそのまま。ch7 は FPGA の UART TX が常に駆動する
ので使わない（B29 以後は UART に最初の 1 バイトを送るまで駆動しないが、UART を使えば駆動する）。ch1 の TDI は SWD のときは出力無効 `tdi_oe = jtag_en`）。

## 7. 本流での API・MCP への組み込みの提案

1. **API**（`fprobe_api.json`）に `testpoint.instrument`（入: ソースのディレクトリ・出力先・変数・only_changed、
   出: `InstrumentResult` の要約と対応表のパス）、`testpoint.start`（入: 方式 gpio_parallel / gpio_serial / itm / rtt、
   チャンネル、対応表、hz）、`testpoint.stop`、`testpoint.report`（`Report.to_dict()`、関数の上位 N・カバレッジ・異常）、
   `testpoint.calls`（時間の区間を指定して `render_calls` の文字列）、`testpoint.intervals`（2 点の ID）。
2. **デーモン**: 方式ごとの受け口を既存の流れにつなぐ — GPIO はロジアナの流し続ける捕捉（`la_stream`）のかたまり
   ごとに `ParallelDecoder.feed(tr, base_sample)`、ITM は SWO の受信（`itm.ItmDecoder` → `Timestamper`、または FPGA の
   時刻付きのバイト）、RTT は `Rtt.poll()` の上りのチャンネル。どれも `Analyzer.feed` → `RecorderSink` で記録へ。
   時刻の写し: GPIO はロジアナのサンプル番号を `collector` と同じ式でタイムベースへ。ITM / RTT はターゲットの
   クロックなので、最初の `Hit` と記録の時計の対応（`ClockMap`）で写す（不確かさを付ける）。
3. **MCP**（`mcp_server.py`）: `testpoint_instrument`、`testpoint_capture(seconds)`、`testpoint_report`、
   `testpoint_calls(t0, t1)`、`testpoint_coverage`（通っていない行の一覧 — AI が試験を足す手がかり）。
4. **GUI**: 記録の時間軸に `testpoint` の `call` を関数ごとの帯（フレームの図）として描く。カバレッジはソースの行の印。
5. **ゲートウェア（将来）**: ロジアナの入力を J12 以外（pmod2・pmod3）からも選べると、デバッグの配線を外さずに
   並列 W=7（1 記号で 6 ビット）にできる。さらに帯域が要るなら、ストローブで取る記号の切り出しを FPGA に置く。
6. pyproject の package-data に `testpoint/*.h` を加えた（ヘッダを配布物に入れるため）。

### 7.1 実装（U17、2026-10-18）

上の 1〜3 を `host/fprobe/probe_testpoint.py`（Probe の mixin `TestpointTools`）と `host/fprobe/testpoint/live.py` で実装した。

| API（MCP の道具） | kind | 内容 |
|---|---|---|
| `testpoint.instrument` | read | 挿入（差分の再挿入・only_changed・変数・include / exclude）。以後の start の既定の対応表 |
| `testpoint.start` | control | method gpio_serial / gpio_parallel（capture deep = 深いバッファ、stream = 流し続ける）/ itm / rtt |
| `testpoint.stop` | control | 止めて解析を終える（duration_s まで待つ）。要約を返す |
| `testpoint.status` | control（observes） | 受けた印・取りこぼし・流し続ける捕捉の速さと overrun・時刻の写し |
| `testpoint.report` | control（observes） | 関数の時間・呼び出しの木・異常・時間の区切り・変数（最中でも） |
| `testpoint.calls` | control（observes） | 関数の遷移の時間軸（最近の 65536 件。全部は記録の call） |
| `testpoint.coverage` | control（observes） | 関数・C0・C1 と通らなかった印（file / function で絞る） |

- 解析は `LiveAnalysis`（逐次・メモリ一定。呼び出しは最近の 65536 件だけ持ち、変数は最新・最小・最大だけ）。
  途中の要約は `Analyzer.finish(final=False)`（no_exit を付けない）。
- 時刻の写し（`TimeMap`）: deep はトリガ時刻（確か）、stream は流し始めの前後のタイムベースの読みの中点（不確かさ = 幅の半分）、
  itm は FPGA が SWO のバイトに付けた時刻そのもの（不確かさ = 1 バイト）、rtt は最初のかたまりの最後の記録をホストの時計で
  受けた時刻に合わせる（不確かさ = 時計の対応付け + RTT の読みの周期。クロックのずれは補正しない）。記録の出来事に不確かさが付く。
- stream は背景のスレッドがかたまりごとに解読・解析する（受け取りの間だけ `_lock`）。止めるのは `LaStream.stop`（残りも受け取る）。
- itm は ITM_TER の全ポートを止めてから SWO の受信と窓を始め、その後で元のポートと port を有効にする（他のポートの printf も
  パケットの途中から受けない）。背景で 0.25 秒ごとに窓の新しいバイトだけを読む（イベント記録のリングが一周しても欠けない）。
  止めると ITM_TER を元に戻す。
- rtt は `ObserveTools._rtt_listen`（RTT の受信の口。貯め場とは別）で受ける。受け始めの途中の記録は取りこぼしに数えない。
- 実機の手順: `gateware/tests/hil/test_testpoint_api_hw.py`。
- 未実装: `testpoint.intervals`（2 点間の時間。記録の `timing_measure` / `recording_intervals` で代わりになる）、
  GUI（4）、ゲートウェアでの記号の切り出し（5）。

## 8. 実機で確かめる手順

配線の注意（前提）: Nucleo と Primer は同じ PC に USB でつなぐ（GND 共通）。**Nucleo の USB を先につなぎ、
信号線は後**。信号はすべて 3.3 V（Primer の入力は 3.3 V、Nucleo の出力は 3.3 V）。Nucleo の PA5・PA9・PC0〜PC3・PB3
は出力にする（Primer の側の ch1・ch4・ch5・ch6 は入力なので、ぶつからない。ch7 にはつながないこと）。

1. 試験用ファームを作る: `python host/firmware/testpoint_demo/build.py`（`testpoints.json` と 3 つの ELF）。
2. **2 線**（配線の変更なし）: `testpoint_demo_serial.elf` を SRAM に書いて走らせる（sram_demo と同じ手順。
   VTOR = 0x20000000）。ロジアナを 100 MS/s・ch4 / ch5 で 10 ms 捕捉 → `SerialDecoder(clock_channel=4,
   data_channel=5)` → `Analyzer(ProbeTable.load("testpoints.json"), hz=100e6)`。`fib` の再帰の木、`classify` の
   case のカバレッジ、`state` の値が出ることを確かめる。
3. **並列**: J12 の ch1・ch4・ch5・ch6 の線を PC0・PC3・PC1・PC2 につなぎ替え（6 節の表）、
   `testpoint_demo_parallel.elf` で同じく。`ParallelDecoder(data_channels=[1, 5, 6], strobe_channel=4)`。
   2 線と同じ ID の列（同じ順）になることを確かめる。
4. **ITM**: `testpoint_demo_itm.elf`（ファームが TPIU を NRZ・2 Mbaud に設定）。fprobe の SWO の受信を 2 Mbaud で
   始め、`itm.ItmDecoder` → `itm.Timestamper` → `testpoint.ItmDecoder(port=1)`、hz = 16e6。GPIO と同じ遷移になること。
5. **負荷の計測**: 3.7 節の表を、ロジアナで 1 記録の長さ（最初のストローブから最後まで）を計って置き換える。
6. **長時間**: 2 線で 1 時間流し続け、`Analyzer(bucket_s=60)` の要約が一定であること、`long_call` が出ないこと。
   ファームの `inject_delay`（デバッガで 1 にすると `classify` が 1 回だけ遅くなる）で `long_call` が出ること。
7. 確かめた後、元の配線（PA15・PA5・PA9・PB3）に戻す。

## 9. 制約・未対応

- マクロに隠れた `return`（`CHECK(x)` が `return` を含む）は見えない。解析は次の出口で `missing_exit` として回復する。
- 式の中の代入（`if ((x = f()))`、`for (i = 0; …)`）は変数の印の対象外（文の水準だけ）。
- 戻り値の型が取れないときの `__auto_type` は GCC / Clang だけ。ビットフィールドを返す式との組み合わせは不可。
- 挿入した本体は C99 以降（宣言の前に文を置く）。
- ITM のローカルのタイムスタンプは FIFO でまとめられた記録に同じ時刻を付ける（精度は SWO の帯域しだい）。
- RTT の時刻は記録の間が 2^32 クロックより長いと戻せない（長く黙るなら心拍のテストポイントを入れる）。
