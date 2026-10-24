# U04 デーモン内蔵の GDB サーバ（設計）

満たす競合の機能: SEG-11, SEG-32, SEG-36, KEIL-17, CUBE-01, OOCD-10（複数 GDB 接続の部分）, PRS-06, PRS-07, BMP-01,
VSC-01, VSC-07, VSC-08, GDB-05, KMC-06（`docs/reports/2026-10-18-gap-analysis.md`）。

## 目的

外部の GDB サーバ（OpenOCD・pyOCD）は USB を自分で握るので、デーモン（ロジアナ・記録・AI・画面）と同時に使えない。
デーモンが握る 1 つのセッションの上で GDB リモートシリアルプロトコル（RSP）を出し、IDE（Cortex-Debug・CubeIDE・
Keil Studio 等の GDB を使うもの）と、ロジアナ・記録・AI を同時に使えるようにする。

## 層と責務

| 層 | ファイル | 責務 |
|---|---|---|
| 枠 | `fprobe/gdb/rsp.py` | パケットの組み立て・読み取り（`$…#cc`、`+`/`-`、^C）、バイナリの逃がし、16 進 |
| 記述 | `fprobe/gdb/description.py` | レジスタの並び（target.xml）、メモリマップ、スレッドの XML。機器に依らない |
| 対話 | `fprobe/gdb/session.py` | 1 接続の RSP の処理（all-stop）。機器の操作は `GdbHost.run` に渡す関数で行う |
| 待ち受け | `fprobe/gdb/server.py` | TCP の待ち受け、接続ごとに利用者（`Client`、via = "gdb"）を作り、デーモンの調停と記録につなぐ |
| 機器 | `fprobe/probe_gdb.py` | Probe の mixin `GdbTools`。RSP の粒度の操作（レジスタ・メモリ・ブレークポイント・実行・停止の判定・スレッド・Flash）。必ず `_lock` の中 |
| 試験用 | `fprobe/gdb/client.py` | 最小の RSP クライアント（試験と実機試験で使う） |

`GdbSession` は機器を直接触らない。すべて `host.run(fn, control=…, record=…, moves=…)` を通り、デーモンでは
`Daemon.run`（調停の作業スレッドで実行・操作の記録・見張りの起こし）になる。よって人・AI・GDB の操作は 1 本の
作業スレッドで順に実行され、人の一時停止（`session.control.pause`）も GDB に効く（GDB の利用者の role は既定 human。
IDE を操作するのは人なので。`--gdb-role ai` で AI が GDB を動かす使い方にも合わせられる）。

## デーモン

    fprobe-daemon --serial … --gdb 3333 [--gdb-target stm32f401xe] [--gdb-role human]

- 待ち受けは常に 127.0.0.1（RSP には認証が無い。`--lan` でも LAN には開かない。遠隔は SSH のトンネルで）。
- `session.status` に `gdb`（待ち受けのポート）、利用者の一覧（`clients` 通知）に GDB の接続（via = "gdb"）が出る。
- GDB の操作は操作の記録（activity）に `gdb.*` として残る（continue・step・interrupt・レジスタ/メモリの書き込み・
  Flash・reset・monitor・attach・detach）。読み・ブレークポイントの出し入れ（GDB は停止のたびに全部を出し入れする）は残さない。

## RSP

- 対応: qSupported、QStartNoAckMode、?、g/G、p/P、m/M/X、Z0〜Z4/z0〜z4、c/C/s/S、vCont（c/C/s/S）、^C、
  qXfer:features:read（target.xml）、qXfer:memory-map:read、qXfer:threads:read、qfThreadInfo/qsThreadInfo、qC、
  H、T、qThreadExtraInfo、qRcmd（monitor）、vFlashErase/vFlashWrite/vFlashDone、qAttached、D、k、vKill。
- 誤りは GDB が error-message+ を申告していれば `E.<文>`（一時停止で保留・機器の誤り等を IDE に出す）。
- レジスタ: m-profile（r0〜r12・sp・lr・pc・xpsr）、m-system（msp・psp・primask・basepri・faultmask・control。
  コアにあるものだけ）、FPU があれば vfp（d0〜d15・fpscr）。
- 停止の理由: breakpoint → SIGTRAP + swbreak/hwbreak、watchpoint → SIGTRAP + watch/rwatch/awatch（DWT の MATCHED で
  どれが当たったか）、step → SIGTRAP、人・AI が止めた / ^C → SIGINT、故障（`target_fault`）→ SIGSEGV と原因を
  コンソールに、セミホスティングの SYS_EXIT → `W<code>`。セミホスティングの出力は実行中にコンソール（O パケット）へ。

## 共有のブレークポイント

GDB のブレークポイントは Probe の Debugger の表（`debug.breakpoint.list` で見える）に入る。場所の名前は
`gdb:0x…`。GDB が外すのは自分が置いたもの（接続ごとの持ち主の集合）だけで、AI・画面が置いた同じ番地の
ブレークポイント（動作つきを含む）は外さない。切断・detach で、その接続が置いたものを外す。

## RTOS のスレッド

ELF が読み込まれていて（`debug.elf.load`、または `monitor elf <path>`）RTOS（FreeRTOS / Zephyr）が見つかれば、
停止中のタスクを GDB のスレッドにする（スレッド ID = TCB の番地）。実行中のタスクは実レジスタ、ほかのタスクは
保存された文脈（`rtos.cortexm`）から読む。保存された文脈のタスクのレジスタは書けない（誤り）。
`monitor threads off` でスレッドを 1 つ（ID 1）にできる。

## monitor

`help`、`reset [halt|init|run]`、`elf <path>`、`threads [on|off]`、`pause`・`resume`（人の一時停止）、`status`、
`<API のメソッド名> [JSON | key=value …]`（例 `monitor la.capture {"mask": 1, "value": 1}`、
`monitor recording.start name=crash`）。結果は JSON でコンソールに出す。API の呼び出しは他の利用者と同じ調停・記録を通る。

## 接続と切断

- 接続（GDB の最初の要求）でターゲットが未接続なら `--gdb-target` で接続して止める。動いていれば止める
  （all-stop の GDB は止まった状態から始まる）。
- detach（D）: 置いたブレークポイントを外して再開。切断・k: 置いたものを外すだけ（ターゲットはそのまま）。
- all-stop の GDB は、他の利用者がターゲットを再開・停止しても知らない（再開中に止まれば停止として返す）。

## トレースポイント（U48）

満たす競合の機能: GDB-03, GDB-04。GDB の `trace` / `actions`（collect・while-stepping）/ `tstart` / `tstop` /
`tstatus` / `tfind` / `tdump` / `passcount` / `condition` / `tvariable` / `set disconnected-tracing` がそのまま使える。

| 層 | ファイル | 責務 |
|---|---|---|
| 式 | `fprobe/gdb/agent.py` | GDB のエージェント式（バイトコード）の評価器。条件と collect の式。浮動小数・printf は扱わない |
| 実験 | `fprobe/gdb/trace.py` | `Experiment`: QTDP 等の読み取り、当たったときの収集、バッファ（大きさ・循環）、QTFrame の検索、qTStatus・qTP・qTV、アップロード（qTfP・qTfV）。機器に依らない（当たったコアは `HitTarget` 越し） |
| 機器 | `fprobe/probe_gdb_trace.py` | Probe の mixin `GdbTraceTools`。実験を持ち（GDB がつなぎ直しても続く）、有効なトレースポイントにブレークポイントを置き、当たったら集めて続ける |
| 対話 | `fprobe/gdb/session.py` | QT… / qT… を実験へ。フレームを選んでいる間（QTFrame）は g・p・m をそのフレームで答え、書き込み・再開は断る |

- 対応: QTinit、QTDP（R・M・X、while-stepping の S、条件 X、パスカウント）、QTDPsrc、QTDV、QTro、
  QTBuffer:size / circular、QTNotes、QTDisconnected、QTStart、QTStop、QTEnable / QTDisable、qTStatus、qTP、qTV、
  qTfP / qTsP、qTfV / qTsV、QTFrame（番号・pc・tdp・range・outside）、qXfer:traceframe-info:read。
  qSupported で ConditionalTracepoints・TracepointSource・EnableDisableTracepoints・DisconnectedTracing・
  InstallInTrace・tracenz を申告する。ファスト・トレースポイント（ftrace）・静的なマーカ・qTBuffer（tsave -r）は扱わない。
- **当たるたびに止める**: Cortex-M には PC の通過を止めずに知らせる仕組みが無い（DWT の比較器の PC 一致は停止か
  ETM へのきっかけだけで、DWT のデータトレースは番地への読み書きにしか効かない。ETM の受信・解読は U34・U35 の後）。
  よってトレースポイントはブレークポイント（ログ・ブレークポイントと同じ `_break_hooks`）で、止まった一瞬に集めて
  続ける。1 回に数 ms と、止まったことに気づくまでの見張りの周期（GDB の continue 中は `Options.poll_s`、それ以外は
  背景の見張り）。集めた時間は `monitor trace` の halts に出す。止めずに値を追うなら変数のライブ観測（`watch.set`）。
- **止めると壊れうるターゲット**: 断るのは使う側が明示したときだけ。`monitor trace deny-halt`（この接続だけ）なら、
  モニタ方式（`target.debug_mode.set` mode=monitor。止まっている間も優先度の高い割り込みは動く）でなければ QTStart を
  理由つきで断る（コンソールにも出す）。既定（`allow-halt`）は断らない。ELF の名前（`debug.halt_risk`）の一致だけを
  根拠に既定で断ることはしない（2026-10-23。名前の一致は「止めると壊れる」の証拠ではない）。
- 共有のブレークポイント: トレースポイントが置いたものの名前は `gdb-trace:0x…`（`debug.breakpoint.list` の tool は
  `gdb.trace`）。同じ番地に GDB・AI・画面のブレークポイントがあれば、集めてから止める。GDB がその番地のブレークポイントを
  外してもトレースポイントの分は残り、トレースを止めても GDB の分は残る。`debug.breakpoint.clear`（全部）で実験は
  誤り（terror）で止まる。
- 切断: QTDisconnected:1 なら実験を続ける（つなぎ直した GDB が qTStatus・qTfP で見つける）。そうでなければ
  tdisconnected で止めてブレークポイントを外す。
- 記録中なら当たるたびに stream "debug"・kind "tracepoint"（a = 番地、body = トレースポイントとフレームの番号）を入れる。
- 誤り: 条件・collect の式が評価できない（0 で割った・読めない番地等）と terror で止める（GDB の tstatus に理由）。
  M の範囲が読めないときは集めずに続ける（GDB には <unavailable>）。フレームに無いメモリは、QTro の区画なら実物を読む。

## 未対応

非停止モード、マルチコア（U31 の後）、範囲ステップ（vCont;r）、条件の評価を
スタブで行う Z の条件（ConditionalBreakpoints）。vFlash は pyOCD の MemoryLoader（`target.flash` と同じ層）で
書き、U03（Flash のアルゴリズム）が入ったらそちらに替える。
