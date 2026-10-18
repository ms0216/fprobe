# U45 RTOS の対応の拡大（続き）: Apache NuttX

µC/OS-III（`2026-10-19-u45-ucos3-design.md`）に続いて、U45 の **NuttX** を足す。NuttX は Apache-2.0 で、µC/OS-III と同じく
ヘッダを出典とライセンスを SOURCE.md に書いて取り込む。

## 1. 試せる本物

GitHub の apache/nuttx のタグ nuttx-13.1.0。NuttX のヘッダは自前の libc のヘッダ（sys/types.h・signal.h・semaphore.h 等）と
build が作る設定 `nuttx/config.h` に強く依るので、試験用の ELF が含める `nuttx/sched.h`・`semaphore.h`・`mutex.h`・
`mqueue.h`・`init.h`・`wdog.h` から GCC の -M で辿れる 69 のヘッダだけを、NuttX の build と同じ include の形
（`include/arch` = `arch/arm/include`、`include/arch/chip` = nRF52）で `host/firmware/nuttx_shape/nuttx/` に取り込んだ。
ほとんどは Apache-2.0、`list.h`・`list_type.h` は BSD-2-Clause、`sys/uio.h` は BSD-3-Clause（どれも条文はファイルの先頭）。
設定は `config/nuttx/config.h` に手で書いた（Cortex-M4F の nRF52832、CONFIG_ARCH_FPU・STACK_COLORATION・
PRIORITY_INHERITANCE・TASK_NAME_SIZE 31・SIG_SIGSTOP_ACTION）。

## 2. 口（既存の層に合わせる）

`fprobe.rtos.nuttx.NuttX`（`KernelAwareness`）を `open_rtos` の表に足す（`detect` は `"nuttx"`）。API・MCP の `rtos.*` は
RTOS に依らないので、そのまま NuttX で動く（summary の種類の列を更新）。

- 検出: 大域変数 `g_pidhash`・`g_npidhash` と型 `struct tcb_s`（`task_state`・`xcp.regs`）。
- タスクの一覧: `g_pidhash[g_npidhash]`（TCB の番地の表。位置は `pid & (g_npidhash − 1)`）。位置の合わないものは警告して
  外し、PID の順に並べる（`number` が PID）。タスクと pthread は同じ `struct tcb_s`（13.x では task_tcb_s は無い）。
- 名前: `name[]`（CONFIG_TASK_NAME_SIZE > 0）、無ければ "pid N"。
- 実行中: `g_running_tasks[0]`（無い版では `g_readytorun` の先頭）。状態 TSTATE_TASK_RUNNING のもの。
- 状態（`task_state`）: 値は設定（SMP・EVENTS・MQUEUE・SIGSTOP）で変わるので、DWARF の `enum tstate_e` の列挙子の名前で
  見る（同じ値は先の名前。TSTATE_SLEEPING は TSTATE_WAIT_SIG の別名）。RUNNING → running、READYTORUN・PENDING・ASSIGNED →
  ready、TASK_STOPPED（SIGSTOP）・TASK_INACTIVE（起動の前）→ suspended、TASK_INVALID → deleted、それ以外 → blocked。
- 待ち: WAIT_SEM → receive（semaphore。`flags` に SEM_TYPE_MUTEX があれば mutex で、持ち主は `val.mholder` の TID
  （bit 31 は待ち手の印）を PID の表で引く）、WAIT_MQNOTEMPTY → receive・WAIT_MQNOTFULL → send（message_queue）、
  WAIT_EVENT → event、WAIT_SIG は `sigwaitmask` が空なら delay（usleep・nxsched_ticksleep）、空でなければ event
  （kind signal、name は "SIGUSR1" 等）。待つものは `waitobj`、名前は変数のシンボル → 番地。
- 残りのティック: タスクの `waitdog`（`func` が NULL でなければ動いている）の `expired`（絶対のティック）− `g_system_ticks`。
  起きるティックは `expired`。
- 優先度: 大きい方が高い（`priority_order = 1`。POSIX、アイドルは 0）。元の優先度は `base_priority`
  （CONFIG_PRIORITY_INHERITANCE）。
- スタック: `stack_base_ptr` から `adj_stack_size`。CONFIG_STACK_COLORATION なら全体が語 0xdeadbeef（arm_checkstack.c）。
  `stackcheck.check_stack` を、1 バイトの塗りだけでなく底から繰り返すバイト列の塗りも受けるようにした（語の塗りなので、
  塗らない設定で偶然あふれと見なすことはまず無い）。止まっているタスクの SP は `xcp.regs`（文脈はスタックの上に積まれる）。
  実行中のタスクの SP は psp・sp のうちスタックの中にある方（CONFIG_ARCH_INTERRUPTSTACK の有無で PSP か MSP で動く）。
- 保存された文脈: `xcp.regs` が指す配列（armv7-m の arm_exception.S）。R13・BASEPRI・r4〜r11・CONTROL・EXC_RETURN・
  [s16〜s31 の枠]・ハードのフレーム。枠（CONFIG_ARCH_FPU）は EXC_RETURN が標準のフレームでも取ってある（subne）ので、
  有無は REG_R13 − regs（= XCPTCONTEXT_SIZE: FPU ありは 216、なしは 80。ハードのフレームの大きさは FPU の分を含む定数で
  数える）で見分け、合わなければ ELF の Tag_FP_arch（`cortexm.arm_has_fpu`）で決める。SP はハードのフレームを
  EXC_RETURN に従って外した値。新しいタスクの文脈は PC = start（nxtask_start）、LR = 0 で巻き戻しが止まる。
  ベースライン（ARMv6-M）の port は配列の並びが違うので、文脈は読まない（ValueError。一覧・状態・スタックは読める）。
- オブジェクト（`fprobe.rtos.nuttx_objects`）: NuttX にオブジェクトの一覧は無いので、型が sem_t・mutex_t・rmutex_t・
  struct mqueue_inode_s の変数（TCB の変数の中の join_sem 等は辿らない）と、タスクが待っているもの（ヒープの mq も）を
  合わせる。semaphore（count。負の semcount は待ち手の数なので 0）・mutex（持ち主の task・PID・優先度・継承、
  priority_inherit）・message_queue（count・max_msgs・msg_size、待ち手は waitfornotempty が受け取り・waitfornotfull が
  送り）。待ちの列の要素は TCB そのもの（先頭の flink）。
- サンプラ（時間軸・逆転の推移）: いまのタスクの語は `g_running_tasks[0]`、保存された SP の語は `xcp.regs`（SP より文脈の
  分だけ低い）。ミューテックスの持ち主は TID なので `mutex_words` は None（逆転の推移はサンプラでは辿れない。止めた瞬間の
  contention は出る）。アイドルは `g_idletcb[]`。
- スケジューラ: `g_nx_initstate` が OSINIT_OSREADY より前なら not_started（OSINIT_PANIC は unknown）、実行中のタスクの
  `lockcount` が正なら suspended（sched_lock）、ほかは running。ティックは `g_system_ticks`。版の変数は無い（None）。
- ヒープ: NuttX の mm_heap の要約は今回は扱わない（`heap()` は None）。

## 3. 残したもの

- ARMv6-M・ARMv8-M の port の文脈（配列の並びが違う。試せる本物のヘッダを足してから）。
- SMP（g_assignedtasks・CPU ごとの実行中）。`g_running_tasks[0]` だけを見る。
- ヒープ（mm_heap）、イベント（nxevent_t）のオブジェクトとしての一覧（待ちの kind event は出る）、メッセージキューの名前
  （inode の名前）。

## 4. 試験

`host/firmware/nuttx_shape/`: 本物のヘッダの構造体とカーネルの変数で、止めたときの様子を .data に置いた ELF
（arm-zephyr-eabi-gcc、Cortex-M4F。NuttX のカーネルそのものではない）。`host/tests/test_rtos_nuttx.py` が、10 のタスク
（アイドル・実行中・準備完了・セマフォの時間切れ付きの待ち・眠り（継承した持ち主）・ミューテックス待ち・空の mq の受け取り・
満杯の mq への送り・合図の待ち・SIGSTOP（壊れた底））、waitdog の残りのティック、0xdeadbeef の塗り、実行中のタスクの
psp / sp、初めの文脈・FPU の枠の無い文脈・巻き戻し、オブジェクト（持ち主の TID・継承・逆転）、位置の違う PID の表、
ベースラインの文脈を断ることを確かめる。
