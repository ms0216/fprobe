// fprobe の RTOS の認識（U45）の試験用: µC/OS-III の本物のヘッダ（ucos3/os.h・os_type.h、ARMv7-M の GNU の port の
// os_cpu.h、µC/CPU・µC/LIB のヘッダ。uC-OS3 v3.08.02）の構造体と変数で、止めたときのカーネルの様子を .data の初期値に
// 置いた小さな ELF。µC/OS-III のカーネル（Source/*.c・Ports/*/os_cpu_a.S）そのものは含まない。
// 構造体の配置はヘッダのまま（コンパイラが決めた形が DWARF に入る）。繋ぎ方は µC/OS-III のソースと同じ:
// - タスク・オブジェクトの一覧は OS_CFG_DBG_EN のときだけの DbgNextPtr の列（OSTaskDbgListPtr 等。作るたびに先頭に足す）
// - 準備完了は優先度ごとの OSRdyList[prio]（NextPtr / PrevPtr）、待ちはオブジェクトの PendList（PendNextPtr。優先度順）
// - 時間（遅延・時間切れ）は OSTickList の差分の列（TickNextPtr。TickRemain は前のタスクからの差。os_tick.c）
// - タイマは OSTmrListPtr の差分の列（Remain は前のタイマからの差。OSTmrTaskTickBase からの経過は未反映。os_tmr.c）
// スタックは OS_OPT_TASK_STK_CLR なら全体を 0 で消す（os_task.c）。保存された文脈は ARMv7-M の PendSV
// （os_cpu_a.S）の積み方: StkPtr → r4〜r11、EXC_RETURN、[s16〜s31]、ハードのフレーム。
// 作り方: python host/firmware/ucos3_shape/build.py（arm-zephyr-eabi-gcc。OS_CFG_DBG_EN = 1 の ucos3_shape.elf と
// 0 の ucos3_shape_nodbg.elf。生成した ELF はリポジトリに置く）。
#include <os.h>

#if (OS_CFG_DBG_EN > 0u)
#define DBG(...) __VA_ARGS__
#define NODBG(...)
#else
#define DBG(...)
#define NODBG(...) __VA_ARGS__
#endif

#define STK_WORDS 128u
// 全体を 0 で消し（OS_OPT_TASK_STK_CLR）、上の used 語は使った
#define STK_INIT(used) {[STK_WORDS - (used) ... STK_WORDS - 1u] = 0xCCCCCCCCu}
#define STK_OPT (OS_OPT_TASK_STK_CHK | OS_OPT_TASK_STK_CLR)

// --- スタック ----------------------------------------------------------------------------------------
__attribute__((aligned(8))) CPU_STK main_stk[STK_WORDS] = STK_INIT(24u);
// 消さない（opt = 0）: 塗りが無い
__attribute__((aligned(8))) CPU_STK logger_stk[STK_WORDS] = STK_INIT(16u);
__attribute__((aligned(8))) CPU_STK sensor_stk[STK_WORDS] = STK_INIT(32u);
__attribute__((aligned(8))) CPU_STK sleeper_stk[STK_WORDS] = STK_INIT(16u);
__attribute__((aligned(8))) CPU_STK ctl_stk[STK_WORDS] = STK_INIT(16u);
// あふれた: 底が 0 のままでない（その上は 0 が残っている）
__attribute__((aligned(8))) CPU_STK parked_stk[STK_WORDS] = {[0 ... 3] = 0x5A5A5A5Au, [STK_WORDS - 16u ... STK_WORDS - 1u] = 0xCCCCCCCCu};
__attribute__((aligned(8))) CPU_STK consumer_stk[STK_WORDS] = STK_INIT(16u);
__attribute__((aligned(8))) CPU_STK waiter_stk[STK_WORDS] = STK_INIT(16u);
__attribute__((aligned(8))) CPU_STK notified_stk[STK_WORDS] = STK_INIT(16u);
__attribute__((aligned(8))) CPU_STK frozen_stk[STK_WORDS] = STK_INIT(16u);
__attribute__((aligned(8))) CPU_STK idle_stk[STK_WORDS] = STK_INIT(16u);
__attribute__((aligned(8))) CPU_STK tmr_stk[STK_WORDS] = STK_INIT(16u);

void AppTaskSensor(void *p_arg);
void AppTaskCtl(void *p_arg);
void blink_cb(void *p_tmr, void *p_arg);
void OS_TaskReturn(void);

extern OS_TCB AppTaskMainTCB, AppTaskLoggerTCB, AppTaskSensorTCB, AppTaskSleeperTCB, AppTaskCtlTCB, AppTaskParkedTCB,
    AppTaskConsumerTCB, AppTaskWaiterTCB, AppTaskNotifiedTCB, AppTaskFrozenTCB;
extern OS_SEM sem_data;
extern OS_MUTEX bus_lock;
extern OS_Q cmd_q, reply_q;
extern OS_FLAG_GRP sys_events;
extern OS_MEM buf_mem;
extern OS_TMR tmr_watchdog, tmr_blink, tmr_oneshot;

// 0 の変数も .data に置く（試験は ELF の初期値を読む。.bss は ELF に中身が無い）
#define ZERO __attribute__((section(".data")))

// --- カーネルの変数（os.h の OS_EXT と同じ型。os_var.c が置くもの） --------------------------------------------
OS_TCB *OSTCBCurPtr = &AppTaskMainTCB;
OS_TCB *OSTCBHighRdyPtr = &AppTaskMainTCB;
OS_PRIO OSPrioCur = 10u;
OS_PRIO OSPrioHighRdy = 10u;
OS_STATE OSRunning = OS_STATE_OS_RUNNING;
OS_STATE OSInitialized = 1u;
ZERO OS_NESTING_CTR OSIntNestingCtr;
ZERO OS_NESTING_CTR OSSchedLockNestingCtr;
OS_TICK OSTickCtr = 1000u;
OS_OBJ_QTY OSTaskQty = 12u;
OS_RDY_LIST OSRdyList[OS_CFG_PRIO_MAX] = {
    [10] = {.HeadPtr = &AppTaskMainTCB, .TailPtr = &AppTaskLoggerTCB DBG(, .NbrEntries = 2u)},
    [OS_CFG_PRIO_MAX - 1u] = {.HeadPtr = &OSIdleTaskTCB, .TailPtr = &OSIdleTaskTCB DBG(, .NbrEntries = 1u)},
};
// 時間の差分の列: sensor（10）→ 時間のタスク（+5 = 15）→ frozen（+15 = 30）→ sleeper（+20 = 50）
OS_TICK_LIST OSTickList = {.TCB_Ptr = &AppTaskSensorTCB DBG(, .NbrEntries = 4u)};
#if (OS_CFG_DBG_EN > 0u)
OS_TCB *OSTaskDbgListPtr = &AppTaskMainTCB;
OS_SEM *OSSemDbgListPtr = &sem_data;
OS_OBJ_QTY OSSemQty = 1u;
OS_MUTEX *OSMutexDbgListPtr = &bus_lock;
OS_OBJ_QTY OSMutexQty = 2u;
OS_Q *OSQDbgListPtr = &cmd_q;
OS_OBJ_QTY OSQQty = 2u;
OS_FLAG_GRP *OSFlagDbgListPtr = &sys_events;
OS_OBJ_QTY OSFlagQty = 1u;
OS_MEM *OSMemDbgListPtr = &buf_mem;
OS_OBJ_QTY OSMemQty = 1u;
OS_TMR *OSTmrDbgListPtr = &tmr_watchdog;
OS_OBJ_QTY OSTmrQty = 3u;
CPU_INT16U const OSDbg_VersionNbr = OS_VERSION;                       // os_dbg.c と同じ
#endif
// タイマの差分の列: watchdog（7）→ blink（+33 = 40）。経過（OSTickCtr − OSTmrTaskTickBase = 2）は未反映
OS_TMR *OSTmrListPtr = &tmr_watchdog;
OS_TICK OSTmrTaskTickBase = 998u;
OS_TICK OSTmrToTicksMult = OS_CFG_TICK_RATE_HZ / OS_CFG_TMR_TASK_RATE_HZ;

// --- オブジェクト ---------------------------------------------------------------------------------------
// 一覧（DbgNextPtr）: sem_data / bus_lock → OSTmrMutex / cmd_q → reply_q / sys_events / buf_mem / watchdog → blink → oneshot
OS_SEM sem_data = {
    .Type = OS_OBJ_TYPE_SEM, DBG(.NamePtr = "sem_data", .DbgNamePtr = " ",)
    .PendList = {.HeadPtr = &AppTaskSensorTCB, .TailPtr = &AppTaskSensorTCB DBG(, .NbrEntries = 1u)}, .Ctr = 0u,
};
// sleeper（元 20）が持ち、ctl（3）が待つ。µC/OS-III のミューテックスは必ず継承する: sleeper は 3 で動く
OS_MUTEX bus_lock = {
    .Type = OS_OBJ_TYPE_MUTEX, DBG(.NamePtr = "bus_lock", .DbgNextPtr = &OSTmrMutex, .DbgNamePtr = " ",)
    .PendList = {.HeadPtr = &AppTaskCtlTCB, .TailPtr = &AppTaskCtlTCB DBG(, .NbrEntries = 1u)},
    .OwnerTCBPtr = &AppTaskSleeperTCB, .OwnerNestingCtr = 1u,
};
OS_MUTEX OSTmrMutex = {
    .Type = OS_OBJ_TYPE_MUTEX, DBG(.NamePtr = "OS Tmr Mutex", .DbgPrevPtr = &bus_lock, .DbgNamePtr = " ",)
};
OS_COND OSTmrCond = {.Type = OS_OBJ_TYPE_COND, .Mutex = &OSTmrMutex,
                     .PendList = {.HeadPtr = &OSTmrTaskTCB, .TailPtr = &OSTmrTaskTCB DBG(, .NbrEntries = 1u)}};
// 空（consumer が受け取るのを待つ）。µC/OS-III のキューは送り手を待たせない
OS_Q cmd_q = {
    .Type = OS_OBJ_TYPE_Q, DBG(.NamePtr = "cmd_q", .DbgNextPtr = &reply_q, .DbgNamePtr = " ",)
    .PendList = {.HeadPtr = &AppTaskConsumerTCB, .TailPtr = &AppTaskConsumerTCB DBG(, .NbrEntries = 1u)},
    .MsgQ = {.NbrEntriesSize = 8u, .NbrEntries = 0u DBG(, .NbrEntriesMax = 5u)},
};
// 名前が無い（OSQCreate の p_name が NULL）。3 つ入っている
OS_MSG reply_msgs[3] = {{.NextPtr = &reply_msgs[1], .MsgSize = 4u}, {.NextPtr = &reply_msgs[2], .MsgSize = 4u},
                        {.MsgSize = 4u}};
OS_Q reply_q = {
    .Type = OS_OBJ_TYPE_Q, DBG(.DbgPrevPtr = &cmd_q, .DbgNamePtr = " ",)
    .MsgQ = {.InPtr = &reply_msgs[2], .OutPtr = &reply_msgs[0], .NbrEntriesSize = 4u, .NbrEntries = 3u
             DBG(, .NbrEntriesMax = 4u)},
};
OS_FLAG_GRP sys_events = {
    .Type = OS_OBJ_TYPE_FLAG, DBG(.NamePtr = "sys_events", .DbgNamePtr = " ",)
    .PendList = {.HeadPtr = &AppTaskWaiterTCB, .TailPtr = &AppTaskWaiterTCB DBG(, .NbrEntries = 1u)}, .Flags = 0x1u,
};
CPU_INT32U buf_mem_storage[8][8];
OS_MEM buf_mem = {
    .Type = OS_OBJ_TYPE_MEM, DBG(.NamePtr = "buf_mem",)
    .AddrPtr = buf_mem_storage, .FreeListPtr = buf_mem_storage[6], .BlkSize = 32u, .NbrMax = 8u, .NbrFree = 2u,
};
// タイマ（Dly・Period・Remain は OS のティック。OSTmrCreate が OSTmrToTicksMult を掛ける）
OS_TMR tmr_watchdog = {
    .Type = OS_OBJ_TYPE_TMR, DBG(.NamePtr = "watchdog", .DbgNextPtr = &tmr_blink,)
    .CallbackPtr = blink_cb, .CallbackPtrArg = 0, .NextPtr = &tmr_blink, .Remain = 7u, .Dly = 0u, .Period = 500u,
    .Opt = OS_OPT_TMR_PERIODIC, .State = OS_TMR_STATE_RUNNING,
};
OS_TMR tmr_blink = {
    .Type = OS_OBJ_TYPE_TMR, DBG(.NamePtr = "blink", .DbgPrevPtr = &tmr_watchdog, .DbgNextPtr = &tmr_oneshot,)
    .CallbackPtr = blink_cb, .CallbackPtrArg = (void *) 0x1234, .PrevPtr = &tmr_watchdog, .Remain = 33u,
    .Dly = 0u, .Period = 100u, .Opt = OS_OPT_TMR_PERIODIC, .State = OS_TMR_STATE_RUNNING,
};
OS_TMR tmr_oneshot = {
    .Type = OS_OBJ_TYPE_TMR, DBG(.NamePtr = "oneshot", .DbgPrevPtr = &tmr_blink,)
    .CallbackPtr = blink_cb, .Dly = 50u, .Period = 0u, .Opt = OS_OPT_TMR_ONE_SHOT, .State = OS_TMR_STATE_STOPPED,
};

// --- タスク（DbgNextPtr の列: 宣言の順） --------------------------------------------------------------
#define TASK(nm, stk, prio, opt, entry, dbg_prev, dbg_next) \
    .StkPtr = &(stk)[STK_WORDS - 16u], .StkLimitPtr = &(stk)[STK_WORDS / 10u], .StkBasePtr = (stk), \
    .StkSize = STK_WORDS, .Prio = (prio), .BasePrio = (prio), .Opt = (opt), .SuspendCtr = 0u \
    DBG(, .NamePtr = (nm), .TaskEntryAddr = (entry), .DbgPrevPtr = (dbg_prev), .DbgNextPtr = (dbg_next), \
        .DbgNamePtr = " ")
#define PENDING(on, object, next) \
    .PendOn = (on), .PendObjPtr = (OS_PEND_OBJ *) &(object), .PendNextPtr = (next), .PendStatus = OS_STATUS_PEND_OK

// 実行中（10）と準備完了（10。同じ優先度の準備完了の列）
OS_TCB AppTaskMainTCB = {TASK("main", main_stk, 10u, STK_OPT, 0, 0, &AppTaskLoggerTCB),
                         .StkPtr = &main_stk[STK_WORDS - 24u], .TaskState = OS_TASK_STATE_RDY,
                         .NextPtr = &AppTaskLoggerTCB};
OS_TCB AppTaskLoggerTCB = {TASK("logger", logger_stk, 10u, 0u, 0, &AppTaskMainTCB, &AppTaskSensorTCB),
                           .TaskState = OS_TASK_STATE_RDY, .PrevPtr = &AppTaskMainTCB};
// sem_data を 10 ティックの時間切れ付きで待つ
OS_TCB AppTaskSensorTCB = {TASK("sensor", sensor_stk, 5u, STK_OPT, AppTaskSensor, &AppTaskLoggerTCB, &AppTaskSleeperTCB),
                           .StkPtr = &sensor_stk[STK_WORDS - 32u], .TaskState = OS_TASK_STATE_PEND_TIMEOUT,
                           PENDING(OS_TASK_PEND_ON_SEM, sem_data, 0), .TickRemain = 10u, .TickNextPtr = &OSTmrTaskTCB};
// OSTimeDly(50)。bus_lock を持ち、ctl（3）から継承した
OS_TCB AppTaskSleeperTCB = {TASK("sleeper", sleeper_stk, 20u, STK_OPT, 0, &AppTaskSensorTCB, &AppTaskCtlTCB),
                            .Prio = 3u, .TaskState = OS_TASK_STATE_DLY, .TickRemain = 20u,
                            .TickPrevPtr = &AppTaskFrozenTCB, .MutexGrpHeadPtr = &bus_lock};
// bus_lock を時間切れ無しで待つ
OS_TCB AppTaskCtlTCB = {TASK("ctl", ctl_stk, 3u, STK_OPT, AppTaskCtl, &AppTaskSleeperTCB, &AppTaskParkedTCB),
                        .TaskState = OS_TASK_STATE_PEND, PENDING(OS_TASK_PEND_ON_MUTEX, bus_lock, 0)};
// OSTaskSuspend（2 回）。スタックの底が壊れている
OS_TCB AppTaskParkedTCB = {TASK("parked", parked_stk, 25u, STK_OPT, 0, &AppTaskCtlTCB, &AppTaskConsumerTCB),
                           .TaskState = OS_TASK_STATE_SUSPENDED, .SuspendCtr = 2u};
// 空の cmd_q から受け取る
OS_TCB AppTaskConsumerTCB = {TASK("consumer", consumer_stk, 9u, STK_OPT, 0, &AppTaskParkedTCB, &AppTaskWaiterTCB),
                             .TaskState = OS_TASK_STATE_PEND, PENDING(OS_TASK_PEND_ON_Q, cmd_q, 0)};
// sys_events のビット 1 と 2 の両方が立つのを待ち、立ったら消す（OS_OPT_PEND_FLAG_SET_ALL | CONSUME）
OS_TCB AppTaskWaiterTCB = {TASK("waiter", waiter_stk, 12u, STK_OPT, 0, &AppTaskConsumerTCB, &AppTaskNotifiedTCB),
                           .TaskState = OS_TASK_STATE_PEND, PENDING(OS_TASK_PEND_ON_FLAG, sys_events, 0),
                           .FlagsPend = 0x6u, .FlagsOpt = OS_OPT_PEND_FLAG_SET_ALL | OS_OPT_PEND_FLAG_CONSUME};
// OSTaskSemPend（タスクの組み込みのセマフォ。待つオブジェクトは無い）
OS_TCB AppTaskNotifiedTCB = {TASK("notified", notified_stk, 14u, STK_OPT, 0, &AppTaskWaiterTCB, &AppTaskFrozenTCB),
                             .TaskState = OS_TASK_STATE_PEND, .PendOn = OS_TASK_PEND_ON_TASK_SEM};
// 遅延の途中で止められた（OS_TASK_STATE_DLY_SUSPENDED）
OS_TCB AppTaskFrozenTCB = {TASK("frozen", frozen_stk, 16u, STK_OPT, 0, &AppTaskNotifiedTCB, &OSTmrTaskTCB),
                           .TaskState = OS_TASK_STATE_DLY_SUSPENDED, .SuspendCtr = 1u, .TickRemain = 15u,
                           .TickPrevPtr = &OSTmrTaskTCB, .TickNextPtr = &AppTaskSleeperTCB};
// カーネルのタスク（OS_TmrInit・OS_IdleTaskInit が作る）。時間のタスクは OSTmrCond を時間切れ付きで待つ
OS_TCB OSTmrTaskTCB = {TASK("uC/OS-III Timer Task", tmr_stk, OS_CFG_PRIO_MAX - 3u, STK_OPT, 0, &AppTaskFrozenTCB,
                            &OSIdleTaskTCB),
                       .TaskState = OS_TASK_STATE_PEND_TIMEOUT, PENDING(OS_TASK_PEND_ON_COND, OSTmrCond, 0),
                       .TickRemain = 5u, .TickPrevPtr = &AppTaskSensorTCB, .TickNextPtr = &AppTaskFrozenTCB};
OS_TCB OSIdleTaskTCB = {TASK("uC/OS-III Idle Task", idle_stk, OS_CFG_PRIO_MAX - 1u, STK_OPT, 0, &OSTmrTaskTCB, 0),
                        .TaskState = OS_TASK_STATE_RDY};

// --- 呼び出し履歴の試験に使う関数 ----------------------------------------------------------------------
volatile CPU_INT32U samples_taken;
volatile CPU_INT32U blinks;

__attribute__((noinline)) OS_SEM_CTR OSSemPend(OS_SEM *p_sem, OS_TICK timeout, OS_OPT opt, CPU_TS *p_ts, OS_ERR *p_err) {
    (void) timeout;
    (void) opt;
    (void) p_ts;
    if (p_sem->Ctr > 0u) {
        p_sem->Ctr--;
        *p_err = OS_ERR_NONE;
        return p_sem->Ctr;
    }
    *p_err = OS_ERR_TIMEOUT;
    return 0u;
}

__attribute__((noinline)) void AppTaskSensor(void *p_arg) {
    OS_ERR err;
    (void) p_arg;
    for (;;) {
        (void) OSSemPend(&sem_data, 10u, OS_OPT_PEND_BLOCKING, (CPU_TS *) 0, &err);
        if (err == OS_ERR_NONE) {
            samples_taken++;
        }
    }
}

__attribute__((noinline)) void AppTaskCtl(void *p_arg) {
    (void) p_arg;
    for (;;) {
        samples_taken++;
    }
}

void blink_cb(void *p_tmr, void *p_arg) {
    (void) p_tmr;
    (void) p_arg;
    blinks++;
}

// タスクの関数が戻ったときの行き先（OSTaskStkInit が最初の LR に置く。os_task.c）
__attribute__((noinline)) void OS_TaskReturn(void) {
    for (;;) {
        blinks++;
    }
}

extern CPU_INT32U __stack_top;

__attribute__((noreturn)) void reset_handler(void) {
    AppTaskSensor(0);
    OS_TaskReturn();
    for (;;) {
    }
}

__attribute__((section(".vectors"), used)) const void *const vectors[2] = {&__stack_top, (void *) reset_handler};
