# U37 止めずにデバッグする方式（モニタ方式。設計）

満たす競合の機能: SEG-27（J-Link の Monitor Mode Debugging）（`docs/reports/2026-10-18-gap-analysis.md`）。

## 目的

ふつうのデバッグ（以下「停止方式」= halting debug）は、ブレークポイント・ステップでコア全体を止める。モーター制御・
通信（BLE・CAN・USB）のように止めると壊れる・切れる処理があると、ブレークポイントが使えない。

モニタ方式では、デバッグの出来事（ブレークポイント・ウォッチポイント・1 命令の実行・ホストからの停止の要求）を
デバッグ用の例外 DebugMonitor（以下 DebugMon。例外番号 12）に送り、その処理（モニタ）の中で待つことを「止まった」
とみなす。DebugMon より優先度の高い割り込みは、止まっている間も動き続ける。

利用者・AI・GDB・画面からは、停止方式と同じ API（`target.halt`・`target.resume`・`target.step`・
`debug.breakpoint.*`・`debug.step`・レジスタ・呼び出し履歴・変数・式）がそのまま使える。違うのは方式の切り替え
（`target.debug_mode.set`）と、止まっている間に動き続ける処理があることだけ。

## J-Link の Monitor Mode との比較

| 項目 | J-Link（JLINK_MONITOR.c・JLINK_MONITOR_ISR_*.s） | fprobe（`host/firmware/monitor/`） |
|---|---|---|
| ターゲット側 | SEGGER の DebugMon_Handler（アセンブラ）と、呼び返しの C（OnEnter・OnExit・OnPoll） | `fprobe_monitor.c` の DebugMon_Handler（入口・出口だけアセンブラ）と、弱い定義の呼び返し `fprobe_monitor_on_enter / on_exit / on_poll` |
| やりとり | DCRDR（デバッグのレジスタ）を窓にした命令（レジスタの読み書き・GO 等を 1 語ずつ） | RAM の箱（`struct fprobe_monitor`）。モニタが入口で例外の枠を箱に写すので、レジスタは **ホストが SWD で箱を直接読み書き** する。命令は「再開」「1 命令」の 2 つだけ |
| 箱の見つけ方 | 固定（DCRDR） | ベクタ表の DebugMon（VTOR + 0x30）が指すハンドラの頭に印（`FPMN`）と箱の番地を埋める。ELF は要らない |
| 止める | DEMCR.MON_PEND | 同じ（DEMCR.MON_PEND） |
| ステップ | DEMCR.MON_STEP | 同じ。割り込みを止めるステップは BASEPRI で DebugMon より低い割り込みを止める（下記） |
| ブレークポイント | FPB と BKPT。Flash の無制限ブレークポイントもモニタ方式で使える | FPB と RAM の BKPT。**Flash の BKPT（U13 の無制限ブレークポイント）はモニタ方式では使わない**（下記） |
| C_DEBUGEN | J-Link が 0 にする | 同じ（`debug_mode.set` が 0 にする） |
| Cortex-M0/M0+ | 不可（DebugMon が無い） | 同じ。ARMv7-M・ARMv8-M Mainline だけ（ARMv6-M では `set` が断る） |
| 速さ | 命令ごとに DCRDR を往復 | レジスタの読みは SWD の 1 回のブロック読み（箱）。命令の往復は再開・ステップのときだけ |
| スタック | 割り込まれた処理のスタックの上で動く | 自分のスタックで動く（SP の下を空け、関数の呼び出し debug.call もできる） |
| ELF なしの使用 | 可 | 可（箱は印から引く） |

DCRDR を窓にする J-Link のやり方は、ホストとモニタが 1 語ずつ手を握るので遅い。fprobe は「止まっている間の
コンテキストはメモリにある」ことを使い、SWD のメモリアクセス（走っているコアでも使える AHB-AP）で直接読み書きする。
モニタの処理は短く（入口で保存・出口で復元・待ちの輪）、検証しやすい。

## 層と責務

| 層 | ファイル | 責務 |
|---|---|---|
| ターゲット | `host/firmware/monitor/fprobe_monitor.{h,c}` | DebugMon_Handler、箱、`fprobe_monitor_init(priority)`、呼び返し |
| 試験用ファーム | `host/firmware/monitor_demo/` | RAM で走る試験用（SysTick = 高い優先度、PendSV = 低い優先度、DebugMon は中間）。`build.py`（zig cc） |
| 手続き | `host/fprobe/monitor.py` | 箱の見つけ方・箱の配置（定数）・`MonitorTarget`（pyOCD の CortexM の口を、箱と枠の読み書きで実現する包み） |
| 道具 | `host/fprobe/probe_monitor.py` | Probe の mixin `MonitorModeTools`。方式の切り替え（`debug_mode_get / set`）、`Probe.target` を包む |
| API | `host/api/fprobe_api.json` | `target.debug_mode.get`（control・observes）、`target.debug_mode.set`（control。デーモンはコアを動かし得る操作に数える） |
| 画面 | `host/ui/.../BreakpointsPanel.svelte` | 「デバッグの方式」の欄（停止 / モニタの選択、DebugMon の優先度、止まっている間も動く割り込み、モニタが無い理由） |

`Probe.target` は、モニタ方式のとき `MonitorTarget` を返す。`Debugger`（debug.py）・行単位のステップ
（stepping.Runner）・関数の呼び出し（calling）・セミホスティング・ブレークポイントの動作・GDB サーバ（probe_gdb）・
呼び出し履歴（CFI の巻き戻し）は、すべて `Probe.target` の pyOCD の口（get_state・halt・resume・step・
read/write_core_register(s)_raw・set/remove_breakpoint・read32・メモリ）だけを使うので、何も変えずにモニタ方式で動く。
画面（host/ui）も API を通すので同じに見える。`target.status` はモニタ方式のとき `debug_mode: "monitor"` を添える。

## 箱（`struct fprobe_monitor`、32 ビット語、版 1）

割り込まれた処理のレジスタは **すべて箱にある**（モニタが入口で例外の枠を箱に写す）。ホストは箱を 1 回のブロック読み
（0x12C バイト）で読み、書くのも箱だけ。モニタは出口で、箱の SP の下に枠を積み直して戻る。

| 番地 | 名前 | 書く側 | 意味 |
|---|---|---|---|
| 0x00 | magic | T | `0x4E4D5046`（"FPMN"）。`fprobe_monitor_init` が最後に書く（初期化前は 0） |
| 0x04 | version | T | 1 |
| 0x08 | size | T | 箱の大きさ（0x12C） |
| 0x0C | flags | T | bit0: FPU の s16〜s31 を保存する版（`__ARM_FP` で作った）、bit1: モニタのスタックが溢れた |
| 0x10 | state | T | 0 = 走っている、1 = 止まっている（待ちの輪の中） |
| 0x14 | reason | T | 入口の DFSR（HALTED=1・BKPT=2・DWTTRAP=4・VCATCH=8・EXTERNAL=16。0 = ホストの停止の要求 MON_PEND） |
| 0x18 | stops | T | 入った回数（ステップの完了の判定） |
| 0x1C | seq_cmd | H | ホストが命令を出すたびに 1 増やす（cmd・arg を書いた後に書く） |
| 0x20 | cmd | H | 1 = GO（再開）、2 = STEP（1 命令） |
| 0x24 | arg | H | STEP: bit0 = 割り込みを止める |
| 0x28 | seq_ack | T | 処理した seq_cmd |
| 0x2C | result | T | 0 = 受理、1 = 知らない命令 |
| 0x30 | exc_return | T | 入口の LR（EXC_RETURN。bit2 = PSP の枠、bit4 = 0 なら FP 付きの枠） |
| 0x34 | msp | T | 入口の MSP（MSP の枠ならハードが積んだ枠の番地） |
| 0x38 | psp | T | 入口の PSP |
| 0x3C | primask | T/H | 入口の PRIMASK。出口でこの値に戻す |
| 0x40 | basepri | T/H | 入口の BASEPRI。出口でこの値に戻す |
| 0x44 | faultmask | T | 入口の FAULTMASK（例外からの戻りで 0 になるので書けない） |
| 0x48 | control | T | 入口の CONTROL（nPRIV だけ意味がある。SPSEL・FPCA は EXC_RETURN から） |
| 0x4C | sp | T/H | 割り込まれた処理の SP。出口でこの下に枠を積み直す（8 バイト境界。詰めたら xpsr.bit9） |
| 0x50 | frame[26] | T/H | 例外の枠の写し: r0 r1 r2 r3 r12 lr pc xpsr（xpsr.bit9 は消してある）、FP 付きなら s0〜s15 fpscr 予約 |
| 0xB8 | r4〜r11 | T/H | 入口で保存し、出口で書き戻す |
| 0xD8 | s16〜s31 | T/H | FP 付きの枠のときだけ保存・書き戻す |
| 0x118 | exit_frame | T | 出口で積み直した枠の番地（内部） |
| 0x11C | step_basepri | T | 割り込みを止めるステップの前の BASEPRI（内部） |
| 0x120 | step_forced | T | 割り込みを止めるステップで書いた BASEPRI（0 = していない。内部） |
| 0x124 | prio_bits | T | 実装された優先度のビット（例 0xF0。init が測る） |
| 0x128 | stack_size | T | モニタのスタックの大きさ（バイト） |

T = ターゲット（モニタ）が書く、H = ホストが書く。配置は C の `_Static_assert` と、試験（試験用ファームの DWARF から
構造体のメンバーの位置を読んで `fprobe.monitor` の定数と比べる）で固定する。

### モニタのスタック

モニタは自分のスタック（`fprobe_monitor_stack`、`FPROBE_MONITOR_STACK_SIZE` 既定 1024 バイト）に切り替えて動く。
割り込まれた処理の SP より下のメモリをモニタが使わないので、停止方式と同じく、ホストはそこを関数の呼び出し（debug.call）の
引数の置き場所に使える。止まっている間に動く優先度の高い割り込みもこのスタックに積むので、大きさはその深さの分も足す。
スタックの底の印（"MSTK"）が壊れたら flags.bit1 を立て、ホストは `debug_mode.get` の `stack_overflow` で知らせる。

### 割り込まれた処理のレジスタ

| レジスタ | 箱の場所 |
|---|---|
| r0〜r3・r12・lr・pc・xpsr | frame[0〜7] |
| r4〜r11 | r4〜r11 |
| sp | sp |
| msp / psp | 枠のある側（EXC_RETURN.bit2）は sp、もう一方は箱の msp / psp |
| primask・basepri・faultmask | 同名 |
| control | 箱の nPRIV、SPSEL = EXC_RETURN.bit2、FPCA = !EXC_RETURN.bit4 |
| s0〜s15・fpscr | frame[8〜24]（モニタの入口の FP 命令で遅延退避を起こして枠に入れたもの） |
| s16〜s31 | s16〜s31 |

FP 付きでない枠（割り込まれた処理が FPU を使っていない）では FP のレジスタは割り込まれた処理の文脈に無いので、
読みは誤り（GDB は「読めない」として扱う）。

書き込み: 上の場所に書く。sp（と、枠のある側の msp / psp）は 4 バイト境界。faultmask・control・枠の無い側の
msp / psp は書けない（いまと同じ値の書き込みは受け付ける。関数の呼び出しの後のレジスタの書き戻しのため）。

## 手続き

### 切り替え（`target.debug_mode.set`）

halt → monitor:
1. コアが ARMv7-M / ARMv8-M Mainline であること（ARMv6-M は DebugMon が無い）。
2. 箱を見つける: VTOR → ベクタ 12 → ハンドラの頭 16 バイトに印 `FPMN` と箱の番地。無ければ
   「ファームにモニタ（host/firmware/monitor）が組み込まれていない」。箱の magic・version を確かめる。magic が 0 なら
   「fprobe_monitor_init が呼ばれていない」。
3. Flash に BKPT を書いたブレークポイント（U13）があれば断る（モニタ方式では Flash を書き換えられない。先に外す）。
4. 優先度（priority を渡せば SHPR3 の DebugMon の欄に書く）。
5. DEMCR に MON_EN（と TRCENA）。止まっていたら MON_PEND も立ててから再開し（DebugMon が最初の命令より前に入る =
   同じ PC で「止まった」になる）、DHCSR ← DBGKEY（C_DEBUGEN = 0）。
6. クロストリガの停止の書き込み（FPGA の割り込み書き込み。既定は DHCSR ← C_HALT）を DEMCR ← MON_PEND に替える
   （ケイパビリティに inject があるとき）。

monitor → halt:
1. 止まっていれば、割り込まれた処理の PC に一時のブレークポイントを置き、DHCSR ← C_DEBUGEN、GO。戻った処理が
   そのブレークポイントで停止方式の停止になる（= 同じ PC で止まったまま切り替わる）。一時のブレークポイントは外す。
   置けなければ（比較器が尽きた）GO の直後に C_HALT。
2. 走っていれば DHCSR ← C_DEBUGEN だけ（走ったまま）。
3. DEMCR の MON_EN を消す。クロストリガの停止の書き込みを DHCSR ← C_HALT に戻す。

切断（`target.disconnect`）では、止まっていれば GO して離す（モニタ方式は切る）。

### 止める・再開・ステップ

- 状態: 箱の state（1 回の SWD 読み）。止まっていれば HALTED。
- 止まった理由（`get_halt_reason`・DFSR の読み）: 箱の reason。stepping.Runner・GDB サーバが DFSR を直接読むので、
  `MonitorTarget` は DFSR（0xE000ED30）の読み書きを箱の reason に向ける。
- halt: DEMCR.MON_PEND を立て、state が 1 になるまで待つ（既定 0.5 秒）。入らなければ MON_PEND を下ろして誤り
  （DebugMon より優先度の高い処理・PRIMASK で割り込み禁止の区間に留まっている、またはモニタが無い）。
- resume: cmd = GO、seq_cmd を増やし、seq_ack が追いつくまで待つ。
- step: cmd = STEP（arg.bit0 = 割り込みを止める）、stops が増えるまで待つ。モニタは DEMCR.MON_STEP を立てて戻る。
  1 命令の後に DebugMon（DFSR.HALTED）。割り込みを止めるステップでは、モニタは BASEPRI を「DebugMon の 1 段下」
  （DebugMon の優先度 + 実装の最小の段）にして戻り、次の入口で、BASEPRI がその値のままなら元に戻す（進めた命令が
  BASEPRI を書き替えたなら、その値を残す）。DebugMon が最も低い優先度のときは、同じ段の割り込みは止められない
  （stepping.LineStepper は割り込みの入口を検出して踏み越えるので、行のステップはそれでも正しく進む）。
- ステップの命令が故障を起こし、故障の処理（優先度 −1）から戻らないと、ステップは時間切れの誤り。その場合は
  `target.debug_mode.set(mode="halt")` で停止方式にして `target.fault` で調べる。

### ブレークポイント・ウォッチポイント

- FPB の比較器（Flash・ARMv7-M の FPB 版 1 はコードの領域 0〜0x1FFFFFFF だけ）と、RAM の BKPT（pyOCD の
  ソフトのブレークポイント）。C_DEBUGEN = 0 かつ MON_EN = 1 では、どちらも DebugMon に行く（DFSR.BKPT）。戻り先は
  その命令そのもの。
- モニタ方式では、置く・外すをすぐに書く（コアは走っているので、再開の直前に溜めて書く pyOCD のやり方は使えない）。
- Flash の番地は比較器だけ（AUTO → HW）。Flash の BKPT（U13）は使わない: Flash の書き換えにはコアを止めて書き込み
  アルゴリズムを走らせる必要があり、モニタ方式の目的（止めない）と反する。比較器が尽きたら誤り。
- DWT のウォッチポイントは DebugMon に行く（DFSR.DWTTRAP。命令の後で止まる）。
- **DebugMon 以上の優先度で走る処理（優先度の高い割り込み・故障の処理・NMI）の中のブレークポイントは使えない**。
  BKPT・FPB は HardFault に格上げされ、DWT は無視される（アーキテクチャの決まり）。API は置くことを拒まない（番地が
  どの優先度で走るかはホストに分からない）ので、仕様と MCP の案内で知らせる。
- ベクタキャッチは停止方式だけの機能（C_DEBUGEN が要る）。モニタ方式では `target.vector_catch` の設定は誤り。

## 割り込みの優先度の決まり

Cortex-M の優先度は数が小さいほど高い。DebugMon の優先度を P とする（`fprobe_monitor_init(priority)` か
`debug_mode.set(priority=…)`。SHPR3 の下位 8 ビット）。

| 処理 | 止まっている間 |
|---|---|
| 優先度の数 < P の割り込み（例: モーター制御の PWM・電流の割り込み） | **動き続ける** |
| 優先度の数 ≥ P の割り込み・スレッド（main・RTOS のタスク） | 止まる（DebugMon に入れない・戻らない） |
| NMI・HardFault | 動く（優先度 −2・−1） |

- P は「動き続けさせたい割り込み」より低く（数を大きく）、「止めたい処理」より高く（数を小さく）する。既定は
  `FPROBE_MONITOR_PRIORITY`（`fprobe_monitor.h`、既定 0xFF = 最も低い。実装のビットに丸められる）。
- PRIMASK = 1・BASEPRI ≤ P の区間（クリティカルセクション）ではモニタに入れない。halt はその区間を抜けてから止まる
  （入れないまま 0.5 秒過ぎれば誤り）。そこにブレークポイントがあると HardFault になる（上記）。
- `target.debug_mode.get` は、P より高い優先度で有効な例外・割り込み（`keeps_running`。SysTick・PendSV・SVCall・
  MemManage/BusFault/UsageFault・NVIC の有効な IRQ）を返す。AI はこれで「止まっている間に何が動くか」を知る。

## 止まっている間の時計・ウォッチドッグ

- 止まっているのはソフトの文脈だけで、クロック・周辺（タイマ・DMA・通信）は止まらない。優先度の高い割り込みが
  それを処理する。DBGMCU の「デバッグ中にタイマを止める」設定は停止方式（C_DEBUGEN の停止）にしか効かない。
- SysTick: 優先度が P より高ければ数え続ける（試験用ファームの `fast_ticks`）。低ければ割り込みは保留のまま
  （カウンタ自体は回り、COUNTFLAG が立つ。再開後に 1 回だけ入る）。
- ウォッチドッグ: 止まっている間もコアは DebugMon の待ちの輪を回るので、IWDG / WWDG は止まらない（DBGMCU の
  停止は効かない）。待ちの輪は毎回 `fprobe_monitor_on_poll()`（弱い定義。既定は何もしない）を呼ぶので、
  そこでウォッチドッグを蹴る（J-Link の JLINK_MONITOR_OnPoll と同じ）。止まった・再開の出来事には
  `fprobe_monitor_on_enter()` / `fprobe_monitor_on_exit()`（例: モーターを安全な出力にする・戻す）。
- 時刻（DWT.CYCCNT・fprobe の共通タイムスタンプ）は止まらない。記録（recording）・RTT・変数の観測（watch・
  サンプラ）・SWO も止まっている間に動き続ける（コアが走っているので AHB-AP のアクセスは常にできる）。

## Flash ブレークポイント（U13）との関係

- モニタ方式では Flash に BKPT を書かない（上記）。`debug.breakpoint.flash` の enabled はそのまま（停止方式に戻れば
  使われる）。モニタ方式に切り替えるとき、Flash に書いた・書く予定のブレークポイントがあれば断る（`debug.breakpoint.clear`
  か `debug.breakpoint.flash_restore` の後に）。休眠中の BKPT は切り替えのときに戻す。
- Flash の書き込み（`target.flash` 等）・止めてのリセット（`target.reset(halt=true)`・`target.program_ram`）は
  コアを停止方式で止めるので、モニタ方式では誤り（先に `target.debug_mode.set(mode="halt")`）。止めないリセット
  （`target.reset`）はできる（DEMCR は電源投入のリセットでしか消えない。リセット後はファームが init を呼ぶまで
  halt が誤りになる）。

## RTOS との関係

- 止まったときの「いまのタスク」は、モニタが割り込んだ文脈（箱）のレジスタで見える。止まっていない
  タスクは TCB の保存された文脈から（既存の rtos と同じ）。GDB のスレッドも同じ。
- FreeRTOS 等のカーネルの割り込み（SysTick・PendSV）は普通は最も低い優先度。DebugMon をそれと同じか高くすると、
  止まっている間はスケジューラが止まる（タスクは切り替わらない。RTOS の時間も進まない）。カーネルより高い割り込み
  （configMAX_SYSCALL_INTERRUPT_PRIORITY より高い = FreeRTOS の API を呼ばない割り込み）は動き続ける。
- DebugMon の優先度を configMAX_SYSCALL_INTERRUPT_PRIORITY 以下（数が大きい）にすると、クリティカルセクションの中で
  止まることは無い（BASEPRI が DebugMon も止める）。より高くすると、クリティカルセクションの中でも止まる（カーネルの
  データ構造が途中の状態で見えることがある）。推奨は前者。
- 止まっている間に動く割り込みが `...FromISR` を呼ぶと、PendSV が保留になり、再開の後にタスクが切り替わる。

## 誤り（分かりやすく返す）

| 状況 | 誤りの文 |
|---|---|
| ベクタ 12 のハンドラに印が無い | ファームにモニタが組み込まれていない（host/firmware/monitor を組み込み、DebugMon_Handler をベクタ表に置く） |
| magic が 0 | fprobe_monitor_init が呼ばれていない |
| 版が違う | 箱の版が違う（ファームのモニタを作り直す） |
| ARMv6-M | このコアに DebugMonitor が無い |
| halt の時間切れ | DebugMon に入らない（優先度の高い処理・割り込み禁止の区間に留まっている）。優先度は… |
| ステップの時間切れ | 1 命令の後に DebugMon に戻らない（故障の可能性。停止方式にして target.fault） |
| 停止方式だけの操作 | モニタ方式ではできない（target.debug_mode.set(mode="halt") の後に） |

## API

| メソッド | 種類 | 引数 | 返す |
|---|---|---|---|
| `target.debug_mode.get` | control（observes） | なし | mode（halt / monitor）、available（モニタが見つかるか）、reason（見つからない理由）、box（番地）、version、priority（DebugMon の優先度）、prio_bits、state、stops、keeps_running |
| `target.debug_mode.set` | control | mode（halt / monitor）、priority（0〜255、省略可） | get と同じ（切り替えた後） |

MCP の道具は `target_debug_mode_get` / `target_debug_mode_set`。案内（INSTRUCTIONS）に 1 段落。

## 試験

- 偽物（`host/tests/monitor_fake.py`）: モニタのファームと同じ規則で動く小さなコア。スレッドの命令を進め、
  高い優先度の割り込み（fast_ticks）は止まっていても数え、低い優先度の割り込み（slow_ticks）とスレッド（loop_count）
  は止まる。DebugMon の入口で枠を積み（FP 付き・整列の詰め物の版も）、箱を書き、命令を処理する。停止方式の口も持つ
  （切り替えの試験）。
- `host/tests/test_monitor.py`: 箱の見つけ方、レジスタの対応（箱・sp・control・FP）、書き込み、sp の書き込み
  （枠を移す）、halt / resume / step、割り込みを止めるステップ、ブレークポイント・ウォッチポイントの理由、誤り。
  試験用ファームの DWARF の構造体の配置が `fprobe.monitor` の定数と一致すること、ハンドラの頭の印。
- `host/tests/test_probe_monitor.py`: Probe の道具で、切り替え・`debug.run_until_halt`・`target.step`・
  `debug.backtrace`・`debug.step`（行）・GDB の操作・status・止まっている間に fast_ticks が増えること。
- 実機（`gateware/tests/hil/test_monitor_mode_hw.py`、Nucleo F401RE、走らせるのは本流の担当）: 試験用ファームを
  RAM に読み込み、モニタ方式でブレークポイントに止め、SysTick（高い優先度）が数え続け、PendSV（低い優先度）と
  main が止まることを確かめる。ステップ・レジスタ・呼び出し履歴・halt / resume・停止方式への戻り。
