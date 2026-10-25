# Cortex-A + FPGA 組み込み Linux を fprobe でデバッグする環境（Zynq DUT）

2026-10-24。状態: 草案（利用者の承認待ち）。

## 目的

fprobe（Tang Primer 20K）を、実際の Cortex-A + FPGA の組み込み Linux システムのデバッグに使い、全体の動作を実運用の条件で確かめる。
これまでの fprobe は「動くところまで」で、実運用していない。この環境が最初の実運用になる。

- 解析力（最優先）: A9 の実行と PL（FPGA）の信号を同じ時刻軸に並べ、「なぜ壊れたか」を見つける。
- 副次: 日常のデバッガ（標準ツールで A9 を止める・読む）としても使えること。

## 決定したこと（利用者、2026-10-24）

- **fprobe = Tang Primer 20K。DUT = PYNQ-Z2（Zynq-7020: Cortex-A9 ×2 + PL）。** 外付けのプローブにする。
- PYNQ 自己デバッグ（PL の fprobe が同じチップの PS を見る）は採らない。理由: PS を止めるとホスト通信（PS 上の中継）も止まる、
  PL を書き直すとデバッガが消える、Linux が固まると調べる道具も固まる、DDR を共有する。後で比較用に足すのは可。

## 目標 / 非目標

目標
1. Primer の JTAG から PS の DAP（A9）に入り、停止・再開・レジスタ・メモリを標準ツール（OpenOCD）と fprobe の API で扱える。
2. PL の注目信号を Primer のロジアナで捕捉し、A9 のイベントと共通のタイムスタンプで並べられる。
3. クロストリガ: PL の条件で A9 を止める、A9 のブレークポイントでロジアナを止める。
4. Linux のアプリ・ドライバ（docs/linux.md の ptrace 系）と PL の挙動を 1 つの記録で突き合わせられる。

非目標
- fprobe 本体への Zynq 固有機能の追加（CLAUDE.md の原則。固有の話は DUT 側の設計かプラグイン）。
- PTM トレースの取り込み（将来。EMIO → PL 端子 → Primer。デコードが重い）。
- PYNQ をプローブとして使う移植（休止）。

## 構成

| ボード | 役割 |
|---|---|
| Tang Primer 20K Dock | fprobe 本体 |
| PYNQ-Z2 | DUT（PS: PynqLinux 3.0 / PL: 本書の DUT 回路） |
| Raspberry Pi Pico WH | 補助（ADC。J14 に UART）。変更なし |
| Pi 2B | 第 2 の Cortex-A（比較用）。変更なし |

## 配線

### フェーズ 1: PS の DAP（JTAG）

Primer の J12（DVP ヘッダ。**番号でなく印字のピン名で指定**）→ PYNQ の RPi ヘッダ（番号は Raspberry Pi と同じ）。
出典: docs/boards/pynq-z2.md「PS の DAP の JTAG」、docs/boards/tang-primer-20k-dock.md「fprobe のデバッグコネクタ」。

| 信号 | Primer（印字） | PYNQ ヘッダ | PYNQ の FPGA ピン | 向き |
|---|---|---|---|---|
| TCK | J14 | 23（GPIO11） | W10 | Primer → PYNQ |
| TDI | J16 | 19（GPIO10） | V8 | Primer → PYNQ |
| TMS | M15 | 24（GPIO8） | F19 | Primer → PYNQ |
| TDO | T11 | 21（GPIO9） | V10 | PYNQ → Primer |
| GND | J12 の GND | 25 | — | — |

- 1・17 番（3V3）と 2・4 番（5V）には何も繋がない。
- 手順: 先に両方の USB を挿して電源を入れ、信号線は後から繋ぐ。外すときは信号線が先。
- 読みが化けるとき（Pi 2B では 5 MHz で化け、Primer T12 に一定の線を足すと 10 MHz まで直った）は T12 の線を試す。
- TRST・nRESET は PYNQ に出ていない。リセットは DUT 回路の EMIO GPIO で代用（フェーズ 2）。
- PYNQ 側の前提: 起動時 MIO2 = 1（independent）。この間、基板の USB-JTAG（hw_server）から PS の DAP は見えない（PL の書き込みは可）。
  **JP1 の位置は docs/boards/pynq-z2.md と基板の印字で確認してから設定する（未確認）。**
- PMODA に XIAO / RP2040 の線が付いているときは、DUT 回路を書く前に抜く（行の線を浮かせるとキーが入る）。

**この 5 本は ARM 側（PS の DAP）だけ**で、PL（FPGA）の TAP は含まない。PS の PJTAG は EMIO で PL の端子に出して配線で受ける（independent の起動のとき）。
PL の TAP は基板の USB-JTAG（Vivado）に残り、fprobe は使わない。PL の中を fprobe で見る道はフェーズ 2 の「PL 内部の観測」で、
Primer の製品デザインには受け口が無いため、計画の Task 7b で可否を先に決める（利用者の指摘、2026-10-24）。

### フェーズ 2: PL の観測

| 目的 | Primer | PYNQ | 備考 |
|---|---|---|---|
| ロジアナ | J14 の T8 P8 R8 T7 T6 P6（専用入力 ch10〜15、3.3 V まで） | PMOD JB（DUT 回路が出力） | 構成 fprobe_la |
| PL の UART | J12 の R11（RX）・P11（TX） | DUT 回路の UART（PMOD） | Linux のコンソールとは別系統 |
| PL 内部の観測 | 第 2 の JTAG の口（要設計） | RPi ヘッダ 15・16・18・22 | fprobe_obs コアを DUT に入れる |
| リセット | M14（nRESET） | EMIO GPIO → PMOD | 任意 |

## DUT 回路（私の案。利用者は未承認）

- PS7 + AXI 経由のデータ処理 IP: 信号生成 → FIFO → DMA → Linux のドライバ（またはユーザ空間）が読む。
- 注目信号（AXI ハンドシェイク、FIFO の満杯・空、割り込み、DMA 完了）を JB に出す。
- 起動の途中も端子は定まった値にする（全信号を明示的に駆動、行の線は PULLDOWN）。
- 意図的に不具合を仕込めるレジスタ（例: 時々 FIFO を溢れさせる）を持たせ、「なぜ壊れたか」を fprobe が見つけられるかを試験にする。

## 検証の基準

1. 配線の自動判定（fprobe.wiring）で JTAG が通る。IDCODE と DAP の ID が Zynq-7000 のものと一致する。
2. AP の構成を実測する（AHB-AP の有無は**未確認**。Pi 2B は APB-AP のみで、止めずに読めなかった）。AHB-AP があれば止めずにメモリを読む。
3. OpenOCD（または pyOCD）で A9 の停止・再開・ステップ・メモリ読み書き。Linux が生きたまま 1 コアを止めて再開できる（Pi 2B と同様、止めるのは 1 コアだけ）。
4. PL の信号と A9 のブレークポイントを同じ記録に時刻順で並べ、時刻の誤差を測って数字で出す。
5. 仕込んだ不具合を、fprobe の API の事実（回数・時刻・間隔・最大値と欠け）だけから人または AI が特定できる。**fprobe 本体は判定しない。**

## リスク / 未確定

- **Primer の資源**: fprobe は配線の限界に近い（CLS 90% 台）。標準の構成に JTAG はなく、JTAG は fprobe_jtag、専用入力のロジアナは fprobe_la と構成が分かれている。
  JTAG とロジアナ専用入力を同時に使えるかは未確認。入らなければ JTAG の構成のロジアナ ch（J12 の T12 等）で足りるかを先に調べる。計画の最初の作業とする。
- **JTAG 速度**: Primer の JTAG は Pi 2B で 2 MHz（のち 10 MHz）。Zynq の PJTAG が耐える速度は実測。
- **DDR 共有**: DUT 側は予約領域（uEnv.txt の mem=479M）を既存どおり使うか、DUT 回路では使わない。
- **PL の書き込み手順**: `fprobe-pl before` でレベルシフタを切ってから JTAG で書く（守らないと Linux が固まる）。再起動は電源の入れ直しを前提にする。
- **DUT 用の PL には fprobe_pynq の回路は使えない**（PJTAG の素通しは同じ考え方で別回路にする）。

## 次の作業（計画で詳細化）

1. Primer の構成の調査（JTAG + ロジアナの両立）。
2. DUT 回路の最小版（PS7 + PJTAG 素通し + JB の信号）を作って JTAG が通るところまで（検証 1〜3）。
3. DUT のデータ処理 IP と Linux ドライバ、不具合の仕込み。
4. 時刻の突き合わせとクロストリガ（検証 4〜5）。

## 最初の計画の後の作業（利用者の決定、2026-10-24）

最初の計画（上の 1〜4）をやり切った後に取り組む。今の計画には入れない。

- **PYNQ のカーネルを UPROBES 付きで作り直す。** 現状の PYNQ（6.6.10-xilinx）は FTRACE・KPROBES・UPROBES が無く、アプリの観測は ptrace だけ。
  ptrace は当たるたびに全スレッドを止めるため、DMA・FIFO の競合のように観測で変わる不具合を見るのに向かない。
  手順の案: `/proc/config.gz` で実際の設定を確認 → linux-xlnx で `UPROBES`・`UPROBE_EVENTS` を有効にしてビルド → SD の控えを取って差し替え。
  uprobe は 32 ビット ARM では ARM 命令の関数だけ（題材のアプリは `-marm` でビルドする）。
- **止めずに読む道（道 B）。** 検証 2 で AHB-AP があれば、JTAG で A9 を止めずに物理メモリのリングを読む（docs/linux.md）。

## PL 内部の観測の道の調査結果（計画の Task 7b、2026-10-25）

調べた事実:
- `FEATURE_FPGA_OBS`（`caps.py`）は「大きい FPGA の構成だけ」の機能で、Primer の製品デザイン（fprobe / fprobe_jtag / fprobe_la）は持たない。
  観測エンジン（`obs_jtag_engine`）の口 `obs_tck/tms/tdi/tdo/oe` は `fprobe_core` にあるが、Primer のトップは繋いでいない。
- Primer は fprobe_jtag・fprobe_la とも資源が配線の限界に近く（CLS 90% 台）、エンジンを足した資源は未測定。
- J12 の空き端子は T12・R11・P11・M14 だけで、観測の 4 本（TCK・TMS・TDI・TDO）に足りるが、M14 は nRESET、R11・P11 は UART で、使うと
  機能を食う。J14 の T8 P8 R8 T7 T6 P6（ロジアナの専用の入力）は、専用の入力の無い構成なら空く。

3 案の結論:
- (c) DUT が注目信号を端子に出してロジアナで見る — 今の計画の中身。実機で動いた（2026-10-25）。PL の信号の「外から見える範囲」だけが見える。
- (a) 新しい Primer の構成 `fprobe_obs`（JTAG + 観測、サンプラ・故障注入・専用の入力なし）を作り、J14 の 4 本を観測の JTAG にする — 実現できる見込みがあるが
  資源・配線の測定が要る（構成が 1 つ増える）。DUT 側には `target_ip/fprobe_obs` を入れる。**別の計画にする。**
- (b) JTAG の口の時分割 — エンジンが別なので意味が薄い。採らない。
