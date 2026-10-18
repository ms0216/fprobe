# R5: ETM（命令トレース）の調査

日付: 2026-10-19。対象の台帳: U34（パラレルトレースの受信）・U35（解読と応用）・U36（事後の命令履歴と逆方向の調査）。
要件は `docs/reports/2026-10-18-gap-analysis.md` の U34〜U36 の行。調査だけで、実機・デーモン・USB 機器には触っていない。

## 0. 結論（先に）

1. **追加購入なしで ETM を始められる。手持ちの Raspberry Pi Pico 2（RP2350）が最適のターゲット。**
   RP2350 の Cortex-M33 は ETMv4.2 の命令トレースを持ち、TPIU の 4 ビット DDR のトレース端子が **GPIO1〜5（Pico 2 の
   ヘッダの 2・4・5・6・7 番）** に出る（RP2350 データシート §3.5.7、表 3 の F9）。さらに、配線なしでトレースを
   チップの SRAM に DMA で溜める「トレース FIFO」もある（§3.5.7.2）。
2. **Nucleo F401RE ではパラレルトレースは取れない。** STM32F401 のコアに ETM はあるが、TRACECLK / TRACED0〜3
   （PE2〜PE6）は LQFP64（Nucleo の STM32F401RET6）に出ていない（ST の公式のピンデータ、§2.2）。ギャップ分析の
   「STM32F401 は 4 ビットのトレース端子を持つので配線で可能な見込み」は**誤り**で、台帳 U34 の「STM32F401 の
   TRACED0〜3 をジャンパ線でつなぐ（要確認）」は成り立たない。XIAO nRF52840 もトレース端子（と SWO）がヘッダに無い。
   Pico（RP2040）は ETM も MTB も無い。
3. Primer 側は **J14（pmod0）に空きが 6 本あり、その中の T7 が大域クロック入力（GCLKT_3）**。TRACECLK = T7、
   TRACEDATA0〜3 = T8・P8・R8・T6 で 5 本取れる（すべて 3.3 V のバンク 3）。
4. 段階: (0) **ゲートウェアなし**で、既存のロジアナで低速のトレースを生で取る・トレース FIFO を SWD で読む →
   (1) ホストの解読器（TPIU のフレーム解除・ETMv4 / ETMv3.5・ELF から命令列の再構成）→ (2) 専用の受信回路と
   別の構成 `fprobe_trace` → (3) U35 の応用 → (4) U36 の事後解析。合計の見積もり 17〜20 日（§6）。
5. 購入は**急がない**。ETMv3.5（Cortex-M3/M4、現場で最も多い）の確認用に、段階 2 が済んでから STM32F4 ディスカバリ
   （STM32F407、LQFP100）を 1 枚足すのが最も安く確実（§5）。

なお全体設計 `2026-10-15-fprobe-architecture-design.md` §1.3 は「ETM 並列トレース」を**非目標**にしている。段階 2
（ゲートウェアの受信回路）に進むには、利用者がこの非目標を改める判断が要る（台帳 U34 の記述どおり）。段階 0・1 は
ホストだけなので、この判断の前でも進められる。

---

## 1. 受け方: TPIU のパラレルのトレース端子を FPGA で受ける

### 1.1 信号と規格

- TPIU（Trace Port Interface Unit）は ETM・ITM（と DWT）のトレースを**フォーマッタ**で 16 バイトのフレームに
  多重化し、TRACECLK と TRACEDATA[n-1:0]（Cortex-M は n = 1・2・4）に出す。データは **TRACECLK の両エッジ**で
  変わる（DDR）。TRACECTL は外部の端子に出ない構成が普通で、Cortex-M の 4 ビットの端子は 5 本だけ。
- フォーマッタのフレーム（CoreSight Architecture Specification v3.0、TPIU / フォーマッタの章。RP2350
  データシートはトレース取り込み装置の要件として同仕様の **§D3.3.3「Decoding requirements for Trace Capture
  Devices」** を引いている）:
  - 16 バイト = 1 フレーム。偶数バイトは「ID の変更」か「データ」（ビット 0 で区別）、最後のバイト（15）が
    偶数バイトの補助ビットを持つ。ID がトレース源（コア 0 の ETM、コア 1 の ETM、ITM…）を分ける。
  - **フレーム同期 FSYNC = 0x7FFF_FFFF**（32 ビット）、**半同期 HSYNC = 0x7FFF**（16 ビット。端子が空いている
    ときの詰め物）。受け手は FSYNC を探してフレームの境目とニブル（4 ビット）の並びを決める。
- 帯域 = 4 ビット × 2 エッジ × TRACECLK。RP2350 は **TRACECLK = clk_sys / 2 に固定**（最大 150 MHz → 75 MHz、
  600 Mbit/s = 75 MB/s。データシート §3.5.7.1）。STM32F4 も TRACECLK = HCLK / 2（168 MHz → 84 MHz）。

### 1.2 FPGA の受信回路の要件（段階 2）

| 項目 | 要件・方針 |
|---|---|
| クロック | TRACECLK を**大域クロックの入力ピン**で受け、専用のクロックドメイン `trace` にする（sys 100 MHz での過剰標本化は 75 MHz DDR に対して足りない） |
| DDR の取り込み | 各データ線を I/O の **IDDR**（Gowin のプリミティブ。ラッパー越しに使う。`rtl/boards/` に置く）で取り込み、1 周期に 8 ビット |
| 位相 | TPIU はクロックのエッジでデータを変える（エッジ揃え）ので、サンプル点を 1/4 周期ずらす。候補は (a) rPLL に TRACECLK を入れて 90° 移相（GW2A-18 の PLL は 4 個、今は sys と DDR3 で 2 個使用。ラッパー `gowin_rpll.vhd` がある）、(b) 入力の IODELAY で調整（GW2A にあるが、段数・刻みは Gowin UG289 で要確認）。最初は (a) |
| 同期 | `trace` ドメインでニブルを 32 ビットに並べ、8 通りのずれの中で FSYNC（0x7FFF_FFFF）を探して揃える。揃ったらフレーム単位で扱い、**FSYNC・HSYNC だけのフレームは捨てる**（空きの時間に帯域とメモリを使わない） |
| ドメインの横断 | 非同期 FIFO（BSRAM）で sys へ。75 MHz × 8 ビットは 32 ビット幅にすれば 18.75 M 語/s で余裕 |
| 記録 | DDR3 のリング（ロジアナの深いバッファ `la_deep_buffer` の部品を流用）。128 ビットの拍に詰め、一定の拍ごとに **fprobe の 64 ビットのタイムベース**（ロジアナ・DAP のイベントと同じ時計）を入れた見出しの拍を挟む。これで命令トレースをロジアナの波形・DAP の出来事と同じ時間軸に並べられる（解析力 = 最優先の目的） |
| 止め方 | 連続ストリーミング（`stream`）と、**事後解析用のリング**（クロストリガ・DAP の「停止」イベントで N 拍後に止める）の 2 つ。後者が U36 の核 |
| 帯域 | 書き込み: 最悪 75 MB/s（端子の全帯域）。DDR3 の実測は書き込み 504 MB/s（M8）なので十分。PC への吸い出しは実測約 40 MB/s（M8 の読み出し）なので、連続ストリーミングは平均がそれ以下のとき。端子が全帯域で埋まると 128 MB は約 1.7 秒 |
| 申告 | ケイパビリティの TLV に TRACE（ビット幅・最大クロック・バッファ）を足し、PC は申告を見て動く（移植性の原則） |

**拍への詰め方は 2 案**ある: (i) `la_deep_buffer` の**イベント用リング**（128 ビットの `ev_data`）に流す（今ある経路。
ただしイベントと領域を共有する）、(ii) 3 つ目の領域「バイト列のリング」を足す。仕様書で決める（推奨は (ii)。
トレースの量はイベントと桁が違い、片方の溢れがもう片方を消さないようにする）。

**信号品質**: 75 MHz DDR（1 ビット 6.7 ns）はジャンパ線では厳しい。最初は RP2350 の clk_sys を下げて（TRACECLK も
下がる。プロセッサの命令の量も同じ比で減るので帯域の要求も下がる。§3.5.7.1）確かめ、線は 10 cm 以下、各信号の隣に
GND、送り側に直列抵抗（数十 Ω）を推奨。J12 の SWD で 25 MHz の二重計数があった経験（`swd-25mhz-design`）から、
75 MHz は**実機で上限を測るまで約束しない**。

### 1.3 Primer のどのピンで受けるか

`gateware/fprobe_gw/boards/primer20k.py` と `docs/boards/tang-primer-20k-dock.md` の資源表から:

| ヘッダ | 状況 |
|---|---|
| J12（pmod1） | 8 本すべて使用中（SWCLK・TDI・nRESET・SWDIO・予備・UART RX・SWO・UART TX）。**残りは無い** |
| J14（pmod0） | P9 = 補助 Pico の UART（`aux`）、T9 = WS2812 と共用（高速信号に不可）。**T8・P8・R8・T7・T6・P6 の 6 本が空き** |
| J6（pmod2）・J5（pmod3） | 全部空き（LCD の FPC と共用。モジュールは外す） |

大域クロック（GCLK）の入力ピンを、手元の SoM の回路図（`Tang_Primer_20K` コア基板の回路図の端子名）で調べた結果:

- **T7 = IOR29A = GCLKT_3**（J14 の 10 番。bank 3、3.3 V）。R8 = IOR29B（同じ対の負側。回路図の文字列では
  GCLKT_3 と書かれているが対の C 側と思われる）。
- 他の GCLK 端子（H11 = 27 MHz 発振器、M11 = UART TX、N10 = SDIO、K15 ほか）は使用中か、ヘッダに出ていない。
- J5・J6 の 16 本には GCLK 端子が無い（B12・C12・B13・A14 は LPLL1 の入力・帰還の端子 IOL7/IOL8 で、PLL に直接
  入れる案もあり得るが、要確認）。

**提案の割り当て（J14、すべて bank 3）**: TRACECLK = **T7**、TRACEDATA0 = T8、TRACEDATA1 = P8、TRACEDATA2 = R8、
TRACEDATA3 = T6（P6 は予備）。プルは NONE（電源の無いターゲットにプルアップで電流を流さない）、ヒステリシスは
タイミングを測ってから決める。J14 は補助 Pico（P9）と同居するが、ピンは重ならない。

### 1.4 FPGA の資源（B22）と別の構成

- 標準の `fprobe` は合成 88%・CLS 97% で、+1,066 で配線できなかった前例がある（B22・B17）。受信回路の見込みは
  **約 600〜900 LUT**（ニブルの整列と FSYNC 探索 150〜300、非同期 FIFO の制御 約 100、拍への詰め・タイムベース
  200 前後、CSR 150〜250。IDDR は I/O の中で LUT を使わない）。標準に足すのは無理。
- B22 の仕組み（`fprobe_gw/designs.py` の構成・ケイパビリティ `DEBUG`・`probe.design.switch` と自動の書き替え）
  に、3 つ目の構成を足す:

| デザイン | SWD | JTAG | DAP サンプラ | 故障注入 | ロジアナ・クロストリガ・イベント | トレース受信 |
|---|---|---|---|---|---|---|
| `fprobe`（標準） | ○ | × | ○ | ○ | ○ | × |
| `fprobe_jtag` | ○ | ○ | × | × | ○ | × |
| **`fprobe_trace`（案）** | ○ | × | × | × | ○ | ○ |

  サンプラ（約 1,000）・故障注入（約 320）を外せば受信回路が入る見込み（`fprobe_jtag` が JTAG 700 を足して
  配線できている）。**ロジアナは残す**（命令トレースと波形を同じ時計で並べるのが本プロジェクトの強み）。
- ホスト側は今の `PROTOCOL_DESIGNS = {"swd": ..., "jtag": ...}`（線の種類で選ぶ）を、**要る機能の集合で選ぶ**形に
  一般化する（`trace` が要る操作なら `fprobe_trace` へ）。`probe.design.switch` はそのまま使える。
- 移植性: 受信回路のコアはボード非依存（IDDR・PLL はラッパー）。小さい FPGA ではビット幅 1・2 やバッファの小ささを
  申告するだけで、同じ PC ソフトが動く。

---

## 2. 代わりの手段と手持ちのターゲット

### 2.1 手段の比較

| 手段 | 何が取れるか | 長所 | 短所 |
|---|---|---|---|
| ETM + TPIU のパラレル端子（本命） | 全命令の流れ（分岐の成否・間接分岐の行き先・例外）、タイムスタンプ | 長く取れる（DDR3 128 MB、ストリーミング）、ターゲットの動作に影響しない | 端子と配線が要る、ゲートウェアが要る |
| ETB / ETF（チップの中のトレース用 RAM） | 同上を数 KB | 配線不要、SWD で読める | 容量が小さい。**手持ちのチップには無い**（STM32F4・nRF52840・RP2040・RP2350 とも無し。STM32H7 などの上位品にある。個別の型番は未確認） |
| MTB（Cortex-M0+ / M23 / M33 の Micro Trace Buffer） | 分岐の元と先の対を SRAM に | 配線不要、簡単な形式 | **RP2040 は無し**（RP2040 データシート §2.4.1.2「Micro Trace Buffer (MTB) is not implemented」）、**RP2350 も無し**（§3.7.2「MTB: no MTB trace」）。持つチップの例: NXP Kinetis KL25Z・Microchip SAMD21 など（未所持） |
| **RP2350 のトレース FIFO + DMA** | ETM・ITM の TPIU の流れそのもの（32 ビット幅）を SRAM へ | 配線不要。DMA の帯域は TPIU より大きい（§3.5.7.2）。形式はパラレル端子と同じなので**解読器を共用**できる | ターゲットの SRAM と DMA チャネルを 1 つ使う（アプリと競合し得る）。DMA のリングは最大 32 KB（DMA の `RING_SIZE` は 2〜32768 バイト）、ENDLESS モードで CPU 無しに回る |
| SWO の DWT の PC サンプリング（既存の `profile.*`） | 一定間隔の PC（統計） | 既にある、1 本の線 | 順序・全命令は分からない（カバレッジは近似）。**RP2040・RP2350 には SWO が無い**（RP2350 は ITM / DWT も TPIU 経由） |
| 停止によるサンプリング（既存の方法 sampled） | 一定間隔の PC | 何も要らない | 遅い、止める（XIAO の BLE のように止められない相手がある） |
| ETM を SWO（非同期）に通す | 命令トレース（極めて低帯域） | 線 1 本 | TPIU の SWO モードでフォーマッタを使えるかはチップ次第で**未確認**。fprobe の SWO の実績は 2 Mbaud で、命令トレースには足りない。採らない |

### 2.2 手持ちのターゲット

| ターゲット | ETM | トレース端子 | ETB / MTB | SWO | 判定 |
|---|---|---|---|---|---|
| **Pico 2（RP2350A）** | **ETMv4.2**（ETM-M33、命令のみ。§3.7.3.4。2 コアそれぞれ） | **GPIO1 = TRACECLK、GPIO2〜5 = TRACEDATA0〜3**（機能 F9。表 3）。Pico 2 のヘッダの 2・4・5・6・7 番、GND は 3・8 番 | MTB 無し。代わりにトレース FIFO + DMA | 無し | **最適。購入不要** |
| Nucleo F401RE（STM32F401RET6、LQFP64） | ETM-M4（ETMv3.5）はコアにある | **出ていない**。TRACECLK / TRACED0〜3 は PE2〜PE6 で、LQFP64 にはポート E が無い（ST の `STM32_open_pin_data` の `STM32F401R(D-E)Tx.xml` に PE と TRACE の信号が 1 つも無い。`STM32F401V(D-E)Tx.xml`（LQFP100）には PE2 = SYS_TRACECLK、PE3〜PE6 = SYS_TRACED0〜3 がある） | 無し | PB3（既存で使用中） | パラレルトレース不可。SWO の PC サンプリングまで |
| XIAO nRF52840 | ETM あり（Cortex-M4） | nRF52840 のトレース端子は TRACECLK = P0.07、TRACEDATA0 = P1.00（SWO と兼用）、TRACEDATA1〜3 = P0.12・P0.11・P1.09（1 と 2 の順は Nordic の資料の版で入れ替わっている）。XIAO の D0〜D10 は P0.02・P0.03・P0.28・P0.29・P0.04・P0.05・P1.11〜P1.15 で、**どれも出ていない** | 無し | P1.00 が出ていないので **SWO も無い** | 不可（チップの足に直接つなぐのは現実的でない。しかも利用者のキーボードの実機） |
| Pico（RP2040） | 無し（Cortex-M0+） | — | MTB 無し（§2.4.1.2） | 無し | 不可（停止によるサンプリングのみ） |

注: Pico 2 の GPIO1 は pico-sdk の既定の UART0 RX。トレースを使うアプリは UART0 を GP0/1 以外に移す（ターゲット側を
合わせる。設計原則「ターゲットを改造して汎用の機能に合わせる」どおり）。

### 2.3 RP2350 の設定の道筋（ホストから）

- トレースの部品（タイムスタンプ生成器 0x06000、ATB ファンネル 0x07000、TPIU 0x08000、CTI 0x09000）は SW-DP の
  ADIv6 のデバッグ用アドレス空間にあり、**システムバスの窓 `CORESIGHT_PERIPH_BASE` からも見える**（§3.5 の図 10。
  TPIU = `CORESIGHT_TPIU_BASE` 0x40148000）。よってコアの AHB-AP 経由の普通のメモリ書き込みで設定できる見込み
  （実機で要確認）。ETM-M33 は各コアの PPB にある。
- トレース FIFO: `CORESIGHT_TRACE_BASE` 0x50700000（CTRL_STATUS・TRACE_CAPTURE_FIFO）、DMA の DREQ 53。
  DMA から読むには `ACCESSCTRL.CORESIGHT_TRACE` の DMA ビット（ビット 6、既定 0）を立てる。ACCESSCTRL の書き込みは
  上位 16 ビットに 0xacce が要り、Secure・特権のプロセッサかデバッガからだけ書ける（§ACCESSCTRL の該当表）。
- GPIO1〜5 の機能選択を F9（TRACE）にし、パッドの駆動を強め・スルーを速くする。
- これらは**RP2350 固有の手順**。設計原則により、FPGA には入れず、ホストの汎用の API（メモリ書き込み・CoreSight の
  ROM テーブルの走査 `coresight_id.py`）の上の**プラグイン**にする。ETM / TPIU / ファンネルの設定自体は CoreSight の
  標準なので本体（`trace/` の汎用部）に置ける。

---

## 3. 解読

### 3.1 層

```
DDR3 / FIFO のバイト列
  → [1] 端子の整列（ニブル順・FSYNC 探し）            … 段階 2 では FPGA、段階 0 ではホスト
  → [2] フォーマッタの解除（16 バイトのフレーム → トレース ID ごとのバイト列）
  → [3] パケット解析（ETMv4 / ETMv3.5 / ITM）
  → [4] 命令の流れの再構成（ELF・実機のメモリと突き合わせ）
  → [5] 応用（一覧・検索・カバレッジ・呼び出し回数・事後の命令履歴）
```

ITM は既に `itm.py` がある。[2] の出力から ITM の ID の流れを `itm.py` に渡せば、Pico 2 で既存の ITM / DWT の機能
（`swo.decode`・`profile.*` の method itm）がトレース端子経由で使える（RP2350 は SWO が無いので副次的に大きい）。

### 3.2 ETMv4（RP2350 の ETM-M33 = ETMv4.2）

出典: Arm IHI 0064「ETMv4 Architecture Specification」、Arm「CoreSight ETM-M33 Technical Reference Manual」。
主なパケット（命令トレースのみ。M-profile にデータトレース・投機は無い）:

| パケット | 役割 |
|---|---|
| A-sync（0x00 × 11 + 0x80） | パケット境界の同期 |
| Trace Info | 同期点の状態（サイクル計数の有無など） |
| Trace On | トレースの再開（途切れの印） |
| Address（Long / Short / Exact match、IS0/IS1） | 間接分岐の行き先・同期点のアドレス。前回との差分で短くなる |
| Atom（形式 1〜6） | 分岐（waypoint）ごとの E（成立）/ N（不成立）を詰めたもの。命令トレースの大半 |
| Exception（M-profile の形式）・Exception Return | 例外の種類と戻り先 |
| Timestamp・Cycle Count | 時刻（RP2350 は 2 コア共通のタイムスタンプ生成器） |
| Event | ETM のリソースの出来事（CTI 等） |

### 3.3 ETMv3.5（Cortex-M3/M4 の ETM-M3/M4。STM32F4・nRF52840 等）

出典: Arm IHI 0014「Embedded Trace Macrocell Architecture Specification ETMv1.0 to ETMv3.5」。
A-sync（0x00 × 5 + 0x80）・I-sync（アドレス + 情報バイト）・**P-header**（E / N の atom の数を詰めたもの）・
Branch Address（1〜5 バイト、例外の情報を含み得る）・Timestamp・Cycle Count・Trigger・Ignore など。
考え方は v4 と同じ（同期点 → atom で分岐の成否 → 間接分岐だけアドレス）だが、形式は別物で、両方の実装が要る。

### 3.4 ELF との突き合わせ（命令の流れの再構成）

1. 同期点のアドレスから、プログラムの像（ELF の実行可能な区画。RAM に写して動く関数はスナップショット U06 の
   RAM、RP2350 のブート ROM は実機から読む）を順に逆アセンブルする（既存の `disasm.py` は capstone で Thumb-2 を
   解読し、ELF でも実機のメモリでも読める）。
2. 分岐（waypoint）に当たるたびに atom を 1 つ消費する。E なら分岐先へ（直接分岐は命令から計算、間接分岐
   （BX・POP {pc}・LDR pc 等）は次のアドレスパケットから）、N なら次の命令へ。
3. 例外パケットで割り込みへ飛び、例外の戻りで戻る。
4. 出力は「アドレス範囲の列 + 時刻」。ここから命令の一覧・関数の呼び出しの木・行ごとの実行回数・分岐の成否
   （命令単位のカバレッジ）を作る。

限界（U36 に効く）: M-profile の ETM は**命令のみ**でデータ値が無い。逆方向のレジスタの再構成は、可逆な命令を
戻すか、スナップショット（U06）から命令列どおりに前へ模擬するしかなく、周辺機器から読んだ値は分からない。
**命令の履歴は確実、レジスタ・メモリの過去の値は「推定」と明示**する。DWT のデータトレース（比較器 4 個、ITM 経由）
で特定の変数だけ補える。

### 3.5 既存の OSS

| OSS | 内容 | Python からの使い方 | 判断 |
|---|---|---|---|
| **OpenCSD**（Linaro、BSD-3、最新 1.8.3） | ETMv3.5・**ETMv4（M-profile は v4.4 まで）**・PTM・ETE・ITM・STM の解読、フォーマッタの解除（FSYNC / HSYNC 対応）、命令の流れの再構成（メモリの読み手を登録）。C API あり。VS2022 のビルド用ディレクトリあり。未対応: V8.1-M の branch-future、ETMv4 のデータトレース | C API を ctypes で（`opencsd_c_api.h`）。Windows では DLL を自前でビルドして同梱する必要。Rust の `opencsd-sys` が C API を包んだ前例 | **正解の照合（試験の神託）に使う**。本体の依存にはしない（ネイティブの DLL を Windows で配る負担。設計を自分で理解・試験できなくなる） |
| **Orbuculum**（BSD-3） | SWO / TPIU / ITM の解読、`orbmortem`（事後解析。`-p` は MTB か ETM35 が既定、ETM4 は 2.2.0 で「始まり」）。Linux・macOS・Windows | 外部プログラムとして | ETM4 が未成熟。形式（Orbuculum のネットワーク形式）の互換出力は将来の候補（`ORB-07`） |
| **ORBTrace**（ECP5、BSD-3） | 1/2/4 ビット DDR のパラレルトレースを受ける FPGA のプローブ（`traceIF.v`）。README に STM32F427 で 4 ビット・ITM 約 12.5 MB/s の例 | — | **受信回路の先例**として読む（`docs/reference/prior-art-fpga-probes.md` にも記載） |

**推奨**: ホストの解読器は **Python で自作**（`host/fprobe/trace/`。層 [2]〜[4]。capstone は既存の依存）。試験は
(a) 仕様の例から作ったバイト列の単体試験、(b) 実機の取り込みを OpenCSD の `trc_pkt_lister` に通した結果との
一致（OpenCSD は開発時だけ。試験は OpenCSD が無ければ skip）。速度が要る部分（[2] と atom の展開）は、遅ければ
後で numpy か C 拡張に置き換える（Python のロジアナの RAW 復号が約 1,250 万サンプル/秒だった実績から、数 MB の
トレースなら Python で間に合う見込み。数百 MB を即時に、は間に合わない）。

---

## 4. ゲートウェアなしで今すぐ試せること（段階 0）

### 4.1 既存のロジアナで低速のトレースを生で取る

- 既存のロジアナは J12 の 8 本を 100 MS/s で取る（M7・M8）。SWD のときは J12 の 1（TDI）・4（予備）・5（UART RX）・
  6（SWO）・7（UART TX）の **5 本が信号として空く**（標準構成は JTAG の PHY が無いので TDI を駆動しない見込み。
  要確認。UART ブリッジ・SWO は使わない）。TRACECLK と TRACEDATA0〜3 をここに入れる。
- 1 ニブルに 3〜4 サンプル欲しいので、ニブルの長さ ≥ 30〜40 ns → **TRACECLK ≤ 約 12.5 MHz → RP2350 の clk_sys ≤
  約 25 MHz**（pico-sdk の `set_sys_clock_khz` で下げる。トレースの量もそれに比例して減るので端子は溢れにくい）。
- RLE（変化点圧縮）で記録し、ホストで TRACECLK のエッジの間の中央でデータを読んでニブル列にする → 層 [1] 以降へ。
- これで**FPGA を 1 行も変えずに**、配線・RP2350 の設定・解読器の全体を実機で確かめられる。クロックは遅いが、
  命令トレースの正しさの確認には十分。

### 4.2 RP2350 のトレース FIFO を SWD で読む（配線ゼロ）

- ホストが SWD で ETM・ファンネル・TPIU・トレース FIFO・DMA（ENDLESS、書き込み側のリング 32 KB、DREQ 53）を設定し、
  止めたとき（またはその場で）SRAM のリングを読む。**「止まる直前の 32 KB ぶんの命令履歴」**が配線なしで得られ、
  U36 の最初の形になる。
- 代償はターゲットの SRAM 32 KB と DMA チャネル 1 つ（リンカで確保する。ターゲット側を合わせる）。
- RP2350 固有の手順なのでプラグイン（§2.3）。

---

## 5. 購入するなら

| 候補 | ETM | 端子 | 長所 | 短所 |
|---|---|---|---|---|
| （購入なし）**Pico 2** | ETMv4.2 | GPIO1〜5 がヘッダ | 手持ち。トレース FIFO もある。2 コア・共通タイムスタンプで解析の題材が豊富 | ETMv3.5 は確かめられない。TRACECLK が clk_sys/2 固定（下げるには CPU も下げる） |
| **STM32F4 ディスカバリ（STM32F407VG、LQFP100）** | ETMv3.5（ETM-M4） | PE2〜PE6（台帳 U34 の記述ではヘッダに出ている。ST の UM1472 で要確認）。ST-LINK 付き | 安い（数千円の見込み、価格は未確認）。現場で最も多い Cortex-M3/M4 の ETMv3.5 を確かめられる。TRACECLK = HCLK/2 で CPU を遅くしても取れる | 段階 2 まではいらない |
| Nucleo-144 F429ZI 等 | ETMv3.5 | PE2〜PE6 の出方は**未確認** | Nucleo の作法に揃う | 端子の確認が先 |

**推奨**: 今は買わない。段階 0〜2 を Pico 2 で進め、ETMv3.5 の解読器（段階 1 の後半）を実機で確かめる段で
STM32F4 ディスカバリを 1 枚買う。Nucleo F401RE の延長（同じ STM32F401 の LQFP100 品の基板）を探すより、ETM の
端子がヘッダに出ていると知られている F407 のディスカバリが確実。

---

## 6. 実装の段階と見積もり

| 段階 | 内容 | 主な成果物 | 前提 | 見積もり |
|---|---|---|---|---|
| 0a | ロジアナで生のトレースを取り、ホストで端子の整列とニブル化 | `trace/capture_la.py` | Pico 2 の配線（J12 の 5 本 + SWD）、clk_sys ≤ 25 MHz のテスト用ファーム | 1 日 |
| 0b | RP2350 の ETM / TPIU / トレース FIFO + DMA の設定と読み出し | プラグイン `rp2350_trace` | SWD で Pico 2 につながること（pyOCD の RP2350 対応・ADIv6 の確認） | 1〜2 日 |
| 1 | 解読器: フォーマッタの解除、ETMv4（M-profile）、ETMv3.5、ELF からの再構成、ITM への分配。OpenCSD との照合試験 | `host/fprobe/trace/`（`tpiu.py`・`etmv4.py`・`etmv3.py`・`reconstruct.py`） | 0a か 0b の実データ | 4〜5 日 |
| 2 | 受信回路（IDDR・PLL の位相・FSYNC 整列・空フレームの破棄・タイムベースの見出し・DDR3 のリング・ストリーミング）、構成 `fprobe_trace`、ケイパビリティ、要る機能での構成の自動選択、CSR、HIL（clk_sys を上げながら上限を測る） | `rtl/trace/`、`designs.py`、`fprobe_map.py`、`caps.py`、`bitstreams.py` | 非目標 §1.3 を改める判断、J14 の配線 | 5〜6 日（75 MHz のタイミングと信号品質に不確かさ） |
| 3 | U35 の応用: 命令の一覧と検索、命令単位のカバレッジ（分岐の成否）、呼び出し回数付きのプロファイル、トレースの開始・停止の条件（ETM のアドレス比較器・ViewInst）、ロジアナ・イベントとの同じ時間軸 | API・MCP の道具 | 1・2 | 3 日 |
| 4 | U36: 停止のイベントでリングを止める事後の命令履歴、命令単位の逆ステップ、スナップショットからの前向きの模擬によるレジスタの推定（限界の明示） | API・MCP の道具 | 3、U06 | 3 日 |

合計 17〜20 日（台帳の U34〜U36 の合計 9 日より多い。台帳は解読の 2 版（v3.5 と v4）・段階 0・構成の追加を
数えていなかった）。段階 0〜1 だけで、Pico 2 では「止まる直前の命令履歴」（U36 の最小形）まで届く。

## 7. 分からないこと（実機・資料で確かめる）

1. RP2350 のトレース部品を AHB-AP 経由の `CORESIGHT_PERIPH_BASE` の窓で設定できるか、ACCESSCTRL の書き込みが
   デバッガから通るか（データシートの記述からは可能な見込み）。pyOCD / fprobe の RP2350（Arm 側）の接続の状況。
2. 標準構成で J12 の TDI が本当に高インピーダンスか（段階 0a）。
3. GW2A の IDDR・IODELAY の詳細と、T7（GCLKT_3）から rPLL へ入れたときのジッタ・位相の調整範囲（Gowin UG289・
   UG286）。R8 が GCLKC_3 か（回路図の文字列の誤記の可能性）。
4. ジャンパ線で 75 MHz DDR が通るか（実機で上限を測る）。
5. ETM-M33 の 1 命令あたりの平均トレース量（実測で決める。ここでは数字を置かない）。
6. STM32F4 ディスカバリ・Nucleo-144 の PE2〜PE6 の出方と価格。
7. TPIU を SWO モードにしたときフォーマッタ（= ETM の多重化）を使えるチップがあるか（使えても帯域が足りないので
   採らない）。

## 出典

- RP2350 データシート（RP-008373-DS）: §3.5（図 10 デバッグの構成）、§3.5.7.1〜3（トレース・トレース FIFO・
  レジスタ）、表 3（GPIO の機能 F9 = TRACECLK / TRACEDATA0〜3）、§3.7.2（Cortex-M33 の構成「ETM: ETM trace」
  「MTB: no MTB trace」）、§3.7.3.4（ETMv4.2）、ACCESSCTRL の CORESIGHT_TRACE、DMA の CTRL.RING_SIZE と ENDLESS。
  https://pip.raspberrypi.com/documents/RP-008373-DS-rp2350-datasheet.pdf
- RP2040 データシート: §2.4.1.2（「Micro Trace Buffer (MTB) is not implemented」）。
  https://datasheets.raspberrypi.com/rp2040/rp2040-datasheet.pdf
- ST の公式ピンデータ（CubeMX の元データ）: `mcu/STM32F401R(D-E)Tx.xml`（LQFP64、PE 無し・TRACE 信号無し）、
  `mcu/STM32F401V(D-E)Tx.xml`（LQFP100、PE2 = SYS_TRACECLK、PE3〜6 = SYS_TRACED0〜3）。
  https://github.com/STMicroelectronics/STM32_open_pin_data
- nRF52840 のピン割り当て（Nordic Product Specification の pin.html、DK のトレースの節。TRACEDATA1/2 の順が版で違う）:
  https://docs.nordicsemi.com/r/bundle/ps_nrf52840/page/pin.html 、
  https://devzone.nordicsemi.com/f/nordic-q-a/21626/nrf52840-trace-pins-mapping
- XIAO nRF52840 のピン（Seeed wiki）: https://wiki.seeedstudio.com/ja/XIAO_BLE/
- OpenCSD（README の対応表・変更履歴）: https://github.com/Linaro/OpenCSD
- Orbuculum（README、orbmortem の対応形式）: https://github.com/orbcode/orbuculum 、ORBTrace: https://github.com/orbcode/orbtrace
- Arm の仕様（本文は読んでいない。名前と役割のみで引用。節番号は実装時に確かめる）: IHI 0064（ETMv4）、IHI 0014
  （ETMv1.0〜v3.5）、CoreSight Architecture Specification v3.0（§D3.3.3 は RP2350 データシートの引用による）、
  CoreSight ETM-M33 TRM。
- Primer の端子名・GCLK: Sipeed の Tang Primer 20K コア基板・Dock 3714 の回路図（テキスト化したもので確認）、
  `gateware/fprobe_gw/boards/primer20k.py`、`docs/boards/tang-primer-20k-dock.md`。
- 本リポジトリ: B22 の仕様 `docs/superpowers/specs/2026-10-18-b22-fpga-area-design.md`、M8 / M8c（DDR3 の帯域・
  ストリーミング）、`gateware/rtl/la/la_deep_buffer.vhd`、`host/fprobe/disasm.py`。
