// fprobe の RTOS の認識（U45）の試験用: CMSIS-RTOS2 RTX5 の本物のヘッダ（CMSIS-RTX/rtx_os.h、RTX V5.5.0）の構造体で、
// 止めたときのカーネルの様子を .data の初期値に置いた小さな ELF。RTX5 のカーネル（rtx_*.c）そのものは含まない。
// 構造体の配置はヘッダのまま（コンパイラが決めた形が DWARF に入る）。リストの繋ぎ方は RTX5 の rtx_thread.c と同じ:
// - 準備完了のリスト osRtxInfo.thread.ready.thread_list（thread_next、優先度の高い順。先頭の thread_prev はリストの持ち主）
// - 時間待ちのリスト delay_list（delay_next、delay は前との差分）と、時間切れの無い待ち wait_list（delay_next）
// - オブジェクトの待ち手 thread_list（thread_next。先頭の thread_prev はオブジェクト = id で見分ける）
// - 終わって join を待つ terminate_list（thread_next）
// スタックは底に osRtxStackMagicWord、その上を osRtxStackFillPattern（0xCC）で塗る（svcRtxThreadNew と同じ）。
// 作り方: python host/firmware/rtx5_shape/build.py（arm-zephyr-eabi-gcc。生成した rtx5_shape.elf はリポジトリに置く）。
#include "rtx_os.h"

#define STACK_WORDS 128
#define STACK_SIZE (STACK_WORDS * 4)
// 底の印と塗り。上の used バイトは使った（0）
#define STACK_INIT(used) {[0] = osRtxStackMagicWord, [1 ... STACK_WORDS - (used) / 4 - 1] = osRtxStackFillPattern}

// --- 止めたときの様子 ----------------------------------------------------------------------------------
__attribute__((aligned(8))) uint32_t main_stack[STACK_WORDS] = STACK_INIT(96);
__attribute__((aligned(8))) uint32_t sensor_stack[STACK_WORDS] = STACK_INIT(128);
__attribute__((aligned(8))) uint32_t sleeper_stack[STACK_WORDS] = STACK_INIT(64);
__attribute__((aligned(8))) uint32_t ctl_stack[STACK_WORDS] = STACK_INIT(64);
__attribute__((aligned(8))) uint32_t parked_stack[STACK_WORDS] = STACK_INIT(64);
__attribute__((aligned(8))) uint32_t logger_stack[STACK_WORDS] = STACK_INIT(64);
__attribute__((aligned(8))) uint32_t idle_stack[STACK_WORDS] = STACK_INIT(64);
__attribute__((aligned(8))) uint32_t timer_stack[STACK_WORDS] = STACK_INIT(64);
// あふれた: 底の印が壊れている
__attribute__((aligned(8))) uint32_t gone_stack[STACK_WORDS] = {[0] = 0x12345678u};

void sensor_entry(void *argument);
void ctl_entry(void *argument);
void blink_cb(void *argument);

extern osRtxThread_t th_main, th_sensor, th_sleeper, th_ctl, th_parked, th_logger, th_idle, th_timer, th_gone;

// sleeper（BelowNormal = 16）が持ち、ctl（High = 40）が待つ。継承していない。logger（Normal = 24）が実行可能 = 優先度の逆転
osRtxMutex_t bus_lock = {
    .id = osRtxIdMutex, .name = "bus_lock", .attr = osMutexRecursive, .thread_list = &th_ctl,
    .owner_thread = &th_sleeper, .lock = 1,
};
osRtxSemaphore_t sem_data = {.id = osRtxIdSemaphore, .name = "sem_data", .thread_list = &th_sensor, .tokens = 0,
                             .max_tokens = 1};
osRtxEventFlags_t sys_events = {.id = osRtxIdEventFlags, .name = "sys_events", .event_flags = 0x5};
uint32_t pool_mem[8 * 4];
osRtxMemoryPool_t buf_pool = {
    .id = osRtxIdMemoryPool, .name = "buf_pool",
    .mp_info = {.max_blocks = 8, .used_blocks = 3, .block_size = 16, .block_base = pool_mem,
                .block_lim = pool_mem + 8 * 4, .block_free = pool_mem + 3 * 4},
};
uint32_t cmd_mq_mem[4 * 5];
osRtxMessageQueue_t cmd_mq = {
    .id = osRtxIdMessageQueue, .name = "cmd_mq",
    .mp_info = {.max_blocks = 4, .used_blocks = 2, .block_size = 20, .block_base = cmd_mq_mem,
                .block_lim = cmd_mq_mem + 4 * 5, .block_free = cmd_mq_mem + 2 * 5},
    .msg_size = 8, .msg_count = 2,
};
// タイマのスレッドが待つ（osRtxInfo.timer.mq）。名前の無いオブジェクト
osRtxMessageQueue_t timer_mq = {
    .id = osRtxIdMessageQueue, .flags = osRtxFlagSystemObject, .thread_list = &th_timer,
    .mp_info = {.max_blocks = 4, .block_size = 20}, .msg_size = 8,
};

// 動いているタイマの列（tick は前との差分）: watchdog（10）→ blink（+20 = 30）。oneshot は止まっている
extern osRtxTimer_t tmr_watchdog, tmr_blink;
osRtxTimer_t tmr_watchdog = {
    .id = osRtxIdTimer, .state = osRtxTimerRunning, .type = osRtxTimerPeriodic, .name = "watchdog",
    .next = &tmr_blink, .tick = 10, .load = 500, .finfo = {.func = blink_cb, .arg = (void *) 0},
};
osRtxTimer_t tmr_blink = {
    .id = osRtxIdTimer, .state = osRtxTimerRunning, .type = osRtxTimerPeriodic, .name = "blink",
    .prev = &tmr_watchdog, .tick = 20, .load = 100, .finfo = {.func = blink_cb, .arg = (void *) 0x1234},
};
osRtxTimer_t tmr_oneshot = {
    .id = osRtxIdTimer, .state = osRtxTimerStopped, .type = 0, .name = "oneshot", .load = 50,
    .finfo = {.func = blink_cb},
};

osRtxInfo_t osRtxInfo = {
    .os_id = osRtxKernelId, .version = osRtxVersionKernel,
    .kernel = {.state = osRtxKernelRunning, .tick = 1000},
    .thread = {
        .run = {.curr = &th_main, .next = &th_main},
        .ready = {.id = osRtxIdThread, .thread_list = &th_logger},
        .idle = &th_idle,
        .delay_list = &th_sensor,
        .wait_list = &th_ctl,
        .terminate_list = &th_gone,
    },
    .timer = {.list = &tmr_watchdog, .thread = &th_timer, .mq = &timer_mq},
};

#define THREAD(nm, stk, used, entry) \
    .id = osRtxIdThread, .name = nm, .stack_mem = stk, .stack_size = STACK_SIZE, \
    .sp = (uint32_t) (uintptr_t) ((uint8_t *) (stk) + STACK_SIZE - (used)), .thread_addr = (uint32_t) (uintptr_t) (entry), \
    .stack_frame = 0xFD

// 実行中（osPriorityNormal）
osRtxThread_t th_main = {THREAD("main", main_stack, 96, 0), .state = osRtxThreadRunning,
                         .priority = osPriorityNormal, .priority_base = osPriorityNormal};
// 準備完了のリスト: logger（Normal）→ idle（Idle）
osRtxThread_t th_logger = {THREAD("logger", logger_stack, 64, 0), .state = osRtxThreadReady,
                           .priority = osPriorityNormal, .priority_base = osPriorityNormal,
                           .thread_prev = (osRtxThread_t *) &osRtxInfo.thread.ready, .thread_next = &th_idle};
osRtxThread_t th_idle = {THREAD("osRtxIdleThread", idle_stack, 64, osRtxIdleThread), .state = osRtxThreadReady,
                         .priority = osPriorityIdle, .priority_base = osPriorityIdle, .thread_prev = &th_logger};
// 時間待ちのリスト: sensor（sem_data を 30 ティックの時間切れ付きで待つ）→ sleeper（osDelay、+20 = 50）
osRtxThread_t th_sensor = {THREAD("sensor", sensor_stack, 128, sensor_entry), .state = osRtxThreadWaitingSemaphore,
                           .priority = osPriorityAboveNormal, .priority_base = osPriorityAboveNormal,
                           .thread_prev = (osRtxThread_t *) &sem_data, .delay = 30, .delay_next = &th_sleeper};
osRtxThread_t th_sleeper = {THREAD("sleeper", sleeper_stack, 64, 0), .state = osRtxThreadWaitingDelay,
                            .priority = osPriorityBelowNormal, .priority_base = osPriorityBelowNormal,
                            .delay = 20, .delay_prev = &th_sensor, .mutex_list = &bus_lock};
// 時間切れの無い待ち: ctl（bus_lock）→ parked（osThreadSuspend）→ タイマのスレッド（timer_mq）
osRtxThread_t th_ctl = {THREAD("ctl", ctl_stack, 64, ctl_entry), .state = osRtxThreadWaitingMutex,
                        .priority = osPriorityHigh, .priority_base = osPriorityHigh,
                        .thread_prev = (osRtxThread_t *) &bus_lock, .delay = osWaitForever, .delay_next = &th_parked};
osRtxThread_t th_parked = {THREAD("parked", parked_stack, 64, 0), .state = osRtxThreadBlocked,
                           .priority = osPriorityLow, .priority_base = osPriorityLow, .delay = osWaitForever,
                           .delay_prev = &th_ctl, .delay_next = &th_timer};
osRtxThread_t th_timer = {THREAD("osRtxTimerThread", timer_stack, 64, 0), .state = osRtxThreadWaitingMessageGet,
                          .priority = osPriorityRealtime7, .priority_base = osPriorityRealtime7,
                          .thread_prev = (osRtxThread_t *) &timer_mq, .delay = osWaitForever, .delay_prev = &th_parked};
// 終わって join を待つ（スタックの底の印が壊れている）
osRtxThread_t th_gone = {THREAD("gone", gone_stack, 64, 0), .state = osRtxThreadTerminated,
                         .priority = osPriorityNormal, .priority_base = osPriorityNormal};

// --- 呼び出し履歴の試験に使う関数 ----------------------------------------------------------------------
volatile uint32_t samples_taken;
volatile uint32_t blinks;

__attribute__((noinline)) osStatus_t osSemaphoreAcquire(osSemaphoreId_t semaphore_id, uint32_t timeout) {
    osRtxSemaphore_t *semaphore = (osRtxSemaphore_t *) semaphore_id;
    if (semaphore->tokens > 0u) {
        semaphore->tokens--;
        return osOK;
    }
    return timeout == 0u ? osErrorResource : osErrorTimeout;
}

__attribute__((noinline)) void sensor_entry(void *argument) {
    (void) argument;
    for (;;) {
        if (osSemaphoreAcquire(&sem_data, 30u) == osOK) {
            samples_taken++;
        }
    }
}

__attribute__((noinline)) void ctl_entry(void *argument) {
    (void) argument;
    for (;;) {
        samples_taken++;
    }
}

void blink_cb(void *argument) {
    (void) argument;
    blinks++;
}

// RTX5 は新しいスレッドの LR を osThreadExit にする（関数が戻ったら終わる）。巻き戻しはここで止める
__attribute__((noinline)) void osThreadExit(void) {
    for (;;) {
    }
}

void osRtxIdleThread(void *argument) {
    (void) argument;
    for (;;) {
    }
}

extern uint32_t __stack_top;

__attribute__((noreturn)) void reset_handler(void) {
    sensor_entry(0);
    osThreadExit();
}

__attribute__((section(".vectors"), used)) const void *const vectors[2] = {&__stack_top, (void *) reset_handler};
