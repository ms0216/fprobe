# F2（一部）: DWARF による「ソースの言葉の変数」と CFI の呼び出し履歴 — 設計

担当 A（[全体計画](2026-10-17-feature-expansion-roadmap.md)）。根拠: [比較](../../reports/2026-10-17-jlink-comparison.md) の
「変数: 構造体・ポインタ・ローカル変数・式の評価 ×」「呼び出し履歴 △（簡易）」。

## 目的

Ozone・IAR・Keil と同じ水準で、止まったターゲットの変数をソースの言葉で見る:

- 型（基本型・構造体・共用体・配列（多次元）・ポインタ・列挙・typedef・const/volatile・ビットフィールド）を DWARF から読み、
  バイト列を構造化した値（JSON にできる）にする。
- 大域・静的変数に加え、**関数の引数とローカル変数**を、位置式（ロケーションリストを含む）から読む。
- C の簡単な式（`a.b[3].c`、`*p`、`p->x`、`&v`、`arr[2]`、キャスト、整数の `+ -`）を型付きで評価する。
- `.debug_frame`（無ければ `.eh_frame`）の CFI で呼び出し履歴を巻き戻す。Cortex-M の例外のフレーム（EXC_RETURN）を越える。
  各フレームの PC・関数名・ファイル:行・引数とローカル変数を出す。

ライブラリ（`host/fprobe/dwarf/`）と単体試験だけを作る。API・MCP・デーモン・UI は本流で組み込む（最後の節に提案）。

## 層と責務

```
fprobe/dwarf/
  access.py     読み手の抽象（TargetAccess: read_memory / read_register）、DWARF のレジスタ番号 ⇄ 名前、
                ELF の初期値を読む ImageAccess（偽物にも、ターゲット無しの静的な評価にも使う）
  typesys.py    型の模型（CType の派生）と DIE からの読み込み（TypeLoader）、型の名前（C の書き方）
  values.py     バイト列 + 型 → JSON にできる値（decode）、値 → バイト列（encode）
  location.py   位置式の評価（スタック機械）→ Place（メモリ・レジスタ・値・断片・無し）と、Place からの読み出し
  info.py       DebugInfo: CU・関数（番地の範囲）・スコープ（字句ブロック）・変数（大域・静的・引数・ローカル）・型の名前の索引
  unwind.py     CFI の表（FDE の検索・行の選択）と巻き戻し（Unwinder）。Cortex-M の例外のフレーム
  cexpr.py      C の式の字句解析・構文解析・評価（Value = 型 + 場所）
  inspector.py  上の部品をまとめる窓口: backtrace / frame_variables / evaluate / read / write
```

依存の向き: `inspector → (unwind, cexpr, info) → (location, values, typesys) → access`。pyOCD には依存しない
（`fprobe.debug` が pyOCD のターゲットを `TargetAccess` に包む `PyocdAccess` を持つ）。
新しい依存は足さない（pyelftools は既存）。pyelftools の DIE・位置式の字句解析・ロケーションリスト・レンジリスト・CFI の
表の展開（`get_decoded()`）を使い、評価（スタック機械・巻き戻し・型の解読）は自前で書く。

### 読み手（access.py）

```python
class TargetAccess(Protocol):
    def read_memory(self, address: int, length: int) -> bytes: ...
    def read_register(self, name: str) -> int: ...          # "r0".."r12" "sp" "lr" "pc" "xpsr" "msp" "psp" "s0".."s31"
class WritableAccess(TargetAccess, Protocol):
    def write_memory(self, address: int, data: bytes) -> None: ...
```

レジスタは名前で受け取る（pyOCD の名前と同じ）。DWARF のレジスタ番号は 0〜15 = r0〜r15（13 = sp、14 = lr、15 = pc）、
64〜95 = s0〜s31、256〜287 = d0〜d31（d*n* = s*2n*:s*2n+1*）。読めないメモリ・レジスタは `AccessError`。
`ImageAccess(segments, registers)` は ELF の PT_LOAD の中身を疎なメモリとして持ち、書き込みも受ける（試験の偽物）。

### 型（typesys.py）

`BaseType(name, size, encoding)`（encoding: signed / unsigned / signed_char / unsigned_char / float / bool）、
`PointerType(target)`、`StructType(kind = struct|union, name, size, members)`、`Member(name, type, offset, bit_size, bit_offset)`、
`ArrayType(element, length)`（多次元は入れ子。DWARF の 1 つの array_type の複数の subrange を入れ子に直す）、
`EnumType(name, size, signed, enumerators)`、`TypedefType`、`QualifiedType(const|volatile|restrict|atomic)`、
`FunctionType`、`VoidType`。`strip()` で typedef・修飾子を外す。`type_name()` は C の書き方（`struct Sensor *`、`int32_t[2][3]`、`const char *`）。

- 自分を指す構造体（`struct Sensor *next`）のため、型は DIE の位置で記憶し、構造体は先に箱を作ってからメンバーを埋める。
- ビットフィールド: DWARF 4/5 の `DW_AT_data_bit_offset`（構造体の先頭からのビット位置）と、DWARF 2/3 の `DW_AT_bit_offset`
  （記憶単位の最上位からのビット位置。GCC が出す）の両方を「構造体の先頭からのビット位置」に直す（リトルエンディアン）。
- 宣言だけの型（不完全型）は size = None。

### 値（values.py）

どの値も `kind`（値の種類。DWARF の型で決まる事実）を持つ JSON の object。使う側（画面・AI・ホストの道具）は `kind` を
読み、キーの有無や型の名前から種類を推さない（2026-10-24 に改めた。前の形はキーの有無でしか種類が分からず、構造体の
メンバー名が `enum`・`value` 等だと別の種類に見え、`float32_t` 等の typedef の浮動小数は整数に見えた。API の版 2）。

| 型 | 値 |
|---|---|
| 整数・文字 | `{"kind": "int", "value": 1}` |
| bool | `{"kind": "bool", "value": true}` |
| float / double | `{"kind": "float", "value": 2.5}`（NaN・無限大は value が文字列 `"nan"` / `"inf"` / `"-inf"`。JSON にできるように） |
| 生のバイト | `{"kind": "bytes", "hex": "ff7f"}`（符号の種類の分からない基本型・4 / 8 バイト以外の浮動小数・void） |
| 列挙 | `{"kind": "enum", "name": "MODE_RUN", "value": 1}`（列挙子に無い値は `"name": null`） |
| ポインタ | `{"kind": "pointer", "address": 536871200, "to": "struct Sensor"}`。`char *` は読み手があれば `"string"` も（NUL の前で読むのをやめたら `string_end`・`string_limit`）。関数ポインタ・番地はシンボルがあれば `"symbol": "square"` |
| 構造体・共用体 | `{"kind": "struct", "members": {メンバー名: 値}}`（宣言の順。無名のメンバーは C11 と同じく外側に展開） |
| 配列 | `{"kind": "array", "length": n, "items": [値, ...]}`（多次元は入れ子）。items は `max_elements`（既定 64）個まで: `len(items) < length` なら切った |
| 文字の配列 | 配列 + `"string": "hello"`（NUL まで。非 ASCII は `\xNN`）。文字 = `char`・`signed char`・`unsigned char`（`uint8_t`・`int8_t` の typedef は数の配列として扱う） |
| ビットフィールド | int・enum・bool の同じ形（符号付きは符号拡張） |
| 読めない | `{"kind": "unavailable", "reason": "理由"}`（最適化で消えた・レジスタが復元できない・関数の入口の値 等） |

`encode(type, value)` は基本型・列挙（名前か数）・ポインタ（数）・bool をバイト列にする（書き込み用）。

### 位置式（location.py）

スタック機械。対応: `addr` `addrx` `const*` `lit*` `reg*` `regx` `breg*` `bregx` `fbreg` `call_frame_cfa` `dup` `drop` `over`
`pick` `swap` `rot` `deref` `deref_size` 算術・論理・比較 `skip` `bra` `nop` `plus_uconst` `stack_value` `implicit_value`
`piece` `convert`（基本型の幅で切り詰め・符号拡張）。`entry_value`（関数の入口のレジスタ値）は呼び出し側の情報が要るので
「読めない（entry value）」とする。`bit_piece` はバイト境界のものだけ。結果は `Place`:
`MemoryPlace(address)` / `RegisterPlace(regno)` / `ValuePlace(data)` / `PiecesPlace((place, size)...)` / `Unavailable(reason)`。

- フレームの基底（`DW_AT_frame_base`）は必要になったとき（`fbreg`）だけ評価する。`DW_OP_call_frame_cfa` は巻き戻しの CFA。
- ロケーションリストは PC で選ぶ（DWARF 4 の `.debug_loc`、DWARF 5 の `.debug_loclists`。基底は CU の low_pc と
  base_address 項目。`default_location` も）。呼び出し元のフレームでは PC は戻り先なので **PC − 1** で引く（呼び出し命令の中）。

### デバッグ情報（info.py）

`DebugInfo(path)`: ELF をメモリに読み、CU ごとに関数（`DW_TAG_subprogram`、low_pc/high_pc か ranges）、字句ブロック、
変数を索引する。`function_at(pc)`、`scope_at(pc)`（関数 → 内側のブロックの順の変数: 引数・ローカル・関数内の static）、
`global_variable(name)`（外部 → ファイルの static。`関数名::名前` で関数の中の static）、`type_named("struct Sensor" | "uint32_t" | "unsigned int")`。
変数は `Variable(name, kind = global|static|parameter|local, type, location, decl_file, decl_line)`。
`DW_AT_const_value` の変数・引数は値として扱う。宣言だけ（`DW_AT_declaration`）の大域は定義を探す。

### 巻き戻し（unwind.py）

1. FDE を PC で引く（`.debug_frame`、無ければ `.eh_frame`）。表の行のうち PC 以下で最後のものを使う。
2. CFA = 規則のレジスタ + オフセット（または式）。各レジスタの規則（offset / val_offset / register / same_value / undefined /
   expression / val_expression）で呼び出し元のレジスタを作る。d レジスタの退避（vpush）は s の 2 つに分ける。
3. 規則の無いレジスタ: callee-saved（r4〜r11、s16〜s31、lr）は「同じ値」、caller-saved（r0〜r3、r12、s0〜s15）は呼び出し元では
   「分からない」（AAPCS。嘘の値を出さない）。sp = CFA。
4. 戻り先（CIE の return_address_register = lr）が **EXC_RETURN**（`0xFFFFFF80` 以上で bit1 = 0）なら例外のフレーム:
   bit2 で PSP / MSP（MSP は例外の入口の SP = ハンドラの CFA）、`r0 r1 r2 r3 r12 lr pc xpsr` を読み、bit4 = 0（FP の拡張フレーム）
   なら 0x68 バイト（s0〜s15・FPSCR。FPCCR.LSPACT = 1（遅延退避がまだ）なら s0〜s15 は分からない）、xPSR の bit9 なら 4 バイトの詰め物。
   中断された処理の PC は正確な番地（−1 せずに引く）。frame の note = `"exception"`（既存の debug.py と同じ）。
5. それ以外: 呼び出し元の PC = 戻り先 & ~1。止める条件: PC = 0、戻り先が分からない、0xFFFFFFFF（リセット直後の LR）、
   CFA が増えない（例外の境界を除く）、最大 64 フレーム。
6. フレーム 0 の PC に FDE が無い（デバッグ情報の無い関数）: 葉とみなし CFA = sp、戻り先 = lr。2 段目以降に FDE が無ければ止める。

各フレーム: `FrameState(index, pc, lookup_pc, cfa, registers, note)`。フレーム 0 のレジスタは読み手から必要な時に読む。
v8-M の Secure の拡張フレーム（DCRS）とフレームポインタ（r7）による推測の巻き戻しは対象外。

### C の式（cexpr.py）

```
expr    := unary (('+' | '-') unary)*
unary   := ('*' | '&' | '-' | '~' | '!') unary | '(' 型名 ')' unary | postfix
postfix := primary ('[' expr ']' | '.' 名前 | '->' 名前)*
primary := 名前 ('::' 名前)? | 整数（10 進・0x・0b・文字 'a'） | '(' expr ')'
型名    := (const|volatile)* (struct|union|enum 名前 | 基本型の語の並び | typedef 名) '*'*
```

名前は、選んだフレームのスコープ（内側のブロック → 関数 → 引数）→ その CU の static → 大域 の順に引く。
評価の結果 `Value(type, place, bit_offset, bit_size)`。`&` はメモリ上の値だけ。配列は `*`・`+` でポインタに崩れる。
添字・ポインタの足し算は要素の大きさを掛ける。キャストは整数・ポインタの間では値を変換し、ポインタの型を変える（`*(struct Sensor *)0x20000100`）。

### 窓口（inspector.py）

```python
insp = Inspector(DebugInfo("app.elf"), access, symbols=Symbols("app.elf"))
insp.backtrace(variables=True)   # [{"index", "pc", "function", "file", "line", "cfa", "note", "arguments", "locals"}]
insp.frame_variables(1)          # {"arguments": [{"name", "type", "value"}], "locals": [...]}
insp.read("head->samples[1]", frame=0, max_elements=64)   # {"expression", "type", "address", "value"}
insp.write("sensors[0].gain", 2.5)                         # 書き込み（メモリ上の値だけ）
```

巻き戻しの結果は Inspector に記憶する（停止のたびに作り直すか `invalidate()`）。

### 既存の Debugger（debug.py）

- `PyocdAccess(target)`: pyOCD のターゲットを `TargetAccess` に包む（`read_core_register_raw`）。
- `Debugger.backtrace()`: ELF に DWARF の CFI があれば CFI で巻き戻す（`Frame` に `cfa` を足す。既存の `pc`/`where`/`note` は同じ意味）。
  無ければ従来の簡易版。呼び出し元のフレームの `where` は呼び出し命令の行（戻り先 − 1 で引く）。
- 追加: `Debugger.frame_variables(frame)`、`Debugger.evaluate(expression, frame, max_elements)`、`Debugger.write_expression(expression, value, frame)`。
- `read_variable` / `write_variable` は互換のため残す（MCP の `variable_read` の戻り値の形を変えないため）。置き換えは `evaluate` で行える。
- 追加: `Debugger.inspector()`（停止のたびに作る窓口）、`Debugger.debug_info()`（DWARF の索引。一度だけ作る）、`Debugger.type_info(name)`。
- `fprobe.symbols.Symbols.symbol_at(address)`: 番地を含む関数・変数とその先頭からのバイト数（ポインタの値の `"symbol": "sensors+80"`）。

### 実装で決めたこと

- float を d レジスタに置いた変数（`DW_OP_regx d8` で 4 バイト）は下位の s（s16）だけを読む（上位の s が復元できないフレームでも読める）。
- 書き込みはメモリ上の値だけ。整数は型の符号に合わせて範囲を確かめる（`int8_t` に 200 は書かない）。ビットフィールドは読み・変更・書き。
- 位置式・巻き戻しで読めないものは例外にせず `{"kind": "unavailable", "reason": 理由}`・フレームの打ち切りにする（AI が途中まででも使える）。

## 試験

`host/firmware/dwarf_demo/`（zig cc、Cortex-M4 + FPU、`-O0`・`-O1` の DWARF 5、`-O1` の DWARF 4 の 3 つの ELF をリポジトリに置く）。
構造体・共用体（無名を含む）・列挙（負の値）・ビットフィールド（符号付き）・多次元配列・自分を指すポインタ・関数ポインタ・
浮動小数・再帰（深い呼び出し）・関数の中の static・例外ハンドラからの呼び出しを持つ。

- 型と値: ELF の初期値（.data）を `ImageAccess` で読み、C の初期化子と一致することを確かめる。
- 位置式: 演算・断片・ロケーションリストの選択を小さな式と実 ELF の両方で。
- 巻き戻し: 偽のスタックを **CFI とは独立に**、逆アセンブル（capstone。pyOCD が既に依存）で関数の前置き
  （push / vpush / mov r7 / add r7 / sub sp / 引数の str）をなぞって組み立て、関数名・行・引数の値を確かめる。
  例外は 8 語・FP の拡張・詰め物・PSP の各場合をスタックに積んで確かめる。

## 制約・対象外

- C だけ（C++ の名前空間・クラス・テンプレート・参照・継承は対象外。DIE は読めるが式は C の範囲）。
- `entry_value`（呼び出し元の call_site_parameter からの復元）は未対応（「読めない」と出す）。
- 式の演算は `+ -` と単項だけ（比較・掛け算・関数呼び出し・代入は無い）。
- 分割 DWARF（.dwo）・型ユニット（.debug_types）は未対応。
- インライン展開（`DW_TAG_inlined_subroutine`）は別のフレームとして出さない（外側の関数のフレームになる）。その中の変数も出さない。
- 実機では未確認（偽のスタックだけ）。本流で Nucleo + dwarf_demo（SRAM に読み込み）で `walk` の再帰の中・SysTick の中で止めて確かめる。
- メモリの読み出しはその都度（キャッシュは本流の組み込みで、停止ごとのキャッシュを読み手に足すとよい）。

## 本流での API・MCP への組み込みの提案

`fprobe_api.json` に次を足し、デーモンは `Debugger` の同名メソッドを呼ぶ（MCP の道具名は API の生成名に従う）。
停止中だけ有効（動作中は `{"error": "not halted"}` 相当）。

| メソッド | 引数 | 結果 |
|---|---|---|
| `debug.backtrace`（既存を拡張） | `variables: bool = false`, `max_frames: int = 64` | `[{index, pc, function, offset, file, line, cfa, note, arguments?, locals?}]`。`note` は `""` / `"exception"` |
| `debug.frame_variables` | `frame: int = 0`, `max_elements: int = 64` | `{"function", "arguments": [{name, type, value}], "locals": [{name, type, value, line}]}` |
| `debug.evaluate` | `expression: str`, `frame: int = 0`, `max_elements: int = 64` | `{"expression", "type", "address"（メモリ上なら）, "value"}`。`value` は上の表の形 |
| `debug.write_expression` | `expression: str`, `value: int \| float \| str`, `frame: int = 0` | なし（列挙は名前でも書ける） |
| `debug.type_info`（任意） | `name: str`（`struct Sensor` 等） | `{"name", "size", "kind", "members": [{name, type, offset, bit_size?, bit_offset?}]}` |

- 既存の `variable_read` は当面そのまま。AI 向けの説明では `evaluate` を勧め、次の版で `variable_read` を `evaluate` の別名にする。
- MCP の説明文の例: 「`debug_evaluate("head->samples[1]")` で構造体の中の配列を読む。`debug_backtrace(variables=true)` で各フレームの引数とローカル変数」。
- UI（変数・ウォッチ・呼び出し履歴の窓）は、この結果の形（入れ子の dict/list）をそのまま木で表示できる。
