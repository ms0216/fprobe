/* freertos_demo の FreeRTOS の設定（STM32F401 = Cortex-M4F、リセット直後の HSI 16 MHz のまま動かす）。
 *
 * fprobe の RTOS の認識（host/fprobe/rtos）が読むものを有効にしている:
 *   - configUSE_TRACE_FACILITY: TCB の番号・キューの種類（ucQueueType）。新しいスタックを 0xA5 で塗る。
 *   - configRECORD_STACK_HIGH_ADDRESS: TCB にスタックの末尾（pxEndOfStack）。スタックの大きさが分かる。
 *   - configQUEUE_REGISTRY_SIZE: キュー・セマフォの名前（xQueueRegistry）。
 *   - configCHECK_FOR_STACK_OVERFLOW 2: 切り替えのたびにスタックの末尾の塗りを確かめる。
 *   - configUSE_TIMERS: ソフトウェアタイマ（タイマのリスト・タイマのタスク Tmr Svc）。カーネルのオブジェクトの一覧の試験。
 * 方法 B（組み込みあり）のトレースフックは fprobe_trace.h・fprobe_trace.c（既定は無効。fprobe_trace_enabled を 1 にすると出す）。
 *   タスクの切り替えのほか、カーネルの出来事（キュー・セマフォ・ミューテックス・準備完了・待ち・優先度・割り込みの出入り）。
 * configUSE_IDLE_HOOK: idle_spins（トレースの負荷の実機の計測）。
 */
#ifndef FREERTOS_CONFIG_H
#define FREERTOS_CONFIG_H

#define configCPU_CLOCK_HZ                      16000000UL
#define configTICK_RATE_HZ                      1000
#define configUSE_PREEMPTION                    1
#define configUSE_TIME_SLICING                  1
#define configUSE_PORT_OPTIMISED_TASK_SELECTION 0
#define configUSE_TICKLESS_IDLE                 0
#define configMAX_PRIORITIES                    5
#define configMINIMAL_STACK_SIZE                128
#define configMAX_TASK_NAME_LEN                 12
#define configTICK_TYPE_WIDTH_IN_BITS           TICK_TYPE_WIDTH_32_BITS
#define configIDLE_SHOULD_YIELD                 1
#define configTASK_NOTIFICATION_ARRAY_ENTRIES   1
#define configQUEUE_REGISTRY_SIZE               8
#define configENABLE_BACKWARD_COMPATIBILITY     0
#define configNUM_THREAD_LOCAL_STORAGE_POINTERS 0
#define configUSE_MINI_LIST_ITEM                1
#define configSTACK_DEPTH_TYPE                  uint32_t
#define configMESSAGE_BUFFER_LENGTH_TYPE        size_t
#define configHEAP_CLEAR_MEMORY_ON_FREE         1
#define configUSE_NEWLIB_REENTRANT              0

#define configSUPPORT_STATIC_ALLOCATION         0
#define configSUPPORT_DYNAMIC_ALLOCATION        1
#define configTOTAL_HEAP_SIZE                   ( 20 * 1024 )
#define configAPPLICATION_ALLOCATED_HEAP        0

#define configKERNEL_INTERRUPT_PRIORITY         ( 15 << 4 )   /* STM32F4 は優先度 4 ビット: 最も低い */
#define configMAX_SYSCALL_INTERRUPT_PRIORITY    ( 5 << 4 )
#define configCHECK_HANDLER_INSTALLATION        1

#define configUSE_IDLE_HOOK                     1
#define configUSE_TICK_HOOK                     0
#define configCHECK_FOR_STACK_OVERFLOW          2
#define configUSE_MALLOC_FAILED_HOOK             1
#define configUSE_DAEMON_TASK_STARTUP_HOOK      0

#define configGENERATE_RUN_TIME_STATS           0
#define configUSE_TRACE_FACILITY                1
#define configUSE_STATS_FORMATTING_FUNCTIONS    0
#define configRECORD_STACK_HIGH_ADDRESS         1

#define configUSE_CO_ROUTINES                   0
#define configUSE_TIMERS                        1
#define configTIMER_TASK_PRIORITY               2
#define configTIMER_QUEUE_LENGTH                4
#define configTIMER_TASK_STACK_DEPTH            192
#define configUSE_MUTEXES                       1
#define configUSE_RECURSIVE_MUTEXES             0
#define configUSE_COUNTING_SEMAPHORES           1
#define configUSE_QUEUE_SETS                    0
#define configUSE_TASK_NOTIFICATIONS            1

#define INCLUDE_vTaskPrioritySet                0
#define INCLUDE_uxTaskPriorityGet               1
#define INCLUDE_vTaskDelete                     1
#define INCLUDE_vTaskSuspend                    1
#define INCLUDE_xTaskDelayUntil                 1
#define INCLUDE_vTaskDelay                      1
#define INCLUDE_xTaskGetSchedulerState          1
#define INCLUDE_xTaskGetCurrentTaskHandle       1
#define INCLUDE_uxTaskGetStackHighWaterMark     1
#define INCLUDE_xTaskGetIdleTaskHandle          0
#define INCLUDE_eTaskGetState                   1

/* 失敗はここで止まる（ブレークポイントの的: freertos_assert_failed）。 */
#ifndef __ASSEMBLER__
void freertos_assert_failed(const char *file, int line);
#endif
#define configASSERT(x)    do { if (!(x)) { freertos_assert_failed(__FILE__, __LINE__); } } while (0)

/* 方法 B: タスクの切り替えとカーネルの出来事を ITM のスティミュラス・ポートに出す（fprobe_trace.h）。 */
#ifndef __ASSEMBLER__
#include "fprobe_trace.h"
#endif

#endif /* FREERTOS_CONFIG_H */
