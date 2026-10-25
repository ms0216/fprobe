# 不具合台帳: DWARF・C の式・値の整形・シンボル・SVD（検出専用。修正は未適用）

調べた日: 2026-10-25。担当範囲: `host/fprobe/dwarf/`（typesys・values・cexpr・location・info・unwind・inspector・access）、
`host/fprobe/symbols.py`、`host/fprobe/svd.py`（`target_peripheral_read` のフィールド分解）。
再現試験は `host/tests/test_dwarf_bugs.py` と `host/tests/test_symbols_bugs.py`（strict xfail。直すと XPASS で失敗するので
マーカーを外して通常の試験にする）。最小 ELF を作る補助は `host/tests/minimal_elf.py`。

分類: A = 静かに誤った結果を返す / B = 失敗が見える / C = 端の条件・まれ / D = 品質。

件数: A 7 / B 2 / C 7 / D 1（疑い 8 件は別表）。

## 確認済み

### BUG-SYM-01 (A) 行表の同じ番地の複数行を、実効の行（最後）でなく行番号の大きいほうに倒す
- 試験: `test_symbols_bugs.py::test_line_entry_uses_the_last_row_at_an_address`
- 原因: `symbols.py` `_read_lines` の `rows.sort()`（約 L240）。タプル (番地, ファイル名, 行, 場所) 全体で並べ替えるので、
  同じ番地の行はプログラムの出現順でなく (ファイル名, 行番号) 順になる。`line_entry`・`describe` は bisect_right で「最後」を
  取るので、DWARF の状態機械で最後に出た行（実効の行）でなく最大の行番号（別ファイルなら名前の最後）を返す。
- 実例: `threadx_shape.elf` 0x20000074 は行表の順が 277, 278, 277（実効は 277）だが 278 を返す。`nuttx_shape.elf` 0x20000016
  （148,149,148 → 149）、`rtx5_shape.elf` 0x20000028（…168,169,168 → 169）、`freertos_demo.elf` 0x2000133e（3832, 2358 → 3832、
  ファイルをまたぐ）でも誤り。
- 直し方の案: 並べ替えは番地だけをキーにする（`rows.sort(key=lambda r: r[0])`、安定ソートで出現順を保つ）。CU をまたぐ同じ番地は
  CU の順で決める。
- 影響: バックトレース・停止位置・ステップの「ファイル:行」が、ループ末尾など同じ番地に複数行が並ぶ場所で隣の行を指す。
- 確信度: 高（実 ELF で再現）。

### BUG-SYM-02 (A) 大きさ 0 の別名（弱いエイリアス）が同じ番地にあると function_at / describe が本体の中で None
- 試験: `test_symbols_bugs.py::test_function_at_with_a_zero_size_alias_at_the_same_start`
- 原因: `symbols.py` `__init__` の `functions = sorted(..., key=lambda s: s.address)`（L120 付近）。同じ番地の関数は
  シンボル表の順のまま並び、`function_at`・`describe`・`_function_end_at` は bisect_right の 1 つ前だけを見る。最後が
  size 0 の別名（`.thumb_set X, Default_Handler` の IRQ ハンドラの別名は size 0 が普通）だと、本体の先頭以外の番地が
  どの関数にも当たらない。`_index_all` は `(address, -size)` で並べて symbol_at は正しいので、同じ ELF で
  symbol_at と function_at が食い違う。
- 直し方の案: `_functions` も `(address, -size)` で並べ、同じ番地で最大の大きさを先にする（bisect は最後を取るので逆順に注意）。
  または function_at を symbol_at と同じ「届く範囲を戻りながら探す」にする。
- 影響: ISR 共通の Default_Handler などの中で停止すると関数名・行が出ない。`line_entry` は `function is None` で
  `_functions` が空でないため None を返し、行も失う。after_prologue も別名の大きさを使う。
- 確信度: 高（最小 ELF で再現。実機の startup が size 0 の別名を作るかは未確認）。

### BUG-SYM-03 (A) 同じ名前の static 関数・変数の 2 つ目以降を黙って捨てる
- 試験: `test_symbols_bugs.py::test_same_named_local_symbols_are_not_silently_dropped`
- 原因: `symbols.py` `_read_symbols` の `out.setdefault(s.name, ...)`（L158）。シンボル名をキーの辞書にするので、別ファイルの
  `static void init()` の 2 つ目は索引（symbol_at・function_at・describe・symbols.names）から消える。`symbol("init")` も
  先に出たほうを曖昧とは言わずに返す（DebugInfo の変数は AmbiguousNameError を出すのと対照的）。
- 直し方の案: 辞書でなく (名前, 番地) の一覧を持ち、symbol_at 系はすべてを索引する。`symbol(名前)` は複数なら候補を挙げた誤り。
- 影響: 2 つ目の `init` の中で停止しても関数名が出ない。`break init` が静かに 1 つ目に付く。
- 確信度: 高。

### BUG-DWARF-01 (A) 先頭 0 の整数リテラルを 10 進で読む
- 試験: `test_dwarf_bugs.py::test_leading_zero_literal_is_octal`
- 原因: `cexpr.py` `_tokens`（L87 付近）の `int(n, 10)`。C では `010` は 8 進の 8。
- 直し方の案: 先頭 0 で後ろに数字が続けば 8 進（`0` 単独は 0）。または 10 進以外と紛らわしいので誤りにする。
- 影響: 条件付きブレークポイント・式で `0644` や `010` を書くと値が違うまま通る。
- 確信度: 高。

### BUG-DWARF-02 (A) unsigned の折り返しを見ない（コード中の ponytail 注記どおりだが、静かに誤る）
- 試験: `test_dwarf_bugs.py::test_unsigned_arithmetic_wraps_like_c`（`0u - 1`・`-1 < 0u`・`~0u`・`(unsigned)0 - 1`）
- 原因: `cexpr.py` `apply`・`add`・`arith`。数学の整数で計算して `constant()` が値から型を決め直す（`0u - 1` は int の -1、
  `-1 < 0u` は int どうしの比較で真、`~0u` は -1）。C の通常の算術変換（int と unsigned は unsigned へ）を行わない。
- 直し方の案: 二項演算の型を C の算術変換で決め、結果をその型の幅で折り返す。それをしないなら、符号なしを含む演算は誤りにする
  （静かに別の値を返さない）。
- 影響: `counter - last > 100`（unsigned のカウンタが折り返した直後）、`x < 0u` 等の条件が C と逆に評価される。条件付き
  ブレークポイントが成立すべき所で成立しない。
- 確信度: 高（挙動は確認済み。設計上の割り切りかどうかは利用者判断）。

### BUG-DWARF-07 (A) DW_AT_data_member_location が式（DWARF 2/3 の block）だと静かに 0
- 試験: `test_dwarf_bugs.py::test_member_location_given_as_plus_uconst_expression`
- 原因: `typesys.py` `_member`（L284 付近）: `offset = location.value if ... isinstance(location.value, int) else 0`。
  `-gdwarf-2/3` の GCC は `DW_OP_plus_uconst N` の block で出すので 0 になり、全メンバーが先頭に重なる。
- 直し方の案: block なら先頭が DW_OP_plus_uconst の ULEB を読む。解けない式は 0 にせず「位置が分からない」にする
  （ビットフィールドの `bit_offset=None` と同じ扱い）。
- 影響: 古いツールチェーン・DWARF 2/3 の ELF で構造体のメンバーがすべて先頭の値に見える（エラーなし）。
- 確信度: 中（合成 DIE で再現。実 ELF の DWARF 2/3 は手元に無い）。

### BUG-DWARF-08 (A) 位置式の値を 32 ビットに切る（64 ビット整数の定数・stack_value）
- 試験: `test_64_bit_stack_value_is_kept`・`test_64_bit_signed_stack_value_is_sign_extended`
- 原因: `location.py` L156 `args[0] & MASK`（DW_OP_constu / const8u / const8s）、`DW_OP_stack_value`
  `(stack.pop() & MASK).to_bytes(4, ...)`、`read_place(ValuePlace)` が 0 で埋める。`uint64_t x = 0x123456789` が最適化で
  定数になる（DW_OP_constu …; DW_OP_stack_value）と上位 32 ビットが消え、`int64_t = -1` は 0x00000000FFFFFFFF になる。
- 直し方の案: スタックは 64 ビット（または Python の整数のまま）で持ち、stack_value は値の大きさ（型の大きさ）まで符号を考えて
  バイト列にする。アドレスの演算だけ 32 ビットに折り返す。
- 影響: -O1 以上でレジスタ・定数に最適化された 64 ビット変数（`uint64_t` のタイムスタンプ等）の値が、エラーなしで違う値になる。
- 確信度: 高（挙動は再現。実 ELF で定数化された 64 ビット変数があるかは未確認）。

## B（失敗が見える）

### BUG-DWARF-03 (B) `1 << -1` が ValueError で漏れる
- 試験: `test_negative_shift_count_is_an_expression_error`
- 原因: `cexpr.py` `apply` L206 の `a << b` / `a >> b`。負のシフト量は ValueError、巨大なシフト量（`1 << 99999999999`）は
  MemoryError / 長い計算（C ではどちらも未定義）。式の誤りとして ExpressionError にしていないので、API の呼び出し側が
  想定外の例外を受ける。
- 直し方の案: 0 ≤ b < 結果の型の幅（64）でなければ ExpressionError。
- 確信度: 高。

### BUG-DWARF-04 (B) `(float)1e40` が OverflowError
- 試験: `test_float_cast_of_too_large_value_is_inf`
- 原因: `cexpr.py` `rvalue` L396 の `struct.pack("<f", float(number))`（範囲外で OverflowError）。C のキャストは inf。
  `scalar()` の float 経路も大きさが 4・8 以外だと `struct.unpack` が `struct.error`（未捕捉）。
- 直し方の案: 4 バイトに収まらなければ ±inf にして詰める。4・8 以外の大きさは ExpressionError。
- 確信度: 高。

## C（端の条件・まれ）

### BUG-DWARF-05 (C) 4・8 以外の大きさの浮動小数の encode が 8 バイトを返す
- 試験: `test_encode_float_of_other_size_has_that_size_or_refuses`
- 原因: `values.py` `encode` L218 `"<f" if s.size == 4 else "<d"`。decode 側は 4・8 以外を bytes にして扱わないのに、
  encode は 2・12・16 バイトの型へ 8 バイトを返す（書き込み側が大きさの違うデータを書く）。
- 直し方の案: 4・8 以外は TypeError。
- 確信度: 高。

### BUG-DWARF-06 (C) float の encode が decode の出力と往復しない／範囲外が OverflowError
- 試験: `test_encode_float_round_trips_decode_output`・`test_encode_out_of_range_float_is_a_value_error`
- 原因: `values.py` `_base` は NaN・無限大を文字列 "nan"/"inf"/"-inf" で返すが、`encode` L214 は数以外を TypeError。
  UI/AI が読んだ値をそのまま書き戻せない。float(4 バイト)に 1e40 を渡すと OverflowError（TypeError/ValueError ではない）。
- 直し方の案: encode が "nan"/"inf"/"-inf" を受ける。範囲外は ValueError か inf（方針を決める）。
- 確信度: 高。

### BUG-DWARF-09 (C) DW_AT_const_value が data1 で型の符号の範囲外だと OverflowError
- 試験: `test_const_value_data1_for_signed_type_is_reinterpreted`
- 原因: `info.py` `_const_bytes` L46: `signed` かつ `value >= 0` で `to_bytes(size, signed=True)`。pyelftools は DW_FORM_data1 を
  符号なしで返すので 255（= int8 の -1）が入らない。列挙子の値（typesys `_simple`）も同じ符号の読み違いがあり得る
  （data1 で負の列挙子を持つ列挙の値が 254 のまま）。
- 直し方の案: dataN の形式は型の幅で符号を解釈し直す。
- 確信度: 中（GCC・clang は負の値に sdata を使うので実害はまれ）。

### BUG-DWARF-10 (C) DWARF に無い `char` を常に signed で補う
- 試験: `test_synthetic_char_is_unsigned_on_arm`
- 原因: `cexpr.py` `_base_type` L333 付近。大きさは ABI 共通だが char の符号は ABI で違う（ARM は unsigned、x86 は signed）。
  `(char)200` が -56 になる（ELF が `char` 型を使っていないときだけ）。
- 直し方の案: e_machine から符号を決める（ARM/AArch64/PPC は unsigned）か、DWARF に無ければ plain `char` の式は誤りにする。
- 確信度: 中。

### BUG-DWARF-11 (C) `uint8_t *` は文字列として読み、`uint8_t[]` は読まない
- 試験: `test_uint8_pointer_and_array_are_treated_alike`
- 原因: `values.py` `_pointer` は `strip(t.target)` を `is_char` に渡す（typedef を外すので uint8_t も文字扱い）。`_array` は
  `is_char(element)` で typedef を外さない（docstring は「uint8_t は数として扱う」）。経路で扱いが違う。
  `uint8_t *buf`（バイナリのバッファ）のポインタを読むと、メモリを 32 バイトずつ読み文字列のように出す（余計な読み・
  出力が backslash エスケープの混ざった文字列になる）。
- 直し方の案: 方針（typedef の uint8_t を文字扱いにするか）を決めて両方で同じにする。
- 確信度: 中（どちらが意図かは利用者判断）。

### BUG-DWARF-12 (C) 場所の番地を 32 ビットに切る（64 ビット ELF）
- 試験: `test_memory_place_beyond_32_bits_is_not_wrapped`
- 原因: `location.py` `ADDRESS_BITS = 32`、`_memory` の `address & MASK`、`cexpr.py` `index`/`offset_place` の
  `& 0xFFFFFFFF`。`DebugInfo.address_size` は ELF クラスから 8 を取り得る（試験はそれを想定）のに、評価は 32 ビット固定。
  `0x1_0000_0000` が 0 番地を読む。
- 直し方の案: ADDRESS_BITS を ELF のアドレス幅から取る（EvalContext に持たせる）。
- 確信度: 中（現行の対象は 32 ビットのみ。RISC-V 64 を足すなら必須）。

### BUG-DWARF-13 (C) 表に無いレジスタ番号の KeyError が巻き戻しを落とす
- 試験: `test_unknown_register_number_is_not_available_not_a_crash`・`test_register_number_of_empty_name_is_key_error`
- 原因: `unwind.py` `RegisterFile.read` が `register_name(number)` を呼ぶ（`access.py` は 16〜63・96〜255 で KeyError）。
  `try_read`（`_restore` の SAME_VALUE・フレーム 0 で呼ぶ）は NotAvailable しか捕まえないので、CFI が Armv8.1-M の
  PAC の RA_AUTH_CODE（143）等に same_value を書くとバックトレース全体が例外で落ちる。`register_number("")` は IndexError。
- 直し方の案: `read` で KeyError を NotAvailable に直す。`register_number` は空名を KeyError。
- 確信度: 中（PAC/BTI でビルドした ELF の CFI が未確認）。

## D

### BUG-DWARF-14 (D) 最外のフレームの LR 未定義（DW_CFA_undefined）が "not_available" で止まる
- 試験: なし（精読のみ）。`unwind.py` `_restore` が UNDEFINED を `lost` に入れ、`restored.read(LR)` が NotAvailable になり、
  stop が `{"reason": "not_available", "detail": ...}` になる。CFI が「ここが最外」と宣言した事実（正常な終端）を
  読めない失敗と区別できない。stop に専用の理由（例 "outermost"）を足す案。確信度: 中。

## 疑い（再現試験なし）

1. `location.py` `_run` の `DW_OP_skip` / `DW_OP_bra` に後方の分岐の上限が無い。後方への skip が自分に戻る壊れた式で無限ループ
   （壊れた入力・敵対的な ELF）。
2. `info.py` `type_named`: `_type_dies.setdefault(prefix + name, die)` で同名の型は最初の CU の物を使う。2 つの CU で
   同じ名前の `struct ctx` が違う中身なら、式の `(struct ctx *)p` が違う配置を使う（静かに誤り）。
3. `info.py` `scope_at`/`Scope.lookup`: 同じ名前のローカルが「内側のブロック」と「その後ろに宣言された外側」にあると、
   DIE の順で `reversed(locals)` が外側を選ぶ可能性（DIE の順が宣言順と一致しない処理系）。
4. `typesys.py` `_subrange_length`: `DW_AT_upper_bound` が data4 の 0xFFFFFFFF（GCC のゼロ長配列）だと長さ 4294967296 になる
   （pyelftools が符号なしで返す）。decode が巨大な範囲を読もうとする恐れ。
5. `access.py` `ImageAccess.from_symbols` は PT_LOAD の `p_paddr`（LMA）で区画を作る。フラッシュに置いた `.data` の初期値を、
   実行時の RAM の番地（VMA）で読めない（読むとエラー。ELF だけで大域変数の初期値を見る用途が、フラッシュ→RAM コピーの
   ELF では空振りする）。
6. `svd.py` `Device.__init__`: `_registers` / `_peripherals` が大文字化した名前の辞書なので、大文字小文字だけが違う 2 つの
   レジスタ（または周辺）が 1 つに潰れる。フィールドがレジスタの幅を超える定義（`mini.svd` の `UART.SR.COUNT` [23:8]・16 ビット）
   を読み込み時に検出しない（decode は範囲外のビットを 0 として出す）。
7. `symbols.py` `_line_address`: 複数の範囲を持つ行（ループ・インライン展開）は最小番地を返すだけ（関数の前置きの手前の
   番地になり得る）。
8. `cexpr.py` 式の言語に `sizeof`・三項演算・キャスト以外の代入が無い（誤りとして通知されるので不具合ではなく機能の不足）。

## 調べた範囲 / 調べていない範囲

調べた: typesys（型の読み込み・型名）、values（decode / decode_bits / encode）、cexpr（字句・二項/単項・キャスト・ポインタ算術・
添字・短絡・暗黙の型）、location（スタック機械・場所の読み出し）、info（索引・スコープ・変数の引き方・const 値）、
unwind（CFI・例外フレーム・EXC_RETURN）、inspector（書き込み・ビットフィールド）、symbols（シンボル表・行表）、svd（解析・
フィールドの抽出と組み立て・同梱の STM32F401.svd と mini.svd で往復と番地の逆引きの整合）。実 ELF（`host/firmware/*`）で
dwarf_demo の式 約 60 件、全 ELF の行表と line_entry の突き合わせ。

調べていない: 実機・HIL、C++/Rust の DWARF（cxx_demo）の名前修飾、location リストの境界（DWARF 5 の loclists の各種エントリ）、
unwind の例外フレームの動的試験（精読のみ）、SVD の dim/derivedFrom の特殊な組み合わせ（STM32F401 と mini.svd 以外の SVD）、
`target_peripheral_read` の書き込み経路（peripheral_write の副作用判定）の動的試験。

## 修正済み（2026-10-25）

- BUG-SYM-01: 行表の並べ替えを番地だけのキーにした（安定。同じ番地は出現順 = 最後が実効の行）。
- BUG-SYM-02: 関数の一覧を (番地, 大きさ) 順にし、同じ番地では大きいほうが後ろ（bisect が取る側）になるようにした。
- BUG-SYM-03: 同じ名前で別の番地・大きさのシンボルを捨てずに索引へ入れた（symbol_at・function_at・describe・names）。
  意味の変更: `symbol(名前)` は、同じ名前が複数あれば先頭を黙って返さず、番地を挙げた KeyError にする（DebugInfo の曖昧さと同じ扱い）。
- BUG-DWARF-01: 先頭 0 の整数は 8 進（`08` は式の誤り）。
- BUG-DWARF-02: 整数どうしの二項演算・単項 `-` `~` は C の整数拡張と通常の算術変換（同じ大きさで片方 unsigned なら
  unsigned、大きいほうの型が勝つ）で型を決め、その幅で折り返す。リテラルの `u` 接尾辞を unsigned として扱う。
  意味の変更: `0x7fffffff + 1` などが long long へ広がらず int の幅で折り返す（C と同じ）。比較は変換後の値で行う。
- BUG-DWARF-03: シフト量が 0〜(型の幅-1) の外なら ExpressionError。
- BUG-DWARF-04: float へのキャストが範囲外なら ±inf。4・8 以外の浮動小数の大きさは ExpressionError。
- BUG-DWARF-08/08b: DW_OP_const* / constu / consts と stack_value は 64 ビットまで保つ（32 ビットに収まる値は従来どおり 4 バイト）。
  番地の演算（plus 等）は従来どおり 32 ビットに折り返す。
- 未修正: BUG-DWARF-05〜07、09〜13 と疑い（時間の都合）。
