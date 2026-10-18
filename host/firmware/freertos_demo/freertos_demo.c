// fprobe の RTOS の認識（F6）の試験用ファームウェア: FreeRTOS（V11.1.0、FreeRTOS-Kernel/）のタスクを 5 つ動かす。
// Cortex-M4F（Nucleo F401RE）の SRAM に読み込んで SRAM から実行する（Flash には書かない）。
// 作り方: python host/firmware/freertos_demo/build.py（zig cc。生成した freertos_demo.elf は試験のためにリポジトリに置く）。
//
// タスク（優先度の高い順）:
//   ctrl    (3) キュー cmd_queue を無期限に待つ（停止リスト + イベントリスト = 待ち）。受け取ったら log_mutex を持って
//               合計に足し、log_stream に書く。4 回に 1 回 done_sem を渡す。
//   sensor  (2) vTaskDelay(5 ms) → cmd_queue に送り、sync_events のビット 0 を立てる（遅延リスト = 待ち）。スタックは小さい。
//   Tmr Svc (2) FreeRTOS のタイマのタスク（tick_timer の 10 ms ごとの呼び出し）。
//   worker  (1) done_sem を 50 ms まで待つ（遅延リスト + イベントリスト）。取れたら log_mutex を約 6 ms 持って深い再帰
//               （その間に ctrl が log_mutex を待つ = 優先度の逆転。継承で worker が優先度 3 に上がる）。msg_buffer に書く。
//   fpu     (1) 浮動小数点の計算（FPU の文脈 = 拡張フレーム）→ sync_events のどれかのビットを 2 ms まで待つ。
//   dormant (1) 作ってすぐ止める（vTaskSuspend = 停止）。
//   IDLE    (0) FreeRTOS のアイドル。
//
// カーネルのオブジェクト（fprobe の rtos.objects の試験）: キュー cmd_queue・2 値セマフォ done_sem・ミューテックス
// log_mutex（以上は登録簿に名前）・数えるセマフォ slots（登録簿に無い: 変数の名前で）・タイマ tick_timer（自動で繰り返す）と
// oneshot_timer（一度だけ。始めない）・イベントグループ sync_events・ストリームバッファ log_stream・メッセージバッファ
// msg_buffer（どちらも読む人がいないので埋まったまま）。ヒープは heap_4。
//
// トレース（fprobe_trace.h・fprobe_trace.c）: タスクの切り替え（ITM ポート 31）とカーネルの出来事（ポート 30・29）。
// ターゲットが出す値とイベント（fprobe_user.h、U16）: fpu の計算の区間 "fpu_step"（start / stop）・値 fpu_value
// （ポート 1、f32）、sensor の値 "i:sensor_count"、ctrl が done_sem を渡す印 "done"。どれも ITM のポートが無効なら出ない。
// idle_spins: アイドルのフックが回った数（トレースの有無で比べるとフックの負荷が分かる。U10 の実機の手順）。
#include <stdint.h>

#include "FreeRTOS.h"
#include "queue.h"
#include "event_groups.h"
#include "message_buffer.h"
#include "semphr.h"
#include "stream_buffer.h"
#include "task.h"
#include "timers.h"

#include "fprobe_user.h"

extern uint32_t __bss_start, __bss_end, __stack_top;
extern const void *const vectors[16];

void vPortSVCHandler(void);
void xPortPendSVHandler(void);
void xPortSysTickHandler(void);

#define SCB_VTOR   (*(volatile uint32_t *) 0xE000ED08u)
#define SCB_CPACR  (*(volatile uint32_t *) 0xE000ED88u)

#define WORKER_DEPTH 6
#define WORKER_HOLD_SPINS 16000u               // log_mutex を持つ間の空回り（16 MHz で約 6 ms）
#define EVT_SENSOR (1u << 0)
#define EVT_TIMER  (1u << 1)

volatile uint32_t fprobe_trace_enabled;      // 方法 B のトレースフック（fprobe_trace.h）。デバッガが 1 を書くと出す

QueueHandle_t cmd_queue;                     // sensor → ctrl（uint32_t を 4 つまで）
SemaphoreHandle_t done_sem;                  // ctrl → worker（2 値セマフォ）
SemaphoreHandle_t log_mutex;                 // worker が再帰の間だけ持つ
SemaphoreHandle_t slots;                     // 数えるセマフォ（最大 3、登録簿に無い）
TimerHandle_t tick_timer;                    // 10 ms ごと（自動で繰り返す）
TimerHandle_t oneshot_timer;                 // 100 ms の一度だけ（始めない = 止まっている）
EventGroupHandle_t sync_events;              // sensor と tick_timer が立て、fpu が待つ
StreamBufferHandle_t log_stream;             // ctrl が書く（読む人がいない）
MessageBufferHandle_t msg_buffer;            // worker が書く（読む人がいない）
TaskHandle_t ctrl_handle, sensor_handle, worker_handle, fpu_handle, dormant_handle;

volatile uint32_t ctrl_total;                // ctrl が受け取った値の合計
volatile uint32_t sensor_count;              // sensor が送った回数
volatile uint32_t worker_result;             // 再帰の結果
volatile uint32_t worker_rounds;
volatile float fpu_value = 1.0f;             // fpu の計算の結果
volatile uint32_t fpu_rounds;
volatile uint32_t timer_ticks;               // tick_timer の呼び出しの回数
volatile uint32_t assert_line;               // configASSERT が失敗した行
volatile uint32_t idle_spins;                // アイドルのフックが回った数

__attribute__((noinline)) void freertos_assert_failed(const char *file, int line) {
    (void) file;
    assert_line = (uint32_t) line;           // ブレークポイントの的
    taskDISABLE_INTERRUPTS();
    for (;;) {
    }
}

void vApplicationStackOverflowHook(TaskHandle_t task, char *name) {
    (void) task;
    (void) name;
    freertos_assert_failed("stack overflow", 0);
}

void vApplicationIdleHook(void) {
    idle_spins++;
}

void vApplicationMallocFailedHook(void) {
    freertos_assert_failed("malloc failed", 0);
}

static void ctrl_task(void *arg) {
    (void) arg;
    for (;;) {
        uint32_t value;
        if (xQueueReceive(cmd_queue, &value, portMAX_DELAY) == pdPASS) {
            xSemaphoreTake(log_mutex, portMAX_DELAY);
            ctrl_total += value;
            xSemaphoreGive(log_mutex);
            xStreamBufferSend(log_stream, &value, sizeof(value), 0);
            if ((value & 3u) == 0u) {
                FPROBE_EVENT_MARK("done");
                xSemaphoreGive(done_sem);
            }
        }
    }
}

static void sensor_task(void *arg) {
    (void) arg;
    uint32_t n = 0;
    for (;;) {
        vTaskDelay(pdMS_TO_TICKS(5));
        n++;
        xQueueSend(cmd_queue, &n, 0);
        xEventGroupSetBits(sync_events, EVT_SENSOR);
        sensor_count = n;
        FPROBE_VALUE_I("sensor_count", n);
    }
}

// 深い呼び出し（タスクごとの呼び出し履歴の試験）。各段にローカルの配列を持つ
__attribute__((noinline)) uint32_t recurse(uint32_t depth, uint32_t seed) {
    volatile uint32_t scratch[8];
    for (uint32_t i = 0; i < 8; i++) {
        scratch[i] = seed + i * depth;
    }
    if (depth == 0) {
        return seed ^ scratch[3];
    }
    return recurse(depth - 1, seed * 31u + depth) + scratch[depth & 7u];
}

static void worker_task(void *arg) {
    (void) arg;
    for (;;) {
        if (xSemaphoreTake(done_sem, pdMS_TO_TICKS(50)) == pdPASS) {
            xSemaphoreTake(log_mutex, portMAX_DELAY);
            worker_result = recurse(WORKER_DEPTH, worker_rounds);
            for (volatile uint32_t i = 0; i < WORKER_HOLD_SPINS; i++) {
            }
            worker_rounds++;
            xSemaphoreGive(log_mutex);
            uint32_t round = worker_rounds;
            xMessageBufferSend(msg_buffer, &round, sizeof(round), 0);
        }
    }
}

__attribute__((noinline)) float fpu_step(float x, uint32_t n) {
    for (uint32_t i = 0; i < n; i++) {
        x = x * 1.0001f + 0.5f;
        if (x > 1000.0f) {
            x *= 0.001f;
        }
    }
    return x;
}

static void fpu_task(void *arg) {
    (void) arg;
    float x = fpu_value;
    for (;;) {
        FPROBE_EVENT_START("fpu_step");
        x = fpu_step(x, 200);
        FPROBE_EVENT_STOP("fpu_step");
        fpu_value = x;
        FPROBE_PLOT_F32(1, x);
        fpu_rounds++;
        xEventGroupWaitBits(sync_events, EVT_SENSOR | EVT_TIMER, pdTRUE, pdFALSE, pdMS_TO_TICKS(2));
    }
}

static void tick_timer_callback(TimerHandle_t timer) {
    (void) timer;
    timer_ticks++;
    xEventGroupSetBits(sync_events, EVT_TIMER);
}

static void oneshot_callback(TimerHandle_t timer) {
    (void) timer;
}

static void dormant_task(void *arg) {
    (void) arg;
    for (;;) {
        vTaskDelay(portMAX_DELAY);
    }
}

__attribute__((noreturn)) void reset_handler(void) {
    for (uint32_t *p = &__bss_start; p < &__bss_end; p++) {
        *p = 0;
    }
    SCB_VTOR = (uint32_t) vectors;                       // SRAM のベクタ表（FreeRTOS が SVC・PendSV の登録を確かめる）
    SCB_CPACR |= (0xFu << 20);                           // CP10・CP11（FPU）を使えるように
    __asm volatile("dsb\n isb");

    cmd_queue = xQueueCreate(4, sizeof(uint32_t));
    done_sem = xSemaphoreCreateBinary();
    log_mutex = xSemaphoreCreateMutex();
    vQueueAddToRegistry(cmd_queue, "cmd_queue");
    vQueueAddToRegistry(done_sem, "done_sem");
    vQueueAddToRegistry(log_mutex, "log_mutex");
    slots = xSemaphoreCreateCounting(3, 3);
    tick_timer = xTimerCreate("tick_timer", pdMS_TO_TICKS(10), pdTRUE, (void *) 1, tick_timer_callback);
    oneshot_timer = xTimerCreate("oneshot", pdMS_TO_TICKS(100), pdFALSE, (void *) 2, oneshot_callback);
    sync_events = xEventGroupCreate();
    log_stream = xStreamBufferCreate(64, 1);
    msg_buffer = xMessageBufferCreate(64);
    xTimerStart(tick_timer, 0);

    xTaskCreate(ctrl_task, "ctrl", 256, NULL, 3, &ctrl_handle);
    xTaskCreate(sensor_task, "sensor", 160, NULL, 2, &sensor_handle);
    xTaskCreate(worker_task, "worker", 512, NULL, 1, &worker_handle);
    xTaskCreate(fpu_task, "fpu", 256, NULL, 1, &fpu_handle);
    xTaskCreate(dormant_task, "dormant", configMINIMAL_STACK_SIZE, NULL, 1, &dormant_handle);
    vTaskSuspend(dormant_handle);

    vTaskStartScheduler();
    freertos_assert_failed("scheduler returned", 0);
    for (;;) {
    }
}

__attribute__((noreturn)) void default_handler(void) {
    for (;;) {
    }
}

// ベクタ表（SRAM の先頭）。SVC・PendSV・SysTick は FreeRTOS の port（ARM_CM4F）
__attribute__((section(".vectors"), used)) const void *const vectors[16] = {
    &__stack_top, (void *) reset_handler, (void *) default_handler, (void *) default_handler,
    (void *) default_handler, (void *) default_handler, (void *) default_handler, 0, 0, 0, 0,
    (void *) vPortSVCHandler, (void *) default_handler, 0, (void *) xPortPendSVHandler, (void *) xPortSysTickHandler,
};
