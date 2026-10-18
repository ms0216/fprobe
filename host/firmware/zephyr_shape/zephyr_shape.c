// fprobe の RTOS の認識（F6）の試験用: Zephyr と同じ名前・形の構造体（k_thread・_kernel・k_sem 等）を持つ小さな ELF。
// カーネルオブジェクト（U08）: k_sem・k_mutex（継承の無い優先度の逆転: ctl(2) が sleeper(7) の bus_lock を待ち、
// logger(4) が実行可能）・k_msgq・k_timer（timeout_list の差分）・k_event・k_fifo・k_heap（_system_heap）。
// Zephyr そのものではない（Zephyr は取り込まない）。Zephyr v3.7〜4.x の Cortex-M の構造体のうち、fprobe.rtos.zephyr が
// 読むメンバー（と、位置を本物に近づけるための周りのメンバー）だけを、同じ名前で持つ。
// スレッドのリストと状態は .data の初期値に「止めたときの様子」として置く（試験は ELF の像をそのまま偽のメモリにする）。
// 作り方: python host/firmware/zephyr_shape/build.py（zig cc。生成した zephyr_shape.elf はリポジトリに置く）。
#include <stddef.h>
#include <stdint.h>

// --- Zephyr の型（include/zephyr/kernel/thread.h・kernel_structs.h・arch/arm/thread.h と同じ名前） ---------
typedef struct _dnode {
    struct _dnode *head;
    struct _dnode *tail;
} sys_dlist_t;

typedef struct {
    sys_dlist_t waitq;
} _wait_q_t;

struct _timeout {
    sys_dlist_t node;
    void *fn;
    int64_t dticks;
};

struct _thread_base {
    sys_dlist_t qnode_dlist;
    _wait_q_t *pended_on;
    uint8_t user_options;
    uint8_t thread_state;
    union {
        struct {
            int8_t prio;
            uint8_t sched_locked;
        };
        uint16_t preempt;
    };
    uint32_t order_key;
    void *swap_data;
    struct _timeout timeout;
};

struct _callee_saved {
    uint32_t v1;   // r4
    uint32_t v2;   // r5
    uint32_t v3;   // r6
    uint32_t v4;   // r7
    uint32_t v5;   // r8
    uint32_t v6;   // r9
    uint32_t v7;   // r10
    uint32_t v8;   // r11
    uint32_t psp;  // r13
};

struct _thread_stack_info {
    uintptr_t start;
    size_t size;
    size_t delta;
};

struct _thread_arch {
    uint32_t basepri;
    uint32_t swap_return_value;
    uint8_t mode_exc_return;   // EXC_RETURN の下位 8 ビット
};

struct k_thread {
    struct _thread_base base;
    struct _callee_saved callee_saved;
    void *init_data;
    _wait_q_t join_queue;
    struct k_thread *next_thread;          // CONFIG_THREAD_MONITOR
    char name[32];                         // CONFIG_THREAD_NAME
    struct _thread_stack_info stack_info;  // CONFIG_THREAD_STACK_INFO
    struct _thread_arch arch;
};

struct _cpu {
    uint32_t nested;
    char *irq_stack;
    struct k_thread *current;
    struct k_thread *idle_thread;
    uint8_t id;
};

struct z_kernel {
    struct _cpu cpus[1];
    uint32_t ready_q[4];
    struct k_thread *threads;              // CONFIG_THREAD_MONITOR
};

struct k_sem {
    _wait_q_t wait_q;
    unsigned int count;
    unsigned int limit;
};

struct k_spinlock {
    uint32_t locked;
};

struct k_mutex {
    _wait_q_t wait_q;
    struct k_thread *owner;
    uint32_t lock_count;
    int owner_orig_prio;
};

struct k_msgq {
    _wait_q_t wait_q;
    struct k_spinlock lock;
    size_t msg_size;
    uint32_t max_msgs;
    char *buffer_start;
    char *buffer_end;
    char *read_ptr;
    char *write_ptr;
    uint32_t used_msgs;
    uint8_t flags;
};

struct k_event {
    _wait_q_t wait_q;
    uint32_t events;
    struct k_spinlock lock;
};

typedef struct {
    int64_t ticks;
} k_timeout_t;

struct k_timer {
    struct _timeout timeout;
    _wait_q_t wait_q;
    void (*expiry_fn)(struct k_timer *timer);
    void (*stop_fn)(struct k_timer *timer);
    k_timeout_t period;
    uint32_t status;
    void *user_data;
};

typedef struct _snode {
    uintptr_t next_and_flags;
} sys_sfnode_t;

typedef struct {
    sys_sfnode_t *head;
    sys_sfnode_t *tail;
} sys_sflist_t;

struct k_queue {
    sys_sflist_t data_q;
    struct k_spinlock lock;
    _wait_q_t wait_q;
};

struct k_fifo {
    struct k_queue _queue;
};

struct z_heap {
    uint64_t chunk0_hdr[2];
    uint32_t end_chunk;
    uint32_t avail_buckets;
    size_t free_bytes;           // CONFIG_SYS_HEAP_RUNTIME_STATS
    size_t allocated_bytes;
    size_t max_allocated_bytes;
};

struct sys_heap {
    struct z_heap *heap;
    void *init_mem;
    size_t init_bytes;
};

struct k_heap {
    struct sys_heap heap;
    _wait_q_t wait_q;
    struct k_spinlock lock;
};

#define _THREAD_PENDING   (1u << 1)
#define _THREAD_SLEEPING  (1u << 2)
#define _THREAD_DEAD      (1u << 3)
#define _THREAD_SUSPENDED (1u << 4)
#define _THREAD_QUEUED    (1u << 7)

#define STACK_SIZE 512
#define FILL {[0 ... STACK_SIZE - 1] = 0xAA}   // CONFIG_INIT_STACKS の塗り

// --- 止めたときの様子 ----------------------------------------------------------------------------------
__attribute__((aligned(8))) uint8_t main_stack[STACK_SIZE] = FILL;
__attribute__((aligned(8))) uint8_t sensor_stack[STACK_SIZE] = FILL;
__attribute__((aligned(8))) uint8_t sleeper_stack[STACK_SIZE] = FILL;
__attribute__((aligned(8))) uint8_t parked_stack[STACK_SIZE] = FILL;
__attribute__((aligned(8))) uint8_t idle_stack[STACK_SIZE] = FILL;

struct k_sem sem_data = {.count = 0, .limit = 1};
struct k_thread main_thread, sensor_thread, sleeper_thread, parked_thread, idle_thread, gone_thread, ctl_thread,
    logger_thread;

// sleeper（7）が持ち、ctl（2）が待つ。継承していない（sleeper の優先度は元のまま）
struct k_mutex bus_lock = {.owner = &sleeper_thread, .lock_count = 1, .owner_orig_prio = 7};

char cmd_msgq_buffer[8 * 4];
struct k_msgq cmd_msgq = {
    .msg_size = 4, .max_msgs = 8, .buffer_start = cmd_msgq_buffer, .buffer_end = cmd_msgq_buffer + sizeof(cmd_msgq_buffer),
    .read_ptr = cmd_msgq_buffer + 4, .write_ptr = cmd_msgq_buffer + 16, .used_msgs = 3,
};

struct k_event sys_events = {.events = 0x5};

void blink_expired(struct k_timer *timer) {
    (void) timer;
}

// timeout_list（kernel/timeout.c）: 期限の差分の列。watchdog_timer（20）→ blink_timer（+30 = 50）
extern struct k_timer watchdog_timer, blink_timer;
sys_dlist_t timeout_list = {.head = &watchdog_timer.timeout.node, .tail = &blink_timer.timeout.node};
struct k_timer watchdog_timer = {
    .timeout = {.node = {.head = &blink_timer.timeout.node, .tail = &timeout_list}, .dticks = 20},
    .expiry_fn = blink_expired, .period = {.ticks = 0},
};
struct k_timer blink_timer = {
    .timeout = {.node = {.head = &timeout_list, .tail = &watchdog_timer.timeout.node}, .dticks = 30},
    .expiry_fn = blink_expired, .period = {.ticks = 100}, .status = 4, .user_data = (void *) 0x1234,
};
struct k_timer stopped_timer = {.expiry_fn = blink_expired, .period = {.ticks = 10}};

sys_sfnode_t rx_items[2] = {{.next_and_flags = (uintptr_t) &rx_items[1]}, {.next_and_flags = 0}};
struct k_fifo rx_fifo = {._queue = {.data_q = {.head = &rx_items[0], .tail = &rx_items[1]}}};

struct z_heap system_heap_state = {.free_bytes = 3000, .allocated_bytes = 1000, .max_allocated_bytes = 1500};
struct k_heap _system_heap = {.heap = {.heap = &system_heap_state, .init_bytes = 4096}};

#define STACK(s) {.start = (uintptr_t) (s), .size = STACK_SIZE, .delta = 0}
#define CALLEE(s, used) {.psp = (uint32_t) (uintptr_t) ((s) + STACK_SIZE - (used))}

struct k_thread main_thread = {
    .base = {.thread_state = _THREAD_QUEUED, .prio = 0},
    .callee_saved = CALLEE(main_stack, 96), .next_thread = &sensor_thread, .name = "main",
    .stack_info = STACK(main_stack), .arch = {.mode_exc_return = 0xFD},
};
struct k_thread sensor_thread = {
    .base = {.thread_state = _THREAD_PENDING, .prio = 5, .pended_on = &sem_data.wait_q},
    .callee_saved = CALLEE(sensor_stack, 128), .next_thread = &sleeper_thread, .name = "sensor",
    .stack_info = STACK(sensor_stack), .arch = {.mode_exc_return = 0xFD},
};
struct k_thread sleeper_thread = {
    .base = {.thread_state = _THREAD_SLEEPING, .prio = 7},
    .callee_saved = CALLEE(sleeper_stack, 64), .next_thread = &parked_thread, .name = "sleeper",
    .stack_info = STACK(sleeper_stack), .arch = {.mode_exc_return = 0xFD},
};
struct k_thread parked_thread = {
    .base = {.thread_state = _THREAD_SUSPENDED, .prio = -2},
    .callee_saved = CALLEE(parked_stack, 64), .next_thread = &idle_thread, .name = "parked",
    .stack_info = STACK(parked_stack), .arch = {.mode_exc_return = 0xFD},
};
struct k_thread idle_thread = {
    .base = {.thread_state = _THREAD_QUEUED, .prio = 15},
    .callee_saved = CALLEE(idle_stack, 64), .next_thread = &gone_thread, .name = "idle",
    .stack_info = STACK(idle_stack), .arch = {.mode_exc_return = 0xFD},
};
struct k_thread gone_thread = {
    .base = {.thread_state = _THREAD_DEAD, .prio = 3}, .next_thread = &ctl_thread, .name = "gone",
};
__attribute__((aligned(8))) uint8_t ctl_stack[STACK_SIZE] = FILL;
__attribute__((aligned(8))) uint8_t logger_stack[STACK_SIZE] = FILL;
struct k_thread ctl_thread = {
    .base = {.thread_state = _THREAD_PENDING, .prio = 2, .pended_on = &bus_lock.wait_q},
    .callee_saved = CALLEE(ctl_stack, 64), .next_thread = &logger_thread, .name = "ctl",
    .stack_info = STACK(ctl_stack), .arch = {.mode_exc_return = 0xFD},
};
struct k_thread logger_thread = {
    .base = {.thread_state = _THREAD_QUEUED, .prio = 4},
    .callee_saved = CALLEE(logger_stack, 64), .next_thread = NULL, .name = "logger",
    .stack_info = STACK(logger_stack), .arch = {.mode_exc_return = 0xFD},
};

struct z_kernel _kernel = {
    .cpus = {{.current = &main_thread, .idle_thread = &idle_thread}},
    .threads = &main_thread,
};

// --- 呼び出し履歴の試験に使う関数 ----------------------------------------------------------------------
volatile uint32_t samples_taken;

__attribute__((noinline)) int k_sem_take(struct k_sem *sem, int32_t timeout) {
    if (sem->count > 0u) {
        sem->count--;
        return 0;
    }
    return timeout == 0 ? -16 : -11;
}

__attribute__((noinline)) void sensor_entry(void *p1, void *p2, void *p3) {
    (void) p1;
    (void) p2;
    (void) p3;
    for (;;) {
        if (k_sem_take(&sem_data, -1) == 0) {
            samples_taken++;
        }
    }
}

extern uint32_t __stack_top;

__attribute__((noreturn)) void reset_handler(void) {
    sensor_entry(0, 0, 0);
    for (;;) {
    }
}

__attribute__((section(".vectors"), used)) const void *const vectors[2] = {&__stack_top, (void *) reset_handler};
