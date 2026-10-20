# PYNQ-Z2（Zynq-7020）への移植 設計

## 1. 目的と、なぜ

fprobe を PYNQ-Z2 でも動かす。ねらいは 2 つ。

- **上位版（LAN だけで動く大きい FPGA 版）**: Zynq-7020 の PL は約 53,000 LUT で Primer 20K の約 2.5 倍。Primer では
  資源が足りず構成を分けている機能（標準・JTAG・専用の入力・R1 の一本化）を 1 つの構成に収められる（B22 の解消）。
- **移植の仕組みの実証**: 全体設計（2026-10-15）が約束した「コア RTL は無変更、ボード層とホストリンク層の差し替えだけで
  別ボード」を、Gowin 以外のベンダー（Xilinx）で初めて確かめる。ここで作る「プロセッサが PC とのつなぎを受け持ち、
  FPGA とフレームを受け渡す」形は、Tang Nano 4K（Cortex-M3 内蔵）・Shrike-Lite（RP2040）・Nano 9K（BL702 / 外付け
  Pico）の安価版にもそのまま使う（§7）。

利用者（2026-10-19）: 「Tang などとのスケーラビリティを意識しつつ移植を開始」。検討のターゲットは XIAO nRF52840
（hhkb-split の右手、ZMK）を実践的にデバッグする構成にする（一石二鳥）。

## 2. ゴール・非ゴール・してはいけないこと

**ゴール（この仕様の完了条件）**
1. PC のデーモン・画面・MCP・Python を **変えずに**、`--eth <PYNQ の IP>` で PYNQ-Z2 の fprobe を使える
   （Primer の Ethernet 版と同じ形）。
2. XIAO に対して、Primer と同じことが LAN 越しにできる: SWD の接続（attach）・止めずに読む（変数・RTOS）・
   モニタ方式のブレークポイントとステップ・クロストリガの出力によるキーの自動操作・ロジアナ（解読を含む）。
3. コア（`gateware/rtl/core` ほか、ボード非依存の RTL）に **PYNQ のための変更を入れない**（入れる場合はボード非依存の
   汎用の改良としてで、Primer の試験も通す）。
4. ビルド・書き込みの道具（`fprobe_gw`）が Gowin と Vivado の両方を扱う。PL の書き込みは PYNQ の Linux 経由
   （ふだんは JTAG を使わない。JTAG は復旧と ILA の調査に限る、§8）。

**非ゴール（この仕様ではやらない。後段）**
- PC と PYNQ を USB でつなぐ口（ARM の Linux の USB ガジェットで CMSIS-DAP v2 を出す）。第 2 段階で別の仕様にする。
- デーモンを PYNQ の上で動かすこと（PC で動かす。2026-10-19 利用者と合意）。
- PS の DDR3 を深いバッファに使うこと（AXI HP）。第 1 段は PL の BRAM のバッファで動かし、深いバッファは §5.3 の
  第 2 段。
- Tang Nano 4K・Shrike-Lite の実装そのもの（§7 で形だけ決める）。

**してはいけないこと**
- PYNQ のためにコアや PC 側に分岐（`if board == "pynq"`）を入れる。違いはボード層・ホストリンク層・メモリ層・道具の
  中に閉じ、PC 側はケイパビリティの申告だけを見る。
- ベンダーの暗号化された IP を使う（PS7 は Zynq の固い回路の設定なので例外。AXI の相互接続・FIFO 等は自作の VHDL）。
- ふだんの書き込みに JTAG を使う。書き込みは Linux 経由にし、JTAG（Vivado の hw_server）は復旧と ILA の調査に限る。JTAG を使うときは必ずシリアル 1234-TUL で口を選び、IDCODE（Zynq-7020）を照合する（Primer と取り違えない、§8）。
- 黙って欠ける: UDP・FIFO で落ちたものは、今の Ethernet 版と同じく seq と数で報告する。

## 3. 全体の形

```
PC（変更なし）                    PYNQ-Z2
 デーモン・画面・MCP     LAN     ┌─ PS（ARM、Linux）────────────┐   ┌─ PL（FPGA）──────────────────────────┐
 fprobe.eth_link.EthLink ──UDP──┤ fprobe-relay（新規、C）        │AXI│ zynq_frame_link（新規）↔ frame_mux（既存）│
   ch | seq | payload            │  UDP 63920 ⇄ フレームの FIFO  ├───┤      ↔ fprobe_core（既存・無変更）      │
                                 │  （mmap した AXI-Lite の窓）   │   │      ↔ PMOD JA（SWD・行・列）・JB（入力）│
                                 └────────────────────────────────┘   └──────────────────────────────────────┘
```

- **線上の形式は Ethernet 版と同じ**（`docs/superpowers/specs/2026-10-17-m11-ethernet-link-design.md` §2: UDP の
  ポート 63920、`ch(1) | seq(1) | payload(0..512)`）。PC からは「Ethernet でつながったボード」に見える。
- PS の中継（`fprobe-relay`）は、データグラムとフレームを 1 対 1 で受け渡すだけ。チャンネルの意味は知らない。
  - PC → PL: データグラムの `ch` と `payload` を、受け取りの FIFO に 1 フレームとして書く（`seq` は捨てる）。
  - PL → PC: 送りの FIFO から 1 フレーム（ch・長さ・payload）を読み、通し番号 `seq` を付けて、最後に受けた送り主へ
    送る（Ethernet 版の udp_stack が FPGA の中でしていたことを、ARM がする）。
- PL の中で Ethernet 版の `udp_stack`・`rmii_*` が担っていた役を、PS の Linux の UDP と中継が担う。`frame_mux` から
  コア側は Ethernet 版と同じ。

## 4. PL 側（ゲートウェア）

### 4.1 ボード層 `rtl/boards/pynq_z2/`
- トップ `pynq_z2_fprobe_top.vhd`: PS7 の包み・クロック・リセット・`zynq_frame_link`・`frame_mux`・`fprobe_core`・
  ピンの選択（`pin_select`、Primer と同じ）。
- **PS7**: Vivado のブロック設計を TCL で作り、包み（`pynq_z2_ps7_wrapper`）の裏に隠す。使うのは M_AXI_GP0
  （AXI-Lite の窓）、FCLK_RESET0（リセット）。第 2 段で S_AXI_HP0（深いバッファ）。PS の DDR 等の設定は、PL を後から
  Linux で書き込むので動作に関係しないが、TUL の PYNQ-Z2 のボードの設定（preset）を使って正しく入れる。
- **クロック**: PL の 125 MHz の発振器（H16）→ MMCM（`xilinx_mmcm` の包み、Gowin の `gowin_rpll` と同じ口）→
  sys 100 MHz（Primer の 100.29 MHz と違う。ケイパビリティの sys_clock_hz・timebase_hz で申告するので PC は合わせる）。
  AXI の窓（M_AXI_GP0）の ACLK には sys をつなぐ（GP0 の ACLK は PL のどのクロックでもよい。クロックまたぎを作らない。
  2026-10-19 の作る人の確認で決めた）。FCLK は使わない。
- ベンダーのプリミティブ（MMCM・IOBUF・BUFG 等）は `rtl/boards/pynq_z2/` の包みの中だけで使う。

### 4.2 ホストリンク `rtl/hostlink/zynq_frame_link.vhd`（新規・ボード非依存の部分はここ）
- AXI4-Lite のスレーブ（32 ビット）。中身は 2 本のフレームの FIFO と状態のレジスタ:
  - 受け取り（PC → PL）: 中継が `RX_DATA` に語を書き、最後に `RX_COMMIT`（ch・長さ）を書くと 1 フレームとして
    frame_mux へ流す。空きの語数は `RX_FREE` で読む（足りなければ中継が待つ = 押し戻し。欠けない）。
  - 送り（PL → PC）: frame_mux が出したフレームを FIFO に溜め、中継が `TX_HEAD`（ch・長さ、無ければ 0）を読み、
    続けて `TX_DATA` を長さ分読む。
  - `STATUS`: 版・FIFO の深さ・溢れの数（PL の中で溢れたら数える。中継は報告に出す）。
- コア側の口は `eth_link` と同じ（frame_mux の ch / payload / last）。frame_mux は Serial・Ethernet と共通のまま。
- 速さ: 第 1 段は中継が AXI-Lite の語を 1 つずつ読み書きする（C で mmap、Zynq-7000 で数十 MB/s の見込み）。
  ロジアナの連続ストリーミング（〜25 MB/s）が足りなければ、第 2 段で AXI DMA（S_AXI_HP、自作の DMA）に替える。
  レジスタの形はそれを見越して「フレーム単位」にしておく。
- cocotb の試験: AXI4-Lite の BFM（cocotbext-axi があればそれ、無ければ小さな自作）で、両方向のフレーム・押し戻し・
  溢れの数・最大長（512）・長さ 0 を確かめる。

### 4.3 メモリ層
- **第 1 段**: 深いバッファは持たない（`la_depth` = 内蔵 BRAM の深さ、Nano 9K と同じ申告）。Zynq-7020 の BRAM
  （約 630 KB）のうち、資源の許す範囲で深くする（目安 128 K サンプル × 16 ビット = 256 KB）。
- **第 2 段**（§5.3）: `mem_port`（128 ビットの拍・コマンド）→ AXI4 のマスタ（S_AXI_HP0、64 ビット）の包み
  `axi_mem_port.vhd`（ボード非依存）を作り、PS の DDR の予約した領域（中継が Linux の CMA / reserved-memory で確保し、
  物理番地を CSR で渡す）を深いバッファにする。コアの `la_deep_buffer` 以降は無変更。

### 4.4 構成（`fprobe_gw/designs.py`）
- `fprobe_pynq`: SWD・JTAG・サンプラ・故障注入・グリッチ・パルス幅・Manchester・RTS/CTS・専用の入力（JB）を全部入れる
  1 つの構成（入りきらなければ報告して決める）。ケイパビリティの `board = "PYNQ-Z2"`、`host_link = eth`（PC から見た
  形）。
- 構成の切り替え（`probe.design.switch`）は Primer の USB の組だけのまま（PYNQ は 1 つで全部入る）。

## 5. PS 側（中継 `host/pynq/fprobe-relay/`）

### 5.1 中身
- C の小さなプログラム（PYNQ の Linux の gcc でビルド）。
  - UDP のポート 63920 を開き、§3 の受け渡しをする。
  - AXI-Lite の窓は `/dev/mem` の mmap（PL の番地は M_AXI_GP0 の 0x4000_0000 から）。可能なら UIO にする（root で
    /dev/mem を開くより安全。どちらにするかは実装で決めてよい）。
  - 送り主の結びつけ・seq・溢れの数の報告は Ethernet 版の決まり（§2 の m11）に合わせる。
- 起動: systemd のサービス。起動時に PL を書き込み（§5.2）、中継を始める。PL を書き直したら中継も始め直す。
- 中継の正しさの試験: PC 上で、偽の AXI の窓（共有メモリ）と偽の PL（Python）で、データグラム ↔ フレームの往復・
  押し戻し・seq の付け方を確かめる（pytest から C の中継を起動する）。

### 5.2 PL の書き込み（`fprobe_gw program --board pynq_z2`）
- Vivado で `.bit` を作り、PYNQ に SSH で送り、Linux の fpga_manager（PYNQ の `fpgautil`、または `pynq.Overlay`）で
  書き込む。ふだんは JTAG を使わない（復旧と ILA の調査だけ、§8）。
- SSH の宛先・ユーザは設定（環境変数か `~/.fprobe/boards.toml`）。PYNQ の既定（`xilinx` / `xilinx`）は使い始めに
  変えるよう案内する。
- `--persistent` は「起動時に書き込む `.bit` を PYNQ の上に置き換える」になる（Flash は無い）。

**実機で分かったことと、変えたこと（2026-10-20。利用者の確認待ち）:**
- Linux 経由（`pynq.Bitstream`・fpga_manager に直接）で書くと、どちらも Linux が固まった。JTAG（Vivado の hw_server）で
  書いたときは固まらなかった。そこで当面は JTAG で書く（`fprobe_gw.toolchain.pynq_loader`。口はシリアル `1234-TUL`、
  IDCODE が Zynq-7020 であることを照合）。CLAUDE.md の「ふだんは Linux 経由」とは逆なので、利用者に判断を仰ぐ。
- JTAG で書いた後、PS と PL の間のレベルシフタ（SLCR `LVL_SHFTR_EN` 0xF8000900）は無効（0）のままで、EMIO・AXI の
  信号が PS に届かない。有効（0xF）にする必要がある。
- レベルシフタが有効なまま PL を書き直した・PS7 を持たない回路でシフタを有効にした後、Linux が固まった（PS7 を持つ回路
  では固まらなかった）。PL から PS への信号（割り込み線等）が定まらないためと考える。Linux 経由が固まったのも、書いた後に
  fpga_manager がシフタを有効にするので同じ原因の可能性がある（PS7 を持つ回路なら Linux 経由も使えるかもしれない。未確認）。
- よって書き込みの手順は: PYNQ の上の補助 `/usr/local/bin/fprobe-pl before`（中継を止め、「fprobe の回路が載っている」印
  `/run/fprobe/pl-design` を消し、`fprobe-relay --isolate` でシフタを無効に）→ JTAG で書く → fprobe の回路（PS7 と
  zynq_frame_link を持つ）のときだけ `fprobe-pl after <id>`（印を置き、中継を起動。中継がシフタを有効にし、ID を確かめて
  から受け渡しを始める）。補助はパスワード無しの sudo で呼べる（据え付けは `python -m fprobe_gw pynq install`）。
- 中継は印が無ければ PL を読まずに終わる（起動直後の未構成の PL・点滅の回路を読むと ARM が固まるため）。
- PL の使わない端子・キーの行の線は必ず引き下げる。引き下げの無い入力の端子に XIAO の行の線をつないだら、浮いた線が隣の
  列の走査を拾い、キーが勝手に入った。

### 5.3 第 2 段の深いバッファ
- 中継（または補助のスクリプト）が CMA から連続した領域（目安 64〜128 MB）を確保し、物理番地と大きさを PL の CSR に
  書く。PL は `axi_mem_port` で S_AXI_HP0 からそこへ書く。ケイパビリティの mem_bytes で申告する。

## 6. 道具（`fprobe_gw`）

- **道具の抽象**: 今の `toolchain/gowin.py`・`programmer.py`（Gowin 専用）と並べて `toolchain/vivado.py` を作り、
  ボードの記述（`Board`）が使う道具（`gowin` / `vivado`）を持つ。`build` の流れ（入力の要約 `inputs.sha256`・3 つの
  配置の試し・タイミングの読み・EX1998 相当の検査・版の印）は共通にし、道具ごとの違いだけを差し替える。
  - Vivado: 非プロジェクトの流れの TCL（`read_vhdl -vhdl2008`、ブロック設計の生成、`synth_design`、`opt`・`place`・
    `route`（試しは `-directive` を変えて 3 つ）、`report_timing_summary`、`write_bitstream`）。場所は
    `C:\AMDDesignTools\2025.2\Vivado\bin\`（環境変数 `FPROBE_VIVADO_HOME` で変えられる）。
  - 駆動されない配線の検査: Vivado の警告（例 Synth 8-3295 / 8-3848 等、「undriven」）を EX1998 と同じく失敗に。
  - タイミング: `report_timing_summary` の WNS・TNS・各クロックを、今の `TimingSummary` の形に読む。
- **ボードの記述** `fprobe_gw/boards/pynq_z2.py`: デバイス（xc7z020clg400-1）・ピン（PMOD JA・JB・LED・ボタン、
  ピンの番号は TUL の公式の master XDC と照らす）・クロック（125 MHz）・書き込みの方法（Linux 経由）。
- 生成物（`python -m fprobe_gw gen`）は今と同じ（ケイパビリティ・CSR・ホストの定数）。Vivado の XDC はビルドのときに
  ボードの記述から作る（Gowin の .cst・.sdc と同じ考え）。

## 7. スケーラビリティ（Tang 系との関係）

この移植で、ボードの「層」を次の 4 つの組み合わせとしてはっきりさせる。PC 側はケイパビリティだけを見る。

| ボード | 道具 | ホストリンク（PC とのつなぎ） | FPGA とのつなぎ | 記録のメモリ | 位置づけ |
|---|---|---|---|---|---|
| Tang Primer 20K | Gowin | USB HS（FPGA 直結の ULPI） | — | DDR3（FPGA 直結） | 標準 |
| Tang Primer 20K（Ethernet 版） | Gowin | UDP（FPGA の RMII） | — | DDR3 | 標準（LAN） |
| **PYNQ-Z2** | **Vivado** | **UDP（ARM の Linux）** | **AXI-Lite のフレームの FIFO** | **BRAM → PS の DDR（AXI HP）** | **上位（LAN だけで動く）** |
| Tang Nano 9K | Gowin | USB FS（BL702 / 外付け Pico） | UART / SPI のフレーム | PSRAM | 安価 |
| Tang Nano 4K | Gowin | USB FS / UART（内蔵 Cortex-M3） | **AHB のフレームの FIFO（PYNQ と同じ形）** | HyperRAM | 安価 |
| Shrike-Lite | （ForgeFPGA の道具） | USB FS（RP2040） | SPI のフレーム | BRAM | 最小 |

- **共通にするもの**: 「プロセッサが PC とのつなぎを受け持ち、FPGA とはフレーム（ch・payload）を受け渡す」形
  （**橋渡しのホストリンク**）。PYNQ の `zynq_frame_link`（AXI-Lite）・Nano 4K の AHB・Nano 9K / Shrike の SPI / UART は、
  どれも frame_mux の口に同じフレームを渡す。PS の中継（`fprobe-relay`）の受け渡しの部分（データグラム ↔ フレーム）は、
  マイコン（RP2040・BL702・Cortex-M3）の中継のひな形にする。
- **道具の抽象**（Gowin / Vivado）は、Shrike-Lite の ForgeFPGA の道具を足すときの差し込み口にもなる。
- メモリ層は `mem_port` の口のまま（DDR3 直結・AXI HP・PSRAM・BRAM を差し替える）。

## 8. ハードウェアと安全

- **CLAUDE.md の決まりを改めた**（2026-10-19 利用者の承認）: PYNQ-Z2 は fprobe 専用の移植先。PL はふだん Linux 経由で
  書く。JTAG（Vivado の hw_server）は Linux が固まったときの復旧と ILA の調査に使い、シリアル 1234-TUL と IDCODE の
  照合を必ず通す。中継は起動時に自動で動かし、既定のパスワードは変える。
- 電圧: PYNQ の PMOD は 3.3 V。XIAO も 3.3 V。5 V の線は無い。PMOD の 3.3 V（6・12 番）には何もつながない。
- 配線（2026-10-20 につなぎ替えた後。7 本すべてを通信で確認済み）。PYNQ-Z2 の基板の PMOD には抵抗がある: PMOD の
  0・1・4・5 番（JA1・JA2・JA7・JA8）= 引き下げ、2・3・6・7 番（JA3・JA4・JA9・JA10）= 引き上げ。FPGA の端子のプルも
  これに合わせる。キーの行（High で押される・浮くと拾う）は引き下げの穴につなぐ。最初は Primer と同じ並びで JA10（引き上げ）に
  D6 をつなぎ、キーが押され続けた。

| 役目 | PMOD JA | ロジアナの ch | 基板の抵抗 | XIAO |
|---|---|---|---|---|
| SWCLK | JA1 | 0 | 引き下げ | 裏のパッド SWCLK |
| TDI（JTAG のとき） | JA2 | 1 | 引き下げ | 2 列目のスイッチの列の側（読むだけ） |
| nRESET | JA3 | 2 | 引き上げ | 裏のパッド RST |
| SWDIO | JA4 | 3 | 引き上げ | 裏のパッド SWDIO |
| 予備（U の行） | JA7 | 4 | 引き下げ | D5（1 kΩ） |
| UART RX（J の行） | JA8 | 5 | 引き下げ | D6（1 kΩ） |
| SWO | JA9 | 6 | 引き上げ | （空き） |
| UART TX | JA10 | 7 | 引き上げ | （空き） |
| GND | JA5 / JA11 | — | — | GND |

- キーの自動操作はクロストリガの出力 ch4・ch5（条件 = 列 ch1 が High の間）。ch4・ch5 は引き下げのある出力の候補。
- PMOD JB は、ロジアナの専用の入力（ch10〜15。Primer の J14 と同じ役）。XIAO の 595 の SPI（キーの走査）等を見るのに
  後で使う。
- つなぐ順: 両方の電源を入れる → GND → 信号。外すときは信号 → GND → 電源。

## 9. 段階

| 段 | 内容 | 確かめ方 |
|---|---|---|
| P0 | 道具: Vivado の流れ・ボードの記述・PS7 の包み・LED を点ける最小の回路を Linux 経由で書き込む | 実機で LED、`fprobe_gw program --board pynq_z2` |
| P1 | `zynq_frame_link` と中継で、PC から CONTROL（ケイパビリティ・CSR）が読める | cocotb・中継の試験・実機で `probe.info` |
| P2 | `fprobe_pynq` の全機能（BRAM のロジアナ）。XIAO で §2 のゴール 2 | 実機の確認（XIAO） |
| P3 | 深いバッファ（AXI HP・PS の DDR） | cocotb・実機 |
| P4（別の仕様） | USB ガジェットで CMSIS-DAP v2（標準のツール） | — |

## 10. 決めたこと（作る人の確認から。私の判断は「判断」と書く）

- **配線は移す**（同時につながない）: XIAO の線は Primer の J12 から外し、PYNQ の JA につなぐ（2026-10-19 に利用者へ
  案内済み）。2 台が同じ線を駆動しないため。Primer の実機の試験（ゴール 3）は Nucleo を J12 の SWD に戻して行う。
- **P2 の合格**（判断）: ロジアナは BRAM（流し続け無し）でよい。R1（ストリームへのイベントの一本化）は外部メモリが
  前提なので P3 から。DAP の速さの目標は「Primer の Ethernet 版と同等以上」（DAP の往復の時間と、読み出しの KB/s で
  比べる）。届かなければ、自作の DMA を P2 に前倒しする。
- **補助（Pico の電圧の計測）**（判断）: PYNQ の構成には第 1 段では入れない（aux = 無し と申告）。Pico は Primer の
  J14 のまま。
- **PYNQ の Linux の今の様子**（版・gcc・fpgautil・UIO・IP・SSH）: 接続の後、P0 の最初に調べ、PL の書き込みの経路
  （fpgautil の .bit.bin か pynq.Overlay の .hwh）と CMA の確保の方法を決める。
- **固まったときの復旧**: まず JTAG（シリアルと IDCODE を照合）から PL を書き直す・PS をリセットする。それでも駄目なら
  利用者に電源を入れ直してもらう。固まりにくくするため: AXI-Lite のスレーブは cocotb で「必ず応答する」ことを検査（どの番地・どの
  状態でも BVALID / RVALID を返す）、中継は PL の状態の読みに時間の上限を設け、異常なら中継を止めて報告する。
- **内蔵ロジアナの深さ**（判断）: 今は全体の定数（LA_DEPTH = 8192）。構成ごとに変えられる generic にする改良は、
  ボード非依存の汎用の改良（ゴール 3 の例外）として入れ、Primer の試験も通す。
- 中継の受け取りの FIFO は、PC の `EthLink.dap_packet_count = 4`（2048 バイト）を受け止める深さ以上にする。

## 11. 作る人に任せること

- 中継の AXI の窓の開き方（`/dev/mem` か UIO）、systemd の単位の細部。
- AXI4-Lite のレジスタの番地の割り付けと FIFO の深さ（目安: 各 4 KB 以上）。
- Vivado の 3 つの試しの `-directive` の選び方。
- BRAM のロジアナの深さ（資源を見て決める）。
- PS7 の設定を TUL のボードのファイルから取るか、必要な項目だけを TCL で書くか。
