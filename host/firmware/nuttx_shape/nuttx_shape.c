// fprobe の RTOS の認識（U45）の試験用: Apache NuttX の本物のヘッダ（nuttx/include/nuttx/sched.h・semaphore.h・
// mutex.h・mqueue.h、ARMv7-M の arch/irq.h 等。NuttX 13.1.0）の構造体で、止めたときのカーネルの様子を .data の初期値に
// 置いた小さな ELF。NuttX のカーネル（sched/・arch/arm/src/）そのものは含まない。設定 config/nuttx/config.h は手で書いた
// （Cortex-M4F の nRF52832、CONFIG_ARCH_FPU・STACK_COLORATION・PRIORITY_INHERITANCE・TASK_NAME_SIZE 31）。
// 構造体の配置はヘッダのまま（コンパイラが決めた形が DWARF に入る）。カーネルの変数は sched/ の定義と同じ型で置く:
// - タスクの一覧は g_pidhash[g_npidhash]（位置は pid & (g_npidhash - 1)。sched/init/nx_start.c）。アイドルは g_idletcb[0]
// - 実行中は g_running_tasks[0]（= 準備完了の列 g_readytorun の先頭）、状態は task_state（enum tstate_e）
// - 待つものは waitobj（sem_t・mqueue_inode_s）、待ちの列は sem の waitlist・mq の waitfornotempty / notfull（優先度順。
//   列の要素は TCB そのもの（先頭の flink）を指す）
// - 時間切れはタスクの waitdog（func が NULL でなければ動いている。expired は絶対のティック。時計は g_system_ticks）
// - ミューテックス（SEM_TYPE_MUTEX）の持ち主は val.mholder の TID（bit 31 は待ち手がいる印）
// - スタックは stack_base_ptr から adj_stack_size。CONFIG_STACK_COLORATION で全体を 0xdeadbeef で塗る（arm_checkstack.c）
// - 保存された文脈は xcp.regs が指す配列（arm_exception.S。スタックの上に積まれる）: R13・BASEPRI・r4〜r11・CONTROL・
//   EXC_RETURN・s16〜s31 の枠（CONFIG_ARCH_FPU）・ハードのフレーム。REG_R13 は regs + XCPTCONTEXT_SIZE
// 作り方: python host/firmware/nuttx_shape/build.py（arm-zephyr-eabi-gcc。生成した nuttx_shape.elf はリポジトリに置く）。
#include <nuttx/config.h>
#include <nuttx/init.h>
#include <nuttx/mqueue.h>
#include <nuttx/mutex.h>
#include <nuttx/sched.h>
#include <nuttx/semaphore.h>
#include <nuttx/wdog.h>

#define STACK_SIZE 512u
#define STACK_WORDS (STACK_SIZE / 4u)
#define STACK_COLOR 0xdeadbeefu                         // arch/arm/src/common/arm_internal.h
#define CTX (STACK_WORDS - XCPTCONTEXT_REGS)            // 文脈（XCPTCONTEXT_SIZE = 216 バイト）の位置
#define TICKS 1000

void nxtask_start(void);

// 止まったタスクの文脈（up_initial_state と同じ形: REG_R13 = スタックの上、PC = start（Thumb の印は付いたまま）、
// EXC_RETURN = EXC_RETURN_THREAD（CONFIG_ARCH_FPU なので拡張フレームの 0xFFFFFFE9））
#define CONTEXT_WORDS(stk) [CTX + REG_R13] = (uint32_t) &(stk)[STACK_WORDS], [CTX + REG_EXC_RETURN] = 0xFFFFFFE9u, \
                           [CTX + REG_PC] = (uint32_t) nxtask_start, [CTX + REG_XPSR] = 0x01000000u
// 全体を塗り、上の 256 バイトは使った（文脈はその中）
#define STACK(stk) uint32_t stk[STACK_WORDS] __attribute__((aligned(8))) = \
    {[0 ... STACK_WORDS - 64u - 1u] = STACK_COLOR, CONTEXT_WORDS(stk)}

// --- スタック ----------------------------------------------------------------------------------------
STACK(idle_stack);
STACK(main_stack);
STACK(logger_stack);
STACK(sensor_stack);
STACK(sleeper_stack);
STACK(ctl_stack);
STACK(consumer_stack);
STACK(producer_stack);
STACK(waiter_stack);
// あふれた: 底が塗りのままでない（その上は塗りが残っている）
uint32_t stopped_stack[STACK_WORDS] __attribute__((aligned(8))) = {
    [0 ... 3] = 0x5A5A5A5Au, [4 ... STACK_WORDS - 64u - 1u] = STACK_COLOR, CONTEXT_WORDS(stopped_stack)};

extern struct tcb_s g_idletcb[CONFIG_SMP_NCPUS];
extern struct tcb_s th_main, th_logger, th_sensor, th_sleeper, th_ctl, th_consumer, th_producer, th_waiter,
    th_stopped;
extern sem_t sem_data;
extern mutex_t bus_lock;
extern struct mqueue_inode_s cmd_mq, full_mq;

// --- カーネルの変数（sched/init/nx_start.c 等と同じ型） ----------------------------------------------------
struct tcb_s *pidhash_table[16] = {&g_idletcb[0], &th_main, &th_logger, &th_sensor, &th_sleeper, &th_ctl,
                                   &th_consumer, &th_producer, &th_waiter, &th_stopped};
FAR struct tcb_s **g_pidhash = pidhash_table;
volatile int g_npidhash = 16;
FAR struct tcb_s *g_running_tasks[CONFIG_SMP_NCPUS] = {&th_main};
// 準備完了の列（優先度順）: main（100）→ logger（100）→ アイドル（0）
dq_queue_t g_readytorun = {(dq_entry_t *) &th_main, (dq_entry_t *) &g_idletcb[0]};
static volatile clock_t g_system_ticks = TICKS;          // sched/clock/clock_sched_ticks.c
volatile enum nx_initstate_e g_nx_initstate = OSINIT_IDLELOOP;

// --- オブジェクト ---------------------------------------------------------------------------------------
// sensor が待つ（semcount < 0 は待っているタスクの数）
sem_t sem_data = {.val = {.semcount = -1}, .flags = SEM_PRIO_NONE,
                  .waitlist = {(dq_entry_t *) &th_sensor, (dq_entry_t *) &th_sensor}};
// sleeper（PID 4）が持ち、ctl（200）が待つ。継承で sleeper は 200 で動く
mutex_t bus_lock = {.sem = {.val = {.mholder = 4u | NXSEM_MBLOCKING_BIT}, .flags = SEM_TYPE_MUTEX | SEM_PRIO_INHERIT,
                            .waitlist = {(dq_entry_t *) &th_ctl, (dq_entry_t *) &th_ctl}}};
// 誰も持っていない
mutex_t idle_lock = NXMUTEX_INITIALIZER;
// 空（consumer が受け取るのを待つ）と満杯（producer が送るのを待つ）
struct mqueue_inode_s cmd_mq = {.cmn = {.waitfornotempty = {(dq_entry_t *) &th_consumer, (dq_entry_t *) &th_consumer},
                                        .nwaitnotempty = 1},
                                .maxmsgs = 8, .nmsgs = 0, .maxmsgsize = 16};
struct mqueue_inode_s full_mq = {.cmn = {.waitfornotfull = {(dq_entry_t *) &th_producer, (dq_entry_t *) &th_producer},
                                         .nwaitnotfull = 1},
                                 .maxmsgs = 4, .nmsgs = 4, .maxmsgsize = 8};

// --- タスク -------------------------------------------------------------------------------------------
#define TASK(nm, id, prio, stk) \
    .pid = (id), .sched_priority = (prio), .init_priority = (prio), .base_priority = (prio), \
    .flags = TCB_FLAG_TTYPE_TASK, .start = nxtask_start, .stack_alloc_ptr = (stk), .stack_base_ptr = (stk), \
    .adj_stack_size = STACK_SIZE, .xcp = {.regs = &(stk)[CTX]}, .name = (nm)
// 時間切れの見張り（wd_start。expired は絶対のティック）
#define WAITDOG(ticks) .waitdog = {.func = (wdentry_t) nxtask_start, .expired = TICKS + (ticks)}

struct tcb_s g_idletcb[CONFIG_SMP_NCPUS] = {
    {TASK("Idle_Task", 0, 0, idle_stack), .flags = TCB_FLAG_TTYPE_KERNEL, .task_state = TSTATE_TASK_READYTORUN,
     .blink = &th_logger},
};
// 実行中（100）と準備完了（100）
struct tcb_s th_main = {TASK("main", 1, 100, main_stack), .task_state = TSTATE_TASK_RUNNING, .flink = &th_logger};
struct tcb_s th_logger = {TASK("logger", 2, 100, logger_stack), .task_state = TSTATE_TASK_READYTORUN,
                          .flink = &g_idletcb[0], .blink = &th_main};
// sem_data を 10 ティックの時間切れ付きで待つ
struct tcb_s th_sensor = {TASK("sensor", 3, 150, sensor_stack), .task_state = TSTATE_WAIT_SEM, .waitobj = &sem_data,
                          WAITDOG(10)};
// usleep（TSTATE_SLEEPING = TSTATE_WAIT_SIG で合図は待たない）。bus_lock を持ち、ctl から 200 を継承した
struct tcb_s th_sleeper = {TASK("sleeper", 4, 50, sleeper_stack), .sched_priority = 200,
                           .task_state = TSTATE_WAIT_SIG, WAITDOG(50)};
// bus_lock を時間切れ無しで待つ
struct tcb_s th_ctl = {TASK("ctl", 5, 200, ctl_stack), .task_state = TSTATE_WAIT_SEM, .waitobj = &bus_lock};
struct tcb_s th_consumer = {TASK("consumer", 6, 120, consumer_stack), .task_state = TSTATE_WAIT_MQNOTEMPTY,
                            .waitobj = &cmd_mq};
struct tcb_s th_producer = {TASK("producer", 7, 110, producer_stack), .task_state = TSTATE_WAIT_MQNOTFULL,
                            .waitobj = &full_mq, WAITDOG(25)};
// sigwaitinfo（SIGUSR1 を待つ）
struct tcb_s th_waiter = {TASK("waiter", 8, 90, waiter_stack), .task_state = TSTATE_WAIT_SIG,
                          .sigwaitmask = {._elem = {1u << SIGUSR1}}};
// SIGSTOP で止められた。スタックの底が壊れている
struct tcb_s th_stopped = {TASK("stopped", 9, 80, stopped_stack), .task_state = TSTATE_TASK_STOPPED};

// --- 呼び出し履歴の試験に使う関数 ----------------------------------------------------------------------
volatile uint32_t samples_taken;

__attribute__((noinline)) int nxsem_wait(FAR sem_t *sem) {
    if (sem->val.semcount > 0) {
        sem->val.semcount--;
        return 0;
    }
    return -4;
}

__attribute__((noinline)) int sensor_main(int argc, FAR char *argv[]) {
    (void) argc;
    (void) argv;
    for (;;) {
        if (nxsem_wait(&sem_data) == 0) {
            samples_taken++;
        }
    }
}

// 新しいタスクの最初の PC（task_start.c と同じく、いまのタスクの入口を呼ぶ）
__attribute__((noinline)) void nxtask_start(void) {
    FAR struct tcb_s *tcb = g_running_tasks[0];
    (void) tcb->entry.main(0, 0);
    for (;;) {
    }
}

extern uint32_t __stack_top;

__attribute__((noreturn)) void reset_handler(void) {
    nxtask_start();
    for (;;) {
    }
}

__attribute__((section(".vectors"), used)) const void *const vectors[2] = {&__stack_top, (void *) reset_handler};
