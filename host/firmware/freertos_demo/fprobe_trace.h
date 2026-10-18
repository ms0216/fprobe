// SPDX-License-Identifier: Apache-2.0
/* 方法 B（組み込みあり）: FreeRTOS のトレースフックで、タスクとカーネルの出来事を ITM のスティミュラス・ポートに書く。
 * 仕様: docs/superpowers/specs/2026-10-18-u10-u16-trace-hooks-user-events-design.md の 1 節（ポート 31 は F6 の 5.2 節）。
 *
 * ポート 31（FPROBE_TRACE_PORT、1 語）: タスクの切り替え。
 *   bit 31..2 = TCB の番地の bit 31..2（TCB は 4 バイト以上に揃う）、bit 1..0 = 出来事
 *   0 = 実行を始めた（traceTASK_SWITCHED_IN）、1 = 実行をやめた（traceTASK_SWITCHED_OUT）、
 *   2 = 作った（traceTASK_CREATE）、3 = 消した（traceTASK_DELETE）
 * ポート 30（FPROBE_KERNEL_PORT）: カーネルの出来事の記録の見出し。ポート 29（FPROBE_PAYLOAD_PORT）: 続く中身の語。
 *   見出し: bit 7..0 = 出来事（FPROBE_EV_*）、bit 9..8 = 中身の語の数、bit 18..10 = IPSR、bit 31..19 = 引数（飽和）
 *   オブジェクトの語: bit 31..2 = 番地、bit 1..0 = 種類（FPROBE_CLASS_*）
 * 記録は割り込みを禁止して書く（fprobe_trace.c の fprobe_trace_record）。
 * ホストの解読: host/fprobe/rtos/timeline.py（ポート 31）、host/fprobe/rtos/kernel_trace.py（ポート 30・29）。
 *
 * 既定は無効。デバッガが fprobe_trace_enabled に 1 を書き、ITM（TCR.ITMENA と TER のポート 29・30・31）と SWO を有効に
 * すると出る。ITM が無効なら何もしない（スティミュラスの FIFO を待たない）。
 * アプリの割り込みの出入りも記録するなら、ISR の始めに FPROBE_ISR_ENTER()、終わりに FPROBE_ISR_EXIT() を書く
 * （例外トレースを使えばフック無しで分かる: rtos.timeline.start の exceptions）。
 */
#ifndef FPROBE_TRACE_H
#define FPROBE_TRACE_H

#include <stdint.h>

#define FPROBE_TRACE_PORT     31u
#define FPROBE_TRACE_IN       0u
#define FPROBE_TRACE_OUT      1u
#define FPROBE_TRACE_CREATE   2u
#define FPROBE_TRACE_DELETE   3u

#define FPROBE_KERNEL_PORT    30u
#define FPROBE_PAYLOAD_PORT   29u
#define FPROBE_ARG_MAX        0x1FFFu   /* 引数の 13 ビット */

/* カーネルの出来事の番号（ホストの fprobe.rtos.kernel_trace.EVENTS と同じ） */
#define FPROBE_EV_READY            1u
#define FPROBE_EV_DELAY            2u
#define FPROBE_EV_SUSPEND          3u
#define FPROBE_EV_BLOCK            4u
#define FPROBE_EV_PRIO_INHERIT     5u
#define FPROBE_EV_PRIO_DISINHERIT  6u
#define FPROBE_EV_PRIO_SET         7u
#define FPROBE_EV_SEND             16u
#define FPROBE_EV_SEND_FAILED      17u
#define FPROBE_EV_RECEIVE          18u
#define FPROBE_EV_RECEIVE_FAILED   19u
#define FPROBE_EV_PEEK             20u
#define FPROBE_EV_CREATE           21u
#define FPROBE_EV_STREAM_SEND      22u
#define FPROBE_EV_STREAM_RECEIVE   23u
#define FPROBE_EV_EVENT_SET        24u
#define FPROBE_EV_NOTIFY           25u
#define FPROBE_EV_ISR_ENTER        32u
#define FPROBE_EV_ISR_EXIT         33u

/* オブジェクトの種類（語の bit 1..0） */
#define FPROBE_CLASS_QUEUE         0u
#define FPROBE_CLASS_SEMAPHORE     1u
#define FPROBE_CLASS_MUTEX         2u
#define FPROBE_CLASS_OTHER         3u

/* block の引数（待ちの向き） */
#define FPROBE_WAIT_RECEIVE        0u
#define FPROBE_WAIT_SEND           1u
#define FPROBE_WAIT_PEEK           2u
#define FPROBE_WAIT_NOTIFY         3u

#define FPROBE_ITM_PORT(n)    (*(volatile uint32_t *) (0xE0000000u + 4u * (n)))
#define FPROBE_ITM_TER        (*(volatile uint32_t *) 0xE0000E00u)
#define FPROBE_ITM_TCR        (*(volatile uint32_t *) 0xE0000E80u)

extern volatile uint32_t fprobe_trace_enabled;

/* 記録を 1 つ書く（見出し + 中身 count 語。count は 0〜2）。fprobe_trace_enabled の確認は呼ぶ側（無効のとき呼ばない）。 */
void fprobe_trace_record(uint32_t code_count, uint32_t arg, uint32_t w0, uint32_t w1);

static inline void fprobe_trace_word(uint32_t word) {
    if (fprobe_trace_enabled && (FPROBE_ITM_TCR & 1u) && (FPROBE_ITM_TER & (1u << FPROBE_TRACE_PORT))) {
        while (FPROBE_ITM_PORT(FPROBE_TRACE_PORT) == 0u) {
            /* FIFO が空くのを待つ（読むと 1 = 書ける） */
        }
        FPROBE_ITM_PORT(FPROBE_TRACE_PORT) = word;
    }
}

#define fprobe_trace_task(tcb, event)    fprobe_trace_word(((uint32_t) (tcb) & ~3u) | (event))

/* 無効のときの負荷は fprobe_trace_enabled の読みと分岐だけ */
#define fprobe_kernel(code, count, arg, w0, w1)                                                             \
    do {                                                                                                    \
        if (fprobe_trace_enabled) {                                                                         \
            fprobe_trace_record((uint32_t) (code) | ((uint32_t) (count) << 8), (uint32_t) (arg),            \
                                (uint32_t) (w0), (uint32_t) (w1));                                          \
        }                                                                                                   \
    } while (0)

#define fprobe_object(obj, cls)          (((uint32_t) (obj) & ~3u) | (cls))
/* queue.c の中だけで使う（Queue_t の欄を読む）: ミューテックスは pcHead が NULL、セマフォは要素の大きさが 0 */
#define fprobe_queue_class(q)            ((q)->pcHead == NULL ? FPROBE_CLASS_MUTEX :                          \
                                          (q)->uxItemSize == 0u ? FPROBE_CLASS_SEMAPHORE : FPROBE_CLASS_QUEUE)
#define fprobe_queue(code, q)            fprobe_kernel(code, 1u, (q)->uxMessagesWaiting,                     \
                                                       fprobe_object(q, fprobe_queue_class(q)), 0u)
#define fprobe_wait(obj, cls, dir)       fprobe_kernel(FPROBE_EV_BLOCK, 1u, dir, fprobe_object(obj, cls), 0u)

/* --- タスク（ポート 31） --- */
#define traceTASK_SWITCHED_IN()          fprobe_trace_task(pxCurrentTCB, FPROBE_TRACE_IN)
#define traceTASK_SWITCHED_OUT()         fprobe_trace_task(pxCurrentTCB, FPROBE_TRACE_OUT)
#define traceTASK_CREATE(pxNewTCB)       fprobe_trace_task(pxNewTCB, FPROBE_TRACE_CREATE)
#define traceTASK_DELETE(pxTCB)          fprobe_trace_task(pxTCB, FPROBE_TRACE_DELETE)

/* --- タスクの状態と優先度（tasks.c） --- */
#define traceMOVED_TASK_TO_READY_STATE(pxTCB)       fprobe_kernel(FPROBE_EV_READY, 1u, 0u, pxTCB, 0u)
#define traceTASK_DELAY()                           fprobe_kernel(FPROBE_EV_DELAY, 1u, 0u, pxCurrentTCB, 0u)
#define traceTASK_DELAY_UNTIL(xTimeToWake)          fprobe_kernel(FPROBE_EV_DELAY, 1u, 0u, pxCurrentTCB, 0u)
#define traceTASK_SUSPEND(pxTCB)                    fprobe_kernel(FPROBE_EV_SUSPEND, 1u, 0u, pxTCB, 0u)
#define traceTASK_PRIORITY_INHERIT(pxTCB, uxPriority)    fprobe_kernel(FPROBE_EV_PRIO_INHERIT, 1u, uxPriority, pxTCB, 0u)
#define traceTASK_PRIORITY_DISINHERIT(pxTCB, uxPriority) fprobe_kernel(FPROBE_EV_PRIO_DISINHERIT, 1u, uxPriority, pxTCB, 0u)
#define traceTASK_PRIORITY_SET(pxTCB, uxPriority)   fprobe_kernel(FPROBE_EV_PRIO_SET, 1u, uxPriority, pxTCB, 0u)
#define traceTASK_NOTIFY_TAKE_BLOCK(uxIndex)        fprobe_wait(0u, FPROBE_CLASS_OTHER, FPROBE_WAIT_NOTIFY)
#define traceTASK_NOTIFY_WAIT_BLOCK(uxIndex)        fprobe_wait(0u, FPROBE_CLASS_OTHER, FPROBE_WAIT_NOTIFY)
#define traceTASK_NOTIFY(uxIndex)                   fprobe_kernel(FPROBE_EV_NOTIFY, 1u, uxIndex, xTaskToNotify, 0u)
#define traceTASK_NOTIFY_FROM_ISR(uxIndex)          fprobe_kernel(FPROBE_EV_NOTIFY, 1u, uxIndex, xTaskToNotify, 0u)
#define traceTASK_NOTIFY_GIVE_FROM_ISR(uxIndex)     fprobe_kernel(FPROBE_EV_NOTIFY, 1u, uxIndex, xTaskToNotify, 0u)

/* --- キュー・セマフォ・ミューテックス（queue.c。give は send、take は receive） --- */
#if (configUSE_TRACE_FACILITY == 1)
#define fprobe_queue_type(q)             ((q)->ucQueueType)      /* 0 = キュー、1 = ミューテックス、2 = 数える、3 = 2 値、4 = 再帰 */
#else
#define fprobe_queue_type(q)             0u
#endif
#define traceQUEUE_CREATE(pxNewQueue)                                                                       \
    fprobe_kernel(FPROBE_EV_CREATE, 2u, fprobe_queue_type(pxNewQueue),                                      \
                  fprobe_object(pxNewQueue, (pxNewQueue)->uxItemSize == 0u ? FPROBE_CLASS_SEMAPHORE : FPROBE_CLASS_QUEUE), \
                  (pxNewQueue)->uxLength)
#define traceQUEUE_SEND(pxQueue)                    fprobe_queue(FPROBE_EV_SEND, pxQueue)
#define traceQUEUE_SEND_FAILED(pxQueue)             fprobe_queue(FPROBE_EV_SEND_FAILED, pxQueue)
#define traceQUEUE_SEND_FROM_ISR(pxQueue)           fprobe_queue(FPROBE_EV_SEND, pxQueue)
#define traceQUEUE_SEND_FROM_ISR_FAILED(pxQueue)    fprobe_queue(FPROBE_EV_SEND_FAILED, pxQueue)
#define traceQUEUE_RECEIVE(pxQueue)                 fprobe_queue(FPROBE_EV_RECEIVE, pxQueue)
#define traceQUEUE_RECEIVE_FAILED(pxQueue)          fprobe_queue(FPROBE_EV_RECEIVE_FAILED, pxQueue)
#define traceQUEUE_RECEIVE_FROM_ISR(pxQueue)        fprobe_queue(FPROBE_EV_RECEIVE, pxQueue)
#define traceQUEUE_RECEIVE_FROM_ISR_FAILED(pxQueue) fprobe_queue(FPROBE_EV_RECEIVE_FAILED, pxQueue)
#define traceQUEUE_PEEK(pxQueue)                    fprobe_queue(FPROBE_EV_PEEK, pxQueue)
#define traceQUEUE_PEEK_FROM_ISR(pxQueue)           fprobe_queue(FPROBE_EV_PEEK, pxQueue)
#define traceBLOCKING_ON_QUEUE_RECEIVE(pxQueue)     fprobe_wait(pxQueue, fprobe_queue_class(pxQueue), FPROBE_WAIT_RECEIVE)
#define traceBLOCKING_ON_QUEUE_SEND(pxQueue)        fprobe_wait(pxQueue, fprobe_queue_class(pxQueue), FPROBE_WAIT_SEND)
#define traceBLOCKING_ON_QUEUE_PEEK(pxQueue)        fprobe_wait(pxQueue, fprobe_queue_class(pxQueue), FPROBE_WAIT_PEEK)

/* --- ストリーム / メッセージバッファ（stream_buffer.c）・イベントグループ（event_groups.c） --- */
#define traceBLOCKING_ON_STREAM_BUFFER_RECEIVE(xStreamBuffer)  fprobe_wait(xStreamBuffer, FPROBE_CLASS_OTHER, FPROBE_WAIT_RECEIVE)
#define traceBLOCKING_ON_STREAM_BUFFER_SEND(xStreamBuffer)     fprobe_wait(xStreamBuffer, FPROBE_CLASS_OTHER, FPROBE_WAIT_SEND)
#define traceSTREAM_BUFFER_SEND(xStreamBuffer, xBytes)                                                      \
    fprobe_kernel(FPROBE_EV_STREAM_SEND, 1u, xBytes, fprobe_object(xStreamBuffer, FPROBE_CLASS_OTHER), 0u)
#define traceSTREAM_BUFFER_SEND_FROM_ISR(xStreamBuffer, xBytes)       traceSTREAM_BUFFER_SEND(xStreamBuffer, xBytes)
#define traceSTREAM_BUFFER_RECEIVE(xStreamBuffer, xLength)                                                  \
    fprobe_kernel(FPROBE_EV_STREAM_RECEIVE, 1u, xLength, fprobe_object(xStreamBuffer, FPROBE_CLASS_OTHER), 0u)
#define traceSTREAM_BUFFER_RECEIVE_FROM_ISR(xStreamBuffer, xLength)   traceSTREAM_BUFFER_RECEIVE(xStreamBuffer, xLength)
#define traceEVENT_GROUP_WAIT_BITS_BLOCK(xEventGroup, uxBits)         fprobe_wait(xEventGroup, FPROBE_CLASS_OTHER, FPROBE_WAIT_RECEIVE)
#define traceEVENT_GROUP_SYNC_BLOCK(xEventGroup, uxSet, uxWait)       fprobe_wait(xEventGroup, FPROBE_CLASS_OTHER, FPROBE_WAIT_RECEIVE)
/* SetBitsFromISR はタイマのタスクに処理を回す（実際に立てるのはタイマのタスクの xEventGroupSetBits）ので記録しない */
#define traceEVENT_GROUP_SET_BITS(xEventGroup, uxBits)                                                      \
    fprobe_kernel(FPROBE_EV_EVENT_SET, 1u, (uxBits) & FPROBE_ARG_MAX, fprobe_object(xEventGroup, FPROBE_CLASS_OTHER), 0u)

/* --- 割り込みの出入り（port の SysTick・portEND_SWITCHING_ISR。アプリの ISR は FPROBE_ISR_ENTER / EXIT） --- */
#define traceISR_ENTER()                 fprobe_kernel(FPROBE_EV_ISR_ENTER, 0u, 0u, 0u, 0u)
#define traceISR_EXIT()                  fprobe_kernel(FPROBE_EV_ISR_EXIT, 0u, 0u, 0u, 0u)
#define traceISR_EXIT_TO_SCHEDULER()     fprobe_kernel(FPROBE_EV_ISR_EXIT, 0u, 0u, 0u, 0u)
#define FPROBE_ISR_ENTER()               traceISR_ENTER()
#define FPROBE_ISR_EXIT()                traceISR_EXIT()

#endif /* FPROBE_TRACE_H */
