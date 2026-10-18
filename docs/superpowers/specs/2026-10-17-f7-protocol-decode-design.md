# F7（一部）: 通信の解読（UART・SPI・I2C・SWD）設計

日付: 2026-10-17 / 担当 C（並行の第 1 陣）/ 全体計画: [機能の拡充の全体計画](2026-10-17-feature-expansion-roadmap.md)

## 目的

ロジアナの捕捉（`fprobe.la.Transitions`）から、外付けロジアナのソフト（Saleae Logic 2、sigrok/PulseView）並みに
UART・SPI・I2C・SWD を解読する。結果はどのプロトコルでも同じ形の「注釈」の列で返し、後で F3（共通の時間軸の記録）と
画面に載せる。AI が使うことを考え、JSON にでき、誤りは文字列でなく機械が読める値（`error`）で示す。

範囲はライブラリと単体試験だけ（`host/fprobe/decode/`）。API（`fprobe_api.json`）・MCP・デーモン・画面には触らない
（組み込みは本流。最後の節に提案）。

## 設計の要点

- **変化点の表現のまま走査する。** 毎サンプルに展開しない。解読に使うチャンネルだけを取り出した変化点の列
  （`project`）を 1 回作り、各解読はその上を進む。UART のビットの中央の値は、そのチャンネルの変化点の表を二分探索で引く。
  計算量は変化点の数に比例（UART はフレーム数 × log）で、捕捉の長さ（サンプル数）に依らない。
- **チャンネルの割り当ては引数で受ける。** ボードのピン配置（Primer 20K では 0 = SWCLK、3 = SWDIO、5 = UART RX …）は
  解読に埋め込まない（移植性。既定値は本流の API 側でボードの情報から与える）。
- **時刻は 2 通り。** 注釈はサンプル番号（`start` 含む・`end` 含まない）を持ち、秒（捕捉の先頭が 0）とトリガを 0 とする
  秒は結果（`Decoding`）がサンプル周期とトリガの位置から計算する（`to_dict` / `to_json` で両方を出す）。
- **誤りは注釈の `error` に。** 値は短い識別子（`"parity"`、`"frame"`、`"nack"`、`"fault"`、`"truncated"` …）。
  NACK・WAIT・FAULT・ブレークのような「プロトコル上の出来事」は種類（`kind`）または `fields` にも出す。

## 共通の結果の形（`fprobe/decode/core.py`）

```python
@dataclass(frozen=True, slots=True)
class Annotation:
    start: int                 # サンプル番号（含む）
    end: int                   # サンプル番号（含まない）。start < end
    kind: str                  # 種類（プロトコルごとに下の表）
    text: str                  # 人が読む短い文字列
    value: int | None = None   # 主な値（バイト・語・アドレス・データ）
    error: str | None = None   # 誤りの識別子（無ければ None）
    fields: Mapping[str, object] | None = None   # 構造化した中身（JSON にできる値だけ）

@dataclass(frozen=True)
class Decoding:
    protocol: str              # "uart" / "spi" / "i2c" / "swd"
    annotations: tuple[Annotation, ...]   # start の昇順（同じ start なら外側の注釈が先）
    sample_period_s: float
    trigger: int
    length: int
    params: Mapping[str, object]          # 解読に使った設定（推定したボーレート等を含む）
    def seconds(sample) / since_trigger(sample)
    def of(*kinds) / errors()
    def between(start, end)    # 区間に重なる注釈（二分探索。画面の表示区間の取り出し用）
    def to_dict() / to_json()
```

`to_dict()` の注釈 1 件は `{"start", "end", "start_s", "end_s", "start_rel_s", "end_rel_s", "kind", "text", "value",
"error", "fields"}`。

補助（同じモジュール）:

- `project(tr, channels) -> (starts, values)`: 指定したチャンネルだけの変化点の列。`values` のビット k が `channels[k]`。
  指定外のチャンネルだけが変わった点は落とす（1 回の走査）。
- `ChannelLevels(tr, channel)`: 1 チャンネルの初期値と変化点の表。`level(sample)` は二分探索、`next_edge(sample, level)` は
  「sample 以降で level になる最初の変化点」。

`DECODERS = {"uart": decode_uart, "spi": decode_spi, "i2c": decode_i2c, "swd": decode_swd}` と `decode(protocol, tr, **params)`
で名前から呼べる（API・MCP からの呼び出しを 1 つにするため）。

## UART（`decode_uart`）

```python
decode_uart(tr, channel, *, baud=None, data_bits=8, parity="none", stop_bits=1, invert=False, lsb_first=True)
```

- `parity`: `"none" | "even" | "odd" | "mark" | "space"`、`data_bits`: 5〜9、`stop_bits`: 1 / 1.5 / 2、`invert`: 線の論理を反転。
- **同期**: 休止（1）から 0 への変化点を開始ビットの始まりとする。サンプルの格子上では本当の変化はその直前の
  半サンプルにあるとみなし、ビット i の中央 = 変化点 − 0.5 + (i + 0.5) × ビット長 の位置の値を読む（オーバーサンプリングが
  4 程度でも中央を外さない）。フレームごとに開始ビットで合わせ直すので、送り手のクロックの誤差は 1 フレームの中だけで効く。
- 開始ビットの中央が 1 なら雑音（`glitch` は出さずに捨て、次の変化点へ）。
- 停止ビットの中央が 0: データとパリティがすべて 0 なら **ブレーク**（`kind="break"`、線が 1 に戻るまで）、そうでなければ
  **フレームの誤り**（`error="frame"`）。次の開始ビットは線が 1 に戻ってから探す（誤りの後に自然に同期し直す）。
- パリティの不一致は `error="parity"`。
- 捕捉の終わりでフレームが切れたら、読めたビットまでで `error="truncated"`。
- 捕捉の途中から始まる場合: 最初の 0 への変化点が開始ビットとは限らない（どの解読器でも原理的に区別できない）。
  誤ったフレームは停止ビットの検査でほぼ誤りになり、休止の後に同期し直す。
- **ボーレートの自動推定**（`estimate_baud(tr, channel)`）: 両端の切れた区間を除いた「同じ値が続いた長さ」の列から、
  十分な数（2 個以上かつ 1% 以上）ある最短の長さを 1 ビットの候補とし、その 0.5〜1.5 倍の区間の平均で 1 ビットを決め、
  さらに整数ビットに近い（±0.2 ビット）区間だけをビット数の合計で割って精度を上げる（停止ビット 1.5 やフレームの間の
  半端な休止は除く）。1 ビットの並びが 1 つも無いデータでは倍の長さを 1 ビットと見る（どのソフトでも同じ限界）。標準のボーレート（300〜12 M の表）と 3% 以内なら標準値に寄せる。推定値は `params["baud"]`、
  推定したことは `params["baud_estimated"]`。
- 注釈: `kind="data"`（`value` = データ、`text` = `0x41 'A'`、`fields`: `{"parity": 0|1|None}`）、`kind="break"`。
- 値の参照は二分探索でなく、前にだけ進む変化点のカーソルで行う（10 万バイト・55 万変化点を手元で約 2 秒）。
- 4 倍のオーバーサンプリングでの余裕（停止ビットで）: 変化点の量子化 0.125 + 中央の丸め 0.125 ビット。残りの 0.25 ビットが
  クロックの差と揺れに使える（試験は 1% の差と ±5% の揺れ）。

## SPI（`decode_spi`）

```python
decode_spi(tr, *, sck, mosi=None, miso=None, cs=None, mode=0, msb_first=True, word_bits=8, cs_active_low=True)
```

- `mode` = CPOL × 2 + CPHA。サンプルする辺は (CPOL xor CPHA) = 0 なら立ち上がり、1 なら立ち下がり。データの値は
  その辺の直前（セットアップ側）の値を読む（データは逆の辺で変わるので、オーバーサンプリングが低く同じサンプルで
  データが変わっても誤らない）。このため CPHA = 0 のデータ（後縁で変わる）はどちらの辺の直前でも同じ値に見え、
  CPOL・CPHA の誤りは語の区切りにしか出ない。CPHA = 1 のデータを CPHA = 0 として読むと 1 ビットずれる（実際の機器と同じ）。
- CS あり: CS が有効な間だけ数え、無効になったら語の途中でも区切る（`error="incomplete"`）。CS が有効な区間ごとに
  `kind="transfer"`（`fields`: `{"mosi": [...], "miso": [...]}`）。捕捉の先頭ですでに有効なら `error="truncated"`
  （語の境目が分からない）、終わりで有効なままなら同じく `truncated`。
- CS なし: 最初の辺から word_bits ごとに区切る（語の境目は捕捉の先頭からの数に依る。CS を使うことを勧める）。
- 注釈: 語ごとに `kind="mosi"` / `kind="miso"`（`value` = 語）。語の範囲は最初の辺から最後のサンプルの辺まで。

## I2C（`decode_i2c`）

```python
decode_i2c(tr, *, scl, sda)
```

- SCL が 1 の間の SDA の 1→0 が START（直前に STOP が無ければ `kind="restart"`）、0→1 が STOP。SCL の立ち上がりで SDA を読む
  （同じサンプルで SDA も変わったときは変わった後の値）。辺だけを見るので、クロックの引き伸ばし（SCL を 0 に保つ）に依らない。
- START の後の最初のバイトがアドレス: 上位 5 ビットが `11110` なら 10 ビットのアドレス（書き込みは次のバイトで下位 8 ビット。
  読みは同じ取引で直前に書いた 10 ビットのアドレスを使う）。
- 9 番目のビットが ACK（0）/ NACK（1）。注釈 `kind="ack"` / `kind="nack"`。アドレスか書きのデータが NACK なら取引に
  `error="nack"`（読みのデータの NACK は主が終わりを示す正常な合図なので誤りにしない）。
- Sr と STOP の直前には必ず SCL の立ち上がりが 1 回ある（条件の一部）。途中のバイトを数えるときはこの 1 ビットを除く。
- 最初の START より前のビットは捨てる（捕捉の途中から始まる）。バイトの途中の START/STOP は `error="incomplete"`。
- 取引の区間（`transfer`）は START か Sr から、次の Sr か STOP まで（Sr ごとに区切る。アドレスと向きが 1 つに決まる）。
- SCL の立ち下がりと SDA の立ち下がりが同じサンプルに落ちたとき（オーバーサンプリングが低い）は、休止中なら START とみなす。
  STOP の無いまま捕捉が終われば取引に `error="truncated"`。
- 注釈: `start`、`restart`、`stop`、`address`（`value` = アドレス、`fields`: `{"rw": "R"|"W", "ten_bit": bool, "ack": bool}`）、
  `data`（`fields`: `{"rw", "ack"}`）、`ack`、`nack`、取引全体の `transfer`（START から STOP まで、`fields`: `{"address", "rw",
  "data": [...], "ack": bool}`）。

## SWD（`decode_swd`）

```python
decode_swd(tr, *, swclk, swdio, drive=None, turnaround=1, orundetect=False)
```

- **ビットの取り出し**: SWCLK の立ち上がりごとに、その直前の SWDIO を 1 ビットとする（ホストは立ち下がりで変え、ターゲットは
  立ち上がりの後に変えるため、両方向とも立ち上がりの直前が安定している。CMSIS-DAP の参照実装の読みと同じ）。
  `drive`（プローブが SWDIO を駆動中、Primer では ch8）を与えると、各ビットの駆動側も読む。
- **並びの認識**（ビット列の上で）:
  - 1 が 50 個以上: `line_reset`。
  - 1 が 50 個以上の後に 16 ビットの `0xE79E` → `jtag_to_swd`、`0xE73C` → `swd_to_jtag`、`0xE3BC` → `swd_to_dormant`。
  - 1 が 8 個以上の後に 128 ビットの選択の合図（`0x19BC0EA2_E3DDAFE9_86852D95_6209F392`、LSB から）→ `dormant_alert`。
    続く 4 ビットの 0 と 8 ビットの起動の符号（`0x1A` = SWD、`0x0A` = JTAG）→ `activation`。
  - 0 は休止（注釈にしない）。
- **パケット**: 開始 1・APnDP・RnW・A[2]・A[3]・パリティ（4 ビットの偶数パリティ）・停止 0・パーク 1。
  合わなければ 1 ビット進めて探し直す（再同期）。続いて ターンアラウンド（`turnaround` ビット）・ACK 3 ビット
  （LSB から OK = 0b001、WAIT = 0b010、FAULT = 0b100）。
  - OK の読み: データ 32 + パリティ、ターンアラウンド。OK の書き: ターンアラウンド、データ 32 + パリティ。
  - WAIT / FAULT: ターンアラウンドだけ（オーバーラン検出 `ORUNDETECT` が有効ならデータの相も続く）。
  - それ以外の ACK（0b111 = 応答なし: `error="no_ack"`、その他: `"protocol"`）。ターンアラウンドの後、次の正しい要求を探す。
  - パリティの誤った要求は、続く ACK の相がプルアップの 1（ターゲットが応答しない）ときだけ要求と認める
    （データのビットを探し直すときの誤検出を防ぐ）。
  - DP の 0xC への書き込みは TARGETSEL（マルチドロップ）。ターゲットは ACK を返さないので ACK の 3 ビットは見ずにデータの相へ。
- **状態の追跡**（書き込みを解読して、後のパケットの解釈に使う）:
  - SELECT（DP 0x8 の書き込み）: APSEL [31:24]・APBANKSEL [7:4]・DPBANKSEL [3:0]（ADIv5）。AP のレジスタ名と DP のバンクに使う。
  - CTRL/STAT（DP 0x4、バンク 0）の書き込みの ORUNDETECT（ビット 0）。引数 `orundetect` は初期値。
  - DLCR（DP 0x4、バンク 1）の書き込みの TURNROUND（ビット [9:8]、ターンアラウンド = 値 + 1）。引数 `turnaround` は初期値。
  - **AP の読みの先送り**: AP の読みが返すデータは前の AP の読みの結果（RDBUFF の読みは最後の AP の読みの結果）。
    `fields["value_of"]` に、そのデータがどのレジスタの値かを書く（例 `"AP0 DRW"`）。
  - ライン リセットで SELECT 等は追跡を続けるが、先送りの読みは捨てる。
- **レジスタ名**: DP — 0x0 DPIDR（読み）/ ABORT（書き）、0x4 CTRL/STAT・DLCR・TARGETID・DLPIDR・EVENTSTAT（DPBANKSEL 0〜4）、
  0x8 RESEND（読み）/ SELECT（書き）、0xC RDBUFF（読み）/ TARGETSEL（書き）。
  AP（MEM-AP、APBANKSEL × 16 + A）— 0x00 CSW、0x04 TAR、0x08 TAR_HI、0x0C DRW、0x10〜0x1C BD0〜BD3、0x20 MBT、0xF0 BASE_HI、
  0xF4 CFG、0xF8 BASE、0xFC IDR。それ以外は `AP 0x..`。
- **誤り**: 要求のパリティ（`request_parity`。要求として受け付けない）、データのパリティ（`parity`）、ACK の WAIT / FAULT
  （`error="wait"` / `"fault"`）、応答なし（`no_ack`）、捕捉の途中で切れた（`truncated`）、`drive` と相の不一致
  （ホストの相でプローブが駆動していない・ターゲットの相で駆動している: `direction`）。
- 注釈: パケット全体の `transfer`（`value` = データ、`fields`: `{"port": "DP"|"AP", "rnw": bool, "addr", "reg", "apsel",
  "ack": "OK"|"WAIT"|"FAULT"|"NONE"|"..."、"data", "value_of"}`）と、その中の `request`・`ack`・`data`。
  並びは `line_reset`・`jtag_to_swd`・`swd_to_jtag`・`swd_to_dormant`・`dormant_alert`・`activation`。
- ビットの取り出しは変化点の列を 1 回走査する（1 万パケット・約 100 万変化点を手元で約 0.8 秒）。
- オーバーサンプリング: 半周期 1 サンプル（SWCLK の 2 倍）は揺れが無いときだけ読める（辺が同じサンプルに重なると
  クロックの辺そのものが消える）。2.5 倍以上なら揺れにも耐える。
- 対応しないもの（今は）: ADIv6 の SELECT（アドレスの形）、JTAG-DP、MEM-AP 以外の AP のレジスタ名。

## 試験用の波形の発生器（`fprobe/decode/synth.py`）

`Wave(channels, sample_rate_hz, idle, jitter, seed)` に、時刻（秒、浮動小数）とチャンネルと値で変化を積み、`build()` で `Transitions` を作る
（変化はその時刻以降の最初のサンプルに置く。同じサンプルの変化は後のものが勝つ）。各プロトコルの発生器は
`Wave` と開始時刻を受け、終わりの時刻を返す（組み合わせられる: 2 本の UART を同時に、SPI と CS、SWD と ch8 等）。
クロックの揺れ（`jitter`、乱数の種を指定）、誤りの注入（パリティ・停止ビット・NACK・ACK の値・データのパリティ）を引数で選べる。
発生器は `uart`・`uart_break`・`spi`・`i2c`・`SwdBus`（`host`/`target`/`undriven` の 1 サイクル単位と、`transfer`・
`line_reset`・`jtag_to_swd`・`dormant_to_swd` 等）。ライブラリに置く（デモや API の試験にも使える）。

## 試験（`host/tests/test_decode_*.py`）

- 共通: `project`・`ChannelLevels`、`Decoding.to_json` の往復、秒とトリガ基準の時刻。
- 各プロトコル: 発生器 → 解読の往復（全設定の組み合わせ）、誤りの検出、捕捉の途中から始まる・途中で終わる、クロックの揺れ、
  低いオーバーサンプリング（UART 4 倍・SPI/I2C/SWD はクロックの半周期 1〜2 サンプル）。
- 速さ: 10 万バイトの UART（55 万変化点）、1 万パケットの SWD（約 100 万変化点）をそれぞれ 5 秒以内（手元で約 2 秒・0.8 秒）。
- 実機の捕捉の記録はリポジトリに無い（VCD 等は保存されていない）ため、SWD は発生器の波形で確かめる。
  本流の組み込みのときに、実機の捕捉（Primer の ch0/ch3/ch8）で確かめることを提案する。

## 本流での API・MCP への組み込みの提案

1. **API の操作 1 つ**: `la.decode`（引数: `protocol`、プロトコルごとの設定、`capture_id`（省略時は最後の捕捉）、
   任意で `start`/`end`（サンプル）と `kinds`・`errors_only`・`limit` の絞り込み）。中身は
   `fprobe.decode.decode(protocol, transitions, **params).to_dict()`。`DECODERS` の引数の表から JSON スキーマを作れる。
2. **チャンネルの既定値はボードの情報から**: Primer 20K なら SWD は `swclk=0, swdio=3, drive=8`、UART は ch5（ターゲットの TX）
   と ch7（RX）。ボードの記述（`fprobe_gw/boards/primer20k.py` か、デバイスが申告するケイパビリティ）に「ロジアナのチャンネル名」を
   持たせ、API はそこから既定値を引く（解読側には埋め込まない）。
3. **MCP**: 道具 `la_decode`。AI 向けに既定で `errors_only` の要約（誤りの数・最初の数件）と件数を返し、全件は `limit`/`offset` で。
   SWD は `transfer` だけを返す既定にする（`request`/`ack`/`data` は画面用）。
4. **F3 の時間軸**: `Annotation` をそのまま記録の出来事にできる（`start_s` とトリガ時のタイムベース `trigger_time` で
   共通の時刻に直す）。SWD の `transfer` を「プローブが自分で出した DAP のコマンド」の記録と突き合わせると、
   ターゲットの応答（WAIT・FAULT）がどのコマンドのものか分かる。
5. **画面**: `Waveform` の表示の区間に重なる注釈は `Decoding.between(start, end)`（二分探索、実装済み）で取り出す。
   解読の結果を捕捉と一緒に保持し（`Probe._keep_capture` と同じ考え方）、`la_view` の返り値に区間内の注釈を足す。
6. **ストリーミング**（後）: 解読器は今は 1 つの `Transitions` を受ける。連続ストリーミング（`LaStream`）に載せるには、
   状態を持ち越す逐次版が要る（UART・I2C は状態が小さいので容易）。
