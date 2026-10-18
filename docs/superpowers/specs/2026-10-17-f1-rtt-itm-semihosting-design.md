# F1: 止めずに見る — ITM/DWT の解読・RTT・セミホスティング（PC 側のライブラリ）

担当 D（並行の第 1 陣）。全体計画: [機能の拡充の全体計画](2026-10-17-feature-expansion-roadmap.md)。
比較の根拠: [J-Link 周辺アプリとの比較](../../reports/2026-10-17-jlink-comparison.md) の 3 節（RTT が最大の欠落、SWO は受信だけで解読が無い、セミホスティングが無い）。

## 目的と範囲

ターゲットを止めずに（または一瞬だけ止めて）中の様子を PC へ出す 3 つの道を、**画面にも API にも依らない
ライブラリ** として作る。

| モジュール | 役割 | 入力 | 出力 |
|---|---|---|---|
| `fprobe/itm.py` | SWO のバイト列を ITM / DWT のパケットへ解読する | バイト列（何回に分けて来てもよい） | パケット（型付きの値）、ポートごとの文字列 |
| `fprobe/rtt.py` | SEGGER RTT の制御ブロックを探し、上り（ターゲット → PC）を読み、下り（PC → ターゲット）へ書く | メモリの読み書き（抽象） | チャンネルごとのバイト列 |
| `fprobe/semihosting.py` | BKPT 0xAB で止まったターゲットの要求を処理し、結果を R0 に書いて PC を進める | レジスタ・メモリの読み書き（抽象） | 出力（コンソール）・終了コード等 |
| `host/firmware/rtt_demo/` | 上の 3 つを実機で確かめるための試験用ファーム（SRAM で動く C、zig cc） | — | `rtt_demo.elf`（リポジトリに置く） |

範囲外（本流で行う）: `fprobe_api.json`・MCP・デーモン・UI への組み込み、実機の試験、FPGA による RTT の高速化。
範囲外（将来）: TPIU のフォーマッタ（16 バイトのフレーム。SWO の UART 方式ではフォーマッタを切るのが普通なので、
本仕様は **フォーマッタ無効のストリーム** を前提とする）、ETM、A プロファイルのセミホスティング（HLT / SVC。F8 で）。

設計の共通方針:

- **入出力を持たない中核**（sans-IO）。ITM の解読器はバイト列を受けてパケットを返すだけ。RTT とセミホスティングは
  メモリ・レジスタを **小さなプロトコル（読み手・書き手）** として受け取り、pyOCD にも fprobe の Session にも依らない。
  偽物（バイト配列のメモリ、辞書のレジスタ）で単体試験する。
- 読み書きの **順序と回数** を設計の一部として明示する（RTT は FPGA へ移すため、最後の節を参照）。
- 例外は意味のある型（`RttError`、`SemihostingError`）にし、値が壊れているときは黙って続けない。

---

## 1. ITM / DWT のパケット解読（`fprobe/itm.py`）

根拠: ARMv7-M Architecture Reference Manual, Appendix D4「Debug ITM and DWT Packet Protocol」。

### 1.1 パケットの種類（最初の 1 バイト = ヘッダで決まる）

| ヘッダ（2 進） | 種類 | 続くバイト | 型 |
|---|---|---|---|
| `0000_0000` ×5 以上 + `1000_0000` | 同期（0 が 47 ビット以上 + 1） | — | `Sync` |
| `0111_0000` | オーバーフロー（ITM の FIFO があふれ、パケットが捨てられた） | 0 | `Overflow` |
| `11TC_0000`（TC = 2 ビット） | ローカルのタイムスタンプ 形式 1 | 1〜4（各バイトの bit7 が続きの印、7 ビットずつ下位から） | `LocalTimestamp(delta, relation)` |
| `0TTT_0000`（TTT = 1〜6） | ローカルのタイムスタンプ 形式 2（1 バイトだけで差分 = TTT） | 0 | `LocalTimestamp(delta, relation=SYNC)` |
| `1001_0100` | グローバルのタイムスタンプ 1（下位 26 ビット） | 1〜4。4 バイト目は `0 Wrap ClkCh TS[25:21]` | `GlobalTimestamp1(value, wrap, clock_change)` |
| `1011_0100` | グローバルのタイムスタンプ 2（上位: [47:26] または [63:26]） | 4 または 6 | `GlobalTimestamp2(value)` |
| `CEEE_1S00` | 拡張（S=0: ITM のスティミュラス・ポートのページ） | C=1 なら続く（7 ビットずつ、最大 4） | `Extension(value, source)` |
| `AAAA_A0SS`（SS≠00） | ソフトウェアのソース（ITM のスティミュラス・ポート A = 0〜31） | SS: 01=1、10=2、11=4 | `Instrumentation(port, value, size)` |
| `AAAA_A1SS`（SS≠00） | ハードウェアのソース（DWT。A = 識別子） | 同上 | 下表 |
| それ以外（下位 2 ビットが 00 の残り） | 予約 | — | `DecodeError`（同期を失う） |

ハードウェアのソース（DWT）の識別子:

| 識別子 | 内容 | 大きさ | 型 |
|---|---|---|---|
| 0 | イベント・カウンタのあふれ（CPI・EXC・SLEEP・LSU・FOLD・CYC の各ビット） | 1 | `EventCounter(flags)` |
| 1 | 例外トレース（番号 9 ビット、機能 1=入る / 2=出る / 3=戻る） | 2 | `ExceptionTrace(number, action)` |
| 2 | PC サンプリング（4 バイト = PC、1 バイト = 眠っていた） | 4 / 1 | `PcSample(pc \| None)` |
| 8 + 2n | データトレース: 比較器 n が一致したときの PC | 4 | `DataTracePc(comparator, pc)` |
| 9 + 2n | データトレース: 比較器 n の番地の下位 16 ビット | 2 | `DataTraceAddress(comparator, offset)` |
| 16 + 2n + RW | データトレース: 比較器 n の値（RW: 0=読み、1=書き） | 1 / 2 / 4 | `DataTraceValue(comparator, write, value, size)` |
| その他 | 未定義 | — | `DecodeError` |

ペイロードはリトル・エンディアン。

### 1.2 解読器 `ItmDecoder`

```python
dec = ItmDecoder(synced=True)       # 受信を始めた直後から来るなら True（既定）。途中から来るなら False
for packet in dec.feed(chunk):      # chunk はどこで切れていてもよい（パケットの途中でも）
    ...
dec.synced                          # 同期が取れているか
```

- **増分で解読する。** 受け取ったバイトを内部の待ち行列に足し、先頭から「完全なパケット」を 1 つずつ取り出す。
  パケットが途中で切れていれば次の `feed` を待つ。各パケットには先頭のバイトの通し番号（`offset`）を付ける。
- **同期の取り直し。** 解読できないヘッダ（予約、未定義の DWT 識別子、大きさが合わない DWT パケット、続きの印が長すぎる）
  に出会ったら `DecodeError` を 1 つ出して **同期を失った** 状態へ移る。同期を失っている間は、同期パケット
  （0 が 5 バイト以上 + `0x80`）を探し、それまでのバイトは捨てる（捨てた数は、同期を取り戻したときの `Sync.skipped` に数える）。
  `synced=False` で始めると、最初の同期パケットまで同じく捨てる（ストリームの途中から受け始めた場合）。
  - 同期パケットはファームウェア（または fprobe）が ITM_TCR.SYNCENA と DWT_CTRL.SYNCTAP で出させる。出していない
    ストリームでは、一度失った同期は戻らない。これは ITM の仕組みの限界（区切りの印が同期パケットしか無い）で、
    仕様書に明記し、`DecodeError` で利用者に見えるようにする。
  - 同期が取れている状態で 0x00 が来たら同期パケットの始まりとみなす。0 が 5 バイト未満で他の値が来たら誤り。
- **オーバーフロー** は `Overflow` として出す（データの欠落の印。統計に数える）。

### 1.3 時刻の付与 `Timestamper`

ローカルのタイムスタンプは「直前のタイムスタンプからの差分」で、**それが計る出来事のパケットの後に** 来る
（ARM の規定）。`Timestamper` はパケットをためて、次のローカルのタイムスタンプが来たときに
累積の時刻（`local_time`、TPIU の前置分周後のクロック数）を付けて吐き出す。グローバルのタイムスタンプは 1 と 2 を
組み合わせて 64 ビットの値（`global_time`）にする（`ClkCh` が立てば上位を無効にする）。

```python
ts = Timestamper()
for timed in ts.feed(packets):     # TimedPacket(packet, local_time, global_time)
    ...
ts.flush()                         # 最後のタイムスタンプより後のパケット（時刻は未定 = None）
```

`Overflow` の後は差分の基準が失われうるので、`TimedPacket.after_overflow` に印を付ける。

### 1.4 ポートごとの文字列 `PortText`

printf 用。`Instrumentation` のペイロード（1/2/4 バイト、リトル・エンディアン）をポートごとのバイト列につなぎ、
改行（`\n`）で行に切る。行は UTF-8 で解き、壊れたバイトは置き換える（`errors="replace"`）。`\r` は行末から落とす。

```python
text = PortText(ports={0, 1})       # None なら全ポート
for line in text.feed(packets):     # TextLine(port, text)
    ...
text.pending(0)                     # まだ改行の来ていない途中の文字列
```

多バイトのパケット（例: 4 バイトの書き込みで 4 文字）にも、UTF-8 の文字がパケットの境目で切れる場合にも対応する
（バイト列でためてから行ごとに解くため）。

---

## 2. RTT（`fprobe/rtt.py`）

根拠: SEGGER の公開している RTT の制御ブロックの形式（J-Link の文書）。**SEGGER のソースは使わず**、
メモリ上の形式（他のデバッガ・probe-rs・pyOCD・OpenOCD も実装している公開の取り決め）だけに従う。

### 2.1 メモリ上の形式（32 ビットのターゲット、リトル・エンディアン）

制御ブロック:

| 位置 | 大きさ | 内容 |
|---|---|---|
| +0 | 16 | ID: `"SEGGER RTT"` + NUL で 16 バイトを埋める |
| +16 | 4 | 上りのバッファの数 `MaxNumUpBuffers` |
| +20 | 4 | 下りのバッファの数 `MaxNumDownBuffers` |
| +24 | 24 × 上りの数 | 上りのバッファの記述子 |
| 続き | 24 × 下りの数 | 下りのバッファの記述子 |

バッファの記述子（24 バイト）:

| 位置 | 内容 | 上りで書くのは | 下りで書くのは |
|---|---|---|---|
| +0 | 名前（NUL 終端の文字列）の番地。0 なら名前なし | ターゲット | ターゲット |
| +4 | リングの番地 | ターゲット | ターゲット |
| +8 | リングの大きさ（バイト） | ターゲット | ターゲット |
| +12 | 書き込み位置 `WrOff` | **ターゲット** | **PC** |
| +16 | 読み出し位置 `RdOff` | **PC** | **ターゲット** |
| +20 | フラグ（下位 2 ビット = モード） | ターゲット | ターゲット |

リング: `RdOff == WrOff` で空。1 バイトは空けておく（`(WrOff + 1) % size == RdOff` で満杯）。
書く側はデータを書いてから位置を進め、読む側はデータを読んでから位置を進める。**位置の 4 バイトは各々 1 回の
32 ビットの書き込みで更新する**（ターゲット側と PC 側で書く欄が分かれているので、ロックは要らない）。

モード（フラグの下位 2 ビット。ターゲットが上りに書くときの振る舞い。PC 側の読み出しには影響しない）:

| 値 | 名前 | 空きが足りないとき |
|---|---|---|
| 0 | `NO_BLOCK_SKIP` | その書き込みを丸ごと捨てる（既定） |
| 1 | `NO_BLOCK_TRIM` | 入るだけ書いて残りを捨てる |
| 2 | `BLOCK_IF_FIFO_FULL` | 空くまで待つ（PC が読まないとターゲットが止まる） |

RTT に「古いデータを上書きする」モードは無い（書く側が読み出し位置に触れないため）。試験では 3 つのモードを
ターゲット側の模擬で再現し、PC 側が正しく受けることを確かめる。

### 2.2 メモリの抽象

```python
class MemoryAccess(Protocol):
    def read_memory(self, address: int, length: int) -> bytes: ...
    def write_memory(self, address: int, data: bytes) -> None: ...
```

pyOCD のターゲットへの橋渡し（`read_memory_block8` 等）は本流の組み込みで作る（`debug.py` は変えない）。

### 2.3 制御ブロックの見つけ方

1. **ELF のシンボル** `_SEGGER_RTT`（`rtt_address_from_elf(path)`。既存の `fprobe.symbols.Symbols.variable` を使う）。
   最も速く確実。
2. **RAM の走査**（`find_control_block(memory, ranges, chunk=4096)`）: 範囲をかたまりで読み、ID の 11 バイト
   （`"SEGGER RTT\0"`）を探す。かたまりの境目をまたぐ ID も見つかるよう、読みを 15 バイトずつ重ねる。
   見つけた候補は **中身を検める**（バッファの数が 0〜64 と 0〜64 で上りが 1 以上、各記述子の大きさが 0 でなく
   リングの番地 + 大きさが 32 ビットに収まり、位置が大きさ未満）。通らない候補は捨てて走査を続ける
   （ファームの読み出し専用データに ID の文字列が残っている場合への備え）。見つからなければ `ControlBlockNotFound`（`RttError` の一種）。

### 2.4 読み書きの手順（何を、どの順で）

`Rtt(memory, address)` の `open()` が制御ブロックを読み、`RttControlBlock`（各 `RttChannel` の名前・リングの番地・
大きさ・フラグ）を持つ。名前とリングの番地・大きさは開くときに 1 度だけ読み、以後は位置だけを読む。

**上りの読み出し `poll()`（全チャンネル）:**

| 順 | 操作 | 番地・長さ | 備考 |
|---|---|---|---|
| R1 | 読む | 上りの記述子の配列全体（24 × 上りの数）**＋（後ろに続くなら）推測したデータ** | 1 回の読みで全チャンネルの `WrOff` を得る |
| R2 | 読む（必要なら） | 各チャンネルの `[RdOff, WrOff)`（折り返していれば 2 区間）のうち R1 で得ていない分 | |
| W1 | 書く（データがあったチャンネルだけ） | そのチャンネルの `RdOff`（4 バイト） | データを受け取った後 |

- `RdOff` は PC だけが書く欄なので、R1 で読んだ値ではなく **PC が覚えている値** を使う（開くときに 1 度だけ読む）。
  R1 で読んだ `RdOff` が覚えている値と違えば、別の道具（J-Link 等）も読んでいるか、ターゲットが初期化し直した
  とみなし、R1 の値に合わせる（`RttChannel.resyncs` に数える）。
- `WrOff >= size` などの壊れた値は `RttError`（黙って読まない）。

**1 回の読みにまとめる工夫（R1 の推測読み）。** 位置とデータを別々に読むと 1 チャンネルに 2 往復かかる。
USB の往復（〜125 µs × 2）はデータ数十バイトの転送より高くつくので、R1 で **記述子の配列に続けて、各チャンネルの
読み出し位置からの `speculate` バイト（既定 64）** を同じ 1 回の読みで読む。安全の条件:

- ターゲットは「データを書いてから `WrOff` を進める」。PC が `WrOff` を **先に** 読み、データを **後で** 読めば、
  `[RdOff, WrOff)` のデータは必ず書き終わっている。1 回のブロック読みは番地の小さい順に読む（MEM-AP の自動増分）
  ので、**推測するデータの番地が記述子より後ろにあるときだけ** まとめてよい。前にあるリングはまとめない（R2 で読む）。
- まとめる区間の間の隙間は `merge_gap`（既定 256 バイト）以下、全体は `max_read`（既定 4096 バイト）以下に限る
  （隙間を読む無駄が往復 1 回の節約を上回らないように）。

結果として、ログが少しずつ出る普通の場合は **1 回の読み + 1 回の書き** で済む。

**下りの書き込み `write(channel, data)`:**

| 順 | 操作 | 番地・長さ | 備考 |
|---|---|---|---|
| D1 | 読む | そのチャンネルの記述子の `WrOff`・`RdOff`（8 バイト） | 空きを知る。`WrOff` は PC が覚えている値と照合 |
| D2 | 書く | リングの `[WrOff, WrOff + n)`（折り返せば 2 区間） | n = min(データ, 空き) |
| D3 | 書く | `WrOff`（4 バイト） | **データの後に**（ターゲットが未完成のデータを読まないように） |

戻り値は書けたバイト数（空きが足りなければ一部だけ。呼び出し側が残りを後で送る）。`write_all(data, timeout_s)` は
空くまで繰り返す（ターゲットが読まなければ時間切れで書けた数を返す）。

### 2.5 型

```python
@dataclass
class RttChannel:   index, direction("up"/"down"), name, descriptor_address, buffer_address, size, flags, mode
@dataclass(frozen=True)
class RttControlBlock: address, up: tuple[RttChannel, ...], down: tuple[RttChannel, ...]
class Rtt:  open() / poll() -> dict[int, bytes] / read(channel) -> bytes / write(channel, data) -> int / write_all(...)
ring_used(size, wr, rd), ring_free(size, wr, rd), ring_spans(start, count, size)   # 純粋関数（FPGA と共有する定義）
```

---

## 3. セミホスティング（`fprobe/semihosting.py`）

根拠: Arm「Semihosting for AArch32 and AArch64」（v2.0）。M プロファイルは `BKPT 0xAB`（Thumb の `0xBEAB`）。
R0 = 操作の番号、R1 = 引数（多くは引数のブロックの番地）。結果は R0 に返す。

### 3.1 処理の流れ

```python
host = SemihostingHost(console=on_output, files=FileAccess(root=None), stdin=None)
result = host.handle(target)          # target: レジスタ・メモリの抽象
```

1. PC の 2 バイトを読み、`0xBEAB` でなければ **何もしない**（`handled=False`。普通のブレークポイント等）。
2. R0・R1 を読み、操作を処理する（引数のブロックは 1 回の読みで取る）。
3. 結果を R0 に書き（SYS_WRITEC・SYS_WRITE0・SYS_HEAPINFO は R0 を変えない）、**PC を 2 進める**（SYS_EXIT では進めない。誤って再開しても終了の要求が繰り返されるだけになる）。errno はターゲットの newlib の値に訳す（`TARGET_ERRNO`）。
4. `SemihostingResult(handled, operation, exit_code)` を返す。再開は呼び出し側（`service(target)` が再開まで
   まとめて行う。`SYS_EXIT` のときは再開しない）。

ターゲットの抽象:

```python
class SemihostingTarget(Protocol):
    def read_core_register(self, name: str) -> int: ...       # "r0", "r1", "pc"
    def write_core_register(self, name: str, value: int) -> None: ...
    def read_memory(self, address: int, length: int) -> bytes: ...
    def write_memory(self, address: int, data: bytes) -> None: ...
```

### 3.2 操作

| 番号 | 名前 | 処理 |
|---|---|---|
| 0x01 | SYS_OPEN | `:tt` は標準入出力（モード r → 入力、w → 出力、a → エラー出力）。`:semihosting-features` は機能のバイト列。他は `FileAccess` 次第 |
| 0x02 | SYS_CLOSE | 閉じる（標準入出力は閉じたことにするだけ） |
| 0x03 | SYS_WRITEC | 1 文字を出力 |
| 0x04 | SYS_WRITE0 | NUL 終端の文字列を出力（64 バイトずつ読んで NUL を探す。上限 64 KiB） |
| 0x05 | SYS_WRITE | 書けなかったバイト数を返す（0 で成功） |
| 0x06 | SYS_READ | 読めなかったバイト数を返す（長さ = EOF） |
| 0x07 | SYS_READC | 1 文字（入力が無ければ -1） |
| 0x08 | SYS_ISERROR | 値が負なら 1 |
| 0x09 | SYS_ISTTY | 標準入出力なら 1 |
| 0x0A | SYS_SEEK | 0 / -1 |
| 0x0C | SYS_FLEN | 長さ / -1 |
| 0x0D | SYS_TMPNAM | -1（対応しない） |
| 0x0E | SYS_REMOVE | `FileAccess` が書き込み可のときだけ |
| 0x0F | SYS_RENAME | 同上 |
| 0x10 | SYS_CLOCK | 開始からのセンチ秒 |
| 0x11 | SYS_TIME | 1970 年からの秒 |
| 0x12 | SYS_SYSTEM | **常に拒否**（-1。PC でコマンドを実行しない） |
| 0x13 | SYS_ERRNO | 直前の errno |
| 0x15 | SYS_GET_CMDLINE | 設定した文字列を書く |
| 0x16 | SYS_HEAPINFO | 設定したヒープ・スタックの 4 語を書く（既定 0 = ターゲットが自分で決める） |
| 0x18 | SYS_EXIT | R1 = 理由（`ADP_Stopped_ApplicationExit` = 0x20026 なら終了コード 0、それ以外は理由の値を非 0 として扱う） |
| 0x20 | SYS_EXIT_EXTENDED | 引数のブロック [理由, 終了コード] |
| 0x30 | SYS_ELAPSED | 64 ビットの刻み数を書く |
| 0x31 | SYS_TICKFREQ | 刻みの周波数（1 MHz） |
| 他 | — | -1、errno = ENOSYS。`SemihostingResult.unsupported` に印 |

`:semihosting-features` は `SHFB` + 1 バイト（bit0 = `SYS_EXIT_EXTENDED` あり、bit1 = `:tt` の `a` を標準エラーに
分ける）を返す。

### 3.3 ファイル操作の安全

ターゲット（つまり任意のファーム）が PC のファイルを開けるのは危険なので、**既定は無効**（`FileAccess(root=None)`:
`:tt` 以外の SYS_OPEN は -1、errno = EACCES）。有効にするときは:

- `root` の下だけ。相対パスだけを受け付け、絶対パス・ドライブ名・UNC（外の共有。開くと認証が外へ出る）・
  `..` で外へ出るもの・シンボリックリンクで外へ出るもの（`resolve()` してから `root` の中かを確かめる）を拒否する。
- `writable=False`（既定）なら読み出しのモード（r / rb）だけ。書き込み・追記・SYS_REMOVE・SYS_RENAME は拒否。
- 名前の長さ・開けるファイルの数（既定 16）に上限。

---

## 4. 試験用ファーム（`host/firmware/rtt_demo/`）

`sram_demo` と同じ方針（zig cc、Cortex-M4、SRAM の先頭 32 KB に置いて SRAM から実行、ELF をリポジトリに置く）。

- **RTT の最小実装（自作）**: 制御ブロック `_SEGGER_RTT` を **静的に初期化**（.data。SRAM に直接読み込むので、
  読み込んだ時点で制御ブロックが有効になる。ELF の中身だけで走査・解析の試験ができる）。上り 2（`Terminal` 512 B、
  `Data` 256 B）・下り 1（`Terminal` 64 B）。モードは上り 0 が `NO_BLOCK_SKIP`、上り 1 が `NO_BLOCK_TRIM`。
  ID の文字列は制御ブロックの 1 か所だけにある（ほかに `"SEGGER RTT"` の並びを置かない）。
- **ITM への printf**: ITM・TPIU（NRZ、2 Mbaud、フォーマッタ無効）・PB3 の SWO をファーム自身が設定する
  （HSI 16 MHz に切り替えてから。`ACPR = 16 MHz / 2 MHz - 1 = 7`）。ポート 0 に文字列、ポート 1 に 32 ビットの値。
  ローカルのタイムスタンプ・同期パケット・例外トレース（SysTick 1 kHz）を出す。設定は変数 `trace_config` で切り替え可能。
- **セミホスティング**: 変数 `semihosting_enabled` が 0 でなければ、1 回だけ `SYS_WRITE0`・`SYS_OPEN(":tt")`・`SYS_WRITE`・
  `SYS_CLOCK`・`SYS_TIME` を呼び、結果を変数に残す。`semihosting_exit` が 0 でなければ `SYS_EXIT_EXTENDED` で終わる。
  既定は 0（セミホスティングを処理しないデバッガでは BKPT で止まったままになるため、PC が書き換えてから有効にする）。
- 下りのチャンネル 0 に来た文字列を上りのチャンネル 0 へ `echo: ...` で返す（双方向の試験）。

---

## 5. 試験

| 対象 | 方法 |
|---|---|
| ITM | 手で組み立てたバイト列: 全パケットの種類、1 バイトずつ・任意の区切りで与えても同じ結果、途中から始まるストリーム（同期パケットまで捨てる）、予約ヘッダでの同期の喪失と回復、オーバーフロー、タイムスタンプの累積、ポートごとの行の組み立て（多バイト・UTF-8 の分断） |
| RTT | 偽のメモリ（`bytearray`、読み書きを記録）に制御ブロックとリングを組み立て、ターゲットの書き込み（3 つのモード）を模擬: 読み出し・折り返し・`RdOff` の更新・読みの回数（普通の場合 1 回）・読み書きの順序・下りの書き込みと空き・壊れた値の検出・走査（かたまりの境目、偽の ID）。`rtt_demo.elf` の `_SEGGER_RTT` と、ELF の中身を偽のメモリに置いた走査・解析 |
| セミホスティング | 偽のターゲット（レジスタの辞書とメモリ）で各操作、BKPT でない停止、ファイル操作の拒否（既定・絶対パス・`..`・UNC・書き込み）と許可（`root` の下）、終了 |

---

## 6. 本流での API・MCP への組み込みの提案

（担当 D は `fprobe_api.json`・`mcp_server.py`・デーモンを変えない。以下は本流への提案。）

1. **メモリ・レジスタの橋渡し**: `debug.py` か新しい小さなモジュールに、pyOCD のターゲットを `MemoryAccess` /
   `SemihostingTarget` に合わせる薄い包み（`read_memory_block8` → `bytes`、`write_memory_block8`、
   `read_core_register` / `write_core_register`）を 1 つ置く。
2. **SWO の解読（API）**: `swo_read` は 16 進の生データのまま残し、解読した版を足す:
   - `swo_decode(timeout_s) -> {"packets": [...], "text": {port: [行]}, "overflows": n, "synced": bool}`。
     デーモンの中で `ItmDecoder`・`Timestamper`・`PortText` をセッションごとに持ち続ける（途中のパケット・行を
     次の呼び出しへ持ち越すため）。
   - `swo_text(port=0, timeout_s)`（printf 用の簡易版）。
   - 例外トレースは F3（共通の時間軸）のイベントへ、PC サンプリングは F5（プロファイル）へ流す。
3. **RTT（API・MCP）**: `rtt_start(address=None, search_ranges=None)`（ELF が読み込まれていれば `_SEGGER_RTT`、
   無ければ RAM を走査）→ チャンネルの一覧を返す、`rtt_read(channel=None, timeout_s)`、`rtt_write(channel, text|hex)`、
   `rtt_stop()`。デーモンが背景で `poll()` を周期的に呼び、チャンネルごとの受信を貯めて配る（GUI と AI が同時に読める）。
   ターゲットを止めずに読めるので、デバッガの操作（停止・再開）と排他にしなくてよい（MEM-AP の読みだけ）。
4. **セミホスティング**: `run_until_halt` の結果が BKPT 0xAB なら、デーモンが自動で `SemihostingHost.handle()` → 再開を行う
   設定（`semihosting_enable(file_root=None, writable=False)`）。出力は RTT と同じ受信の貯め場（チャンネル名
   `semihosting`）に流す。`SYS_EXIT` は停止の理由 `"exit"` と終了コードとして API に返す（AI の自動試験で使える）。
   ファイル操作の `root` は API で明示されたときだけ有効にし、MCP からは既定で無効のままにする。
5. **試験**: `rtt_demo.elf` を `load_to_ram` して、RTT の受信（`tick` の行）、下りの echo、SWO の行と例外トレース、
   `semihosting_enabled` を書いてからのセミホスティングを実機（Nucleo F401RE）で確かめる。

## 7. FPGA での高速化の考察

RTT を PC から読むと、`poll()` 1 回に USB の往復が 2 回（読み 1 + 書き 1）かかり、全体で数 kHz が上限になる。
FPGA が自分で読めば、SWD の速さ（25 MHz なら 1 語 ≈ 2〜3 µs）で回り、データだけを PC へ流せる。

**FPGA に移す範囲（ハードウェアの FSM）:** §2.4 の上りの手順 R1 → R2 → W1 は、分岐の少ない固定の手順で、
FPGA に移しやすい。

- PC が `open()` で解いた静的な情報（各チャンネルの記述子の番地・リングの番地・大きさ）を CSR に書く
  （チャンネル数は FPGA のケイパビリティで申告。小さい FPGA では 1〜2）。名前の読み出し・走査は PC に残す。
- FPGA は周期（CSR で設定、例えば 100 µs）ごとに:
  1. 記述子の `WrOff` を読む（各チャンネル 1 語。記述子の配列を 1 回のブロック読み）。
  2. `RdOff`（FPGA の中のレジスタ。PC が持っていた値を引き継ぐ）と比べ、差があれば `[RdOff, WrOff)` を
     SWD のブロック読み（折り返しで 2 区間）。MEM-AP の TAR の自動増分は 1 KB 境界で止まるので、境界で TAR を書き直す
     （既存の SWD エンジンのブロック読みの規則と同じ）。
  3. 読んだデータを「チャンネル番号・時刻（共通タイムスタンプ）・長さ」の見出しを付けて、ロジアナ・イベントと同じ
     拡張ストリームへ出す（F3 の共通の時間軸に、RTT の出力が時刻付きで並ぶ — J-Link にない強み）。
  4. `RdOff` を 1 語書く。
- 推測読み（§2.4 の工夫）は FPGA では不要（SWD の往復は µs 単位で、USB の往復がない）。逆に 1 語ずつ読む方が
  バスの占有が短い。順序の規則（`WrOff` を読んでからデータ、データを読んでから `RdOff` を書く）はそのまま守る。
- **CMSIS-DAP との共有**: SWD のバスは 1 本なので、デバッガ（pyOCD）の要求と RTT の自動読みは調停が要る。
  CMSIS-DAP のコマンドの切れ目（DAP_Transfer の間）にだけ RTT の手順を挟む。RTT の手順の途中で SELECT・TAR・CSW を
  変えるので、終わったら元に戻す（または DAP 側のキャッシュを無効にする印を立てる）。ターゲットが止まっていても
  RTT は読める（MEM-AP はコアの状態に依らない）。
- **下り**（PC → ターゲット）は頻度が低いので PC 側の手順（§2.4 D1〜D3）のままでよい。ただし FPGA が `RdOff` 等を
  キャッシュしている場合と競合しないよう、下りは FPGA の対象外のチャンネルとして扱う。
- **ITM の解読**: SWO の受信は既に FPGA にある。パケットの区切り（ヘッダから長さが決まる）と同期の取り直しは小さな
  FSM で書けるので、FPGA でパケットの境目にタイムスタンプ（共通の時間軸）を付けて流すと、ITM のローカルの
  タイムスタンプより正確な絶対時刻が得られる。解読（意味づけ）は PC に残す（`ItmDecoder` がそのまま使える）。
- 純粋関数 `ring_used` / `ring_free` / `ring_spans` は FPGA の FSM の参照モデルとして cocotb の試験から使う。
