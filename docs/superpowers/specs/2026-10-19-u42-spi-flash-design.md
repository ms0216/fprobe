# U42: SPI Flash の直接書き込み（J-Flash SPI 相当）設計

- 日付: 2026-10-19
- 親: 台帳 U42（SEG-07・SEG-40・CTX-09）、U41（試験用 I/O の API）、B22（FPGA の資源がほぼ満杯）
- 方針: FPGA と本体には汎用の機能だけを入れる。SPI Flash の手順（JEDEC の命令）は本体ではなくプラグインにする。

## 1. ゴールと完了条件

| 完了条件 | 測り方 |
|---|---|
| 拡張ヘッダ（J12）の端子で、汎用の SPI マスタ（mode 0〜3・語長 1〜32・MSB / LSB 先頭・CS の極性）が使える | pytest host（偽の SPI の相手、全 mode × 読みの遅れ 2 通り） |
| 1 ビットごとに USB を往復しない（1 往復で 63 ビット） | pytest（状態の数）、cocotb（EXCHANGE） |
| SPI NOR Flash の JEDEC ID・SFDP・消去（セクタ・ブロック・全体）・ページの書き込み・読み出し・ベリファイ・保護の解除 | pytest host（偽の SPI Flash） |
| Flash の手順は本体から読み込まれないプラグイン | pytest（本体が fprobe.plugins を import しない） |
| FPGA の増分が小さい | 合成（§6） |

## 2. 方式の選択

| 案 | 中身 | 速さ（読み） | FPGA |
|---|---|---|---|
| A. 今の gpio だけ | 1 ビット = SCK 2 回の WRITE + level の READ = USB 3 往復 | 約 0.2 KB/s（1 MB に 80 分） | 0 |
| **B. EXCHANGE + gpio の io（採用）** | CONTROL に「同じ番地に書いて読むを繰り返す」要求を足し、gpio に「書くと drive・読むと端子」の io を足す。1 ビット = 2 状態、1 往復 126 状態 | 約 30〜40 KB/s | 制御の状態機械に分岐、CSR 1 語 |
| C. ロジアナで MISO を拾う | 書きだけまとめ、MISO はロジアナの記録から復号 | B と同程度 | 0（要求の op は要る） |
| D. FPGA の SPI マスタ | シフトレジスタ・分周・CPOL/CPHA。EXCHANGE と組んで 1 語 = 1 バイト | 約 500 KB/s | +100〜150 LUT の見込み |

- B を選んだ。理由: 足すものがどちらも汎用（EXCHANGE は FIFO・シフトレジスタ等「書いて応答を読む」レジスタ全般に
  使える CONTROL の要求、io はマイコンのポートと同じ形）で、資源がほぼ 0。C はロジアナ（解析の主役）を SPI のために
  占有し、捕捉と読み出しの手間が増える。D は資源（B22）を使うので、速さが要るときの次の段にする（§8）。
- 速さの差（A → B）は 100 倍以上で、Flash の読み書き・ベリファイが実用の時間（1 MB で 1 分前後）になる。

## 3. FPGA（汎用）

### 3.1 CONTROL の EXCHANGE（op 0x03、プロトコル 2）

- 形は WRITE と同じ（ヘッダ 8 バイト + count ワード）。各ワードを addr に書き、続けて同じ addr を読み、読んだワードを
  応答に流す（番地は進めない）。応答は READ と同じ形。長さ・番地の検査は WRITE と同じ。バスエラーはそのワードで止める。
- `control_plane.vhd` の WRITE の後に READ の状態へ進む分岐を足しただけ（バッファなし）。
- プロトコルの版を 2 にした（1 の要求はそのまま）。ホストはケイパビリティの protocol_version ≥ 2 で EXCHANGE を使う
  （古いビットストリームには分かる誤り）。

### 3.2 gpio の io（0x020C）

- 読むと level（2 段で同期化した端子）、書くと drive（同じ記憶）。CSR の生成器に「書き先のレジスタ（write_to）」を
  足した（読みはこのレジスタのフィールド、書きは同じブロックの別のレジスタの RW フィールドへ）。
- EXCHANGE で io を使うと、1 ワードごとに「端子を書く → 読む」。読みは同期化の分（2 周期）だけ書く前の端子のことがある。
  同じ値を 2 回書けば 2 回目には必ず見える（cocotb で確かめた）。
- 1 ワードの処理は約 14 周期（受信 4 バイト・書き・読み・送信 4 バイト）= 約 140 ns。パケットの中の SCK は約 3.5 MHz。

## 4. ホスト（本体、汎用）

- `fprobe.control`: `encode_exchange`、`ControlChannel.exchange`（要求の大きさで分ける）。`Device.exchange`。
- `fprobe.gpio.Gpio`: state / set（mask の端子だけ。drive を先に書いてから enable）/ release / sequence（EXCHANGE）。
- `fprobe.spi.SpiMaster`: 1 ビット = 2 状態（CPHA 0: 出す辺 → 受ける辺、CPHA 1: 逆）。受ける辺の状態の読みから MISO を
  取る。MISO は相手が出す辺の少なくとも 1 状態（約 140 ns）後に読むので、Flash の出力の遅れ（≤ 10 ns）より十分長い。
  長い配線で足りなければ stretch（各状態を 1 + stretch 回）。転送の後も SPI の端子は休止の値で駆動したまま（CS を
  離した状態）で、離すのは gpio.release。SPI 以外の端子（nRESET を Low に保つ等）は変えない。
- API（`host/api/fprobe_api.json`）: `gpio.read` / `gpio.set` / `gpio.release` / `gpio.sequence` / `spi.transfer`
  （16 進の data、read_length、mode・bits・lsb_first・cs_active_high・keep_cs・stretch・fill。端子はロジアナの ch）。
  端子を駆動するものはターゲットに接続中は断る（gpio はデバッグの線より優先して駆動するため）。MCP の道具は
  デーモンの API から自動で出る（`gpio_*`・`spi_transfer`）。

## 5. プラグイン `fprobe.plugins.spiflash`

```python
from fprobe.plugins import ProbeCaller
from fprobe.plugins.spiflash import SpiFlash
with SpiFlash(client, hold={2: 0}) as flash:      # DaemonClient か ProbeCaller(probe)。nRESET（ch2）を Low に保つ
    flash.unprotect()
    flash.write(0, image)                         # 読み直し → 消去 → 書き込み → ベリファイ（J-Flash の Auto）
```

- 端子の既定は JTAG の線に合わせる: SCK = ch0（TCK）、MOSI = ch1（TDI）、MISO = ch6（TDO）、CS# = ch3（TMS）。
  ch1・ch3・ch6 はプルアップなので、離した CS# は High、Flash が無ければ MISO は 1（JEDEC ID が FF → 「応答が無い」）。
- open: hold の端子を駆動 → ABh（省電力から起こす）→ 9Fh（JEDEC ID）→ 5Ah（SFDP の基本表: 容量・ページ・消去の種類・
  QE の規則・番地のバイト数）。SFDP が無ければ JEDEC の容量のバイト（2^n）と定番の消去（20h・52h・D8h）。16 MB を超えれば
  B7h で 4 バイトの番地にし、close で E9h。
- erase（範囲の両端は最小の消去の倍数。並びに合う一番大きい種類を選ぶ）、erase_chip（C7h）、program（ページの境目で
  分ける。全部 FF のページは飛ばす）、read（chunk バイトずつ）、verify（最初に違う番地）、write（一部だけ書く消去の単位は
  先に読んで残りを保つ → 消去 → 書き込み → ベリファイ。段ごとの時間と速さを返す）。
- unprotect: 状態レジスタの BP・TB・SEC・SRP0 を 0 にする。QE（クアッドの有効）は SFDP の DWORD15 の規則に従って残す:
  010（Macronix）は SR1 の bit 6 を残し、001・101（Winbond 等）は SR2 を 35h で読んで 2 バイト目に書き戻す（1 バイトの
  書き込みで SR2 が消える型があるため）。Macronix に 35h を送らない（QPI に入るため）。SFDP が無ければ会社の番号で決める。
  SST26（BFh）は 98h（一括解除）も送る。WP# が Low で SRP0 が 1 なら外れないので注意を返す。
- 機器固有の知識（QE の規則・SST26 の 98h）はすべてプラグインの中にある。本体・FPGA は SPI の汎用の転送だけ。

## 6. 速さの見積もり（実機なし。USB の往復の仮定から）

前提: CONTROL の 1 往復（512 バイトの OUT + 508 バイトの IN）= 200〜250 µs（DAP_Info の往復の実測 188 µs から）。
1 往復 = 126 状態 = 63 ビット ≈ 7.9 バイト。デーモンの API の 1 呼び出しの手間 0.5〜1 ms。

| 操作 | 見積もり |
|---|---|
| 読み出し・ベリファイ | 31〜39 KB/s（1 MB で 27〜33 秒、16 MB で 7〜9 分） |
| ページの書き込み（256 B） | PP 260 バイト = 34 往復 ≈ 7〜8.5 ms + WREN・状態の確認で API 4 回 ≈ 3〜5 ms → 11〜14 ms / ページ = 18〜23 KB/s |
| 1 MB の write（消去 + 書き込み + ベリファイ） | 消去 2〜8 秒（64 KB × 16）+ 書き込み 45〜57 秒 + ベリファイ 27〜33 秒 ≈ 1.5 分 |

J-Link（J-Flash SPI）はおおむね数百 KB/s〜1 MB/s 程度なので、1/10〜1/30。日常の設定データ・ブートローダ・数 MB の
イメージには足りる。速さが要るなら §8。

## 7. 実機で確かめる手順（HIL）

`gateware/tests/hil/test_spiflash_hw.py`（`pytest -m hil --hil`、さらに `FPROBE_SPIFLASH=1`。書き換えてよい Flash の
最後の 64 KB を使う）。**3.3 V の SPI NOR Flash に限る**（Primer の I/O は 3.3 V。1.8 V の Flash はつながない）。

| Flash の端子 | J12 のピン（pmod1、ロジアナの ch） |
|---|---|
| CLK | J12-5（pmod1[0]、ch0、TCK） |
| DI（IO0） | J12-6（pmod1[1]、ch1、TDI） |
| /CS | J12-8（pmod1[3]、ch3、TMS） |
| DO（IO1） | J12-11（pmod1[6]、ch6、TDO） |
| VCC・/WP・/HOLD | J12 の 1〜4 番の 3.3 V（シルクで確かめる） |
| GND | J12 の GND（信号線の隣に） |

- 順序: **Primer の USB をつないでから** Flash の基板をつなぐ。Flash の VCC は Primer の 3.3 V から取り、別の電源から
  給電しない（電源の無い Flash の入力に 3.3 V をかけない・逆流させない）。電圧の範囲は 2.7〜3.6 V の品種であること。
- 基板に載った Flash（マイコンが同じ線を使う）なら、マイコンを nRESET で止める: J12-7（pmod1[2]、ch2）→ マイコンの
  nRESET、`hold={2: 0}`。マイコンの電源は基板のもの（基板の電源を先に入れ、Flash の VCC に J12 の 3.3 V はつながない）。
  基板の電圧が 3.3 V であることを先に `probe.voltage` で確かめる。
- 手順: 1) JEDEC ID が FF / 00 でない、SFDP の容量が品種と合う。2) unprotect → 最後の 64 KB に乱数を write（verified）。
  3) 読み出しの速さを表示（見積もり 31〜39 KB/s）。4) 化けたら stretch = 1, 2 と上げて差を見る（ロジアナで ch0・ch1・
  ch3・ch6 を同時に記録すると波形で確かめられる）。

## 8. 残したこと

- FPGA の SPI マスタ（案 D）: 1 バイト / 状態にして読み 10 倍以上。ケイパビリティで有無を切り替え、ロジアナと同じ
  端子を使う形で。資源（B22）の整理の後に。
- ホストの先送り（EXCHANGE の要求を 2 つ以上応答を待たずに出す）: 往復の待ちを重ねて 1.5〜2 倍の見込み。
- Dual / Quad の読み（IO2・IO3 を使う）・1.8 V の Flash（レベル変換が要る。U39）。
- 実機での確認（§7）。

## 9. 結果

- ビルド（`python -m fprobe_gw build fprobe`、2026-10-19）: 全クロックでタイミングを満たした（sys_clk Fmax 101.3 MHz /
  100.3 MHz、ulpi_clk 64.4 / 60 MHz）。EX1998 なし。
- 資源（部品ごとの合成、本流の直近のビルドと比べて）: control_plane 350 → 359 LUT（+9）、csr_gpio 21 → 22 LUT（+1）、
  レジスタの増加 0。全体は配置配線で Logic 17,061 / 20,736（83%）、CLS 96%（本流の直近 17,095・95%。比べた本流の
  ビルドは同じ版とは限らないので、全体の差は誤差の内）。
- 試験: cocotb（control_plane の EXCHANGE 4 件、生成器の write_to、fprobe_core の gpio.io）、pytest host
  （SPI の全 mode × 読みの遅れ 2 通り × 語長、API、プラグイン 17 件）。
