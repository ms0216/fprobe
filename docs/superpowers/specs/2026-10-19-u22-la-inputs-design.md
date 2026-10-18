# U22: ロジアナの専用チャンネルと外部トリガ

台帳 U22（要件 T32-11・T32-13・WVF-05・CTX-08。`docs/reports/2026-10-18-gap-analysis.md`）。

## 1. 決めたこと

| 項目 | 決定 | 理由 |
|---|---|---|
| 専用の入力 | J14（`pmod0`）の空き 6 本（T8・P8・R8・T7・T6・P6）を入力だけのピン in0〜in5 にする | J14 の 5 番 P9 は補助 Pico の UART、6 番 T9 は WS2812 と共用。J12 は 8 本すべて使用中 |
| ch の位置 | ロジアナの ch 10〜15（ピン 0〜7、内部 8・9 の後） | サンプルの幅 16（`LA_WIDTH`）に空いていた 6 本に入る。深いバッファ・RLE・トリガ・多段トリガ・パルス幅の幅は**変わらない**。内部の ch 8・9 の番号も今までの記録・道具と同じ |
| 申告 | ケイパビリティ TLV `LA_LAYOUT`（18）: ch 1 本に 1 バイト = 種類 << 4 \| 番号（pin / internal / input / none）。`la_channels` はサンプルの幅のまま | PC は申告から名前（`in0` 等）・選べる ch・フィルタをかけられる ch を決める。無い古いデバイスは ch 0〜7 = ピン、8・9 = 内部 |
| ピンの電気 | 入力だけ・プルダウン・ヒステリシス | つないでいない入力を 0 に止める（浮いた入力の変化で RLE を無駄にしない）。プルアップと違い電源の無いターゲットに電流を流さない |
| グリッチフィルタ | 専用の入力にもかける。CSR `la.filter.channels` を 8 → 16 ビット（ビット n = ch n） | ジャンパ線の入力は J12 と同じくひげを拾う。全 ch が同じ遅れになるよう、ピンと同じ `la_filter` を通す |
| 外部トリガの入力 | **新しい回路を足さない**。専用の入力はロジアナの ch なので、(a) ロジアナのトリガ条件（値・エッジ・パルス幅）、(b) ロジアナを動かさずに: 多段トリガのパターン（1 段、その ch の rise / fall）→ クロストリガの源 bit3 → ターゲット停止・ロジアナ・出力の端子 | 既存の汎用の機能で全部表せる。資源の余裕（Logic 85%・sys の余裕 0.18%）を、重なる回路に使わない |
| 外部トリガの出力 | J14 を候補に**加えない**。U41 の出力の候補のうち J12 の 4 番（予備、T12）が普段の機能を持たない専用の出力として使える | 候補を 5 本にすると `ctrl.pin` が 3 ビットになり、入力が 1 本減る。予備の端子で用が足りる |
| 構成 | 新しい構成 `fprobe_la`（SWD・専用の入力 6 本。サンプラ・故障注入・JTAG なし）にだけ入れる。ほかの構成は入力 0 本（申告も 0 本。J14 のピンは制約するが使わない） | 標準 `fprobe` に入れると Logic +347（17443 → 17790、86%）・CLS 97% で、3 通りの配置のどれも sys（TNS −0.96〜−1.16 ns）と mem_clk（TNS −24〜−40 ns）のタイミングを満たさなかった（2026-10-19）。入力 0 本の同じ RTL は満たす（sys 100.48 MHz） |
| 構成の選び方 | ゲートウェアの `FEATURE_DESIGNS = {"la_inputs": "fprobe_la"}` → ホスト定数 `DESIGN_FOR_LA_INPUTS`。`probe_design_switch(design="fprobe_la")` で明示に選ぶ。SWD の接続では標準に戻さない（機能のために選んだ構成）。JTAG の接続では `fprobe_jtag` へ | 線で選ぶ構成（B22）と機能で選ぶ構成を分ける。サンプラ・故障注入が要るときは標準へ戻す |
| 無い ch の検査 | ホストはトリガ（mask・rise・fall）・フィルタが申告に無い ch を指したら誤り（構成 `fprobe_la` を案内） | 成り立たない条件を黙って待たない |

## 2. 実装

- `fprobe_core`: generic `LA_INPUT_COUNT`・ポート `la_inputs`。外部の入力（ピン + 専用の入力）を 1 つの `la_filter` に通し、
  ピンは ch 0〜、専用の入力は ch `LA_PIN_COUNT + 2` 〜 に置く。`LA_PIN_COUNT + 2 + LA_INPUT_COUNT <= LA_WIDTH` を検査。
- `fprobe_gw`: 資源 `la_in`（`boards/primer20k.py`）、`Board.la_inputs()`、`Capabilities.la_pins / la_inputs`、
  `design_caps_pkg.LA_INPUTS`、`primer20k_pkg.LA_INPUT_PINS`。
- ホスト: `fprobe.caps.LaChannel`・`Capabilities.la_layout`、`fprobe.la.channel_layout / channel_names / external_channels`
  （VCD・.sr の名前、フィルタの検査）。画面は `waveform.channelName / channelChoices` が申告に従う。

## 3. 資源とタイミング

2026-10-19 のビルド（U51・U52 を取り込んだ後。選ばれた配置の値）:

| 構成 | Logic | CLS | sys_clk Fmax（制約 100.29 MHz） | mem_clk Fmax（制約 99.56 MHz） |
|---|---|---|---|---|
| `fprobe`（入力なし） | 17709（86%） | 9994（97%） | 100.42 MHz | 101.69 MHz |
| `fprobe_jtag`（入力なし） | 16890（82%） | 9808（95%） | 102.03 MHz | 104.83 MHz |
| `fprobe_la`（入力 6 本） | 16333（79%） | 9595（93%） | 108.69 MHz | 103.07 MHz |
| 参考: `fprobe` に入力 6 本（U51・U52 の前） | 17790（86%） | 9996（97%） | 98.46 MHz（未達） | 96.47 MHz（未達） |

専用の入力の回路は約 +350 LUT（`la_filter` が 14 ch で 364 LUT・162 FF。ch あたり約 26 LUT）。未達の経路は
入力の回路ではなく深いバッファの読み出し（`mem_reader` → DDR3 の受け口）で、混雑による配置の悪化と見る。
