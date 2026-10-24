# fprobe の価値が最大に出る使い方 — 根拠からの考え直し（2026-10-24）

- 依頼: 既定の画面の厳選の基準として統括が挙げた使い方の候補 3 つ（壊れた瞬間の前後を見る・止めると壊れる不具合を
  止めずに見る・AI が試した結果を人が確かめる）が、本当にそれだけか、価値の最大化に繋がっているかを根拠から考え直す。
- 読んだもの: CLAUDE.md、全体設計（2026-10-15）、画面の作り直しの案（2026-10-23）、`host/api/fprobe_api.json`（348 メソッド）、
  残務の台帳（2026-10-18-backlog）、ギャップ分析・競合調査・J-Link 比較・UI/UX 調査・システム解析の報告（docs/reports/）、
  画面の窓の一覧 `host/ui/src/components/panels/registry.ts`、既定の配置 `host/ui/src/lib/dock.ts`、各窓が呼ぶ API（grep）。
  外の事実は Web で調べ、§8 に出典を並べた。
- 書き方: 事実（API・実機での確認・出典）と、私の判断（**推奨**と明記）を分ける。場面の頻度は、測った資料がほとんど
  見つからなかった（§3.1）。順位は経験則による判断なので、材料を並べた上で統括への推奨として書く。

---

## 0. 要約

1. fprobe だけ（または高価な組み合わせだけ）にあるものは、**個々の機能ではなく「つながり」**。止めずに変数を読む
   （Ozone の Data Sampling）・AI から操作する（Saleae の公式 MCP、J-Link の有志の MCP）・FPGA の中を見る（ILA）は、
   それぞれ単独なら他にある。他に無いのは、(a) MCU・ボードの信号・対象の FPGA・Linux・人と AI の操作が**同じ時計の 1 本の
   記録**に入ること、(b) そのどれの条件でも**ハードウェアで**止める・取り込む・書き込むこと、(c) それを AI と人が
   **同じ API・同じ画面**で扱うこと。
2. 候補 3 つは、すべて「**不具合が起きた後に見る（受け身）**」だった。抜けていたのは次の 4 つ。
   - **捕まえる段取り**: まれな不具合は「前後を見る」より前に、条件を置いて長く待ち、起きた瞬間を残すのが難所。
     候補 1 はそれが済んだ後だけを見ていた。
   - **FPGA と MCU（と回路）にまたがる不具合**: fprobe でいちばん他に無い使い方で、実機の証拠（Shrike-Lite）もある。
     候補に入っていなかった。
   - **時間を測る**（割り込みの遅れ・ジッタ・区間の時間）: 組み込みの難しい不具合の原因の上位（Barr の 10 項目の
     ジッタ・優先度・優先度逆転）。共通の時計の価値が最も直接に出るのに、独立した場面になっていなかった。
   - **能動的に試して確かめる**（刺激を入れる・故障注入・規則で合否・基準の記録との比較）: AI の自動化が最も
     効く所（何百回でも同じ手順を回せる）。候補 3 は「AI が試した結果を見る」だったが、試す道具そのもの
     （`inject.run`・`rules.check`・`gpio`・クロストリガの出力）には**専用の窓が無い**。
3. 候補 2（止めずに見る）は、変数の周期の読みだけなら J-Link + Ozone にもある（1 µs 分解能と公表）。fprobe の差は、
   それがロジアナ・FPGA と**同じ時計**に入ることと、値の**条件でハードが止める・取り込む**こと（`sampler.trigger_set`）。
   既定に入れる理由は「止めずに見る」ではなく、こちらで説明するべき。
4. **推奨する既定の配置**（§6）: 中央に時間軸（`timeline`）、その右に「条件」の窓（今の `system` が近い）、下段に
   ソース・呼び出し履歴（時刻のコードの位置）、ロジアナ（`logicAnalyzer`）、記録（`recordings`／`eventLog`）、
   故障（`fault`）、変数の観測（`dataSampling`）、操作の記録（`activity`）。足りない窓は「時刻 → コードの位置」
   「クロストリガの経路を 1 か所で」「試す（注入・規則）」の 3 つ。

---

## 1. 根拠にするもの

- 目的と優先順位（CLAUDE.md）: (1) 解析力（ロジアナとデバッガの一体化・クロストリガ・共通のタイムスタンプで
  「なぜ壊れたか」）、(2) 日常のデバッガ、(3) 本来の目的は **AI による開発自動化の効率化・高精度化**（人にも便利は
  副次）。
- 設計原則: 本体は判断しない（事実と欠けを出す）。機器固有の機能を足さない。
- 画面の原則（2026-10-23 の案 §2）: 日常のデバッグは IDE と分担できる（GDB サーバ U04）。既定は価値が最大に出る
  使い方から決める。
- 今の既定の配置（`dock.ts`）は 2 つ: `default`（デバッガの型。Source・Watch・Locals・Registers・Call Stack・Threads・
  Breakpoints・Peripherals・Memory・Timeline・Terminal・Disassembly・Activity）と `system`（Timeline・System・FpgaObs・
  Watch・LogicAnalyzer・Terminal・Memory・EventLog）。**既定の `default` は Ozone・VS Code の型で、fprobe だけの価値が
  出る窓（ロジアナ・条件・記録）が入っていない。**

---

## 2. fprobe にしかできないこと（または、組み合わせないとできないこと）

各項目: fprobe の事実（API・実機）→ 競合の事実（出典）→ 注意（制約・未確認）。

### 2.1 共通の時計の 1 本の記録（解析力の中心）

- **fprobe**: `recording.start` の間、ロジアナの捕捉・流し続け（`recording.la_stream.*`、日単位も可）・DAP / UART /
  SWO / RTT / 電圧の出来事・変数の周期の読み（stream "sample"）・RTOS のタスク・FPGA の観測（`fpga_obs.*`）・Linux の
  出来事（`linux.trace.*`、ネットワークの時刻合わせ `linux.sync`）・**人と AI のすべての操作**・印（`recording.mark`）が
  デバイスの 64 ビットのタイムベース（10 ns）の上の 1 つの記録に入る。読み出しは `recording.events/find/query（SQL）/
  intervals/correlate/transactions/durations/waveform_measure`、書き出しは VCD・CSV・Perfetto。
  時間軸の窓には操作の段（AI と人を分けて）がある（`lib/timeline/titles.ts`）。
- **競合**:
  - SEGGER Ozone の Timeline は、変数の標本（Data）・消費電力（Power）・命令トレースからの呼び出し（Code）を共通の
    時間軸に並べる [S1][S2]。**外部のデジタル信号（ロジアナ）の段は無い**（社内の UI 調査 2026-10-17 の第 3 部 B）。
    Code の段は命令トレース（J-Trace PRO）が要り、止まってから更新されるとの報告がある [S3]。
  - Lauterbach TRACE32 + PowerIntegrator は、デバッガとロジアナを 1 つの画面から、クロストリガと**共通のタイム
    スタンプ**で扱う（204 ch・48 ビットのタイムスタンプ）[S4][S5]。fprobe の 2.1・2.2 に最も近い既存品。価格は
    公開されていない（PowerDebug のモジュールに付ける拡張）。
  - Saleae Logic 2 はロジアナ単体（デバッガとの統合は見つからなかった）[S6]。
  - 「デバッガとロジアナが同じ時刻を共有するのは事実上 TRACE32 + PowerIntegrator と DT+ だけ」（社内の UI 調査、
    2026-10-17 第 3 部 B）。
- **注意**: 外部のロジアナの入力は 8 本 + 専用の 6 本（構成 `fprobe_la`）、100 MS/s、3.3 V だけ。命令トレース（ETM の
  パラレル）は受けない（U34 保留）。電流は測れない（U38 保留）。

### 2.2 ハードウェアのクロストリガ（どの源からどの動作へ）

- **fprobe**: 源 = ロジアナのトリガ・多段トリガ（4 段）・DAP の番地一致・ホスト・**FPGA の観測の値の一致**
  （`fpga_obs.trigger_set`）・**サンプラの値の一致**（`sampler.trigger_set`。DHCSR の S_HALT で「止まった」も源になる）、
  動作 = ロジアナの捕捉・割り込み書き込み（既定はターゲット停止。任意の番地に 1 語）・出力の端子（`xtrig.output_*`）。
  実機: ロジアナのトリガ → 停止の要求まで 80 ns（2026-10-16）。FPGA の条件 → MCU の停止は SWD 4 MHz で 63.5 µs、1 MHz で
  205.6 µs（2026-10-21、Shrike）。
- **競合**:
  - AMD（Xilinx）の ILA は Zynq の CPU とのクロストリガを持つ（ILA のトリガで CPU を止める・CPU の停止で ILA を取る）[S7][S8]。
    ただし**同じチップの中の PS と PL の間だけ**。別の MCU や、Xilinx 以外の FPGA にはつながらない。
  - TRACE32 + PowerIntegrator はクロストリガを持つ [S4]。
  - J-Link・Saleae の間をつなぐ公式の仕組みは見つからなかった（つなぐなら外の配線とスクリプト）。
- **注意**: 割り込み書き込みは SWD の転送の境界で入るので、遅れは SWD のクロックで決まる（数十〜数百 µs）。
  「CPU を 1 サイクルで止める」ものではない。

### 2.3 止めずに読む（FPGA の DAP サンプラ）と、値の条件のハードのトリガ

- **fprobe**: `watch.set`（決まった番地は FPGA のサンプラ、8 か所・各 16 語、pyOCD・GDB と同時に使える）、
  `watch.trigger`（条件つきの取得）、`watch.buffer_*`（ターゲット側のバッファを吸い出す）、`watch.write`（走行中に書く）、
  `rtos.timeline.*`（方法 sampled なら組み込み無しでタスクの時間軸）。速さは SWD 4 MHz で 1 巡回約 120 µs（B17）。
- **競合**: Ozone の Data Sampling は記号と C の式を 1 µs までの分解能で取り、Timeline に描く [S2]。J-Scope・C-SPY の
  Live Watch・µVision も止めずに読む（競合調査 9.1「当たり前の水準」）。
- **差**: 単独の「止めずに読む」は**他にもある**。fprobe の差は、(1) ロジアナ・FPGA と同じ時計に入る、(2) 値の条件で
  ハードが止める・取り込む（`sampler.trigger_set`）、(3) 組み込みのコード無しで RTOS の時間軸が取れる、の 3 点。

### 2.4 対象の FPGA の中の観測（fpga_obs）と MCU の連動

- **fprobe**: 対象の FPGA に観測の回路（`target_ip/fprobe_obs`、記録型 / 標本型）を入れ、その JTAG で読み、MCU・ピンと
  同じ時刻に並べる。FPGA の条件で MCU を止める・ロジアナを取る。`recording.correlate`（結果の直前に多い出来事。判定は
  しない）・`recording.transactions`（区間ごとの集計）・`recording.durations`。
  実機: Shrike-Lite（RP2040 + ForgeFPGA）の仕込んだ不具合で、AI 役のサブエージェントがソースを読まずに 2 回とも
  原因を突き止めた（2026-10-21 の報告）。
- **競合**: ILA（AMD）・GAO（Gowin）・Reveal（Lattice）は各社の FPGA の中だけを見る。外の MCU と時刻をそろえる仕組みは
  Zynq の PS との間（2.2）以外に見つからなかった。
- **注意**: ILA と同じく、対象の FPGA に回路を入れて作り直す必要がある。小さい FPGA（Shrike の ForgeFPGA）は標本型だけ。

### 2.5 故障注入（ハードの時刻で書き込む）と規則の検査

- **fprobe**: `inject.run`（条件の成立から数 µs でハードが任意の番地に 1 語書く。試行ごとに pass / fail /
  not_triggered と注入の時刻）、`rules.check`（記録に区間・周期・順序・回数・値・状態遷移の規則を当てる。基準の記録と
  比べる baseline）、`rules.report`（要件 → 規則・試行 → 判定の証跡）。
- **競合**: デバッガを使った故障注入は研究としてある（走行中のターゲットのメモリのビットを止めずに反転する等）[S9]。
  市販のデバッグプローブでこれを製品の機能として持つものは**今回の調査では見つからなかった**（無いとは確かめていない）。
  ChipWhisperer 等の電圧・クロックのグリッチは別の種類（物理の故障）。
- **注意**: 実機での確認は一部（U25: 規則は XIAO で確認、注入の台本は未）。**画面に専用の窓が無い**（§5）。

### 2.6 Linux の上のアプリと、JTAG・ボードの信号を同じ時間軸に

- **fprobe**: `linux.*`（SSH のエージェント、uprobe / ptrace のプローブ、ftrace、journal、perf、core dump、JTAG で
  止めて読む道 B、xsdb の memap で止めずに読む）。時刻はネットワークの時刻合わせで記録に入る。
- **競合**: TRACE32 は Linux を認識する（ギャップ分析 T32-20）。perf・ftrace は外の信号を見ない。
- **注意**: 時刻合わせは往復の半分の不確かさがある（`linux.sync` が組で返す）。実機は Pi 2B・PYNQ-Z2。

### 2.7 AI と人が同じ API・同じ画面で（交代も）

- **fprobe**: 全 348 メソッドを MCP でも画面でも呼べる（API パネル、R23 段階 A）。`ui.state.get/set`（AI と人が同じ
  視点・カーソル）、`session.control.pause/resume`（人が AI の操作を保留させる）、`session.activity.list`、時間軸の
  操作の段、`hooks.*`（AI・画面・GDB のどの操作にも同じフック）。
- **競合**:
  - Saleae は Logic 2 の**公式の MCP サーバ**を出している（試験的。捕捉・解読・書き出し）[S6]。
  - J-Link・probe-rs・OpenOCD・pyOCD には**有志の MCP サーバ**が複数ある（レジスタ・メモリ・書き込み・RTT・Fault の
    解読）[S10][S11][S12]。
- **差**: 「AI が操作できる」だけでは**もう差にならない**。差は、(1) デバッガ・ロジアナ・FPGA・Linux を 1 つの MCP で、
  同じ時計の記録の上で扱える、(2) AI のした操作が人の時間軸に段として残り、人が止めて引き継げる、(3) 本体が判断を
  返さず、欠けを示す（AI が道具の判断を信じ込まない。CLAUDE.md の原則）。(3) が競合の MCP に無いかは確かめていない。

### 2.8 刺激を入れる（試験用の I/O）

- **fprobe**: `gpio.*`・`gpio.sequence`（ビットバング）・`spi.transfer`・`xtrig.output_*`（条件で端子を動かす）・
  `uart.write`・`rtt.write`。実機: XIAO のキーを押したことにして 600 回取りこぼしなし（U41）。
- **競合**: Keil ULINKplus に試験用の I/O がある（競合調査 9.2）。Saleae は入力だけ（出力の機能は見つからなかった）。
- **差**: 刺激と応答が同じ記録に入る。

### 2.9 安さ・小さいボードへの移植・ボードを足して 1 台に

- **事実**: 本体は Tang Primer 20K。比較の価格（税抜、販売店の表示）: J-Link PLUS 約 €694、J-Trace PRO（Cortex-M）
  約 €1,800 [S13]、Saleae Logic Pro 16 約 €1,449 [S14]。モジュール化（R15）は段階 0・1 を実行中。
- **注意**: これは使い方（場面）ではなく、どの場面でも効く条件。既定の画面の決め方には直接は効かない。

### 2.10 fprobe に無いもの（場面を選ぶときに要る）

命令トレースのストリーミング（J-Trace PRO）、電流（J-Link の Power Sampling・ULINKplus）、1.8 V、外部の信号の数
（Saleae 16 ch・PowerIntegrator 204 ch に対して 8 + 6）、Monitor Mode 以外での CPU の止めない実行トレース。
命令トレースが要る場面（「どの経路を通って壊れたか」を 1 命令ずつ）は、fprobe は PC の標本・止まる直前の
ETM の履歴（ETB 等、`trace.etm.history`、実機未確認）で近づけるだけ。

---

## 3. それが効く場面

### 3.1 頻度・困る度合いの資料（事実）

- 測った頻度の資料は少ない。見つかったもの:
  - 組み込みの開発でデバッグが最も時間を食う。ESC の 354 人の調査で 35 % が開発時間の半分以上をデバッグに使う
    （Virtutech、2007。EE Times・EDN の記事。二次情報）[S15]。古い。
  - Barr Group の「再現しにくい不具合の原因の上位 10」: ジッタ・優先度の割り当て・優先度逆転・デッドロック・メモリ
    リーク・ヒープの断片化・スタックのあふれ・volatile の抜け・再入できない関数・競合状態 [S16]。
    **10 項目のうち 3 つ（ジッタ・優先度・優先度逆転）は時間の問題、ほかもほとんど「まれに・タイミングで起きる」**。
  - 再現しない不具合は、開発者が調べ切れずに閉じがち（Firefox・Eclipse の 576 件の研究等）[S17]。
- 止めると壊れる例（出典あり）:
  - Nordic の BLE（SoftDevice）: ブレークポイントで止めると相手は接続を失い、再開すると SoftDevice が時間の制約違反で
    assert する。回避は UART・GPIO の印か、Monitor Mode [S18]。fprobe の実機: XIAO nRF52840 で停止方式は 5 秒で BLE が
    落ち、モニタ方式は 15 秒でもつながったまま（U37）。
  - TI C2000 のモータ・電源: 止めても PWM が出続ける設定があり、止めた結果の事故の報告がある。回避は Real-Time Mode
    （止めずに読み書き）[S19]。
- fprobe 自身の使われ方の記録（activity、8,649 件）は開発中の試験がほとんどで、場面の頻度の根拠にならない
  （画面の作り直しの案 §1）。

### 3.2 場面の一覧

| # | 場面 | 開発者が困る点 | fprobe の何が効くか（API） | ほかの道具だと |
|---|---|---|---|---|
| P1 | **ハードとソフトの境目**（周辺の設定・ピン・バス。SPI/I2C の応答とコードの対応） | コードの「送った」と線の上の事実がずれる。オシロ・ロジアナとデバッガが別で時刻を手で合わせる | 2.1・2.2: 番地一致でロジアナを取る（`xtrig.set_match`）、解読（`la.decode`）と DAP の出来事が同じ時刻 | Ozone はロジアナの段が無い。Saleae はコードを知らない。TRACE32 + PI は高価 |
| P2 | **タイミング**（割り込みの遅れ・ジッタ・周期の乱れ・区間の時間） | GPIO を足してオシロで測るのが定番。印の時刻とコードの対応・統計は手作業 | 2.1: `recording.intervals`・`timing.measure`（分位）・`rules.check` の period / interval・`profile.exceptions`・`cycles.measure`・`testpoint.*` | SystemView・Tracealyzer は組み込みのコードが要り、外の信号を見ない |
| P3 | **まれにしか起きない**（数時間に 1 回） | 起きた瞬間に居合わせない。長く記録すると見つけられない | 2.1・2.2: 日単位の記録（分割・縮図 `recording.overview`）、条件で印・停止（`recording.trigger_set`・`sampler.trigger_set`・多段トリガ）、`recording.find`・SQL | 長い記録とデバッガの組は TRACE32 級。ロジアナ単体は止められない |
| P4 | **止めると壊れる**（BLE・モータ・電源・相手のタイムアウト） | ブレークポイントが使えない。printf も時間を変える | 2.3: サンプラ（止めない・組み込み無し）、`watch.trigger`、Monitor Mode（`target.debug_mode.set`）、RTOS の時間軸 sampled | J-Link + Ozone で多く代替可（Data Sampling・Monitor Mode）。fprobe の差は同じ時計・ハードの条件 |
| P5 | **壊れた後の解析**（HardFault・アサート・ウォッチドッグ） | 止まった時には原因の瞬間が過ぎている | `target.fault`・`snapshot.*`・`trace.etm.history`・止まった瞬間の直前の波形（R29: サンプラで DHCSR を見てロジアナを止める）・Fault の前の記録 | IAR の Fault viewer・Ozone の Snapshot・Memfault（現場）。直前の外の信号まで残すのは TRACE32 級 |
| P6 | **FPGA と MCU（と回路）にまたがる** | 原因が FPGA・MCU・配線のどれかを切り分けられない。ILA と IDE の時刻が別 | 2.4: `fpga_obs.*`、FPGA の条件で MCU を止める、`recording.correlate/transactions/durations` | Zynq の内部だけ ILA のクロストリガ。別チップの組は手段が見つからない |
| P7 | **堅牢性の確かめ**（故障注入・異常系・要件の証跡） | 異常系は狙って起こせない。起こしても時刻がずれる・回数をこなせない | 2.5・2.8: `inject.run`・`rules.check/report`・`gpio`・`xtrig.output_*` | 研究の道具・自作の治具。市販のプローブには見つからなかった |
| P8 | **性能の測定**（CPU 負荷・区間時間・スタック・ヒープ） | 測る方法がツールごとに違う | `profile.*`・`rtos.timeline.load/profile`・`cycles.*`・`target.stack.*`・`heap.*`・`testpoint.*` | Ozone・SystemView・Tracealyzer でほぼ足りる（fprobe の独自性は低い） |
| P9 | **AI に任せた自動の試験と原因の切り分け** | AI の結論が正しいか人が確かめにくい。AI が道具の判断を信じ込む | 2.7: 全 API の MCP、AI の操作が時間軸に段、`session.control.pause`、判断せず欠けを返す API | 有志の MCP は単機能（デバッガだけ・ロジアナだけ） |
| P10 | **回帰（CI）**: 前の版と同じ振る舞いか | 振る舞い（タイミング）の回帰は単体試験で捕まらない | `rules.check` の baseline・U07（CI の実行コマンド）・記録の比較 | 一般の CI はロジック、タイミングは見ない |
| P11 | **Linux のアプリとボード**（Cortex-A） | ユーザ空間・カーネル・ボードの信号の時刻が別 | 2.6: `linux.*` | TRACE32 は Linux を認識。外の信号との統合は TRACE32 + PI 級 |
| P12 | **日常の書き込み・ステップ** | 速さ・安定 | `target.flash`・`debug.*`・GDB サーバ | IDE と J-Link・ST-LINK で十分（fprobe の独自性は無い。壊れないことが条件） |

### 3.3 場面ごとの価値の材料

記号: 頻度・困る度合いは出典のあるものだけ書き、無いものは「資料なし」。代わりの道具は 2 章の事実から。
「fprobe の独自性」は、§2 の「他に無い」項目がどれだけ効くか（事実の数え上げ）。

| # | 頻度の資料 | 困る度合いの資料 | 代わりの道具 | fprobe の独自性（効く §2 の項目） | fprobe 側の前提・未確認 |
|---|---|---|---|---|---|
| P1 | 資料なし | 資料なし | TRACE32 + PI（高価）か、手で時刻を合わせる | 2.1・2.2 | ロジアナの線を配線。8 + 6 本 |
| P2 | Barr の 10 項目のうち 3 つ [S16] | 同左（再現しにくい） | 部分的に SystemView・Tracealyzer（組み込みが要る）、オシロ | 2.1（2.8） | 印の GPIO か ITM か testpoint の組み込みが要る場合あり |
| P3 | Barr の 10 項目すべてが「再現しにくい」 [S16][S17] | 調べ切れず閉じられる [S17] | TRACE32 級 | 2.1・2.2・2.3 | 記録の容量（分割で日単位）・欠けの明示は実装済み |
| P4 | Nordic・TI の公式フォーラムの定番の質問 [S18][S19] | 事故・接続断 [S18][S19] | J-Link + Ozone（Data Sampling・Monitor Mode）で多くは可 | 2.3 の差分（同じ時計・ハードの条件） | サンプラの速さ約 8 kHz（SWD 4 MHz） |
| P5 | 資料なし | 資料なし | IAR・Ozone・Memfault（中の状態は可、外の信号は不可） | 2.1・2.2（R29） | ETM の履歴は Pico 2 で実機未確認 |
| P6 | 資料なし（MCU + FPGA の構成の比率は未調査） | 資料なし | Zynq の内部だけ | **2.4・2.2・2.1（最も多い）** | 対象の FPGA に観測の回路を入れる |
| P7 | 資料なし（機能安全の規格では故障注入の試験が求められることがあるが、今回は確かめていない） | 資料なし | 研究の道具・自作 | 2.5・2.8・2.2 | 注入の台本は実機未確認（U25）。**専用の窓なし** |
| P8 | 資料なし | 資料なし | Ozone・SystemView・Tracealyzer | 低い | — |
| P9 | （fprobe の本来の目的） | AI が道具の判断を信じ込む（CLAUDE.md） | 有志の MCP（単機能）・Saleae の MCP | 2.7（2.1 の上に乗る） | AI の試験（2026-10-21）で 2 回とも原因に到達 |
| P10 | 資料なし | 資料なし | — | 2.5（規則）・2.1 | U07 は実機未確認 |
| P11 | 資料なし | 資料なし | TRACE32 | 2.6 | 時刻合わせの不確かさ |
| P12 | 毎日（日常） | — | IDE + J-Link 等で十分 | なし | — |

---

## 4. 推奨（統括への。順位は私の判断で、経験則を含む）

判断の物差し（これも私の選んだもの）: (a) §2 の「他に無い」がどれだけ効くか、(b) 目的と優先順（解析力 > 日常 >、
本来は AI の自動化）、(c) 頻度・困る度合いの資料がある程度、(d) 代わりの道具の無さ。

| 推奨の順 | 使い方（場面をまとめたもの） | 含む場面 | 理由 |
|---|---|---|---|
| 1 | **条件を置いて捕まえ、その前後を 1 本の時間軸で見る** | P3・P5・P1 | 解析力の中心（CLAUDE.md の 1）。2.1・2.2・2.3 が全部効く。再現しにくい不具合の資料がある。候補 1 を「捕まえる段取り」まで広げたもの |
| 2 | **FPGA と MCU（と回路）を同じ時間で見て切り分ける** | P6 | 他に無い度合いが最大で、実機の証拠（AI が原因に到達）もある。頻度の資料は無い |
| 3 | **時間を測る**（遅れ・ジッタ・区間・周期の規則） | P2（P8 の一部） | 不具合の原因の上位に直結 [S16]。共通の時計の価値が最も直接に出る |
| 4 | **試して確かめる**（刺激・故障注入・規則・基準との比較） | P7・P10 | AI の自動化（本来の目的）が最も効く（何百回の試行・合否・証跡）。他のプローブに見つからなかった。ただし画面が無く、実機の確認が残る |
| 5 | **止めずに見る** | P4 | 困る度合いの資料は強いが、単独なら J-Link + Ozone で多く代替できる。1 の中の手段（条件の源・欠けの無い記録）として扱うのがよい |
| 横断 | **AI の作業を人が同じ時間軸で見て、止めて引き継ぐ** | P9 | 場面ではなく、1〜4 のすべてに乗る層。独立の配置ではなく、時間軸の段と操作の記録の窓で満たす |
| 対象外（既定では） | 日常のデバッグ・性能の測定・Linux | P12・P8・P11 | P12・P8 は他の道具で足りる（独自性が低い）。P11 は対象（Cortex-A + Linux）がある人だけ。足せる窓として残す |

候補 3 つとの対応:

| 統括の候補 | 推奨での扱い |
|---|---|
| 壊れた瞬間の前後を見る | 推奨 1 に拡げる（「捕まえる」段取りと条件の窓が要る） |
| 止めると壊れる不具合を止めずに見る | 推奨 5。単独なら他にもある。差は同じ時計とハードの条件 |
| AI が試した結果を人が確かめる | 横断の層に。加えて、AI が**試す道具**（推奨 4）が要る |
| （欠けていた） | 推奨 2（FPGA と MCU）・推奨 3（時間を測る）・推奨 4（試して確かめる） |

---

## 5. 場面を画面でたどる（要る窓・操作、足りないもの）

窓の id は `registry.ts`。「無し」は専用の窓が無い（API パネルから呼ぶしかない）もの。

### 5.1 推奨 1: 条件を置いて捕まえ、前後を見る

| 手順 | 窓・操作 | 今の状態 |
|---|---|---|
| 接続・ELF | ツールバー（StatusLine）・`flash`（ELF の読み込み） | 有り |
| 見る値を決める | `dataSampling`（`watch.set`）・`logicAnalyzer`（ch の名前） | 有り |
| 条件を置く | ロジアナ・多段・番地一致・経路: `logicAnalyzer`（`xtrig.route/set_match/set_sequence`）。記録の条件: `system`（`recording.trigger_set`・`fpga_obs.trigger_set`）。値の一致: **無し**（`sampler.trigger_set`） | **ばらばら**: 同じクロストリガの経路を 2 つの窓が別々に書き、サンプラの源は窓が無い |
| 記録して待つ | `recordings`（`recording.start`・長期の記録）・`eventLog` | 有り |
| 起きた瞬間へ | `eventLog`（`recording.find`・印）・`timeline`（縮図 `recording.overview`） | 有り |
| その時刻の CPU の行 | `source`・`callStack`・`locals` | **時刻 → コードの位置が無い**（`recording.code_at` は案の段階。画面の作り直しの案 §4） |
| 止まった後 | `fault`（`target.fault`・`snapshot.save`）・`disassembly` | 有り。**止まった瞬間の直前の波形（R29）は未** |
| 調べた経緯を残す | `eventLog`（`recording.mark`） | 有り |

### 5.2 推奨 2: FPGA と MCU を切り分ける

| 手順 | 窓 | 今の状態 |
|---|---|---|
| 観測を開く | `fpgaObs` | 有り |
| MCU の値 | `dataSampling`・`watch` | 有り |
| 条件（FPGA → MCU の停止・ロジアナ） | `system` | 有り |
| 時間軸 | `timeline` | 有り |
| 結果の直前に多い出来事・区間ごと・継続時間 | `system` | 有り |
| 止まった MCU の中 | `system`「中を見る」→ `disassembly`・`callStack`・`registers` | 有り |
| その時刻のソースの行 | `source` | **時刻 → コードの位置が無い**（5.1 と同じ） |

`system` の既定の配置（`dock.ts` の `systemLayout`）がほぼこの場面の形。

### 5.3 推奨 3: 時間を測る

| 手順 | 窓 | 今の状態 |
|---|---|---|
| 印を出す | ロジアナの ch・ITM（`swoTrace`）・testpoint | testpoint は**無し**（`testpoint.*` に窓が無い） |
| 2 点間の時間の統計 | `eventLog`（`timing.measure`） | 有り。**`recording.intervals` は無し** |
| 割り込みの時間 | `interrupts`（`profile.exceptions`） | 有り |
| 区間のサイクル | `source`（`cycles.measure`） | 有り |
| 周期・区間の規則で合否 | — | **無し**（`rules.check`） |
| 時間軸で外れ値の回へ | `timeline` | 統計 → 実際の回への往復は要確認（UI 調査の「統計と生データの往復」） |

### 5.4 推奨 4: 試して確かめる

| 手順 | 窓 | 今の状態 |
|---|---|---|
| 刺激（端子・UART・RTT） | `lowLevel`（`gpio.*`・`xtrig.output_*`）・`terminal` | `lowLevel` は既定に入れない窓（低レベルの扱い） |
| 故障注入の台本と試行の結果 | — | **無し**（`inject.run`） |
| 規則の検査・基準との比較・証跡 | — | **無し**（`rules.check/states/report`） |
| フック | — | **無し**（`hooks.*`） |
| 結果を時間軸で | `timeline`（stream "inject"・"rule" の段） | 段は記録に入る（窓は有り） |

### 5.5 推奨 5: 止めずに見る

`dataSampling`・`watch`・`threads`・`rtosObjects`・`cpuLoad`・`terminal`／`log`・`timeline`、Monitor Mode の切り替えは
`breakpoints`（`target.debug_mode.set`）。そろっている。

### 5.6 横断: AI の作業を見る・引き継ぐ

`activity`（操作の記録）・`timeline` の操作の段（AI と人を分ける）・ツールバーの一時停止（`session.control.pause`）・
`favorites`／`api`（AI の呼び出しを同じ引数で開く、は案 §3.1 の段階）。そろっているが、**操作の記録の 1 件から同じ窓を
開く**は未。

### 5.7 足りないもののまとめ（既定の配置に関わるもの）

1. **時刻 → コードの位置**（`recording.code_at` と、時間軸のカーソルに合わせるソースの窓）。推奨 1・2 の両方で要る。
   いちばん効く欠け。
2. **クロストリガの経路を 1 か所で見て書く窓**: 今は `logicAnalyzer`（経路の表）と `system`（FPGA の条件・記録の条件）に
   分かれ、`sampler.trigger_set`・`xtrig.output_*` は別の所か窓が無い。源 × 動作の表は機器の事実（レジスタ）として 1 つ
   （全体設計 §4.5 の「源 × 動作のマトリクス」）。
3. **試す窓**（`inject.run`・`rules.check/report`・`hooks`）。推奨 4 の入口。
4. 止まった瞬間の直前の波形（R29）。
5. `recording.intervals`・`testpoint.*` の窓（推奨 3）。汎用の窓（案 §3.1）で足りる可能性がある。

---

## 6. 既定の配置への含意（推奨）

### 6.1 考え方（推奨）

- 既定は **1 つの配置で推奨 1 を始めから終わりまでたどれる**ことを最優先にする。推奨 2〜4 は、その配置に窓を
  1〜2 個足せば済む形にする（ケイパビリティ・接続しているものが無い窓は出さない。案の原則 4）。
- 今の `default`（デバッガの型）は P12 の配置で、IDE と分担する方針（案の原則 5）と重なる。fprobe の価値の出る窓
  （ロジアナ・条件・記録）が 1 つも無い。**既定を `default` から、時間軸が中心の配置に替える**のを推奨する。
  今の `default` は「デバッガの型」として名前で選べる配置に残す。

### 6.2 初めて開いたときに並べる窓（推奨）

| 位置 | 窓 | 理由（場面） |
|---|---|---|
| 中央（大） | `timeline` | 共通の時計（2.1）。推奨 1〜4 と横断のすべての終点 |
| 右（縦に全部） | 条件の窓（今は `system`。§5.7-2 の「クロストリガの経路を 1 か所で」に育てる） | 捕まえる段取り（推奨 1）、FPGA の条件（推奨 2）。fprobe だけの機能（2.2）の入口 |
| 下段・タブ 1 | `source`（＋ 時刻のコードの位置）・`callStack`・`locals` | 時間軸の時刻 → CPU の行（推奨 1・2）。止まったときの中 |
| 下段・タブ 2 | `logicAnalyzer` | 外の信号の名前・トリガ（推奨 1・3） |
| 下段・タブ 3 | `recordings`／`eventLog` | 記録の開始・長期・印・検索・時間の統計（推奨 1・3） |
| 下段・タブ 4 | `dataSampling` | 止めずに読む値の設定（推奨 1・5 の手段） |
| 下段・タブ 5 | `fault` | 壊れた後（P5） |
| 下段・タブ 6 | `activity` | AI の作業（横断） |
| ツールバー | 接続・止める・走らせる・一時停止（AI の保留） | 最小の操作 |

条件で出すもの（ケイパビリティ・状態で。推奨）:
- `fpgaObs` — FPGA の観測を開いたとき（推奨 2）。
- 試す窓（新設）— 窓ができたら既定の下段に（推奨 4）。それまでは名前で探して足す。
- `threads`・`rtosObjects` — ELF に RTOS があるとき（`rtos.info`）。

既定に入れない（足せる）: `registers`・`memory`・`peripherals`・`breakpoints`・`disassembly`・`watch`（`dataSampling` と
重なる）・`flash`・`profile`・`cpuLoad`・`heap`・`interrupts`・`callGraph`・`power`・`linux`・`swoTrace`・`terminal`／`log`
（出力があるときに出す案もある）・`lowLevel`・`dashboard`・`api`・`favorites`。
理由: P12・P8 の窓は他の道具で足りる、または特定の対象（Linux・RTOS）だけのもの。

### 6.3 この推奨の弱いところ（判断の材料として）

- 場面の頻度の資料がほとんど無い（§3.1）。推奨 2（FPGA と MCU）は、使う人の割合が分からない。fprobe の利用者
  （今は開発者 1 人と AI）の対象（Shrike・PYNQ・XIAO・Nucleo・Pi）に寄っている。
- 推奨 4 は、画面も実機の確認も残る。既定に入れる時期は、窓ができて実機で確かめた後。
- 既定の配置の見直しは、使われ方の記録（案 §3.4: どこから・誰が）を足してから、事実で行うのがよい。

---

## 7. 今の案（候補 3 つ）に欠けていたもの（短く）

1. 「起きた後に見る」だけで、**捕まえる段取り**（条件・長期の記録・待つ）が無かった。
2. いちばん他に無い **FPGA と MCU の連携**（実機の証拠あり）が無かった。
3. 不具合の原因の上位に直結する **時間を測る** が独立していなかった。
4. AI の本来の目的に最も効く **試して確かめる**（注入・規則・刺激・回帰）が無く、その窓も無い。
5. 候補 2・3 は単独では競合にもある（Ozone の Data Sampling、Saleae・J-Link の MCP）。fprobe の差（同じ時計・ハードの
   条件・1 つの MCP・判断しない）で言い直す必要がある。
6. 今の既定（`default`）はデバッガの型で、候補のどれにも合っていない（ロジアナ・条件・記録の窓が無い）。

---

## 8. 出典

- [S1] SEGGER, Ozone's Timeline Window: correlating and visualizing data sampling, current consumption, and program execution —
  https://www.segger.com/news/ozones-timeline-window-correlating-and-visualizing-data-sampling-current-consumption-and-program-execution
- [S2] SEGGER, Ozone Product Brief — https://www.segger.com/fileadmin/documents/Product_briefs/Product-Brief_Ozone_01.pdf
- [S3] SEGGER Forum, live tracing - timeline — https://forum.segger.com/thread/9560-solved-live-tracing-timeline
- [S4] Lauterbach, Logic Analyzer Extensions — https://www.lauterbach.com/pi.html ／ Advanced debug with PowerIntegrator —
  https://repo.lauterbach.com/publications/advanced_debug_with_powerintegrator.pdf
- [S5] Lauterbach, PowerIntegrator Logic Analyzer (LA-7940) — https://cn.lauterbach.com/products/LA-7940
- [S6] Saleae, Logic 2 MCP Server: Getting Started — https://docs.saleae.com/mcp/guides/getting-started ／ Saleae API Documentation — https://docs.saleae.com/
- [S7] AMD, UG908 ILA Cross Trigger — https://docs.amd.com/r/2023.2-English/ug908-vivado-programming-debugging/ILA-Cross-Trigger
- [S8] AMD, UG1400 FPGA to CPU Triggering — https://docs.amd.com/r/2023.2-English/ug1400-vitis-embedded/FPGA-to-CPU-Triggering
- [S9] Real Time Fault Injection Using On Chip Debug Infrastructures（Porto 大学）— https://repositorio-aberto.up.pt/handle/10216/84678
- [S10] jlink-mcp（有志）— https://glama.ai/mcp/servers/Klievan/jlink-mcp
- [S11] dbgprobe-mcp-server（有志）— https://glama.ai/mcp/servers/es617/dbgprobe-mcp-server
- [S12] Embedded Debugger MCP（probe-rs / OpenOCD、有志）— https://cdn.jsdelivr.net/gh/adancurusul/embedded-debugger-mcp@main/README.md
- [S13] The Debug Store, SEGGER の価格（税抜、取得日により変わる）— https://thedebugstore.com/en-de/collections/segger-microcontroller-gmbh-catalogue-debug-store
- [S14] Meilhaus, Saleae Logic Pro 16 — https://www.meilhaus.de/en/logic-pro-16.htm?b2b=1
- [S15] EE Times / EDN、Virtutech の調査（2007、二次情報）— https://www.eetimes.com/?p=1166090 ／ https://www.edn.com/?p=4071406
- [S16] Barr Group, Top 10 Causes of Nasty Firmware Bugs — https://barrgroup.com/blog/top-10-causes-nasty-firmware-bugs
- [S17] Works for me! Cannot reproduce – a large scale empirical study of non-reproducible bugs —
  https://www.mozillafoundation.org/es/research/library/works-for-me-cannot-reproducea-large-scale-empirical-study-of-non-reproducible-bugs/
- [S18] Nordic DevZone, How can I debug a SoftDevice application — https://devzone.nordicsemi.com/question/81/how-can-i-debug-a-softdevice-application/
- [S19] TI E2E, C2000 halts ... causing catastrophic failure in power converters —
  https://e2e.ti.com/support/microcontrollers/c2000-microcontrollers-group/c2000/f/c2000-microcontrollers-forum/1130272/tms320f28069-c2000-halts-when-there-is-high-noise-causing-catastrophic-failure-in-power-converters
  ／ real time debug options — https://e2e.ti.com/support/microcontrollers/c2000-microcontrollers-group/c2000/f/c2000-microcontrollers-forum/1050916/tms320f28076-real-time-debug-options-clarification
- 社内: docs/reports/2026-10-17-debug-ui-ux-survey.md（第 3 部 B）、2026-10-17-jlink-comparison.md（§7・§8）、
  2026-10-18-competitors-commercial.md（§9）、2026-10-18-gap-analysis.md（§4）、2026-10-21-system-analysis-loop.md、
  docs/superpowers/plans/2026-10-18-backlog.md（U25・U37・U41・B17・R15・R29）。

未確認のまま残したこと: 市販のプローブの故障注入の有無（見つからなかっただけ）、競合の MCP が判断を返すか、
Saleae の端子の出力、MCU + FPGA の構成の普及の割合、機能安全の規格での故障注入の要求の具体（ISO 26262 等）。
