# FPGA の内部観測ツールの使い勝手（UI/UX）の比較（2026-10-20 調査）

fprobe の PC ツール（Python ライブラリ・MCP サーバ・Svelte + dockview の画面）が、既存の FPGA の内部観測ツールに
追いつき追い越すための参考。Web の調査だけ（ツールは触っていない）。

## 0. 凡例と調べ方

- **[事実]** 出典（URL）の本文で確かめたこと。版は出典に書いた版。
- **[記憶]** 私の既存の知識で、今回の調査で出典を確かめられなかったこと。正しい可能性は高いが、使う前に確かめる。
- **[推測]** 出典と知識からの推し量り。
- 値段は公式の価格表がほぼ無い（見積もり制）ため、目安は [記憶]/[推測] が多い。
- 「作り直し」= 合成・配置配線のやり直し。「実行中に変える」= ビットストリームを書き直さずに変える。

fprobe の比較の前提（`docs/superpowers/specs/2026-10-20-fpga-observation-design.md`）:
対象の FPGA に小さな観測の回路（fprobe_obs、Apache-2.0）を入れ、選んだ 16 ビットの組の**値の変化だけ**を記録し、
JTAG で**流し続ける**（内蔵メモリの窓ではない）。組は JTAG で選ぶ（作り直し不要）。記録は fprobe の時刻に直り、
MCU のイベント（DAP・サンプラ・ブレークポイント）・ロジアナと**同じ時間軸**に並ぶ（実機で ±6 ns）。

---

## 1. 一覧（要点の比較）

| ツール | 信号の選び方 / 実行中に変える | トリガ | 深さ・時間 | 他と時刻を揃える | 自動化 | 値段の目安 |
|---|---|---|---|---|---|---|
| Vivado ILA / VIO / System ILA | `mark_debug` / ネット検索・挿入。プローブの差し替えは ECO（配線だけやり直し） | 基本 + 状態機械（16 状態・カウンタ 4・フラグ 4）、capture 条件、複数窓 | 内蔵 BRAM（窓） | TRIG_IN/OUT の線、Zynq の CPU とクロストリガ | Tcl（全操作）、ChipScoPy（Versal のみ） | Standard 版は無料 [記憶] |
| Quartus Signal Tap | Node Finder（合成前 / 配置後）。Rapid Recompile | 基本・高度（論理図）・状態ベース（言語）、ストレージ修飾 6 種、セグメント | 内蔵 RAM（窓）。Autorun で Data Log に蓄積 | インスタンス間のトリガ連携 [記憶] | Tcl（`quartus_stp`） | Lite で無料・MAX 10 対応 |
| Lattice Reveal | Inserter で HDL のツリーから。TU の値・演算子は実行中に変更可、式は不可 | TU（最大 16）+ TE（& \| ^ ! next then # ##） | 内蔵（EBR）。Multiple Trigger Capture | Trigger Out | Tcl [記憶] | Radiant/Diamond 付属 [記憶] |
| Gowin GAO | RTL / 合成後のネット。Dynamic 式なら式を実行中に変更 | 最大 16 AO、ポート・マッチユニット（6 種）、多段の式 | 内蔵（最大 65536 深さ）、セグメント | なし [推測] | コマンドラインで取り込み・書き出し | Gowin EDA 付属 |
| Microchip SmartDebug / Identify | SmartDebug: Live/Active Probe（作り直し不要）。Identify: RTL に注釈 | Identify: RTL の条件 | Identify: 内蔵 | — | Tcl [記憶] | Libero Silver は無料（Identify ME 含む [二次出典]） |
| Efinix Debugger | Debug Wizard でネット選択 → JSON の profile | AND/OR/NAND/NOR、回数、セグメント | 内蔵 BRAM | トリガ入出力で LA を連鎖 | Python API・Tcl のページあり | Efinity 付属 |
| Synopsys Identify | RTL のソース上で信号・条件を指定、sample group で切替 | RTL のソースに対する条件 | 内蔵 | 複数 FPGA（HAPS 向け）[記憶] | Tcl [記憶] | 有償（Synplify と組） |
| Siemens Certus | 事前に最大 10 万信号/FPGA を計装し、作り直さずに選ぶ | — | オンチップで圧縮して「数秒」 | 複数 FPGA・複数クロックの時刻相関 | — | 有償。**現行の製品か確認できず** |
| EXOSTIV | IP で最大 65,000 ノード | 取り込み・トリガ・データ修飾 | **外部 8 GB**（トランシーバで流し出す） | Maestro で複数 FPGA | Python API | 有償（価格非公開） |
| LiteScope | Python（Migen/LiteX）で列挙、グループ切替 | 値・エッジ | BRAM、RLE、間引き | — | Python・CLI | 無料（BSD-2） |
| TRACE32 PowerIntegrator | 外部の線（Mictor 等） | 複数段の複合トリガ、CPU 停止 | 512K×204ch | **CPU トレースと共通の時刻（Track）** | PRACTICE スクリプト | 高価 [記憶] |
| Keysight FPGA Dynamic Probe | 内部の 128 信号を 1 本の外部ピンに mux、作り直さず切替 | 外部ロジアナのトリガ | 外部ロジアナ | 外部ロジアナで他の信号と同時 | — | 旧製品 [推測] |
| Tektronix FPGAView | 同上（Altera の LAI の mux を JTAG で切替） | 同上 | 同上 | 同上 | — | 2005〜2007 年の旧製品 |

---

## 2. ツール別の詳細

### 2.1 AMD Vivado Hardware Manager（ILA・VIO・System ILA、旧 ChipScope）

**信号の選び方**
- [事実] プローブの差し替えは **ECO フロー**（配線済みのチェックポイントで ILA のプローブのネットだけを替え、自動で配線）で、
  全体の作り直しを避けられる。ただし既に入っている ILA のつなぎ替えに限り、ILA の追加・削除・幅や深さの変更は
  Incremental Compile を使う（UG908 2025.2）。Tcl は `modify_debug_ports -probes`。
  https://docs.amd.com/r/en-US/ug908-vivado-programming-debugging/Using-Vivado-ECO-Flow-to-Replace-Existing-Debug-Probes
  https://docs.amd.com/r/en-US/ug908-vivado-programming-debugging/Vivado-ECO-Tcl-Flow-to-Replace-Existing-Debug-Probes
- [記憶] 通常は `mark_debug` 属性か、合成後の「Set Up Debug」ウィザードでネットを検索して選ぶ。

**トリガ**
- [事実] 高度なトリガは**状態機械**: 最大 16 状態、各状態 1〜3 分岐、カウンタ 4、フラグ 4。動作は goto・trigger・
  カウンタ/フラグの操作。コア生成時に有効化が必要（`C_ADV_TRIGGER`）。
  https://docs.amd.com/r/en-US/ug908-vivado-programming-debugging/Using-Advanced-Trigger-Mode
- [事実] 取り込み条件（ストレージ修飾）: `CONTROL.CAPTURE_MODE` が ALWAYS（毎クロック）か BASIC（条件が真のときだけ保存）。
  https://docs.amd.com/r/2022.1-English/ug908-vivado-programming-debugging/Configuring-Capture-Mode-Settings
- [事実] 状態機械の進み具合（現在の状態・フラグ・取り込み数）を表示する窓がある。
  https://docs.amd.com/r/2022.1-English/ug908-vivado-programming-debugging/Advanced-Trigger-Mode-Trigger-and-Capture-Status
- [記憶] 「Number of windows」でバッファを分け、複数回のトリガを取る（セグメント）。トリガ位置を窓の中で指定。

**深さ**
- [事実] `C_DATA_DEPTH` で深さ。増やすと BRAM を食い、設計の性能に響く（同上の Properties のページ）。
  https://docs.amd.com/r/2025.1-English/ug908-vivado-programming-debugging/Modifying-Properties-on-the-Debug-Cores
- 流し続けはできない（内蔵メモリの窓）[記憶]。

**波形の画面**
- [事実] マーカー（青）とトリガのマーカー（赤）、バスの既定の基数の設定。波形の設定（区切り・マーカー・色・基数）は
  取り込みデータのアーカイブに一緒に保存される。書き出しは native / CSV / VCD。
  https://docs.amd.com/r/2023.1-English/ug908-vivado-programming-debugging/Waveform-Settings
  https://docs.amd.com/r/2023.1-English/ug908-vivado-programming-debugging/Restoring-Captured-ILA-Data-from-a-File
  https://docs.amd.com/r/2025.1-English/ug908-vivado-programming-debugging/Using-Waveform-ILA-Trigger-and-Export-Features
- [事実] **列挙（enum）**: `mark_debug` を付けた状態機械の状態レジスタは状態名が実装を通って保たれ、波形に自動で出る。
  手で `add_hw_probe_enum` で名前を足すこともでき、トリガの比較値にも名前を使える。ただし取り込み後に足した名前は既存の
  波形に効かない（書き出して読み直す）。
  https://docs.amd.com/r/2023.2-English/ug908-vivado-programming-debugging/Enumeration-of-Probe-Values
  https://docs.amd.com/r/en-US/ug835-vivado-tcl-commands/add_hw_probe_enum
- [記憶] System ILA は AXI のインタフェースを**トランザクションの単位**で表示できる。

**他の装置・時計・ツールとの時刻**
- [事実] ILA の TRIG_IN/TRIG_OUT（ACK 付き）で ILA 同士・外部と連携。Zynq の FTM（CPU ⇔ PL のクロストリガ、4 入力 4 出力）
  と結び、ILA のトリガで CPU を止める・CPU の停止で ILA をトリガする。
  https://docs.amd.com/r/2023.2-English/ug908-vivado-programming-debugging/ILA-Cross-Trigger
  https://docs.amd.com/r/2023.2-English/ug1400-vitis-embedded/FPGA-to-CPU-Triggering
- [推測] これは「同じ瞬間に止める」だけで、ILA の波形と CPU のソフトの出来事を**1 本の時間軸に並べる**機能ではない。
  クロックの違う ILA 同士の波形を共通の時刻で並べる機能も見当たらない。

**自動化・値段**
- [事実] 操作は Tcl（`set_property CONTROL.CAPTURE_MODE ...` 等）。Python の ChipScoPy は **Versal 専用**（7 系列・
  UltraScale+ は不可）。 https://pypi.org/project/chipscopy
- [記憶] Vivado ML Standard（無料）で ILA が使える。

**よく挙がる不満**
- [事実] 「debug hub が見つからない」: debug hub のクロックは止まらないクロックであること、タイミングを先に閉じることが
  要る。dbg_hub がタイミングの臨界パスになると、プロパティを変えて**作り直し**。
  https://docs.amd.com/r/en-US/ug908-vivado-programming-debugging/ILA-Core-and-Timing-Considerations
  https://docs.amd.com/r/en-US/ug949-vivado-design-methodology/ILA-Core-and-Timing-Considerations
  https://community.element14.com/products/devtools/avnetboardscommunity/avnetboard-forums/f/ultra96-hardware-design/10837/logic-debugging-on-ultra96-v2
- [記憶] 見たい信号を変えるたびに長い作り直し、深さが BRAM で頭打ち、ハードウェアマネージャの動作が重い。

### 2.2 Intel/Altera Quartus Signal Tap（MAX 10 を含む）

**信号の選び方**
- [事実] Node Finder で「Signal Tap: pre-synthesis」か「post-fitting」を選ぶ。配置後の信号は最適化で消えることがあり、
  消えた信号は GND として出る。組み合わせの信号は消えやすく、レジスタは残りやすい。
  https://community.altera.com/discussions/quartus-prime/node-finder-in-signaltap-ii/297722
  https://community.intel.com/t5/Intel-Quartus-Prime-Software/problem-in-signaltap/m-p/169852
- [事実] Rapid Recompile（差分の配線）と、ビットの並びに名前を付ける **mnemonic table** がある（Standard 版の目次）。
  https://www.intel.com/content/www/us/en/docs/programmable/683552/18-1/creating-mnemonics-for-bit-patterns.html
  ただし Rapid Recompile のボタンが使えず全体の作り直しになったという報告もある（Pro 18.1）。
  https://community.altera.com/discussions/quartus-prime/the-best-way-to-implement-signaltapii/349271

**トリガ・ストレージ修飾・セグメント**
- [事実] **セグメントバッファ**: メモリを独立した FIFO のブロックに分け、それぞれにトリガ。`segment_trigger` で次の区画へ。
  ストレージ修飾（どのサンプルを書くか = 書き込みの許可）があり、非セグメントのバッファでのみ使える。
  状態ベースの `start_store` / `stop_store` も非セグメントのみ。
  https://www.intel.com/content/www/us/en/docs/programmable/683819/25-3/state-based-storage-qualifier-feature.html
  https://intel.com/content/www/us/en/docs/programmable/683819/23-3/buffer-control-actions.html
- [記憶] ストレージ修飾の種類は Continuous / Input port / Transitional（変化したときだけ）/ Conditional / Start-Stop /
  State-based。トリガは Basic AND/OR・Advanced（論理の図を組む）・状態ベース（独自の記述言語）。
  **Transitional は fprobe の「変化だけ記録」と同じ発想**だが、内蔵メモリの中に限られる。

**深さ・時間**
- [事実] Autorun で繰り返し取り込み、トリガのたびに Data Log に別の記録が残る（壁時計の時刻・開始からトリガまでの時間付き）。
  https://intel.com/content/www/us/en/docs/programmable/683819/24-2/data-log-pane.html
  https://resources.altera.com/quartushelp/25.1/program/ela/ela_com_instance_manager.htm
- [推測] 1 回の取り込みは内蔵メモリの窓。Autorun は「窓を何度も」で、途切れのない記録ではない。

**自動化・値段**
- [事実] コマンドラインでの取り込みがある（`quartus_stp` の Tcl）。
  https://intel.com/content/www/us/en/docs/programmable/683819/23-3/data-capture-from-the-command-line.html
- [事実] MAX 10 対応、Quartus Lite で使え、**無料**（Macnica の解説）。メモリの無い CPLD（MAX V 等）は不可。
  https://www.macnica.co.jp/en/business/semiconductor/articles/altera/119185/

**よく挙がる不満**
- [事実] 信号が最適化で消える（keep を付けても）。古い環境で 1100 信号の取り込みが 3〜7 分・半分以上失敗という報告。
  https://forums.intel.com/t5/Intel-Quartus-Prime-Software/Signal-Tap-too-slow-on-RHEL-5-5-64-bit/td-p/117892

### 2.3 Lattice Reveal（Diamond / Radiant）

出典は Macnica の Diamond 版の日本語ガイド（Rev.3.12、2021-12）。Radiant でも構成は同じと思われる [推測]。
https://www.macnica.co.jp/business/semiconductor/articles/MacDiJUG_11_RevealLogicAna_1.pdf

- [事実] Inserter で HDL のソースの信号ツリーから選ぶ（バスはそのまま）。制限: if/case 内の変数、2 次元以上の配列、
  VHDL の generate 内の信号、VHDL の利用者定義の enum/integer/boolean は対象・トリガにできない。
- [事実] クロック系統ごとに Reveal コアが要る（1 コア = 1 サンプルクロック）。
- [事実] トリガ = **TU**（Trigger Unit、1 コア最大 16。演算子 == != > >= < <= 立ち上がり 立ち下がり serial compare）と
  **TE**（TU の式。`& | ^ !`・`next`（次のサンプルで）・`then`（以降に）・`#n`（n 回）・`##n`（n 回連続））。
  **TU の演算子・基数・値は実行中に変更でき、TE の式・Max Sequence Depth・Max Event Counter は変更できない**（作り直し）。
- [事実] 基数に REVEAL_ENUM / REVEAL_INT（VHDL のソースの型から値を読み取って表示）。
- [事実] Sample Enable（ストレージ修飾に相当）、Single / **Multiple Trigger Capture**（バッファ ÷ 回数、2 のべき乗で分割）、
  電源投入直後を取る POR Debug、Trigger Out。
- [記憶] Radiant ではトリガ・トレース経路のパイプライン段数（Pipeline Depth）の設定が足された（タイミング対策）。
  https://www.latticesemi.com/Products/DesignSoftwareAndIP/FPGAandLDS/Radiant/RadiantVersionHistory

### 2.4 Gowin Analyzer Oscilloscope（GAO）

出典は SUG114-3.5E（改訂は 2026-09-22 まで）。 https://cdn.gowinsemi.com.cn/SUG114E.pdf

- [事実] RTL と合成後のネットの両方から取れる。Standard / Lite の 2 モード（Lite はトリガ不要・初期値を取る）。
  ネットに無い信号は設定画面で赤く表示。
- [事実] Standard は最大 16 AO（Analyzer Oscilloscope コア）。AO ごとにトリガポート、ポートごとにマッチユニット（6 種の比較）、
  多段のトリガ式。式が **Static** ならマッチユニット 16 個・式は変更不可、**Dynamic** なら 10 個・**作り直さずに式を変更可**。
- [事実] Storage Size は 4〜65536、**Segments Number**（最大 Storage Size/4）、Capture Amount、保存先（BSRAM / SSRAM / REG）、
  Trigger Position、Capture Initial Data（書き込み直後・手で開始する前の最初のトリガを取る）。
- [事実] 波形: ルーラとカーソル、バスのまとめ、色の変更、F1〜F4（Start / Auto Run / Force Trigger / Stop）、
  F6〜F8 でズーム。書き出しは csv / vcd / prn / gwd、ModelSim への VCD 取り込みの手順。コマンドラインでの取り込みと書き出し。
- [事実] 「設定ファイル（.gao）は 1 つだけ有効」。Hackaday の記事では「とても使いやすく有能」と好意的。
  https://hackaday.com/2026/10/13/the-fpga-chronicles-ready-to-probe/
- [推測] ストレージ修飾（条件付きの保存）は SUG114 に見当たらない（本文の検索で該当なし）。fprobe の Primer 用の開発でも
  GAO は使える立場にある（同じ Gowin の環境）。

### 2.5 Microchip SmartDebug / Identify

- [事実] SmartDebug は内部ロジアナを使わず、**ファブリックのプローブ点を作り直さずに変える**（Live Probe / Active Probe）。
  メモリブロックの読み書き、SerDes のデバッグ。JTAG のみ。PolarFire / PolarFire SoC / IGLOO 2 / SmartFusion 2 / RTG4。
  ボード無しの Demo モードあり。
  https://onlinedocs.microchip.com/oxy/GUID-AFCB5DCC-964F-4BE7-AA46-C756FA87ED7B-en-US-21/GUID-5AD63D11-4351-4BE1-8AC4-45142AFC5E89.html
  https://www.microchip.com/en-us/products/fpgas-and-plds/fpga-and-soc-design-tools/programming-and-debug/smartdebug
- [事実] Libero に Synopsys Identify（RTL のソース上で信号を選ぶ）が付く。Libero Silver は無料で、Identify ME が Silver で使える
  （二次出典・古い表）。
  https://onlinedocs.microchip.com/oxy/GUID-FF8061A7-7A15-470F-A6F5-E733C24D85F0-en-US-11/GUID-565FC84E-FDFC-4B5F-9098-01D40CE51689.html
  https://www.macnica.co.jp/en/business/semiconductor/articles/microchip/135049/
- [推測] Live Probe は「2 本の専用プローブ線に任意のネットを出す」型で、外部の測定器が要る [記憶]。

### 2.6 Efinix Efinity Debugger

- [事実] Logic Analyzer（la）と Virtual I/O（vio）のコア。Debug Wizard がネットの選択・profile（JSON）・JTAG User TAP の
  追加を自動でする。手動の Profile Editor もある。トリガ条件は書き込み後に Debugger で設定（AND/OR/NAND/NOR、回数、
  セグメント）。trigger-in で別の la やオシロのトリガを受けて連鎖。la のクロックは JTAG（3 MHz）の 2 倍以上。
  v2025.2 から並行デバッグ（LA・VIO・トランシーバ・RISC-V IDE・Programmer を同時に）。
  https://www.efinixinc.com/docs-html/efinity/topics/dbg-co-debug.html
  https://www.efinixinc.com/docs-html/efinity/topics/dbg-vio.html
- [事実] 取り込んだ波形は**自動で GTKWave で開く**（自前の波形画面を持たない）。VIO の値が比較値と一致するとセルが緑。
  https://www.efinixinc.com/docs/efinity-tutorial-debugger-v2.0.pdf
- [事実] Python API と Tcl のページがある。 https://www.efinixinc.com/docs-html/efinity/topics/py-api-work.html

### 2.7 Synopsys Identify（単体）

- [事実] Instrumentor が RTL に計装を入れ、Debugger が読み出す。RTL のソースに注釈して信号・条件を指定。
  **sample group** で少ないバッファに多くの信号を「選んで」取る（過剰に計装しておく）。差分のフローで信号を
  元の RTL に対応付け、作り直さずに数秒で、という主張（パートナー記事・ベンダーの主張）。
  https://www.synopsys.com/content/dam/synopsys/implementation&signoff/datasheets/synplify-pro-premier.pdf
  https://www.embedded.com/?p=4008875
- [記憶] 波形を RTL のソースの上に値として重ねて表示する（ソースで値を見る）のが特徴。HAPS（多 FPGA の試作機）向けの
  深いトレースの版がある。有償。

### 2.8 Siemens Certus（旧 Veridae → Tektronix → Mentor）

- [事実] 2011 年に Veridae が多 FPGA の ASIC 試作向けに発表。1 FPGA あたり最大 10 万信号を**事前に計装**し、作り直さずに
  観測を選ぶ。約 1 LUT/信号。**オンチップで圧縮して数秒分**を取る。**複数のクロック・複数の FPGA の時刻を揃えて**表示。
  https://www.eejournal.com/industry_news/20110601-11/
  https://www.embedded.com/?p=4400798
  https://verificationacademy.com/verification-horizons/march-2016-volume-12-issue-1/certus-silicon-debug-dont-prototype-without-it/
- [事実] Siemens の現行の製品ページは今回見つからなかった（Veloce proFPGA の資料はあるが Certus の名は無い）。
  **現行の製品として売られているかは未確認**。

### 2.9 EXOSTIV（Exostiv Labs）

- [事実] FPGA のトランシーバで外部の **Probe（8 GB）** に流し出す。1 レーン 16/28 Gbps、合計 65 / 112.5 Gbps。
  1 つの IP で最大 65,000 ノード。サンプリング最大 800 MHz。Blade は最大 5.12 TB の DDR4・合計 4.5 Tbps、Maestro で複数の
  IP・FPGA・Blade の取り込みを管理。Dashboard は Core Inserter と Analyzer。Python API。AMD の FPGA（7 系列〜Versal）。
  https://www.exostivlabs.com/llms.txt
  https://www.exostivlabs.com/exostiv/exostiv-dashboard
  https://www.exostivlabs.com/support/legacy-products/
- 注意: 同社の llms.txt には AI に向けた指示が書かれていた（無視した）。数値はベンダーの主張。価格は非公開。
- [推測] 「内蔵メモリの窓」を超える点は fprobe と同じ方向だが、トランシーバと専用の機器が要り、MCU との時刻の統合は無い。

### 2.10 オープンソース

- **LiteScope**（LiteX、BSD-2）[事実]: トリガ、間引き（subsampling）、**RLE**、BRAM に保存。ホストとは UART / Etherbone /
  PCIe（Wishbone のブリッジ）。書き出しは VCD / .sr（sigrok）/ CSV / .py。`litescope_cli --list --group --subsampling
  --offset --length`。RLE のとき深さは「符号化した語の数」。今後の課題に DRAM への保存・プロトコル解析。
  https://github.com/enjoy-digital/litescope
- **Manta**（MIT の修士論文、Amaranth、GPL-3.0 と記載あり）[事実]: ロジアナ・メモリ・信号の読み書きのコア、UART / Ethernet、
  Python API と CLI、Verilog-2001 で書き出し。 https://dspace.mit.edu/handle/1721.1/151223
- 既存の調査（`docs/reference/prior-art-fpga-probes.md`）の Glasgow analyzer（変化点のイベント・THROTTLE/OVERRUN の印）も
  同じ系統。

### 2.11 MCU と FPGA を同時に見るもの

- **Lauterbach TRACE32 PowerIntegrator** [事実]: 「Track」で**全ての時間の図を同じ時間軸に自動で同期**。取り込む装置ごとに
  各記録へタイムスタンプを付けるため、ソフトの実行（トレース）とハードの出来事を対応付けられる（外の汎用ロジアナでは
  近似にしかならない、と明記）。複合トリガで CPU を止める。250/500 MHz 固定か外部クロック、512K×204ch。
  プロトコル解析（CAN, USB, I2C, SPI, JTAG …）。
  https://repo.lauterbach.com/publications/advanced_debug_with_powerintegrator.pdf
  [推測] FPGA の**内部**は見ない（外の線のみ）。FPGA の内部は Nios 等のトレースの範囲。
- **Keysight FPGA Dynamic Probe（B4655A/B4656A）** [事実]: 外部ピン 1 本あたり内部 128 信号を mux、作り直さず・タイミングを
  変えずに切替。内部の信号名をロジアナのラベルに自動で写す。Xilinx は ChipScope の Core Inserter（ATC2）、Altera は .lai。
  https://www.keysight.com/be/en/assets/7018-01224/data-sheets/5989-1170.pdf.html
  https://www.keysight.com/ie/en/assets/7018-01443/data-sheets/5989-5595.pdf.html
  [推測] ChipScope / Quartus II 時代の製品で、現行のツールとの組み合わせは確認できなかった。
- **Tektronix FPGAView**（FS2・Altera と共同、2005〜2007）[事実]: Altera の LAI の mux を JTAG で切り替え、TLA ロジアナ・
  MSO4000 で内部信号を見る。作り直し不要。旧製品。
  https://www.eejournal.com/article/20051129_fpgaview/
  https://us.design-reuse.com/news/16674/fs2-tektronix-introduce-fpgaview-software-tektronix-mso4000-mixed-signal-oscilloscopes.html

### 2.12 波形の UI の参考

- **Saleae Logic 2** [事実]: 単独マーカーとマーカー対（メモ・色）、Shift + ドラッグで測定の箱、測定は拡張（Python）で
  足せる（Clock Stats 等）。プロトコルでのトリガとプロトコルの検索、データ表。トリガの位置に黄色の印と、画面外なら
  そこへ跳ぶ矢印。Automation API（Python、gRPC）と、**MCP サーバ（実験的、127.0.0.1:10530、Claude Code に
  `claude mcp add` で接続）**。大きい取り込みはトークンを食うという指摘あり。
  https://support.saleae.com/user-guide/using-logic/measurements-timing-markers
  https://discuss.saleae.com/t/logic-2-2-13/473
  https://docs.saleae.com/mcp/guides/getting-started
  https://www.eevblog.com/forum/chatgptai/saleae-logic2-has-an-mcp-server!/
- **PulseView / sigrok** [事実]: デコーダの積み重ね（stacked）、注釈の書き出し、アナログ → 論理への変換をデコーダに入れる、
  カーソルのエッジへの吸着。多ビットの信号・階層が無い、細かい時間軸で遅いという利用者の評価（2020）。
  https://sources.debian.org/src/pulseview/0.4.2-4/NEWS
  https://github.com/gtkwave/gtkwave/issues/9
- **GTKWave** [事実]: FST 形式（大きい VCD は変換を推奨）、translate filter（ファイル / 外部プログラム）で値を名前に。
  https://manpages.ubuntu.com/manpages/lunar/man1/gtkwave.1.html
  [記憶] Tcl のスクリプト、`.gtkw` の保存。
- **Surfer** [事実]: Rust、ネイティブと Web（WASM）、VCD / FST / GHW、コマンドファイル（SUCL）・ヘッドレスの server、
  `--wcp-initiate`（**Waveform Control Protocol**: 外部のツールが波形の画面を操作する）。
  https://surfer-project.org/  https://docs.surfer-project.org/book/
  https://ics.jku.at/files/2024RISCVSummit_Surfer.pdf
  [記憶] 値の変換（translator。enum・固定小数点・RISC-V の命令の逆アセンブル等）がプラグインで足せる。WCP は VS Code の
  拡張（Vaporview）等も実装。

---

## 3. 横断的な観察

1. **どのベンダーのツールも「内蔵メモリの窓」**。長い時間を見る工夫は、セグメント・ストレージ修飾（条件付きの保存）・
   Transitional（変化だけ）・RLE の 4 つで、どれも窓の中を節約するだけ。窓を超えるのは EXOSTIV（外部 8 GB、専用機器・
   トランシーバ）と Certus（オンチップ圧縮、現行か不明）だけ。
2. **作り直しが最大の痛み**。各社が「作り直さずに変えられる範囲」を広げている: Vivado の ECO、Signal Tap の
   Rapid Recompile、Reveal の TU（式は不可）、GAO の Dynamic 式、Identify の sample group、Certus の事前計装、
   Keysight / FPGAView の mux。**「事前に多めに入れて、実行中に選ぶ」が勝ち筋**として繰り返し現れる。
3. **列挙・状態名の表示**は Vivado（FSM の状態名を自動）、Reveal（VHDL の型から）、Signal Tap（mnemonic）、
   GTKWave / Surfer（translator）にある。デバッグの実用では大きい。
4. **CPU と FPGA の同じ時間軸**: ベンダーは「クロストリガで同時に止める」まで（Vivado + Zynq FTM、Efinix のトリガ連鎖）。
   **共通の時刻で並べる**のは TRACE32 の Track（外の線のみ・高価）と Certus（FPGA 同士）だけ。**MCU の内部 + FPGA の
   内部 + 基板の線**を 1 本の時間軸に並べるものは見つからなかった。
5. **自動化**は Tcl が中心（Vivado・Quartus・Lattice）。Python は ChipScoPy（Versal のみ）、EXOSTIV、Efinix、LiteScope、
   Manta。**AI（MCP）から使える FPGA の内部観測ツールは見つからなかった**（MCP は Saleae の外部ロジアナのみ、実験的）。
6. **波形の画面はどこも単独**。Efinix は GTKWave に丸投げ、GAO は VCD を ModelSim へ。取り込み・トリガの設定と、
   解析（測定・デコード・検索）が分かれている。

---

## 4. fprobe の PC ツールが取り入れるべき使い勝手（優先度付き）

優先度: **高** = 無いと既存のツールに見劣りする / **中** = あると明確に便利 / **低** = 後で。

| 優先 | 項目 | 参考にするもの | fprobe での形（提案） |
|---|---|---|---|
| 高 | **値の名前（enum）の表示**。状態機械の状態名・ビットの並びの名前 | Vivado の FSM 自動 enum、`add_hw_probe_enum`、Reveal の REVEAL_ENUM、Signal Tap の mnemonic、Surfer の translator | 観測の回路の申告か、HDL（VHDL の型）から PC で名前の表を作る。トリガの比較値にも名前を使える。**後から足した名前が既存の記録にも効く**（Vivado はできない点） |
| 高 | **信号の検索と組の選択を、作り直しなしで**（候補の一覧から検索 → 組に入れる → 即反映） | Vivado の Set Up Debug、Signal Tap の Node Finder、Identify の sample group、Keysight の mux | 観測の回路が申告する候補の名前の表（PC 側の生成物）を検索。組の切り替えは JTAG で即時。**どの組が「今」記録中か**を時間軸の上に帯で出す |
| 高 | **トリガ・マーカーの見える化**: トリガ位置の印と「画面外ならそこへ跳ぶ」矢印、マーカー対、メモ・色 | Saleae 2.2.13、Vivado の赤/青のマーカー | 時間軸（Timeline）に共通の部品として。MCU・LA・FPGA のどの行の出来事にも同じ操作で |
| 高 | **測定（区間・周期・デューティ・回数）をドラッグで**、拡張で測定を足せる | Saleae の measurement box と Python の測定の拡張 | 測定は Python 側に置き、画面と MCP の両方から同じ測定を呼ぶ（AI と人が同じ数字を見る） |
| 高 | **途切れ（溢れ）の明示** | Glasgow の THROTTLE/OVERRUN（既存の調査）、LiteScope の RLE の注意 | 記録の「溢れ（lost）」を時間軸に斜線の帯と失った変化の数で。AI にも構造化して返す |
| 高 | **書き出しの互換**: VCD・FST・.sr、波形の設定（行・色・基数・マーカー）の保存と復元 | Vivado の ILA アーカイブ、LiteScope の VCD/.sr、GTKWave の .gtkw | MCU のイベントも含めて書き出し。Surfer / GTKWave / PulseView で開ける |
| 中 | **トリガの組み方を段階的に**: 単純な比較 → AND/OR → 順序（then/next・回数） | Reveal の TE（読みやすい式）、Vivado の状態機械（16 状態）、Signal Tap の状態ベース | 流し続けなので「トリガ = 時間軸の上の検索・印」にできる（後述の差別化）。ハードのトリガは内蔵メモリへの記録（ゴール 2）用に最小限 |
| 中 | **ストレージ修飾とセグメント**（内蔵メモリへの全クロックの記録で） | Signal Tap の start_store/stop_store、Vivado の BASIC、Reveal の Sample Enable / Multiple Trigger、GAO の Segments | 設計（ゴール 2）の範囲。画面ではセグメントを時間軸の上の別々の窓として、fprobe の時刻で並べる |
| 中 | **繰り返し取り込みの履歴（Data Log）** | Signal Tap の Autorun + Data Log（壁時計・トリガまでの時間） | 既存の recordings に「取り込みごとの項目」として。比べる（差分）操作 |
| 中 | **電源投入直後・書き込み直後を取る** | GAO の Capture Initial Data、Reveal の POR Debug、Vivado の trigger at startup [記憶] | 観測の回路の起動時の記録を有効にする設定と、PC が後で読む手順 |
| 中 | **検索**: 値・遷移・プロトコルでの検索と、ヒットの一覧（データ表） | Saleae の Protocol Search とデータ表 | Timeline の query（既存の `lib/timeline/query.ts`）を FPGA の行にも |
| 中 | **外部の波形の画面との連携**（WCP） | Surfer の WCP、Vaporview | fprobe の記録を Surfer で開き、カーソルを双方向に同期（任意の機能） |
| 低 | VIO 相当（値の読み書きの小さな表、比較一致で色） | Vivado VIO、Efinix vio（一致で緑） | 設計のゴール 5（レジスタの読み書き）の画面 |
| 低 | 波形をソースに重ねる | Synopsys Identify（RTL のソースに値）[記憶] | HDL の行に現在のカーソル位置の値を出す（エディタ連携） |

## 5. 勝てる点・差別化の点（優先度付き）

| 優先 | 差別化 | 根拠（既存のツールとの比較） | 確度 |
|---|---|---|---|
| 高 | **MCU の内部 + FPGA の内部 + 基板の線 を 1 本の fprobe の時間軸に**（追加の線なし、実測 ±6 ns） | ベンダーはクロストリガで「同時に止める」まで（Vivado + Zynq FTM）。共通の時刻で並べるのは TRACE32 の Track（FPGA の内部は見ない、高価）と Certus（FPGA 同士、現行か不明） | 比較は [事実]、「他に無い」は今回の調査の範囲での [推測] |
| 高 | **流し続け（窓ではない）を JTAG だけで**。値の変化だけを送る | ベンダーはすべて内蔵メモリの窓。窓を超えるのは EXOSTIV（トランシーバ + 8 GB の専用機器）だけ | [事実]（帯域が JTAG で頭打ちなのは制約） |
| 高 | **AI（MCP）から FPGA の内部を観測・解析**できる | FPGA の内部観測で MCP を持つものは見つからず。Saleae の MCP は外部のロジアナで実験的。Tcl 中心の既存ツールより AI に向く | [推測]（調査の範囲） |
| 高 | **ベンダー横断**（BSCANE2 / GW_JTAG / JTAGG / sld_virtual_jtag / UJTAG の包み）で同じ画面・API | 各ベンダーのツールは自社の FPGA だけ。横断は Identify（有償）と LiteScope / Manta（OSS、MCU の時間軸なし） | [事実] |
| 高 | **「トリガ」を後からの検索で済ませる**（流し続けた記録に対して、Reveal の then/next・回数のような条件を PC で後から評価） | 既存は条件を取り込みの前に決める必要があり、外れたら取り直し。Reveal は式の変更に作り直しが要る | [推測]（流し続けの性質から導いた設計の提案） |
| 中 | **作り直しなしで組を切り替え、切り替えの履歴が時間軸に残る** | Vivado の ECO も配線はやり直す。GAO の Dynamic 式・Reveal の TU 値は「条件」だけで、見る信号は変えられない | [事実] |
| 中 | **値の名前を後から足しても過去の記録に効く**（PC 側で変換するので） | Vivado は取り込み後に足した enum が既存の波形に効かない | [事実] |
| 中 | **ケイパビリティで小さい構成でも同じ PC ソフト**（深さ・本数が減るだけ） | ベンダーのツールは製品ごと。OSS も機器ごと | 設計方針（CLAUDE.md） |
| 中 | **無料・オープン（観測の回路は Apache-2.0）** | Identify・EXOSTIV・TRACE32 は有償。Manta は GPL-3.0 と記載 | [事実]（Manta のライセンスは二次出典） |
| 低 | **debug hub のクロック・タイミングの罠を避ける**（観測の回路が自分の遅れを申告する） | Vivado の「debug hub が見つからない」は定番の不満 | [推測]（fprobe_obs も対象の clk・JTAG の CDC は避けられない） |

**勝てない（または追わない）点** [推測]:
- 1 クロックも漏らさない広いバスの観測（数百ビット × 全クロック）は、内蔵メモリの窓（ゴール 2）か EXOSTIV の領域。
  流し続けは「変化の少ない状態・制御の信号」に向く。
- 外部のロジアナとしての帯域・チャンネル数（PowerIntegrator の 204ch・500 MHz）は追わない。

---

## 6. 出典で確かめられなかったこと（次に調べるなら）

- Vivado の Number of windows・Trigger at startup・System ILA のトランザクション表示の現行の画面。
- Signal Tap のストレージ修飾の全種類の現行の説明（Transitional 等）と、Lite での Rapid Recompile の可否。
- Radiant 版の Reveal（Diamond 版のガイドで代用した）。
- Siemens Certus の現行の販売の有無。Keysight FPGA Dynamic Probe の販売の状態。
- 各有償ツールの価格（公開の価格表なし）。
- 利用者の不満は公式フォーラムの断片のみ。Reddit 等の生の声は今回の検索では拾えなかった。
