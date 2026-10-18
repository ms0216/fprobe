# F2（一部）: 逆アセンブル・周辺レジスタ（SVD）・メモリの表示 設計

[機能の拡充の全体計画](2026-10-17-feature-expansion-roadmap.md) の並行の第 1 陣、担当 B。
ライブラリ（`host/fprobe`）と単体試験だけを作る。API（`fprobe_api.json`）・MCP・デーモン・画面への組み込みは
本流が行う（最後の節に提案）。

## 1. ゴールと完了条件

| 完了条件 | 検証 |
|---|---|
| Thumb / Thumb-2（Cortex-M）の番地の範囲を命令の列にする。各命令に番地・バイト・ニーモニック・オペランド・分岐先とその名前・PC 相対で読む定数（リテラル）の番地と値 | 単体（sram_demo.elf・disasm_demo.elf） |
| ソースとの混在表示: 各命令にファイル:行・関数名+オフセット、行が変わる所に印 | 単体 |
| 関数単位の逆アセンブル、PC の前後 N 命令（前へ戻るときは確かな命令の境界から揃える） | 単体 |
| リテラルプール（.text の中のデータ）を命令と読まない: ELF のマッピングシンボル（`$t` / `$d` / `$a`）、無ければ PC 相対の読み込みの先と `tbb`/`tbh` の表から推定 | 単体（disasm_demo.elf、マッピングシンボル有り・無しの両方） |
| メモリの読み手は抽象（`read_memory(address, length) -> bytes`）。ELF からも読める | 単体 |
| CMSIS-SVD を読む: 周辺・レジスタ・フィールド（ビット位置・幅・アクセス・列挙値）、derivedFrom、dim（配列・リスト・クラスタ）、既定値の継承・リセット値 | 単体（手書きの小さな SVD） |
| レジスタの値 → フィールドごとの解読（列挙の名前つき）、フィールドの値 → レジスタの値（読み・変更・書き込み） | 単体 |
| 番地 → どの周辺のどのレジスタか | 単体（手書き + STM32F401 の実物） |
| STM32F401 の SVD をリポジトリに置き、出典とライセンスを記録する。デバイス名から SVD を探す | 単体 |
| 16 進ダンプ（幅・単位 8/16/32 ビット・ASCII）と前回との差分 | 単体 |

非目標: ARM（A32）・RISC-V の逆アセンブル（F8。`$a` の区間は A32 として読むだけは用意する）、命令の意味の解析
（データフロー）、SVD の `writeConstraint`・`interrupt` の扱い（読み込むが検査には使わない）、実機での確認（本流）。

## 2. 判断

| 判断 | 理由 | 誤っていた場合のコスト |
|---|---|---|
| 逆アセンブラは capstone（5.x）。`.venv` に pyOCD の依存として既に入っている。`fprobe[debug]` の依存に明記する | Thumb-2・M-class の全命令（IT ブロック・FPU）を自作するのは割に合わない。BSD ライセンス | 低（差し替えは `_Decoder` の 1 か所） |
| モジュールは 3 つ: `memview.py`（読み手の約束・ELF の像・16 進ダンプ・差分）、`disasm.py`、`svd.py`。互いの依存は `disasm`/`svd` → `memview` だけ | 単独で理解・試験できる。読み手の約束は 3 つが共有する | 低 |
| ELF の像（`ElfImage`）は `Symbols` と別に ELF を読む（区画の中身と `$t/$d/$a`）。`Symbols` は変えない | `Symbols` は担当 A が変える。番地の中身は区画（VMA）で引く。`Symbols.segments` は LMA（書き込み用）で、.data などでは実行時の番地と違う | 低（同じ ELF を 2 回開くだけ） |
| 命令の境界の「確かな点」は、関数の先頭（`Symbols.describe` の offset から）、`$t` の区間の始まり。前へ戻る（PC の前 N 命令）ときはその点から PC まで順に読む。確かな点が無い（ELF 無し）か遠すぎる（64 KiB 超）ときは、PC の前の候補の始点から PC にちょうど着地するものを選ぶ推定（「heuristic」と結果に記す） | Thumb は命令長が 2/4 バイトで可変。後ろ向きには読めない。関数の先頭から読めば必ず正しい | 中（推定は誤り得る。結果の `sync` で見分ける） |
| リテラルプールの判定の優先順: (1) マッピングシンボル、(2) 関数の中で、PC 相対の `ldr` の先（4 バイト）・`tbb`/`tbh` の直後の表（直前の `cmp idx, #n` で大きさを知る）を、その後ろで出会ったらデータにする、(3) 解読できないバイトは `.short` のデータ | (1) はコンパイラが付ける正確な印。(2) は ELF 無しの実機のメモリでも大半のリテラルを当てる | 低 |
| データは `.word`（4 バイト揃い・4 バイト入る）・`.short`・`.byte` で、値を付けて出す | 逆アセンブルの一覧の中で値が見えるのが便利（リテラルの値・表） | 低 |
| 命令の結果は不変のデータクラス（`Instruction`）。辞書（API 用）は `as_dict()` | 他のモジュール（`Symbols` の `Location`）と同じ流儀 | 低 |
| SVD の読み込みは自作（`xml.etree.ElementTree`）。pyOCD の `svd` は使わない | pyOCD の読み手は dim・cluster・列挙の扱いが部分的で、内部 API。読み込みは SVD 仕様に沿って 400 行程度 | 中（SVD の方言の取りこぼし。試験に足して直す） |
| SVD は読み込み時に **平らに展開** する（derivedFrom を解決し、dim を名前の付いた個々のレジスタにし、クラスタは `CLUSTER.REG` の点つなぎの名前で、番地は絶対番地） | 引く・番地から探すが単純になる。表示・API も平らな一覧で足りる | 低（巨大 SVD でも数千レジスタ。読み込みは STM32F401 で 1 秒未満） |
| 読むと状態が変わるレジスタ（SVD の `readAction`、例: USART の DR）は `Register.read_action` に持ち、読み手（`read_register`）は既定で読まない（明示の `allow_side_effects=True` が要る） | デバッガが表示のために読んだだけで受信データやフラグが消える事故を防ぐ。AI が一括で周辺を読むときに大事 | 低 |
| レジスタの読み書きは、レジスタの大きさの幅で 1 回のアクセス（`RegisterAccess.read(address, width_bits)`）。バイト単位の `read_memory` は使わない | 周辺はアクセス幅を区別するもの（8 ビットで読むと誤り・バスエラー）がある | 低 |
| 書き込みのフィールドの値は整数か列挙の名前。幅を超える値・書けないフィールド（read-only）は誤り | 誤って隣のフィールドを壊さない | 低 |
| SVD を探す場所: 明示の場所 → 環境変数 `FPROBE_SVD_PATH`（`os.pathsep` 区切りのディレクトリ）→ `~/.fprobe/svd` → 同梱（`fprobe/data/svd`）→ pyOCD の内蔵（`svd_data.zip`、あれば）。各場所でデバイス名に最も長く合うファイルを選ぶ | ユーザーの SVD を優先し、同梱は最小（F401 のみ）。pyOCD の内蔵（Apache-2.0）で他の多くの MCU も引ける | 低 |
| 名前の合わせ方: 大文字にして、ファイル名（拡張子を除く）の小文字の `x` は任意の 1 文字、ファイル名がデバイス名の先頭に合うもの（`STM32F401` は `STM32F401RETx` に合う）。最も長く合うもの | ST の名前の付け方（`STM32F41x.svd`）と、パッケージ名・温度範囲の付いた部品名（`STM32F401RETx`）を両方扱える | 低 |
| 同梱の STM32F401 の SVD は ST の公式（v1.8、Apache-2.0、ファイルの先頭にライセンスの記載）。入手は modm-io/cmsis-svd-stm32（ST の配布物の改行だけ変えた写し）。出典・版・ライセンスを `fprobe/data/svd/README.md` に、Apache-2.0 の全文を同じ場所に置く | ライセンスが明確（ST が Apache-2.0 で配布）。Nucleo F401RE が手持ちのテスト用ターゲット | 低 |

## 3. memview.py（読み手・ELF の像・16 進ダンプ・差分）

```python
class MemoryReader(Protocol):
    def read_memory(self, address: int, length: int) -> bytes: ...     # 読めなければ例外（MemoryReadError など）

class RegisterAccess(Protocol):                                         # 周辺レジスタ用（アクセス幅を守る）
    def read(self, address: int, width: int) -> int: ...                # width はビット（8/16/32）
    def write(self, address: int, width: int, value: int) -> None: ...

class TargetMemory:            # pyOCD の target（read_memory_block8 / read8/16/32 / write..）を両方の約束に合わせる
class BytesMemory:             # base 番地 + bytes（試験・保存したダンプ用）

class ElfImage:                # ELF の割り当てる区画（SHF_ALLOC、NOBITS 以外）を VMA で読む MemoryReader
    path, regions: tuple[Region(address, data, name)], code_map: CodeMap
    symbol(name) / symbol_at(address, kind=None) -> ElfSymbol / name_of(address) -> "sink" | "tick+4"   # 番地 → 名前（変数も）
    read_memory(address, length)   # 範囲が区画の外に出れば MemoryReadError
    contains(address, length=1) -> bool

class CodeMap:                 # $t / $a / $d の区間。kind_at(address) -> "t" | "a" | "d" | None、region_end(address)、last_code_start(address)
```

16 進ダンプ:

```python
hexdump(data: bytes, base: int, *, width=16, unit=8|16|32, ascii=True, previous: bytes | None = None) -> list[DumpRow]
DumpRow(address, values: tuple[int], text: str, changed: tuple[bool])     # changed は単位ごと（previous と比べて）
format_hexdump(rows, unit) -> str      # "20000000: 11111111 22222222 ...  |....""|"。変わった単位は "*" を後ろに付ける
diff(previous: bytes, current: bytes, base: int) -> list[Change(address, before, after)]   # 変わった連続区間
```

`unit` が 16/32 のときは小さい端が先（リトルエンディアン）の値で並べる。`width` は単位の倍数でなければ誤り。

## 4. disasm.py

```python
@dataclass(frozen=True)
class Instruction:
    address: int
    data: bytes                    # 命令のバイト（データなら中身）
    mnemonic: str                  # "ldr" / "bl" / ".word"
    operands: str                  # "r1, [pc, #0x10]"
    kind: str                      # "code" / "data"
    target: int | None = None      # 分岐・呼び出しの先（b / bl / cbz / b<cc>）
    target_name: str | None = None # 先の名前 "tick" / "tick+4"
    literal: int | None = None     # PC 相対で読む番地（ldr rX, [pc, #n]）・adr の番地
    literal_value: int | None = None   # その番地の 4 バイトの値（読めたとき。ldr のみ）
    literal_name: str | None = None    # 値（無ければ番地）の名前（シンボルが分かれば）
    function: str | None = None; offset: int | None = None
    file: str | None = None; line: int | None = None
    new_line: bool = False         # 直前の命令とファイル:行が変わった（混在表示で、ここにソースの行を挟む）
    flow: str = ""                 # "call" / "jump" / "return" / ""（流れの種類。表示の矢印・巻き戻しの補助）
    conditional: bool = False      # 条件付き（b<cc>・cbz/cbnz・IT ブロックの中）
    def as_dict(self) -> dict      # 数値は整数のまま、data は 16 進文字列
    def __str__(self) -> str       # "20000050: 80b5      push {r7, lr}"

@dataclass(frozen=True)
class Listing:
    instructions: tuple[Instruction, ...]
    sync: str                      # "function" / "mapping" / "start" / "heuristic"（どこから命令の境界を揃えたか）
    pc_index: int | None           # around のとき PC の命令の位置（text では "=> " の印）
    def as_dict(self) -> dict
    def text(self, *, source: Callable[[str, int], str | None] | None = None) -> str   # 混在表示（行が変わる所にソース）

class Disassembler:
    def __init__(self, memory: MemoryReader, symbols: Symbols | None = None, image: ElfImage | None = None): ...
        # image: $t/$d と変数名。省略時、symbols があれば同じ ELF から。どちらも無ければ番地だけ（リテラルは推定）
    @classmethod
    def from_elf(cls, path, *, memory: MemoryReader | None = None) -> "Disassembler"   # memory 省略時は ELF の中身
    def range(self, start: int, end: int) -> Listing                   # start は確かな境界とみなす
    def function(self, location: str | int) -> Listing                 # 関数名・番地（含む関数の全体）
    def around(self, pc: int, before: int = 8, after: int = 8) -> Listing   # PC は必ず命令の境界とみなす
```

`around` の結果は PC の前の `before` 命令、PC の命令、PC の後の `after` 命令（合わせて最大 `before + 1 + after`）。

### 読み方

1. 範囲の中身を `memory.read_memory` で一度に読む（`around` は始点〜PC + after × 4 バイト）。
2. 区間の種類を決める: `code_map` があればそれ、無ければすべて Thumb。
3. 先頭から順に: データの区間・推定したリテラル（2.の(2)）の番地なら `.word`/`.short`/`.byte`。そうでなければ capstone で 1 命令。
   解読できなければ 2 バイトの `.short`。
4. 各命令に: 分岐先（capstone の `CS_GRP_JUMP`/`CS_GRP_CALL` の即値オペランド）、PC 相対の読み込み（ベースが PC の
   メモリオペランド: `Align(PC + 4, 4) + disp`）と `adr`、Symbols があれば `describe` で関数・行、名前は
   `関数名+オフセット` か変数名。
5. 流れ: `bl`/`blx` → call、無条件で PC を書く（`b`, `bx`, `pop {.., pc}`, `ldr pc`, `mov pc`）→ jump / return
   （`bx lr`・`pop {pc}` は return）、条件付き（`b<cc>`、`cbz`、IT の中）→ cond。

IT ブロックの条件（`popne` など）は capstone が続けて読むときだけ分かる。そのため 1 命令ずつではなく、コードの区間を
まとめて capstone に渡し、データ（推定したリテラルを含む）に掛かったところで切る。`around` も確かな点から PC の後ろまで
一続きに読む。

### PC の前を揃える

1. 確かな点 = max(含む関数の先頭, PC 以前の最後の `$t` の始まり)。PC - 確かな点 ≤ 64 KiB ならそこから PC まで読み、
   PC にちょうど着地することを確かめる（着地しなければ推定へ）。`sync = "function"` / `"mapping"`。
2. 推定: 始点の候補 PC - 2k（k = 1 … 2 × before + 8）を遠い方から試し、命令とみなして読んで PC にちょうど着地し、
   途中に解読できない命令が無い最初の候補。`sync = "heuristic"`。Thumb は数命令で同期するため、遠い候補ほど誤りにくい。

## 5. svd.py

```python
@dataclass(frozen=True)
class EnumValue: name: str; value: int | None; mask: int; description: str; is_default: bool
    # value の "#1x0" の x は無関係のビット（mask で表す）。is_default は「それ以外」

@dataclass(frozen=True)
class Field:
    name; description; bit_offset; bit_width; access: str      # read-write / read-only / write-only / writeOnce / read-writeOnce
    enums: tuple[EnumValue, ...]   # 読みの列挙（usage が read / read-write）。書きの列挙は write_enums
    write_enums: tuple[EnumValue, ...]
    read_action: str | None; modified_write_values: str | None
    mask (property) / extract(reg) -> int / insert(reg, value) -> int / name_of(value) -> str | None

@dataclass(frozen=True)
class Register:
    name: str                       # "MODER" / "S0CR" / "CH[2].CR"（クラスタは点つなぎ）
    peripheral: str                 # "GPIOA"
    address: int                    # 絶対番地
    size: int                       # ビット
    access; reset_value; reset_mask; description; fields: tuple[Field, ...]; read_action
    path (property) -> "GPIOA.MODER"
    decode(value) -> list[FieldValue]                   # FieldValue(field, value, name)
    encode(current: int, values: dict[str, int | str]) -> int   # 読み・変更・書き込みの「変更」

@dataclass(frozen=True)
class Peripheral: name; base_address; description; group: str | None; derived_from: str | None
    address_blocks: tuple[tuple[int, int], ...]   # (絶対番地, 大きさ)
    registers: tuple[Register, ...]

class Device:
    name; description; cpu: dict; width; peripherals: tuple[Peripheral, ...]
    @classmethod parse(cls, text: str | bytes) / load(cls, path)
    peripheral(name) -> Peripheral
    register(path: str) -> Register                     # "RCC.CR"、"DMA1.S0CR"、大文字小文字を区別しない
    find(address) -> RegisterHit | None                 # RegisterHit(peripheral, register, offset)。同じ番地の別名（TIM の CCMR1_Output/Input）は registers_at で全部
    registers_at(address) -> list[Register]
    peripheral_at(address) -> Peripheral | None         # addressBlock の中（レジスタの無い番地でも）
    read_register(access: RegisterAccess, path, *, allow_side_effects=False) -> RegisterValue   # RegisterValue(register, value, fields)
    write_fields(access, path, values, *, allow_side_effects=False) -> int   # 読み・変更・書き込み。書いた値を返す

def find_svd(device: str, *, paths: Sequence[Path] = ()) -> SvdSource | None   # SvdSource(name, origin, path, member)。origin: explicit / env / user / bundled / pyocd
def load_device(device: str, *, paths=()) -> Device                          # 見つからなければ FileNotFoundError（探した場所を並べる）
```

### 読み込みの規則（CMSIS-SVD 1.3）

- 数値: `0x` / `0X` 16 進、`#` / `0b` 2 進（列挙では `x` が無関係のビット）、10 進。
- 既定の継承: `size` / `access` / `resetValue` / `resetMask` は device → peripheral → cluster → register → field（access）の順に、近い方が勝つ。
- `derivedFrom`: 周辺（名前）、クラスタ・レジスタ（同じ周辺の中の名前、または `PERIPH.REG` の点つなぎ）、
  `enumeratedValues`（名前）。派生側に書いた要素が元を上書きする（XML の要素ごとに、元の要素の写しの上に重ねる）。
- `dim`: `dim`・`dimIncrement`・`dimIndex`（`0-3` / `A,B,C`、省略時 0 … dim-1）。名前の `%s` を置き換える。`[%s]` は配列で
  `NAME[0]` の名前。周辺・クラスタ・レジスタ・フィールドに適用する。
- フィールドの位置: `bitOffset`+`bitWidth`、`lsb`+`msb`、`bitRange` = `[msb:lsb]` のどれか。
- 試験: 手書きの SVD（`host/tests/data/mini.svd`）に上のすべての形を入れる。

### 番地から引く

全レジスタを (番地, 大きさのバイト) で並べ、二分探索。レジスタの途中の番地は `offset` に入る。

## 6. 試験

| 試験 | 内容 |
|---|---|
| `test_memview.py` | ELF の像（区画の読み・外は誤り・$t/$d）、16 進ダンプ（8/16/32・ASCII・差分の印）、diff |
| `test_disasm.py` | sram_demo.elf: `tick` の全命令（capstone と、手で確かめた `push {r7, lr}`、`b.w reached_milestone`）、分岐先の名前、混在表示の行の区切り、`around` が関数の先頭から揃う・推定でも同じ結果。disasm_demo.elf: リテラルプールが `.word 0xdeadbeef` になる（マッピングシンボル有り）、マッピングシンボル無し（`code_map=None`、生のメモリ）でも ldr の先から当てる、`ldr` に値と名前（`sink`）が付く |
| `test_svd.py` | mini.svd: 継承・derivedFrom・dim（配列・リスト）・クラスタ・列挙（`x` の無関係ビット・isDefault）・bitRange、decode/encode、範囲外の値・read-only への書き込みの誤り、`readAction` の読みの拒否。STM32F401: `RCC.CR` = 0x40023800 の `HSEON` = bit 16、`GPIOA.MODER` = 0x40020000、リセット値 0xA8000000、derivedFrom の `GPIOB` の番地、`find(0x40020014)` = `GPIOA.ODR`、`find_svd("STM32F401RETx")` が同梱を選ぶ、名前の `x` の合わせ方 |

試験用の ELF `host/firmware/disasm_demo/disasm_demo.elf` は Cortex-M0+（movw/movt が無いのでリテラルプールができる）で作り、
リポジトリに置く（作り方は `build.py`、sram_demo と同じ zig cc）。

## 7. 本流での API・MCP への組み込みの提案

デーモン側は `Disassembler` と `Device` をセッション（ELF・デバイスを読み込んだ状態）に 1 つずつ持つ。メモリの読み手は
`TargetMemory(target)`（停止中でなくても読めるが、PC の前後は停止中に使う）。ELF を読み込んだら `code_map` は ELF から。

| API（案） | 種類 | 引数 | 結果 |
|---|---|---|---|
| `debug.disasm.range` | read（observes） | `address`, `length`（≤ 4096） | `Listing.as_dict()`: `{"sync", "instructions": [{"address", "bytes", "mnemonic", "operands", "kind", "target", "target_name", "literal", "literal_value", "literal_name", "function", "offset", "file", "line", "new_line", "flow"}]}` |
| `debug.disasm.function` | read | `location`（関数名・番地） | 同上 |
| `debug.disasm.around` | read | `address`（省略時は PC）, `before`=8, `after`=8 | 同上 + `"pc"` |
| `debug.disasm.text` | read | 上のどれか + `source: bool` | `{"text"}`（AI 向けの混在表示の文字列。ソースの行を挟む） |
| `target.svd.load` | control | `device`（部品名）か `path` | `{"name", "origin", "peripherals": 数}`。セッションの ELF・pyOCD のターゲット名から自動でも読む |
| `target.svd.peripherals` | read | `filter`（任意） | `[{"name", "base_address", "group", "description"}]` |
| `target.svd.registers` | read | `peripheral` | `[{"name", "address", "size", "access", "reset_value", "description", "fields": [{"name", "bit_offset", "bit_width", "access", "enums": [{"name", "value"}]}]}]` |
| `target.peripheral.read` | control（observes） | `path`（`"RCC.CR"` か `"GPIOA"` で周辺の全部）, `allow_side_effects`=false | `{"registers": [{"path", "address", "value", "fields": [{"name", "value", "enum", "bits": "[17:16]"}]}], "skipped": [読みに副作用があって読まなかったもの]}` |
| `target.peripheral.write` | control | `path`, `fields`（`{"HSEON": 1, "MODER5": "Output"}`）か `value` | `{"before", "after"}` |
| `target.memory.view` | read（observes） | `address`, `length`, `unit`=8/16/32, `width`=16, `compare: bool`（前回の読みとの差） | `{"rows": [{"address", "values", "text", "changed"}], "names": [{"address", "name"}]}`（番地に SVD のレジスタ・シンボルの名前を付ける） |

MCP の道具は API の生成名（`debug_disasm_around` など）で出す。AI 向けには `debug.disasm.text` と
`target.peripheral.read`（フィールドを解読した結果）が最も役に立つ。

## 8. 制約・未対応

- `Symbols` に行表の番地の一覧（`line_addresses()`）の公開が無いため、行の境界は「確かな点」に使っていない（関数の先頭と
  `$t` で足りる）。担当 A の変更の後で足せる。
- 推定の揃え（ELF 無し）は誤り得る（`sync="heuristic"` で示す）。
- `tbb`/`tbh` の表の大きさは直前の `cmp` から推定する。見つからなければ表を命令として読む（マッピングシンボルがあれば正しい）。
- SVD の `writeConstraint`・`interrupt` は読まない。`alternateRegister` は `Register.alternate_of` に持つだけ（同じ番地の
  別名は `registers_at` で全部出る）。
- 読むと状態が変わるかは SVD の `readAction` だけで判断する。ST の STM32F401 の SVD には `readAction` が無い（USART の DR
  でも付いていない）ため、F401 では守りが効かない。周辺の一括読みを API にするときは、利用者が足せる「読まないレジスタ」
  の一覧（デバイスごと）を本流で検討する。
- SVD は `xml.etree.ElementTree`（expat）で読む。外部実体は読まない。利用者の SVD は信頼する前提（ELF と同じ扱い）。
- フィールドは SVD に書かれた順（ST の SVD は上位のビットから）。
- 読み・変更・書き込みで、触らないフィールドのうち書くと効くもの（`modifiedWriteValues` が oneToClear 等）には効かない値
  （0、zeroTo* なら 1）を書く。書くだけのレジスタはリセット値の上で変える。
- 同梱の SVD は STM32F401 だけ。他は pyOCD の内蔵か、利用者が `~/.fprobe/svd` に置く。
