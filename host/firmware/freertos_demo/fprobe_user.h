// SPDX-License-Identifier: Apache-2.0
/* fprobe: ターゲットが出す値とイベント（U16）。ヘッダだけ・RTOS に依らない・Cortex-M3/M4/M7/M33（ITM）。
 * 仕様: docs/superpowers/specs/2026-10-18-u10-u16-trace-hooks-user-events-design.md の 3 節。
 * ホストの解読: host/fprobe/usertrace.py（user.trace.start / get / stats、記録の stream "user"）。
 *
 * 1) 値（J-Scope・DataPlot と同じ: ポートごとに型をホストで決める）
 *      FPROBE_PLOT_F32(1, temperature);      // ホスト: {"source": "itm", "index": 1, "type": "f32", "name": "temp"}
 *    ポートは 1〜23（0 は printf の慣例、24・25 は名前付きイベント、29〜31 は fprobe_trace.h）。
 * 2) 名前付きイベント（自己記述: 名前は ELF の文字列。ホストの設定は要らない）
 *      FPROBE_EVENT_START("adc");  ...  FPROBE_EVENT_STOP("adc");     // 区間（入れ子・割り込みの中も可）
 *      FPROBE_EVENT_MARK("button");                                    // 印
 *      FPROBE_VALUE_I("queue_len", n);  FPROBE_VALUE_F("volt", v);  FPROBE_VALUE_Q("gain", q16_16);
 *    ポート 24 の語 = 名前の番地 | 種類（0 = mark、1 = start、2 = stop、3 = value）。value はポート 25 に値が続く。
 *    名前は文字列リテラルだけ（マクロが 4 バイトに揃えた static const にする）。
 * 3) RTT（ITM の無い Cortex-M0+ 等）: FPROBE_USER_RTT_WRITE(data, length) を定義してから読み込むと、名前付きイベントを
 *    同じ語の形で RTT に書く（1 回の書き込み = 1 記録）。例:
 *      #define FPROBE_USER_RTT_WRITE(p, n)  SEGGER_RTT_Write(2, (p), (n))     // ホスト: event_rtt_channel=2
 *
 * どれも ITM が無効（TCR.ITMENA か TER のそのポートが 0）なら何もしない（FIFO を待たない）。
 */
#ifndef FPROBE_USER_H
#define FPROBE_USER_H

#include <stdint.h>

#define FPROBE_USER_EVENT_PORT   24u
#define FPROBE_USER_VALUE_PORT   25u
#define FPROBE_USER_MARK         0u
#define FPROBE_USER_START        1u
#define FPROBE_USER_STOP         2u
#define FPROBE_USER_VALUE        3u

#define FPROBE_USER_ITM_U32(n)   (*(volatile uint32_t *) (0xE0000000u + 4u * (n)))
#define FPROBE_USER_ITM_U16(n)   (*(volatile uint16_t *) (0xE0000000u + 4u * (n)))
#define FPROBE_USER_ITM_U8(n)    (*(volatile uint8_t *) (0xE0000000u + 4u * (n)))
#define FPROBE_USER_ITM_TER      (*(volatile uint32_t *) 0xE0000E00u)
#define FPROBE_USER_ITM_TCR      (*(volatile uint32_t *) 0xE0000E80u)

static inline int fprobe_user_on(uint32_t port) {
    return (FPROBE_USER_ITM_TCR & 1u) && (FPROBE_USER_ITM_TER & (1u << port));
}

static inline void fprobe_user_wait(uint32_t port) {
    while (FPROBE_USER_ITM_U32(port) == 0u) {
        /* FIFO が空くのを待つ */
    }
}

static inline void fprobe_user_put32(uint32_t port, uint32_t word) {
    if (fprobe_user_on(port)) {
        fprobe_user_wait(port);
        FPROBE_USER_ITM_U32(port) = word;
    }
}

static inline void fprobe_user_put16(uint32_t port, uint16_t half) {
    if (fprobe_user_on(port)) {
        fprobe_user_wait(port);
        FPROBE_USER_ITM_U16(port) = half;
    }
}

static inline void fprobe_user_put8(uint32_t port, uint8_t byte) {
    if (fprobe_user_on(port)) {
        fprobe_user_wait(port);
        FPROBE_USER_ITM_U8(port) = byte;
    }
}

static inline uint32_t fprobe_user_f32_bits(float f) {
    union { float f; uint32_t u; } v;
    v.f = f;
    return v.u;
}

/* 名前付きイベント: 見出し（と値）を割り込みを禁止して書く（ISR のイベントが 2 語の間に入らない） */
static inline void fprobe_user_event(const char *name, uint32_t kind, uint32_t value) {
    uint32_t head = ((uint32_t) name & ~3u) | kind;
#ifdef FPROBE_USER_RTT_WRITE
    uint32_t record[2] = {head, value};
    FPROBE_USER_RTT_WRITE(record, kind == FPROBE_USER_VALUE ? 8u : 4u);
#endif
    if (!fprobe_user_on(FPROBE_USER_EVENT_PORT)) {
        return;
    }
    uint32_t primask;
    __asm volatile("mrs %0, primask\n cpsid i" : "=r"(primask) : : "memory");
    fprobe_user_wait(FPROBE_USER_EVENT_PORT);
    FPROBE_USER_ITM_U32(FPROBE_USER_EVENT_PORT) = head;
    if (kind == FPROBE_USER_VALUE && fprobe_user_on(FPROBE_USER_VALUE_PORT)) {
        fprobe_user_wait(FPROBE_USER_VALUE_PORT);
        FPROBE_USER_ITM_U32(FPROBE_USER_VALUE_PORT) = value;
    }
    __asm volatile("msr primask, %0" : : "r"(primask) : "memory");
}

/* 名前の文字列（4 バイトに揃える: 語の下 2 ビットに種類を入れる）。used: 最適化で消さない */
#define FPROBE_USER_NAME(literal)                                                                           \
    ({ static const char fprobe_name_[] __attribute__((aligned(4), used)) = literal; fprobe_name_; })

#define FPROBE_PLOT_I32(port, v)     fprobe_user_put32(port, (uint32_t) (int32_t) (v))
#define FPROBE_PLOT_U32(port, v)     fprobe_user_put32(port, (uint32_t) (v))
#define FPROBE_PLOT_F32(port, v)     fprobe_user_put32(port, fprobe_user_f32_bits(v))
#define FPROBE_PLOT_I16(port, v)     fprobe_user_put16(port, (uint16_t) (int16_t) (v))
#define FPROBE_PLOT_I8(port, v)      fprobe_user_put8(port, (uint8_t) (int8_t) (v))

#define FPROBE_EVENT_MARK(name)      fprobe_user_event(FPROBE_USER_NAME(name), FPROBE_USER_MARK, 0u)
#define FPROBE_EVENT_START(name)     fprobe_user_event(FPROBE_USER_NAME(name), FPROBE_USER_START, 0u)
#define FPROBE_EVENT_STOP(name)      fprobe_user_event(FPROBE_USER_NAME(name), FPROBE_USER_STOP, 0u)
/* 値の型は名前の先頭（ホストが読む）: i: = int32、u: = uint32、f: = float、q: = Q16.16 */
#define FPROBE_VALUE_I(name, v)      fprobe_user_event(FPROBE_USER_NAME("i:" name), FPROBE_USER_VALUE, (uint32_t) (int32_t) (v))
#define FPROBE_VALUE_U(name, v)      fprobe_user_event(FPROBE_USER_NAME("u:" name), FPROBE_USER_VALUE, (uint32_t) (v))
#define FPROBE_VALUE_F(name, v)      fprobe_user_event(FPROBE_USER_NAME("f:" name), FPROBE_USER_VALUE, fprobe_user_f32_bits(v))
#define FPROBE_VALUE_Q(name, v)      fprobe_user_event(FPROBE_USER_NAME("q:" name), FPROBE_USER_VALUE, (uint32_t) (int32_t) (v))

#endif /* FPROBE_USER_H */
