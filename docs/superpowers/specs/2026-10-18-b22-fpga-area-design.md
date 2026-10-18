# B22: FPGA の面積を減らす（機能を減らさない削減 + 標準 / JTAG の 2 構成）設計

## 1. ゴールと完了条件

製品デザイン fprobe（Tang Primer 20K、GW2A-18）は合成 17,946 / 20,736（88%）、配置配線の CLS 97% で、機能を
足すと配線できない（B17 の初版は +1,066 で配線できなかった）。2026-10-18 に利用者が次を決めた。

- (A) 機能を減らさない削減: CSR の読み出しの選択をまとめる・DAP_Info を BSRAM の ROM へ・重い計算の回路の共有。
- (B) Primer のビットストリームを「標準（SWD）」と「JTAG」の 2 構成に分け、PC のツールがターゲットに合わせて自動で
  書き替える。

| 完了条件 | 検証 |
|---|---|
| (A) の 3 つで機能・CSR の見え方・DAP の応答が変わらない | cocotb（既存の試験がそのまま緑） |
| (A) の 3 つそれぞれで合成の大きさが減る | 合成（変更ごとに測る） |
| fprobe_core / debug_port のジェネリック JTAG・INJECTOR で JTAG の PHY と経路、故障注入を外せる。外した機能はケイパビリティから消え、DAP_Connect（JTAG）は失敗で答える | cocotb（外した構成の試験） |
| 2 構成 `fprobe`（標準）と `fprobe_jtag` がビルドでき、それぞれ合成の大きさが減る | 合成 |
| ホストがケイパビリティから今の構成を知り、要る構成でなければ書き込んでからつなぎ直す | pytest（偽の書き込み・再列挙）+ HIL（親のセッション） |
| API に構成を知る・切り替える道具（`probe.design.get` / `probe.design.switch`）がある | pytest（仕様と MCP の道具） |

## 2. 測った内訳（2026-10-18、本流の最後のビルドの階層別の表）

| 部品 | LUT+ALU | 備考 |
|---|---|---|
| usb_device | 3,425 | 対象外 |
| dap_processor | 2,720 | config 500（DAP_Info の文字列を含む）、divider 208、transfer 627、sequence 481、packet 346 |
| primer20k_ddr3（IP） | 2,077 | 対象外 |
| la_deep_buffer | 1,927 | |
| debug_port | 1,598 | swd_phy 827、jtag_phy 702 |
| dap_sampler + csr_sampler | 996 | B17 が作り直し中（触らない） |
| uart_bridge | 565 | divider 214 |
| CSR（decoder 400 + 各バンク） | 約 1,760 | decoder は窓ごとの優先順の選択 |
| swd_injector | 324 | |

## 3. 機能を減らさない削減

### 3.1 CSR の読み出しの選択をまとめる

今: 各バンクが応答（rdata・err）をレジスタに保持し、csr_decoder が「覚えた窓（hits）」の応答を優先順の選択
（窓 16 × 32 ビット）で選ぶ。

新: CSR バスの約束を 1 つ足す: **スレーブは ack を出す周期以外、rdata と err を 0 にする。** これで decoder の
選択は窓ごとの応答の論理和で済む（hits による選択が要らない）。

- 生成器（`fprobe_gw/csr/vhdl.py`）: バンクは毎周期 rdata・err を 0 に戻し、要求を受けた周期だけ値を入れる
  （同期リセット付きのレジスタになり、LUT は増えない）。
- 手書きのスレーブ（caps_rom・la_mem_csr・csr_absent・内蔵ロジアナが無い窓）も同じ約束に合わせる。
- csr_decoder: ack / err / rdata を全窓の論理和にする。窓に当たらない要求への応答（miss）は今のまま。
- csr_bus_pkg の説明に約束を書く。

### 3.2 DAP_Info の文字列と表を BSRAM の ROM へ

今: `dap_info_pkg` の関数 info_length / info_char（ID × 位置の case）が論理になっている。

新: `fprobe_gw/dap_info.py` が 512 バイトの ROM（16 枠 × 32 バイト）を生成する。枠 k = DAP_Info の ID k
（0x01〜0x0F の文字列）。枠の先頭バイト = NUL 込みの長さ（無い ID は 0）、続いて文字と NUL。
dap_cmd_config は ROM を同期読み出し（1 周期の遅れ）で読む（Gowin は pROM = BSRAM に推論する。
USB のディスクリプタ ROM と同じ書き方）。ID 0x10 以上の文字列（無い）は長さ 0。数値の ID（0xF0・0xFD〜0xFF）は今のまま。

DAP_Info の Capabilities（ID 0xF0）は構成で変わる（JTAG の bit1）ので、パッケージの定数ではなく dap_cmd_config の
ジェネリック JTAG から作る。

### 3.3 割り算器の共有

今: udiv（32 ビット、復元型）が dap_processor（SWJ_Clock・SWO のボーレート。中で共有済み）と uart_bridge
（CDC-ACM のボーレート → 分周比）に 1 つずつある。

新: fprobe_core が 1 つの割り算器 `udiv_shared`（udiv + 調停）を持ち、2 つの使い手で共有する。

- 名前は `udiv_shared`（udiv と調停を 1 つに。`rtl/common/udiv_shared.vhd`）。
- 使い手の約束（要求の保持）: `div_req` を 1 にし、`div_num` / `div_den` を保ったまま `div_done`（1 周期）を待つ。
  done の次の周期に req を下ろす。
- 調停: 空いていれば使い手 0（DAP）を優先し、取り込んだ使い手に done を返す。同時の要求は待たせるだけで失わない
  （今の uart_bridge は割り算中のボーレート変更を捨てていた。これも直る）。
- dap_processor・uart_bridge はジェネリック `SHARED_DIVIDER : boolean := false` を持つ。false なら今のとおり中に
  udiv を持つ（単体の試験・他のデザインはそのまま）。true なら div_* ポートで外の割り算器を使う。

## 4. 構成での取捨

### 4.1 ジェネリック

| ジェネリック | 置き場所 | false のとき |
|---|---|---|
| `HAS_JTAG`（ポート jtag と名前が衝突するため。VHDL は大小文字を区別しない） | fprobe_core → dap_processor・debug_port | jtag_phy を作らない。DAP_Connect（port = 2）は失敗（0）で答える。DAP_Info の Capabilities から JTAG を消す。JTAG のチェーンの設定は残す（JTAG_Configure は受けるだけ） |
| `INJECTOR` | fprobe_core | swd_injector を作らない。xtrig の inject_status.result は 3（構成に無い）、route.to_inject は何もしない |

故障注入の CSR（inject_*）は xtrig の窓の中にあり、窓ごとバスエラーにはしない（クロストリガのほかの機能は使える）。
無いことはケイパビリティと inject_status.result = 3 で分かる。既存の XTRIG = false では今のとおり窓ごとバスエラー。

### 4.2 ケイパビリティ

TLV に種類 14 `DEBUG`（u32 ビットマップ）を足す: bit0 = JTAG、bit1 = 故障注入（割り込み書き込み）。
**この TLV が無い古いデバイスは両方ある**（今までのビットストリームはすべて持っていた）。生成するパッケージ
`design_caps_pkg` に定数 `JTAG`・`INJECTOR` を足し、トップがジェネリックに渡す（申告と実装が食い違わない）。

### 4.3 デザイン

| デザイン | SWD | JTAG | DAP サンプラ | 故障注入 | ほか（ロジアナ・クロストリガ・多段トリガ・イベント・UART・SWO・補助） |
|---|---|---|---|---|---|
| `fprobe`（標準） | ○ | × | ○ | ○ | ○ |
| `fprobe_jtag` | ○ | ○ | × | × | ○ |

トップ（primer20k_fprobe_top）・ピン・IP は共通。ケイパビリティだけが違う（`design` の名前と DEBUG・FEATURES）。
`fprobe_jtag` も SWD を持つ（外しても JTAG の構成は配線できる大きさで、SWD のターゲットにも書き替えずに使える）。

ほかのデザイン（memtest・fprobe_serial・fprobe_eth・fprobe_nano9k）は今までどおり全部持つ（JTAG・INJECTOR = true）。

### 4.4 ビルドの成果物の置き場所

`build_best` は配置を変えた版を `build/<design>/p<n>/` に作り、採用した版を `build/<design>/top.fs` に写す
（成功したビルドの印 `inputs.sha256` も一緒に）。ホストはこの決まった場所を見る。

## 5. ホストの自動の書き替え

### 5.1 構成の対応（ゲートウェアが正）

`fprobe_gw/designs.py` の `PROTOCOL_DESIGNS = {"swd": "fprobe", "jtag": "fprobe_jtag"}` を `fprobe gen` が
ホスト定数 `DESIGN_FOR_SWD` / `DESIGN_FOR_JTAG` に出す。書き替えるのは、今のデザインがこの組のどちらかのとき
（USB の製品デザイン）だけ。ほかのデザイン（Ethernet 版等）では書き替えず、要る機能が無ければ誤りにする。

### 5.2 要る線の決め方

| 場面 | 要る線 |
|---|---|
| `target_connect` に jtag_chain がある / target が Cortex-A・ARM11 系（名前に cortex_a・arm11・arm1176・bcm283 等） | JTAG |
| それ以外の `target_connect` | SWD |
| `target_connect` の target = "auto" | SWD で判別。つながらず、今の構成に JTAG が無ければ JTAG の構成に替えて判別し直す |
| 低レベルの JTAG の道具（jtag_scan 等） | JTAG |

### 5.3 流れ（`fprobe.bitstreams` と Probe の `_ensure_design`）

1. 書き替えが要らなければ何もしない: JTAG は今の構成に JTAG があれば（ケイパビリティの DEBUG）、SWD は今が標準なら。
   SWD はどちらの構成にもあるが、JTAG の構成から標準（サンプラ・故障注入がある）に戻す。
2. 組の外のデザインで JTAG が無い・ビットストリームが無い → 誤り（理由とビルドの手順を言う）。自動の書き替えが
   切ってある（`FPROBE_AUTO_SWITCH=0`）とき、JTAG が要るのに無ければ誤り（`probe_design_switch` を言う）、SWD はそのまま。
3. Probe を閉じる（ターゲット・ロジアナ・ストリーム等。FPGA が入れ替わるので全部切れる）。
4. `python -m fprobe_gw program <ビットストリーム> --board primer20k` を子プロセスで呼ぶ（Primer の USB シリアル
   番号でポートを引き、IDCODE を照合してから SRAM に書く。既存の安全な道具そのもの）。
5. USB の再列挙を待つ（同じシリアル番号の fprobe が現れ、CONTROL が応答するまで。上限 30 秒）。
6. ケイパビリティを読み直し、`design` が求めたものか確かめる。
7. 元の操作（接続）を続ける。結果に `design_switch`（前後のデザイン・かかった時間）を添える。

ビットストリームの置き場所: 既定は `<リポジトリ>/gateware/build/<design>/top.fs`。環境変数
`FPROBE_BITSTREAMS`（`<dir>/<design>/top.fs` の dir）か、デーモン / MCP の `--bitstreams` で変える。

### 5.4 API

| メソッド | 種類 | 内容 |
|---|---|---|
| `probe.design.get` | read | 今のデザイン・JTAG / 故障注入 / サンプラの有無・切り替えられる構成（線・ビットストリームの場所・有無）・自動の書き替えの有無 |
| `probe.design.switch` | control | design（"fprobe" / "fprobe_jtag"）か protocol（"swd" / "jtag"）を指定して書き替える。未接続のときだけ |

MCP の INSTRUCTIONS に、存在する道具だけを書く（`probe_design_get` / `probe_design_switch`）。

## 6. 試験

- cocotb: csr_decoder（論理和の選択・miss・外した窓）、生成したバンク（ack 以外の rdata = 0）、dap_processor
  （DAP_Info の文字列・Capabilities、HAS_JTAG = false で DAP_Connect が失敗）、udiv_shared（同時の要求・順番）、
  uart_bridge（共有の割り算器で）、fprobe_core を全部の構成・標準・JTAG の 3 つのケイパビリティで（申告と CSR の窓の
  バスエラー、inject_status.result = 3）。
- pytest（host）: ケイパビリティの解析（DEBUG の有無）、要る線の判定、ビットストリームの場所、書き替えの流れ（偽の
  子プロセス・偽の再列挙）、API の仕様・MCP の道具・INSTRUCTIONS。
- HIL（親のセッションが行う）: 7 章。

## 7. 実機で確かめる手順（親のセッション）

1. `python -m fprobe_gw build fprobe` と `python -m fprobe_gw build fprobe_jtag`（それぞれタイミングと EX1998 なし）。
   `gateware/build/<design>/top.fs` と `inputs.sha256` ができる。
2. `python -m fprobe_gw program gateware/build/fprobe/top.fs`。`probe_design_get` が design = fprobe、jtag = false。
3. Nucleo（SWD）に `target_connect`（stm32f401xe）: 書き替えなしでつながる。
4. `target_connect(jtag_chain=[4, 5])`: 自動で fprobe_jtag に書き替わり（結果の design_switch）、JTAG でつながる。
5. `target_connect`（SWD）: fprobe に戻る。`probe_design_switch(protocol="jtag")` も同様。
6. 既存の HIL（`pytest gateware -m hil` の SWD・ロジアナ・イベント・サンプラ）を fprobe で、JTAG の HIL を fprobe_jtag で。
