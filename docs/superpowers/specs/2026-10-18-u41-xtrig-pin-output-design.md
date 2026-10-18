# U41（一部）: クロストリガの動作「出力の端子を動かす」設計

- 日付: 2026-10-18
- 親: `2026-10-16-m9-cross-trigger-design.md`（源と動作の表）、台帳 U41（試験用 I/O の API）・B22（FPGA の資源）
- 方針（利用者と決めたこと）:
  - 既存のクロストリガ（源 = 条件、動作の表）を広げる。新しい読み取り回路は作らない（ロジアナの入力の端子をそのまま見る）。
  - FPGA と本体の API には汎用の機能だけを入れる。動作は「有効にする・無効にする」と呼ぶ（「押す・離す」は使わない）。
  - 特定の機器（キーボード等）の処理は PC 側の取り外せる層（プラグイン）にし、本体からは読み込まない。

## 1. ゴールと完了条件

| 完了条件 | 測り方 |
|---|---|
| 源（ロジアナの入力の端子の並び・既存の 4 源）で、J12 の決めた端子を、脈・保持・条件の間の 3 つの形で動かせる | cocotb（`xtrig_out` 単体・fprobe_core 経由）、HIL |
| 「条件の間」の遅れがクロックの数周期以内（入力 → 出力 4 周期 = 40 ns @ 100 MHz） | cocotb で周期を数える。HIL でロジアナに入力と出力を並べて測る |
| 出力に使う端子を「その機能（UART・SWO）に使う / 出力に使う」で CSR から選べる。出力に使うとき、その機能の入力は休止の値（1）を見て、出力は駆動しない | cocotb（fprobe_core 経由） |
| 動いていないときは高インピーダンス（駆動しない） | cocotb（oe = 0）、HIL（ターゲットのプルダウンで Low に落ちる） |
| ケイパビリティで出力の端子の候補（ロジアナのチャンネル・プルアップの有無）と同時に動かせる数を申告し、0 にできる | pytest（caps）、cocotb（0 の構成で窓がバスエラー） |
| 足す論理が小さい（目安 +150 LUT） | 合成（§7） |
| 本体の API と MCP に、汎用の設定・有効・無効・状態・解放（名前にキーボードの言葉を使わない） | pytest host |
| キーボードの行列をまねるプラグイン（本体と分離。本体からは読み込まない） | pytest host |

## 2. 用途（汎用の機能の例）

- オシロスコープ・他の計測器へのトリガ出力（ロジアナのトリガ・DAP の番地一致・多段トリガの発火 → 決めた長さの脈）。
- ターゲットの割り込みの端子をたたく（ホストの指令 → 脈。または条件の間）。
- ボタン・センサーのまね（保持 = 押したまま、条件の間 = 走査に合わせて返す）。
- キーボードの行列（列の線を見て、その列が高い間だけ行の線を上げる）= 条件の間 + 端子の並びの条件。§6 のプラグイン。

## 3. FPGA

### 3.1 構成

```
la_pins(7:0) ─2 段で同期化─┐
                           ├─▶ 動作 k の条件 c_k ──▶ 形（脈・保持・条件の間）──▶ xout_o(k) / xout_oe(k)
sources(3:0)（既存の源）────┘                                              │ xout_claim(k)・xout_pin(k)
                                                                         ▼ ボードのトップ（pin_select）
time_count の bit 4·8·…·28 の変化 ─▶ 脈の長さの刻み        候補 PIN_CHOICES(xout_pin(k)) の端子
                                                            （gpio > 動作 > 機能）
```

- 新しい entity `xtrig_out`（`rtl/core/xtrig_out.vhd`、ボード非依存）。動作の数 `COUNT`。
- **動作（engine）と端子を分ける。** 動作 k は 2 本（`XOUT_ACTIONS`、§3.9 の資源のため）、動かす端子は CSR
  `ctrl<k>.pin` で候補 4 本（ボードが決める）から選ぶ。端子への割り当てとその機能との切り替えはボード非依存の
  `pin_select`（`rtl/core/pin_select.vhd`、組み合わせ回路）がトップの中で行う。
- CSR は新しいブロック `xtrig_out`（0x0D00、0x40）。クロストリガ（`xtrig`）の窓は 16 語すべて使っていること、
  出力の数が 0 の構成で窓ごと外して（`csr_absent`）資源を 0 にできることから、窓を分ける。概念上はクロストリガの
  「動作」の 1 つで、源は `xtrig` と同じ 4 本（bit0 ロジアナのトリガ、bit1 DAP の番地一致、bit2 ホストの指令、bit3 多段トリガ）。
- fprobe_core のジェネリック `XOUT_COUNT`（動作の数 0〜2、ケイパビリティから）。0 なら回路も CSR も無い。
  ポート `xout_o` / `xout_oe` / `xout_claim`（各 2 ビット）・`xout_pin`（2 ビット × 2）。使わないビットは 0 を駆動。

### 3.2 出力 k の条件

```
pattern_k = 端子の並び（同期化した la_pins(7:0)。mask = 0 なら不成立）
            all: (pins and mask) = (value and mask)         -- すべての見る端子が値に一致
            any: ((pins xnor value) and mask) /= 0         -- 見る端子のどれかが値に一致
c_k       = pattern_k or ((sources and src_k) /= 0)
```

- 端子の並びの照合は毎周期その場で行う（条件は「端子の並び」だけで済む）。ロジアナ・多段トリガの設定には依存しない。
- `any` は「どれかの列が高い」（キーボードで同じ行の複数のキーを同時に有効にする）や「どれかの線が落ちた」に使う。
- 源（`sources`）は 1 周期の脈なので、条件の間の形では 1 周期だけ動く（意味は定義どおり。主に脈・保持に使う）。

### 3.3 形（mode）

| mode | 名前 | 動き |
|---|---|---|
| 0 | 脈（pulse） | c_k の立ち上がりで長さ n × 刻みの脈。脈の途中の立ち上がりは長さを読み直す（延びる） |
| 1 | 保持（hold） | c_k の立ち上がりで動き、無効にするまで動いたまま |
| 2 | 条件の間（while） | c_k が成り立つ間だけ動く（3 は 2 と同じ） |

- 有効（enable）= 0 で状態を消し、出力を止める（駆動しない）。保持を戻すのは「無効にする」（再び使うときは有効にする）。
- 有効にした時点で条件が既に成り立っていても、脈・保持は次の立ち上がりを待つ（c_k の前の値は有効・無効に関係なく追う）。
- 動いている間: `oe = 1`、`o = level`（1 = 高、0 = 低）。動いていない間: `oe = 0`（高インピーダンス）。
- 遅れ（条件の間）: 端子 → 同期化 2 段 → 条件のレジスタ → 出力のレジスタ = 4 周期（100 MHz で 40 ns）。
  端子の I/O のレジスタ・配線を入れても 50 ns 程度。

### 3.4 脈の長さ

- 長さ = `length`（1〜255）× 刻み、刻み = 16^`unit` 周期（unit 0〜7: 1 周期〜2^28 周期 = 2.7 s @ 100 MHz）。
  最長 255 × 2^28 周期 ≈ 683 s。
- 刻みは共通のタイムベース（time_count）の bit 4·8·…·28 の変化を使い、出力ごとの前置分周器を持たない（資源のため）。
  そのため unit ≥ 1 では最初の刻みの位置がずれ、実際の長さは ((length − 1) × 刻み, length × 刻み]。
  ホストは length ≤ 255 になる最小の unit を選ぶので、unit ≥ 1 のとき length ≥ 16 で誤差は 1/16 未満。
  unit 0（≤ 2.55 µs）は周期単位で正確。オシロのトリガ（短い脈）は正確、ボタンのまね（ms 単位）は数 % で足りる。
- length = 0 は脈を出さない（ホストは 1 以上を書く）。

### 3.5 端子の選択（その機能に使う / 出力に使う）

- 端子の候補はボードが決める（`Board.output_pins`、RTL の `XOUT_PIN_CHOICES`）。動作 k は `ctrl<k>.pin` で 1 本を選ぶ。
  Primer 20K（J12 = pmod1）:

| 候補 | 端子 | ロジアナの ch | 普段の機能 | プル |
|---|---|---|---|---|
| 0 | pmod1[4]（T12） | 4 | 予備（無し） | **無し**（§3.6） |
| 1 | pmod1[5]（R11） | 5 | UART RX（ターゲット → プローブ） | 上げ |
| 2 | pmod1[6]（T11） | 6 | SWO / TDO | 上げ |
| 3 | pmod1[7]（P11） | 7 | UART TX（プローブ → ターゲット） | **無し**（§3.6） |

- CSR `ctrl<k>.claim` = 1 で、選んだ端子を「出力に使う」。そのとき（ボードのトップの pin_select）:
  - その端子の機能の出力（UART TX）は駆動しない。
  - その端子の機能の入力（UART RX・SWO・TDO）には休止の値 1 を入れる（浮いた線・出力の値を受信しない）。
  - 端子は動作 k が動いている間だけ駆動する。
- 優先: gpio ブロック（配線確認・ビットバング）> 動作 k（claim のとき。同じ端子を 2 つの動作が選んだら番号の小さい
  動作。ホストは同じ端子に 2 つの動作を割り当てない）> 機能。
- ロジアナはいつも端子そのものを見る（出力した波形を同じ時間軸で記録できる）。
- claim は有効・無効とは別（無効にしても端子は機能に戻らない。戻すのは claim = 0）。キーボードの行の線が、
  無効にしたとたん UART TX の休止の High で駆動される、ということが起きない。

### 3.6 プルアップの扱い（結論）

前提: Gowin の I/O のプル（UP / DOWN / KEEPER / NONE）は制約（.cst）で決まり、実行中に変えられない。
今の pmod1 は全ピン `pull=UP`。ターゲットの内部プルダウンの入力（例: ZMK の行の線）につなぐと、
高インピーダンスにしても弱いプルアップとプルダウンの分圧になり、Low にならない（上げっぱなし）。

**結論: 出力の候補のうち pmod1[4]（予備）と pmod1[7]（UART TX）を `pull=NONE` にする。pmod1[5]（UART RX）と
pmod1[6]（SWO / TDO）は `pull=UP` のまま。** 端子ごとのプルの有無はケイパビリティで申告し、ホストが使い分ける。

理由:

| 端子 | プル無しにした場合の影響 | 判断 |
|---|---|---|
| [4] 予備 | 普段の機能が無い。何もつながないとロジアナの ch4 が浮いて揺れることがあるだけ | 無しにする（失うものが無い） |
| [7] UART TX | 普段は FPGA が駆動する（休止 = High）のでプルは効いていない（B29 以後: ホストが UART に最初の 1 バイトを送るまでは駆動せず浮く。頼まれるまでターゲットの線を駆動しないため）。出力に使って動いていない間だけ浮く（ターゲットの RX を浮かせるのは claim した利用者の選択） | 無しにする（普段の機能に影響なし） |
| [5] UART RX | 何もつながないと浮き、隣の SWCLK を拾ってゴミのバイトを受ける（CDC-ACM の端末にゴミ、記録に UART 受信のイベント） | **上げのまま**（日常の使い勝手を壊す） |
| [6] SWO / TDO | SWO を始めたまま外すとゴミ。JTAG の構成でターゲットが無いとき TDO が全 1 にならず、走査の結果がでたらめになる | **上げのまま** |

- 結果として、内部プルダウンの入力（キーボードの行等）を動かせる「本当の高インピーダンス」の端子は 2 本（ch4・ch7）。
  これは同時に動かせる動作の数（2）と一致する。ch5・ch6 は、動いていない間は弱いプルアップ（= High 寄り）になる。プルアップのある入力・オシロの入力への
  「Low の脈」、オープンドレイン風の使い方（level = 0）には問題ない。
- ホストは、出力の端子にプルアップがあり level = 1（高）を選んだとき、結果に注意（note）を返す（動いていない間も
  High に見える）。キーボードのプラグインは、行の出力にプルアップの端子を選ぶと誤りにする（active-high の行）。
- 将来、行が 3 本以上要るなら: (a) [5] も NONE にして UART RX の浮きは「UART を使わないときは端子を claim しておく」
  運用で吸収する、(b) J14（pmod0）の空きピンを出力に足す、のどちらか。いまは (b) が素直（ボードの定義と
  `XOUT_PIN_CHOICES` を足すだけ。候補は 4 本まで = ctrl.pin の 2 ビット）。

### 3.7 CSR `xtrig_out`（0x0D00）

| 番地 | 名前 | フィールド |
|---|---|---|
| 0x00 | status（RO） | active[1:0] = 動作 k が動いている（駆動している） |
| 0x04 + 8k | when&lt;k&gt;（k = 0, 1） | mask[7:0]・value[15:8]（端子の並び。ロジアナの ch 0〜7）、sources[19:16]（xtrig の源）、any[20] |
| 0x08 + 8k | ctrl&lt;k&gt;（k = 0, 1） | enable[0]、mode[5:4]、level[8]、claim[12]、length[23:16]、unit[26:24]、pin[29:28] |

- 動作の数がケイパビリティより少ない k の when・ctrl は読み書きできるが何もしない。

### 3.8 ケイパビリティ

- TLV 種類 15 `OUTPUTS`: 先頭 1 バイト = 同時に動かせる動作の数、続いて端子の候補 1 本に 1 バイト（bit6:0 = その
  端子のロジアナのチャンネル、bit7 = プルアップがある。ctrl.pin の番号の順）。無ければ出力は無い。
- 生成: `Capabilities.outputs`（designs.py がボードの `output_pins` とピンのプルから作る）・`output_actions`。
  VHDL の定数 `XOUT_COUNT`（候補が無ければ 0）。
- Primer の USB の 2 構成（fprobe・fprobe_jtag）は候補 4 本・動作 2 本。Ethernet・SerialLink・Nano 9K・memtest は 0
  （トップに pin_select をつなげば同じコアで使える。残り）。

### 3.9 資源の工夫

- 最初に「端子 4 本 = 動作 4 本」で作ると合成で +341（LUT +309・ALU +32）だった（xtrig_out 235 LUT、CSR 118 LUT）。
  動作を 2 本にし、端子は候補から選ぶ形に変えて +89（§9）。プル無しで本当の高インピーダンスにできる端子が 2 本
  （§3.6）なので、同時に 2 本で実用上の不足は小さい。
- 長さの刻みはタイムベースの共通のビットの変化（動作ごとの分周器なし。§3.4）。
- 端子の同期化は 8 本 × 2 段（動作の数に依らない）。
- 脈の計数は 8 ビット。
- 駆動（oe）はレジスタから出す（端子に組み合わせのひげを出さない）。

## 4. ホスト（本体）

### 4.1 ライブラリ `fprobe.xtrig`

- `OutputMode`（PULSE / HOLD / WHILE）、`OutputConfig`（mask・value・any・sources・mode・level・length・unit・claim）、
  `OutputStatus`。
- `PinOutputs(dev)`: `configure(action, pin, cfg)`（無効にしてから書く）、`enable` / `disable`、`release`（無効・
  claim を外す・条件を消す = 動作が空く）、`status()`、`action_for(pin, allocate=)`（端子を受け持つ動作。無ければ
  空いている動作）、`pins`（候補の (ch, pull_up)）、`actions`。
- `pulse_ticks(seconds, clock_hz) -> (length, unit, actual_min_s, actual_max_s)`。

### 4.2 API（`host/api/fprobe_api.json`）と MCP

出力は端子のロジアナのチャンネル（`channel`）で指す（Primer: 4〜7）。動作の割り当ては Probe の道具が行い、
使う側は動作の番号を知らない（同時に動かせる数を超えると誤り。`xtrig.output_release` で空ける）。

| 名前 | kind | 内容 |
|---|---|---|
| `xtrig.output_set` | control | channel、mode（pulse / hold / while）、level（high / low）、pattern {mask, value, any}、sources（源の名前）、pulse_us、enable（既定 false）。設定すると端子を claim する。結果は実際の脈の長さの範囲・プルの注意（notes） |
| `xtrig.output_enable` | control | channel。有効にする |
| `xtrig.output_disable` | control | channel。無効にする（保持も戻る。端子は claim のまま高インピーダンス） |
| `xtrig.output_release` | control | channel。無効・claim を外す（端子を普段の機能に戻す） |
| `xtrig.output_status` | control（observes） | 候補ごとの channel・プル・設定済みか・有効・形・動いているか・claim、actions（同時に動かせる数）・free |

## 5. 試験

- cocotb `xtrig_out`（単体）: 3 つの形、遅れ 4 周期、any / all、空の mask、源、脈の長さ（unit 0 は正確、unit 1 は
  範囲）、脈の途中の再トリガ、保持と無効、有効にした時点で成立している条件、level = 0、無効は駆動しない。
- cocotb `pin_select`: claim なしは機能、claim で機能を止めて動作に従う、動作 × 候補の対応、2 動作、gpio が最優先。
- cocotb fprobe_core（4 構成。うち 1 つは出力なし）: CSR 経由の端子の並び → 出力・claim・pin、無効でも claim は
  残る、ホストの指令 → Low の脈 25 周期、出力なしの構成で窓がバスエラー。
- pytest host: 符号化（偽の機器）、API、プラグイン。
- HIL（`gateware/tests/hil/test_xtrig_out_hw.py`、走らせない）: §8。

## 6. プラグイン（キーボードの行列）

### 6.1 プラグインの形（最小限）

- 方針（2026-10-18 利用者）: 特定の機器のための機能をデバッガに足さない。まずターゲット側を改造して汎用の機能に
  合わせる。FPGA には汎用の機能だけを入れる。PC のソフトだけで済む機器固有の処理はプラグインにする。
- 置き場所: `host/fprobe/plugins/<名前>.py`。本体（mcp_server・daemon・API）からは import しない（試験で確かめる）。
  利用者・AI が明示して import して使う。MCP の道具にはしない。
- プラグインは本体の公開 API（`fprobe_api.json` の名前と引数）だけを呼ぶ。呼び先は `call(method, **params)` を
  持つもの: デーモンにつないだ `DaemonClient`、またはプロセス内の Probe を包む `fprobe.plugins.ProbeCaller(probe)`
  （API の定義の impl を引いて Probe のメソッドを呼ぶ）。どちらでも同じプラグインが動く。
- 状態はプラグインのオブジェクトが持つ（デバイスには汎用の設定しか書かない）。

### 6.2 `keymatrix`

```python
from fprobe.plugins import ProbeCaller
from fprobe.plugins.keymatrix import KeyMatrix
km = KeyMatrix(client)                         # DaemonClient か ProbeCaller(probe)
km.add_key("A", column=1, row=4)               # 列 = ロジアナの ch1（74HC595 の出力）、行 = 出力の ch4
km.press("A"); km.release("A"); km.tap("A", 0.05)
```

- キー = (列の入力の ch、行の出力の ch、active_high)。active_high = True: 列が High の間、行を High にする。
- 行ごとに、押しているキーの列の集まりで `xtrig.output_set(channel=row, mode="while", pattern={mask: 列の和,
  value: 列の和 or 0, any: true}, level=high/low, enable=true)`。押しているキーが無くなれば `output_disable`。
- 同じ行の複数のキーを同時に押せる（any）。違う行は別の出力。
- 行の出力の端子にプルアップがあって active_high なら、登録で誤り（§3.6）。
- tap は press → 待つ → release（ホストの時計。キーボードの走査は ms 単位なので足りる）。
- 制約: 行は同時に 2 本まで（動作 2 本。内部プルダウンの行に使えるプル無しの端子も ch4・ch7 の 2 本）。
  同じ列の線を使う別の行のキーとの同時押しは、行ごとに独立なので問題ない。走査の周期より短い tap は
  取りこぼされうる（ZMK の既定のデバウンス 5 ms より長く）。

## 7. 合成（資源）

`syn_measure.sh` で合成だけ（配置配線なし）。基準 16,716（14,004 LUT, 1,878 ALU, 139 RAM16）/ 20,736。結果は §9。

## 8. 実機で確かめる手順（HIL）

`gateware/tests/hil/test_xtrig_out_hw.py`（`pytest -m hil`。走らせるのは本流の担当）。配線: ターゲットは外す。
ジャンパ A = pmod1[2]（ch2、gpio で駆動）→ pmod1[1]（ch1、条件の入力）。pmod1[4]（ch4）と GND の間に 10 kΩ。
**Primer の USB をつないでからジャンパをつなぐ。** どれも FPGA の 3.3 V の端子同士で、逆流は無い。

1. ケイパビリティ: 候補 (4, プル無し)・(5, 上げ)・(6, 上げ)・(7, プル無し)、動作 2 本。
2. 高インピーダンス: 保持の設定（無効）で ch4 が Low（10 kΩ）、ch5 が High（プルアップの申告と一致）。
   ホストの指令で ch4 が High のまま、無効で Low に戻る。
3. 脈 1 µs（刻み 1 周期）: ロジアナ（深いバッファ、100 MS/s）で ch4 の High の幅 = 100 サンプル ± 1。
4. 条件の間: ch1 の立ち上がり → ch4 の立ち上がりの遅れ ≤ 60 ns。ch1 を下げると ch4 も下がる。
5. claim: ch7（UART TX）を設定すると、UART に書いても ch7 が動かない。release で UART に戻り休止の High。
6. プラグイン: KeyMatrix（列 = ch1、行 = ch4）で press 中、列を上げ下げすると行が追う。
7. 手で: XIAO nRF52840（ZMK）。列（74HC595 の出力）を ch1（SWD では使わない TDI）か出力に使っていない ch5・ch6 に、行を
   ch4 か ch7 に。`KeyMatrix.tap` でキーが入ること（PC の USB HID）。XIAO は 3.3 V。行の線は XIAO の入力
   （内部プルダウン）なので逆流は無い。

## 9. 結果

- 合成（`syn_measure.sh` と同じ手順。合成だけ）: 基準 16,716（14,004 LUT, 1,878 ALU, 139 RAM16）→
  **16,805（14,077 LUT, 1,894 ALU, 139 RAM16）= +89（LUT +73、ALU +16）**。部品ごと: xtrig_out 129 LUT・16 ALU・31 FF、
  csr_xtrig_out 58 LUT・109 FF、pin_select 19 LUT（合成の最適化で、部品の和より全体の増分が小さい）。EX1998 なし。
- 実機の試験: `gateware/tests/hil/test_xtrig_out_hw.py`（走らせていない）。
