// fprobe の RTOS の認識（U45）の試験用: Eclipse ThreadX の本物のヘッダ（threadx/tx_api.h・Cortex-M4 の GNU の port の
// tx_port.h・カーネルの内部のヘッダ。ThreadX 6.5.2）の構造体と変数で、止めたときのカーネルの様子を .data の初期値に
// 置いた小さな ELF。ThreadX のカーネル（common/src・ports/*/src）そのものは含まない。
// 構造体の配置はヘッダのまま（コンパイラが決めた形が DWARF に入る）。繋ぎ方は ThreadX のソースと同じ:
// - スレッド・オブジェクトは種類ごとの「作られた」環（_tx_*_created_ptr、*_created_next / _previous。数は _created_count）
// - オブジェクトの待ち手は suspension_list の環（tx_thread_suspended_next / _previous）。スレッドの
//   tx_thread_suspend_control_block が待つオブジェクト、tx_thread_state が待ちの種類
// - 時間（スリープ・時間切れ・アプリのタイマ）は 32 個の時間の輪 _tx_timer_list。_tx_timer_current_ptr が今の枠、
//   32 ティックより先は最後の枠に置き、残りを tx_timer_internal_remaining_ticks に持つ（tx_timer_system_activate.c）
// スタックは全体を TX_STACK_FILL（0xEF）で塗り、tx_thread_stack_end は最後のバイト（tx_thread_create.c）。
// 保存された文脈は Cortex-M4 の PendSV（tx_thread_schedule.S）の積み方: SP → EXC_RETURN、[s16〜s31]、r4〜r11、
// ハードのフレーム。
// 作り方: python host/firmware/threadx_shape/build.py（arm-zephyr-eabi-gcc。生成した threadx_shape.elf はリポジトリに置く）。
#include "tx_api.h"
#include "tx_block_pool.h"
#include "tx_byte_pool.h"
#include "tx_event_flags.h"
#include "tx_initialize.h"
#include "tx_mutex.h"
#include "tx_queue.h"
#include "tx_semaphore.h"
#include "tx_thread.h"
#include "tx_timer.h"

#define STACK_SIZE 512
// 全体を 0xEF で塗り、上の used バイトは使った（0）
#define STACK_INIT(used) {[0 ... STACK_SIZE - (used) - 1] = 0xEF}

// --- スタック ----------------------------------------------------------------------------------------
__attribute__((aligned(8))) UCHAR main_stack[STACK_SIZE] = STACK_INIT(96);
__attribute__((aligned(8))) UCHAR logger_stack[STACK_SIZE] = STACK_INIT(64);
__attribute__((aligned(8))) UCHAR sensor_stack[STACK_SIZE] = STACK_INIT(128);
__attribute__((aligned(8))) UCHAR sleeper_stack[STACK_SIZE] = STACK_INIT(64);
__attribute__((aligned(8))) UCHAR ctl_stack[STACK_SIZE] = STACK_INIT(64);
__attribute__((aligned(8))) UCHAR parked_stack[STACK_SIZE] = STACK_INIT(64);
__attribute__((aligned(8))) UCHAR producer_stack[STACK_SIZE] = STACK_INIT(64);
__attribute__((aligned(8))) UCHAR consumer_stack[STACK_SIZE] = STACK_INIT(64);
__attribute__((aligned(8))) UCHAR waiter_stack[STACK_SIZE] = STACK_INIT(64);
__attribute__((aligned(8))) UCHAR alloc_stack[STACK_SIZE] = STACK_INIT(64);
// あふれた: 底が塗りのままでない（その上は塗りが残っている）
__attribute__((aligned(8))) UCHAR gone_stack[STACK_SIZE] = {[0 ... 15] = 0x5A, [16 ... STACK_SIZE - 64 - 1] = 0xEF};

void sensor_entry(ULONG id);
void ctl_entry(ULONG id);
void blink_cb(ULONG id);

extern TX_THREAD th_main, th_logger, th_sensor, th_sleeper, th_ctl, th_parked, th_producer, th_consumer, th_waiter,
    th_alloc, th_gone;
extern TX_MUTEX bus_lock;
extern TX_SEMAPHORE sem_data;
extern TX_QUEUE cmd_q, reply_q;
extern TX_EVENT_FLAGS_GROUP sys_events;
extern TX_BLOCK_POOL buf_pool;
extern TX_BYTE_POOL heap_pool;
extern TX_TIMER tmr_watchdog, tmr_blink, tmr_oneshot;

// --- 時間の輪（今の枠は 4） -----------------------------------------------------------------------------
// sensor の時間切れ（10 ティック: 枠 13）、watchdog（7: 枠 10）、sleeper（50: 枠 3 に 31 ティック先として置き、
// 残り 50）と blink（40: 同じ枠 3、残り 40）
TX_TIMER_INTERNAL *_tx_timer_list[TX_TIMER_ENTRIES] = {
    [3] = &th_sleeper.tx_thread_timer, [10] = &tmr_watchdog.tx_timer_internal, [13] = &th_sensor.tx_thread_timer,
};
TX_TIMER_INTERNAL **_tx_timer_list_start = _tx_timer_list;
TX_TIMER_INTERNAL **_tx_timer_list_end = &_tx_timer_list[TX_TIMER_ENTRIES];
TX_TIMER_INTERNAL **_tx_timer_current_ptr = &_tx_timer_list[4];
volatile ULONG _tx_timer_system_clock = 1000;
// 0 の変数も .data に置く（試験は ELF の初期値を読む。.bss は ELF に中身が無い）
#define ZERO __attribute__((section(".data")))
ZERO TX_TIMER_INTERNAL *_tx_timer_expired_timer_ptr;
TX_TIMER *_tx_timer_created_ptr = &tmr_watchdog;
ULONG _tx_timer_created_count = 3;

// --- カーネルの変数（tx_thread_initialize.c 等と同じ型） ------------------------------------------------
CHAR _tx_version_id[] =
    "(c) 2024 Microsoft Corp. (c) 2026-present Eclipse ThreadX contributors.  *  ThreadX Cortex-M4/GNU Version 6.5.2.202603 *";
TX_THREAD *_tx_thread_current_ptr = &th_main;
TX_THREAD *_tx_thread_execute_ptr = &th_main;
TX_THREAD *_tx_thread_created_ptr = &th_main;
ULONG _tx_thread_created_count = 11;
ZERO volatile ULONG _tx_thread_system_state = TX_INITIALIZE_IS_FINISHED;
UINT _tx_thread_highest_priority = 10;
TX_THREAD *_tx_thread_priority_list[TX_MAX_PRIORITIES] = {[10] = &th_main};
ZERO volatile UINT _tx_thread_preempt_disable;
TX_MUTEX *_tx_mutex_created_ptr = &bus_lock;
ULONG _tx_mutex_created_count = 1;
TX_SEMAPHORE *_tx_semaphore_created_ptr = &sem_data;
ULONG _tx_semaphore_created_count = 1;
TX_QUEUE *_tx_queue_created_ptr = &cmd_q;
ULONG _tx_queue_created_count = 2;
TX_EVENT_FLAGS_GROUP *_tx_event_flags_created_ptr = &sys_events;
ULONG _tx_event_flags_created_count = 1;
TX_BLOCK_POOL *_tx_block_pool_created_ptr = &buf_pool;
ULONG _tx_block_pool_created_count = 1;
TX_BYTE_POOL *_tx_byte_pool_created_ptr = &heap_pool;
ULONG _tx_byte_pool_created_count = 1;

// --- オブジェクト ---------------------------------------------------------------------------------------
// sleeper（20）が持ち、ctl（3）が待つ。継承しない = 優先度の逆転（main・logger（10）が間で動ける）
TX_MUTEX bus_lock = {
    .tx_mutex_id = TX_MUTEX_ID, .tx_mutex_name = "bus_lock", .tx_mutex_ownership_count = 1,
    .tx_mutex_owner = &th_sleeper, .tx_mutex_inherit = TX_NO_INHERIT, .tx_mutex_original_priority = 20,
    .tx_mutex_suspension_list = &th_ctl, .tx_mutex_suspended_count = 1,
    .tx_mutex_created_next = &bus_lock, .tx_mutex_created_previous = &bus_lock,
    .tx_mutex_highest_priority_waiting = 3, .tx_mutex_owned_next = &bus_lock, .tx_mutex_owned_previous = &bus_lock,
};
TX_SEMAPHORE sem_data = {
    .tx_semaphore_id = TX_SEMAPHORE_ID, .tx_semaphore_name = "sem_data", .tx_semaphore_count = 0,
    .tx_semaphore_suspension_list = &th_sensor, .tx_semaphore_suspended_count = 1,
    .tx_semaphore_created_next = &sem_data, .tx_semaphore_created_previous = &sem_data,
};
// 満杯（producer が送るのを待つ）。メッセージの大きさは語（ULONG）の数
ULONG cmd_q_mem[4 * 2];
TX_QUEUE cmd_q = {
    .tx_queue_id = TX_QUEUE_ID, .tx_queue_name = "cmd_q", .tx_queue_message_size = 2, .tx_queue_capacity = 4,
    .tx_queue_enqueued = 4, .tx_queue_available_storage = 0, .tx_queue_start = cmd_q_mem,
    .tx_queue_end = cmd_q_mem + 8, .tx_queue_read = cmd_q_mem, .tx_queue_write = cmd_q_mem,
    .tx_queue_suspension_list = &th_producer, .tx_queue_suspended_count = 1,
    .tx_queue_created_next = &reply_q, .tx_queue_created_previous = &reply_q,
};
// 空（consumer が受け取るのを待つ）。名前が無い
ULONG reply_q_mem[8];
TX_QUEUE reply_q = {
    .tx_queue_id = TX_QUEUE_ID, .tx_queue_message_size = 1, .tx_queue_capacity = 8, .tx_queue_enqueued = 0,
    .tx_queue_available_storage = 8, .tx_queue_start = reply_q_mem, .tx_queue_end = reply_q_mem + 8,
    .tx_queue_read = reply_q_mem, .tx_queue_write = reply_q_mem,
    .tx_queue_suspension_list = &th_consumer, .tx_queue_suspended_count = 1,
    .tx_queue_created_next = &cmd_q, .tx_queue_created_previous = &cmd_q,
};
TX_EVENT_FLAGS_GROUP sys_events = {
    .tx_event_flags_group_id = TX_EVENT_FLAGS_ID, .tx_event_flags_group_name = "sys_events",
    .tx_event_flags_group_current = 0x1, .tx_event_flags_group_suspension_list = &th_waiter,
    .tx_event_flags_group_suspended_count = 1, .tx_event_flags_group_created_next = &sys_events,
    .tx_event_flags_group_created_previous = &sys_events,
};
// 使い切った（alloc が待つ）
UCHAR buf_pool_mem[8 * (32 + sizeof(UCHAR *))];
TX_BLOCK_POOL buf_pool = {
    .tx_block_pool_id = TX_BLOCK_POOL_ID, .tx_block_pool_name = "buf_pool", .tx_block_pool_available = 0,
    .tx_block_pool_total = 8, .tx_block_pool_start = buf_pool_mem, .tx_block_pool_size = sizeof(buf_pool_mem),
    .tx_block_pool_block_size = 32, .tx_block_pool_suspension_list = &th_alloc, .tx_block_pool_suspended_count = 1,
    .tx_block_pool_created_next = &buf_pool, .tx_block_pool_created_previous = &buf_pool,
};
UCHAR heap_pool_mem[4096];
TX_BYTE_POOL heap_pool = {
    .tx_byte_pool_id = TX_BYTE_POOL_ID, .tx_byte_pool_name = "heap_pool", .tx_byte_pool_available = 1000,
    .tx_byte_pool_fragments = 5, .tx_byte_pool_list = heap_pool_mem, .tx_byte_pool_search = heap_pool_mem,
    .tx_byte_pool_start = heap_pool_mem, .tx_byte_pool_size = sizeof(heap_pool_mem),
    .tx_byte_pool_created_next = &heap_pool, .tx_byte_pool_created_previous = &heap_pool,
};

// タイマ（作られた環: watchdog → blink → oneshot）。動いているものは時間の輪にある（list_head が枠を指す）
TX_TIMER tmr_watchdog = {
    .tx_timer_id = TX_TIMER_ID, .tx_timer_name = "watchdog",
    .tx_timer_internal = {.tx_timer_internal_remaining_ticks = 7, .tx_timer_internal_re_initialize_ticks = 500,
                          .tx_timer_internal_timeout_function = blink_cb, .tx_timer_internal_timeout_param = 0,
                          .tx_timer_internal_active_next = &tmr_watchdog.tx_timer_internal,
                          .tx_timer_internal_active_previous = &tmr_watchdog.tx_timer_internal,
                          .tx_timer_internal_list_head = &_tx_timer_list[10]},
    .tx_timer_created_next = &tmr_blink, .tx_timer_created_previous = &tmr_oneshot,
};
TX_TIMER tmr_blink = {
    .tx_timer_id = TX_TIMER_ID, .tx_timer_name = "blink",
    .tx_timer_internal = {.tx_timer_internal_remaining_ticks = 40, .tx_timer_internal_re_initialize_ticks = 100,
                          .tx_timer_internal_timeout_function = blink_cb, .tx_timer_internal_timeout_param = 0x1234,
                          .tx_timer_internal_active_next = &th_sleeper.tx_thread_timer,
                          .tx_timer_internal_active_previous = &th_sleeper.tx_thread_timer,
                          .tx_timer_internal_list_head = &_tx_timer_list[3]},
    .tx_timer_created_next = &tmr_oneshot, .tx_timer_created_previous = &tmr_watchdog,
};
TX_TIMER tmr_oneshot = {
    .tx_timer_id = TX_TIMER_ID, .tx_timer_name = "oneshot",
    .tx_timer_internal = {.tx_timer_internal_remaining_ticks = 50, .tx_timer_internal_timeout_function = blink_cb},
    .tx_timer_created_next = &tmr_watchdog, .tx_timer_created_previous = &tmr_blink,
};

// --- スレッド（作られた環: 宣言の順） ------------------------------------------------------------------------
#define THREAD(nm, stk, used, prio, entry, next, prev) \
    .tx_thread_id = TX_THREAD_ID, .tx_thread_name = nm, .tx_thread_stack_start = stk, \
    .tx_thread_stack_end = (stk) + STACK_SIZE - 1, .tx_thread_stack_size = STACK_SIZE, \
    .tx_thread_stack_ptr = (stk) + STACK_SIZE - (used), .tx_thread_stack_highest_ptr = (stk) + STACK_SIZE - (used), \
    .tx_thread_priority = prio, .tx_thread_user_priority = prio, .tx_thread_preempt_threshold = prio, \
    .tx_thread_user_preempt_threshold = prio, .tx_thread_inherit_priority = TX_MAX_PRIORITIES, \
    .tx_thread_entry = entry, .tx_thread_created_next = next, .tx_thread_created_previous = prev
// 時間の輪にある、スレッドの時間（スリープ・時間切れ）
#define THREAD_TIMER(self, remaining, slot, next) \
    .tx_thread_timer = {.tx_timer_internal_remaining_ticks = remaining, .tx_timer_internal_timeout_function = _tx_thread_timeout, \
                        .tx_timer_internal_timeout_param = (ULONG) &(self), \
                        .tx_timer_internal_active_next = next, .tx_timer_internal_active_previous = next, \
                        .tx_timer_internal_list_head = &_tx_timer_list[slot]}
// 待ち手が 1 つだけの待ちの環
#define SUSPENDED_ON(self, object) \
    .tx_thread_suspend_control_block = &(object), .tx_thread_suspended_next = &(self), \
    .tx_thread_suspended_previous = &(self), .tx_thread_suspending = TX_FALSE

// 実行中（10）と準備完了（10。同じ優先度の準備完了の環）
TX_THREAD th_main = {THREAD("main", main_stack, 96, 10, 0, &th_logger, &th_gone), .tx_thread_state = TX_READY,
                     .tx_thread_ready_next = &th_logger, .tx_thread_ready_previous = &th_logger, .tx_thread_run_count = 7};
TX_THREAD th_logger = {THREAD("logger", logger_stack, 64, 10, 0, &th_sensor, &th_main), .tx_thread_state = TX_READY,
                       .tx_thread_ready_next = &th_main, .tx_thread_ready_previous = &th_main};
// sem_data を 10 ティックの時間切れ付きで待つ
TX_THREAD th_sensor = {THREAD("sensor", sensor_stack, 128, 5, sensor_entry, &th_sleeper, &th_logger),
                       .tx_thread_state = TX_SEMAPHORE_SUSP, SUSPENDED_ON(th_sensor, sem_data),
                       THREAD_TIMER(th_sensor, 10, 13, &th_sensor.tx_thread_timer)};
// tx_thread_sleep(50)。bus_lock を持つ
TX_THREAD th_sleeper = {THREAD("sleeper", sleeper_stack, 64, 20, 0, &th_ctl, &th_sensor), .tx_thread_state = TX_SLEEP,
                        THREAD_TIMER(th_sleeper, 50, 3, &tmr_blink.tx_timer_internal),
                        .tx_thread_owned_mutex_count = 1, .tx_thread_owned_mutex_list = &bus_lock};
// bus_lock を時間切れ無しで待つ
TX_THREAD th_ctl = {THREAD("ctl", ctl_stack, 64, 3, ctl_entry, &th_parked, &th_sleeper),
                    .tx_thread_state = TX_MUTEX_SUSP, SUSPENDED_ON(th_ctl, bus_lock)};
// tx_thread_suspend
TX_THREAD th_parked = {THREAD("parked", parked_stack, 64, 25, 0, &th_producer, &th_ctl), .tx_thread_state = TX_SUSPENDED};
// 満杯の cmd_q に送る
ULONG producer_msg[2] = {0xC0DE, 1};
TX_THREAD th_producer = {THREAD("producer", producer_stack, 64, 8, 0, &th_consumer, &th_parked),
                         .tx_thread_state = TX_QUEUE_SUSP, SUSPENDED_ON(th_producer, cmd_q),
                         .tx_thread_additional_suspend_info = producer_msg};
// 空の reply_q から受け取る
ULONG consumer_msg[1];
TX_THREAD th_consumer = {THREAD("consumer", consumer_stack, 64, 9, 0, &th_waiter, &th_producer),
                         .tx_thread_state = TX_QUEUE_SUSP, SUSPENDED_ON(th_consumer, reply_q),
                         .tx_thread_additional_suspend_info = consumer_msg};
// sys_events のビット 1 と 2 の両方（TX_AND_CLEAR）を待つ
ULONG waiter_flags;
TX_THREAD th_waiter = {THREAD("waiter", waiter_stack, 64, 12, 0, &th_alloc, &th_consumer),
                       .tx_thread_state = TX_EVENT_FLAG, SUSPENDED_ON(th_waiter, sys_events),
                       .tx_thread_suspend_info = 0x6, .tx_thread_suspend_option = TX_AND_CLEAR,
                       .tx_thread_additional_suspend_info = &waiter_flags};
// 使い切った buf_pool から確保する
TX_THREAD th_alloc = {THREAD("alloc", alloc_stack, 64, 15, 0, &th_gone, &th_waiter),
                      .tx_thread_state = TX_BLOCK_MEMORY, SUSPENDED_ON(th_alloc, buf_pool)};
// 終わった（tx_thread_terminate）。スタックの底が壊れている
TX_THREAD th_gone = {THREAD("gone", gone_stack, 64, 10, 0, &th_main, &th_alloc), .tx_thread_state = TX_TERMINATED};

// --- 呼び出し履歴の試験に使う関数 ----------------------------------------------------------------------
volatile ULONG samples_taken;
volatile ULONG blinks;

// tx_api.h が tx_semaphore_get を _txe_semaphore_get（引数を確かめる版）にする
__attribute__((noinline)) UINT _txe_semaphore_get(TX_SEMAPHORE *semaphore_ptr, ULONG wait_option) {
    if (semaphore_ptr->tx_semaphore_count > 0u) {
        semaphore_ptr->tx_semaphore_count--;
        return TX_SUCCESS;
    }
    return wait_option == TX_NO_WAIT ? TX_NO_INSTANCE : TX_WAIT_ABORTED;
}

__attribute__((noinline)) void sensor_entry(ULONG id) {
    (void) id;
    for (;;) {
        if (tx_semaphore_get(&sem_data, 10u) == TX_SUCCESS) {
            samples_taken++;
        }
    }
}

__attribute__((noinline)) void ctl_entry(ULONG id) {
    (void) id;
    for (;;) {
        samples_taken++;
    }
}

void blink_cb(ULONG id) {
    (void) id;
    blinks++;
}

VOID _tx_thread_timeout(ULONG timeout_input) {
    (void) timeout_input;
}

// 新しいスレッドの最初の PC（tx_thread_shell_entry.c と同じく、いまのスレッドの入口を呼ぶ）
__attribute__((noinline)) VOID _tx_thread_shell_entry(VOID) {
    TX_THREAD *thread_ptr = _tx_thread_current_ptr;
    (thread_ptr->tx_thread_entry)(thread_ptr->tx_thread_entry_parameter);
    for (;;) {
    }
}

extern ULONG __stack_top;

__attribute__((noreturn)) void reset_handler(void) {
    _tx_thread_shell_entry();
    for (;;) {
    }
}

__attribute__((section(".vectors"), used)) const void *const vectors[2] = {&__stack_top, (void *) reset_handler};
