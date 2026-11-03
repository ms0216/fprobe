# 垂れ流し用の回路（fprobe_pynq_deep）にも試験用の方形波（la_testgen）を足す

2026-11-01。仕様のみ（実装・ビルド・実機操作はまだしない）。`2026-10-31-la-testgen-design.md` の 5 章末「未着手」の設計。
書き分け: **［事実］** は既存のコード・仕様から確認したこと、**［推測］** は測っていない見積り、**［決める］** は利用者の判断が要ること。

## 1. なぜ

- 垂れ流し（`fprobe_pynq_deep`）の画面と記録では、矩形波が実際に流れてくるのを見たい。いまの実機の入力は何もつないでおらず信号が一定で、
  **チャンネル詰め（`la_pack`）の中身の並びの確認はシミュレーションのみ**（`2026-10-31-la-stream-pack-design.md` 5 章）。
- 試験用の方形波 `la_testgen` は `fprobe_pynq_cap` にだけある。［事実］`fprobe_core` が `CAP_RATIO > 1` の中（`la_cap_info`）にだけ置き、
  それ以外では `tg_q`・`tg_oe` を 0 に固定している（`fprobe_core.vhd` 826〜846 行）。ホストの `LogicAnalyzer.testgen` も `cap_ratio <= 1` を断る（`_require_testgen`）。
- ［事実］ピンは既にある。`_pynq()` の `pin_groups` に `testgen`（RPi ヘッダ 32 番、B20、DRIVE 8・SLEW FAST）が全 PYNQ デザイン共通で入っており、
  トップは `testgen_pin <= testgen_q when testgen_oe = '1' else 'Z'`。`deep` ではこのピンが常に Hi-Z になっているだけ。**ピン配置・XDC は変えなくてよい。**

## 2. ゴール・非ゴール

- ゴール: `la_testgen` を、ケイパビリティ `la_testgen` のある構成すべて（`fprobe_pynq_cap` と `fprobe_pynq_deep`）で使えるようにする。
  有効にしたとき以外はピンを駆動しない（Hi-Z）。
- ゴール: ホストは構成の名前でなくケイパビリティで判定する。無い構成は `RuntimeError`（黙って何もしない形にしない）。
- ゴール: 回路はボード非依存のまま。ボード固有はトップ（ピンの `'Z'` 合成）だけ。
- 非ゴール: 垂れ流しでの外部ピン入力の限界の測定。deep の取り込みは sys の 1 サンプル/周期で、300 MHz の測定は `fprobe_pynq_cap` の役目のまま。
  deep の方形波は「流れる中身の確認」の刺激である。
- 非ゴール: 測定結果の判定・合否を fprobe 本体に持たせること。本体は方形波を出すだけで、合否は使う側（試験・人・AI）が見る。
- 非ゴール: Primer など他ボードへの展開（面積に限りがあり、観測の導入と同様に言い出さない。ケイパビリティで無効にできる形にだけしておく）。

## 3. 形

### 3.1 ケイパビリティ

- `caps.Capabilities` に `la_testgen: bool = False`、FEATURES に `FEATURE_LA_TESTGEN = 1 << 13`（［事実］0〜12 は使用済み、13 は空き）。
  生成する `design_caps_pkg` に `constant LA_TESTGEN : boolean`。ホストの `Capabilities.la_testgen`（FEATURES に bit が無ければ False。他の機能と同じ流儀）。
- `_pynq()` の既定: `la_testgen = cap or deep`（`fprobe_pynq` の内蔵 BRAM 版は 取り込みが sys で、足してもよいが今回は含めない）。［決める］ 8 章の 4。

### 3.2 コア（fprobe_core）

- 条件を `CAP_RATIO > 1` から `LA_TESTGEN`（generic）に変える。`testgen_i` の `cap_clk` は、`CAP_RATIO > 1` なら `cap_clk`、そうでなければ **`clk`（sys）** を渡す
  （generate の分岐で配線を選ぶ。クロックを式で切り替えない）。［事実］deep のトップは `cap_clk` を `'0'` に固定する（`xilinx_mmcm.vhd` の `CAP_DIV = 0`）ので、
  そのまま渡すと方形波が動かない。
- `la_testgen` 自体は変えない（`cap_clk` というポート名は「出力を刻むクロック」の意味で残す。名前の付け替えは差分が大きいのでしない）。
  sys と同じクロックを `clk` と `cap_clk` の両方に渡したとき、`cdc_value` が動くこと（余計な段が増えるだけで値は正しく届くこと）を、テストで確かめる。
  ［推測］`cdc_value` は 2 つのクロックの関係を仮定しない受け渡しなので同一クロックでも動くはずだが、**未確認**（実装前にソースとシミュレーションで確かめる）。
- 使わない構成（`LA_TESTGEN = false`）は、いまの `no_la_cap_info` と同じく `tg_q <= '0'; tg_oe <= '0'`（全信号を駆動する）。

### 3.3 レジスタ（CSR）

いまの試験用の方形波のレジスタ（`testgen`・`testgen_period`・`testgen_high`）は、`la_cap` ブロック（0x0C80）にある。このブロックは「取り込みを速いクロックで動かす構成だけ」の情報で、
deep（比 1）では丸ごと無く、読むとエラーになる。選択肢:

| 案 | 内容 | 良い点 | 悪い点 |
|---|---|---|---|
| A | `la_cap` を `CAP_RATIO > 1 or LA_TESTGEN` で置く | 変更が小さい。既存の名前・番地・HIL が無傷 | deep に `ratio = 1` の `info`・`selftest`（意味が無い）が見える。ブロックの趣旨が崩れる |
| **B（推奨）** | 方形波の 3 レジスタを新ブロック `la_testgen`（0x0CC0。［事実］0x0C80 の次、0x0D00 の前が空き）に移す | 責務が分かれる。`la_cap` は速いクロックの構成だけのまま、`la_testgen` はケイパビリティ `la_testgen` のある構成だけ | `fprobe_gw gen` の再生成、ホスト定数 `LA_CAP_TESTGEN*` → `LA_TESTGEN*` の改名、既存の cap 用 HIL・ホスト試験・`tools/pynq_ops/tg.py` の追随が要る |

- ［決める］ A か B か。推奨は B（「正しく美しく」。移行は機械的で、既存の cap のビットストリームは再ビルドするので互換の問題は出ない）。
- 番地の変更は、ビットストリームとホストが対で更新される前提（本拠地の運用）。古い cap のビットストリームにホストが新しい番地を書くと、`la_testgen` ケイパビリティが
  無いので `RuntimeError` で止まる（番地を黙って書かない）。

### 3.4 ホスト（LogicAnalyzer）

- `_require_testgen()` を `caps.la_testgen` で判定する（`cap_ratio` を見ない）。エラー文は「この構成には試験用の方形波が無い（ケイパビリティ la_testgen が無い）」。
- 周波数の計算: いまは `self.clock_hz / period`。［事実］`clock_hz` は `caps.la_sample_hz`。cap では cap_clk、deep では sys（`_pynq` が `la_sample_hz = pll.fout_hz` を入れる）で、
  どちらも「方形波を刻むクロック」と一致する。ただし**暗黙の一致に頼らない**ため、返す周波数は「方形波を刻むクロック」を 1 か所に定義して使う
  （`testgen` 側の定義: `CAP_RATIO > 1` なら `la_sample_hz`、そうでなければ `sys_clock_hz`。コア側の配線の選びと同じ規則を、ホストの 1 関数に置く）。
- 範囲検査（period 2〜4095、high 1〜period − 1、edge 0/1）は変えない。回路が黙って丸める値は、ホストが断る。
- deep の `LogicAnalyzer` は `DeepAnalyzer` を持つ構成。`testgen` は deep 用のバッファやストリームの状態に触れない（CSR を書くだけ）ので、ストリームの実行中に出し入れできる。

### 3.5 クロック領域での周期・high（cap_clk との違い）

| 項目 | `fprobe_pynq_cap` | `fprobe_pynq_deep`（新） |
|---|---|---|
| 刻むクロック | cap_clk 300 MHz（3.33 ns） | sys 100 MHz（10 ns）［事実: 100 MHz。ビルドの実測 Fmax は 107〜109 MHz］ |
| period 1 単位 | 3.33 ns | 10 ns |
| 周波数 | 300 MHz / period（最大 150 MHz） | 100 MHz / period（最大 50 MHz、period = 2。最小 24.4 kHz） |
| high 1 単位 | 3.33 ns | 10 ns（最小の 1 パルス幅 10 ns） |
| `edge = 1` の遅れ | 半周期 1.67 ns | 半周期 5 ns |
| 設定が出力に届くまで | 6 cap 周期（20 ns） | 6 sys 周期（60 ns）+ 受け渡しの段 |
| 取り込みとの位相 | 同じ MMCM の整数比で固定 | 同じ sys で固定 |

- ［事実］`la_testgen` の周期・high は「渡されたクロックの周期の数」で、クロックの値を知らない。したがって RTL の意味は変わらず、単位が変わるだけ。
- deep の取り込みは sys の 1 サンプル/周期（`rate` の分周で 25 MS/s 等に落ちる）。サンプル周期より短い方形波は、サンプルされる位相で見え方が変わる
  （これは測定の事実であり、補正しない）。方形波の半周期がサンプル周期の整数倍になるような period を選べば、各サンプルの値は規則的になる。
  どの period・rate で確かめるかは使う試験が明示する（本体は既定を持たない）。
- `edge = 1` の意味（半周期遅れる）は cap と同じ。ただし deep では 5 ns と大きく、**入力の位相の偏りを調べる刺激としては粗い**（5 ns = サンプル周期の半分）。
  位相の偏りの測定は引き続き `fprobe_pynq_cap` の役目。

### 3.6 ピンとチャンネル（RPi ヘッダ 32 番と PMOD JB・JA）

［事実］ロジアナのチャンネルの並びは、ch 0〜7 = PMOD JA（`la_pins`、SWD・JTAG の線と兼用）、ch 8 = SWDIO 駆動中、ch 9 = DAP 処理中、ch 10〜15 = PMOD JB の専用入力（6 本）。
既存の仕様（`2026-10-31-la-testgen-design.md`）の配線は **RPi ヘッダ 32 番 → PMOD JB の 1 番 = ch 10**。

- **16 ch の RAW の垂れ流し**: 上の既存の配線のまま、ch 10 に方形波が見える。追加の配線は要らない。ただし［事実］16 ch の RAW は約 50 MB/s = 25 MS/s が壁。
- **`pack`（8・4・2 ch）の確認**: `pack` が流すのは ch 0〜7 だけで、ch 10 は流れない。したがって `pack` の中身を実機で確かめるには、方形波を ch 0〜7 に入れる配線が別に要る。案:

  | 案 | 内容 | 良い点 | 悪い点 |
  |---|---|---|---|
  | **P（推奨）** | RPi ヘッダ 32 番 → PMOD JA の SWO の線（ch 6 = `DBG_SWO`）にもう 1 本ジャンパ（JB 1 番と並べて接続） | 回路の変更なし。外部ピンを通る実信号。汎用の入力端子にそのまま入る | JA は SWD・JTAG の線と兼用。SWO の線を他の機能（クロストリガ出力・UART の RTS/CTS・pin_select）に割り当てていない間だけ安全。**電気的な衝突が無いことを確かめてから配線する**（配線の指示には電源順・電圧範囲・逆流を書く、の方針） |
  | Q | コアの中で `tg_q` を `la_probes` の ch 0〜7 の 1 本に入れる（内部の経路） | 配線不要 | 外部ピンを通らない。`la_probes` の本流にテスト用の分岐が入り、利用者の ch の意味が崩れる。ターゲット固有でなくても「試験のための特別扱い」が増える |
  | R | `pack` の確認はシミュレーションのみとし、実機は ch 10 の 16 ch だけ | 実機の作業が増えない | 実機で `pack` の中身（上位 ch が落ちるのを含む）が確かめられないまま |

  ［決める］ P か Q か R か。推奨は P（回路を変えずに実機で確かめられ、本体に試験用の経路を足さない）。Q は採らない案として残す（ch の意味を崩す）。
- 配線の追加（案 P）はユーザの作業。配線を指示する前に、SWO に使うピンが入力のままであること（`pin_select` の割り当てが無いこと）を実機の CSR で確認する手順を試験に入れる。

## 4. 確かめ方

### 4.1 シミュレーション（cocotb）

- `la_testgen` 単体（`gateware/tests/tb/la_testgen_tb.py`、`test_la_testgen.py`）: 既存 6 件（周期・high、範囲外の丸め、駆動しない、edge の遅れ、リセット）に加えて、
  **`clk` と `cap_clk` に同一のクロックを渡す構成**（周期・high・enable = 0 で駆動しない・リセット）を足す。`cdc_value` の同一クロック動作を確かめる。
- コア全体（`test_fprobe_core.py` の `CONFIGURATIONS` に `cap_ratio = 1` かつ `la_testgen = true` の構成を足す）: CSR（案 B なら `la_testgen` ブロック）が読める、
  enable で `testgen_oe` が立ち `testgen_out` が方形波になる、`LA_TESTGEN = false` の構成ではブロックが `csr_absent`（読むとエラー）で `testgen_oe` が常に 0。
- ケイパビリティ（`test_caps.py`）: `FEATURE_LA_TESTGEN` が `_pynq` の cap・deep に立ち、それ以外（`fprobe_pynq`・Primer）に立たない。
  `test_designs_pynq.py` に `la_testgen` のデザインの一覧を足す。
- 生成物: `python -m fprobe_gw gen` を再実行して CSR・ケイパビリティ・ホスト定数を再生成し、差分が意図したものだけであること（コミット前に確認）。

### 4.2 ホスト（偽のデバイス。`host/tests/test_la_testgen_host.py`）

- `la_testgen` のある偽のデバイス（`cap_ratio = 1`、deep 相当の caps）で `testgen` が通る: 書く順序（period・high → enable）、返す周波数 = sys / period。
- `la_testgen` のない偽のデバイスで `testgen`・`testgen_off` が `RuntimeError`（既存の「対応しない構成の拒否」の判定をケイパビリティに変えたもの）。
- 範囲外（period < 2・> 4095、high < 1・>= period、edge が 0/1 以外）は `ValueError`（既存のまま）。
- ケイパビリティの解読（`host/fprobe/caps.py`）: FEATURES bit 13 → `la_testgen`、bit が無ければ False。

### 4.3 実機（PYNQ-Z2。実装後。ここは未実施）

ビルド（Vivado 2025.2、`fprobe_pynq_deep`）→ 書き込み（CLAUDE.md の手順どおり、JTAG の IDCODE を照合）→ 次の試験。ジャンパ線がつながっていなければ skip、`FPROBE_PYNQ_HOST` 未設定なら skip（既存の HIL と同じ）。

1. 新規 `gateware/tests/hil/test_pynq_deep_testgen_hw.py`（ジャンパ線 32 番 → JB 1 番 = ch 10）:
   - `test_testgen_square_wave_is_streamed_with_the_exact_run_lengths`: 16 ch の RAW 垂れ流し（25 MS/s 以下）で方形波を取り、1 と 0 の連続の長さが、
     サンプル周期と period から分かる範囲に収まる（具体的な許容は、試験が引数で明示し、本体の既定にしない）。サンプル数がレート × 時間と一致、overrun なし、lost_frames 0。
   - `test_testgen_is_hi_z_when_off`: `testgen_off` 後に ch 10 が変わらない（ジャンパ線に別の駆動を足さない確認ではなく、CSR の enable = 0 の読み戻し）。
2. 案 P を採った場合のみ、`test_packed_stream_shows_the_square_wave_on_channel_6`（JA の SWO 線にジャンパ）: pack = 8・4・2 の垂れ流しで、方形波が ch 6（pack = 8 まで。4・2 では ch 6 は流れない）に
   見える／見えないことを、チャンネル数ごとに確かめる。展開後の連続の長さが 16 ch の RAW の同じ区間と一致する。これで「チャンネル詰めの中身の並び」が実信号で確かめられる。
3. 既存の `test_pynq_ps_stream_hw.py`（欠けなし、25〜50 MB/s の 60 秒）を、`la_testgen` を足したビットストリームで再実行し、壁と欠けなしが変わらないことを確かめる。
4. 既存の `test_pynq_cap_testgen_hw.py`（9 件）は、案 B ならレジスタ名の追随のあと、`fprobe_pynq_cap` の再ビルドで再実行する。

## 5. 面積・タイミングへの影響（見積りは推測）

- ［事実］`la_testgen` は 34 ビットの設定の受け渡し（`cdc_value`）、12 ビット幅のレジスタ数段、12 ビットの下りカウンタ、出力の 2 本のフリップフロップ（立ち上がり・立ち下がり）で、大きな演算（乗除算・メモリ）を持たない。
- ［推測］規模は FF が 250 前後、LUT が 100 前後。PYNQ-Z2（Zynq-7020: LUT 53,200・FF 106,400）の 1 % 未満で、BRAM・DSP は使わない。**測っていない。**ビルド後に、使用量の差を実測して記録する。
- ［推測］タイミング: 刻むクロックが sys の 100 MHz で、cap の 300 MHz で合格した回路を 1/3 の速さで使うので、この回路の内部の経路は余裕が大きい。
  懸念は**配置の揺れ**。［事実］deep の sys_clk の余裕は小さい（Fmax 107.1〜109.3 MHz = 目標 100 MHz に対して 7〜9 %）。足した回路が他の経路の配置を動かして悪化させる可能性がある（cap の `Default` 配置が −0.007 ns で不合格だった例がある）。
  ビルドで sys_clk の WNS・TNS・Fmax を、足す前の値（107.1 MHz・TNS 0）と比べる。TNS 0 でなければ `Explore` 等の配置の指示を試す。
- 出力の OBUFT（B20）は、deep では今は `testgen_oe = 0` に固定されて合成で畳まれている可能性が高い［推測］。足すと実際に OBUFT と配線が残る（ピンの制約は既にある）。
- ビルドの時間・合成の警告 EX1998（駆動されない配線）が 0 であること（CLAUDE.md の全信号の明示駆動）を、ビルドの合格条件にする。

## 6. 設計原則との整合

- ヒューリスティクス禁止: 本体は周期・high・edge を設定どおり出すだけで、経験則の閾値・推定・合否を持たない。範囲外は丸めず、ホストが断る（回路が丸める値は 12 ビットの実装の限界で、ホストが先に断る）。
  試験の許容幅・period・レートは、試験が引数で明示し、既定の暗黙の値にしない。
- 画面は薄い層: 画面の変更は無い。もし後で画面に出すなら、API（`la.testgen`）の応答をそのまま描くだけで、周波数の計算・範囲の判定は持たない
  （ホストが `testgen` の戻り値で返す）。人と AI が同じ API を呼ぶ。
- 全信号を駆動: `LA_TESTGEN = false` の分岐で `tg_q`・`tg_oe` を明示的に 0 にする。`generate` で外した側の信号も駆動する。
- ボード非依存: 回路・コア・ホストにボードの名前を入れない。判定はケイパビリティ。ボード固有（ピン B20、OBUFT 合成）はトップと `pynq_z2.py` の既存のまま。
- 特定の機器のための機能ではない: 測定器の自己検証・流れの中身の確認のための汎用の刺激。

## 7. 作業の段階

1. （実装前に）`cdc_value` の同一クロック動作をソースで確かめる。
2. ケイパビリティ（Python・生成物）→ コア（generic・クロックの配線の選び）→ CSR（案 B なら移設）→ ホスト（判定・周波数）→ 試験（4.1・4.2）。
3. ビルド（`fprobe_pynq_deep` と `fprobe_pynq_cap`）：タイミング・面積・EX1998 を記録。
4. 実機（4.3）。案 P の配線は利用者に依頼し、配線の指示に電源順・電圧・逆流の注意を書く。
5. 結果を 2026-10-31 の仕様（5 章末「未着手」）と、`2026-10-31-la-stream-pack-design.md` の 5 章（中身の確認がシミュレーションのみ）に反映する。

## 8. 決めてほしいこと

1. **CSR の置き場**: A（`la_cap` に残す）か B（新ブロック `la_testgen`）。推奨 B。
2. **pack の実機確認の配線**: P（32 番 → JA の SWO 線にもう 1 本）、Q（内部の経路）、R（実機は ch 10 だけ）。推奨 P。
3. **ケイパビリティ名・ビット**: `la_testgen`・FEATURES bit 13 でよいか。
4. **`fprobe_pynq`（内蔵 BRAM・取り込み sys）にも足すか**: 今回は cap と deep だけを提案。
5. **deep の `edge = 1`**: 半周期 5 ns と粗いが、cap と同じ意味で残す案でよいか（deep では edge を使わない運用でもよい）。
