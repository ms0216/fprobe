// SPDX-License-Identifier: Apache-2.0
/* fprobe テストポイント: ターゲット側の出力（挿入ツールが写しの先頭で取り込む）。
 *
 *   FPROBE_TP(id)            ID だけ（関数の入口・出口・分岐）
 *   FPROBE_TP_VALUE(id, v)   ID と 32 ビットの値（変数の書き込み）
 *
 * 方式はビルドの -D で選ぶ（仕様: docs/superpowers/specs/2026-10-17-f7-testpoint-design.md の 3 節）:
 *   -DFPROBE_TP_BACKEND=FPROBE_TP_NONE           何も出さない（既定）
 *   -DFPROBE_TP_BACKEND=FPROBE_TP_GPIO_PARALLEL  W 本のデータ（最上位が先頭の印）+ ストローブ。BSRR に書く
 *       FPROBE_TP_GPIO_BSRR  BSRR のアドレス、FPROBE_TP_PAR_SHIFT  D0 のビット、FPROBE_TP_PAR_WIDTH  W（2〜16）、
 *       FPROBE_TP_PAR_STROBE ストローブのビット（同じポート）
 *   -DFPROBE_TP_BACKEND=FPROBE_TP_GPIO_SERIAL    2 線（CLK・DAT、アイドルは両方 1）
 *       FPROBE_TP_GPIO_BSRR、FPROBE_TP_SER_CLK・FPROBE_TP_SER_DAT（ビット）、FPROBE_TP_GPIO_GAP()（開始・終了の間）
 *   -DFPROBE_TP_BACKEND=FPROBE_TP_ITM            ITM のスティミュラス・ポート FPROBE_TP_ITM_PORT（既定 1）
 *   -DFPROBE_TP_BACKEND=FPROBE_TP_RTT            FPROBE_TP_RTT_WRITE(ptr, len) を利用者が定義する
 *   -DFPROBE_TP_BACKEND=FPROBE_TP_CUSTOM         利用者の fprobe_tp_user_emit(id) / fprobe_tp_user_emit_value(id, v)
 *
 * GPIO のピンの設定（出力・速度）とアイドルの値は利用者が行う（fprobe_tp_gpio_idle() がアイドルの値を書く）。
 * 複数の書き込みからなる記録は割り込みを止めて出す（FPROBE_TP_NO_IRQ_LOCK で止めない）。
 */
#ifndef FPROBE_TP_H
#define FPROBE_TP_H

#include <stdint.h>

#define FPROBE_TP_NONE          0
#define FPROBE_TP_GPIO_PARALLEL 1
#define FPROBE_TP_GPIO_SERIAL   2
#define FPROBE_TP_ITM           3
#define FPROBE_TP_RTT           4
#define FPROBE_TP_CUSTOM        5

#ifndef FPROBE_TP_BACKEND
#define FPROBE_TP_BACKEND FPROBE_TP_NONE
#endif

#if defined(__GNUC__) || defined(__clang__)
#define FPROBE_TP_FUNC static __attribute__((noinline, unused))
#else
#define FPROBE_TP_FUNC static
#endif

/* --- 割り込みの禁止（Cortex-M の PRIMASK） --- */
#if defined(FPROBE_TP_NO_IRQ_LOCK) || !defined(__ARM_ARCH)
static inline uint32_t fprobe_tp_lock(void) { return 0u; }
static inline void fprobe_tp_unlock(uint32_t saved) { (void)saved; }
#else
static inline uint32_t fprobe_tp_lock(void) {
    uint32_t saved;
    __asm volatile("mrs %0, primask\n\tcpsid i" : "=r"(saved) : : "memory");
    return saved;
}
static inline void fprobe_tp_unlock(uint32_t saved) {
    __asm volatile("msr primask, %0" : : "r"(saved) : "memory");
}
#endif

#ifndef FPROBE_TP_GPIO_GAP
#define FPROBE_TP_GPIO_GAP() __asm volatile("nop\n\tnop\n\tnop\n\tnop")
#endif
#ifndef FPROBE_TP_GPIO_DELAY
#define FPROBE_TP_GPIO_DELAY() ((void)0)
#endif

#define FPROBE_TP_REG32(addr) (*(volatile uint32_t *)(uintptr_t)(addr))

/* ===================================================================================== */
#if FPROBE_TP_BACKEND == FPROBE_TP_NONE

#define FPROBE_TP(id) ((void)0)
#define FPROBE_TP_VALUE(id, v) ((void)0)

/* ===================================================================================== */
#elif FPROBE_TP_BACKEND == FPROBE_TP_GPIO_PARALLEL

#if !defined(FPROBE_TP_GPIO_BSRR) || !defined(FPROBE_TP_PAR_SHIFT) || !defined(FPROBE_TP_PAR_WIDTH) || \
    !defined(FPROBE_TP_PAR_STROBE)
#error "FPROBE_TP_GPIO_PARALLEL needs FPROBE_TP_GPIO_BSRR, FPROBE_TP_PAR_SHIFT, FPROBE_TP_PAR_WIDTH, FPROBE_TP_PAR_STROBE"
#endif
#define FPROBE_TP_PAR_MASK ((1u << FPROBE_TP_PAR_WIDTH) - 1u)
#define FPROBE_TP_PAR_BITS (FPROBE_TP_PAR_WIDTH - 1)                 /* 1 記号の中身のビット数 */

static inline void fprobe_tp_gpio_idle(void) {
    FPROBE_TP_REG32(FPROBE_TP_GPIO_BSRR) = (FPROBE_TP_PAR_MASK << (FPROBE_TP_PAR_SHIFT + 16)) |
                                          (1u << FPROBE_TP_PAR_STROBE);
}

#define FPROBE_TP_PAR_SYMBOLS(nbits) (((nbits) + FPROBE_TP_PAR_BITS - 1) / FPROBE_TP_PAR_BITS)

/* 左詰めの bits を P ビットずつの記号で symbols 個出す（末尾は 0 で埋まる）。最初の記号に先頭の印。
 * シフトはすべて定数（64 ビットの可変シフトを避ける） */
FPROBE_TP_FUNC void fprobe_tp_par_send(uint64_t bits, uint32_t symbols) {
    uint32_t flag = 1u << FPROBE_TP_PAR_BITS;
    uint32_t saved = fprobe_tp_lock();
    do {
        uint32_t sym = (uint32_t)(bits >> (64 - FPROBE_TP_PAR_BITS)) | flag;
        bits <<= FPROBE_TP_PAR_BITS;
        flag = 0u;
        FPROBE_TP_REG32(FPROBE_TP_GPIO_BSRR) = (sym << FPROBE_TP_PAR_SHIFT) |
            (((~sym & FPROBE_TP_PAR_MASK) << FPROBE_TP_PAR_SHIFT) << 16) | (1u << (FPROBE_TP_PAR_STROBE + 16));
        FPROBE_TP_GPIO_DELAY();
        FPROBE_TP_REG32(FPROBE_TP_GPIO_BSRR) = 1u << FPROBE_TP_PAR_STROBE;      /* 立ち上がりでデータを取る */
        FPROBE_TP_GPIO_DELAY();
    } while (--symbols);
    fprobe_tp_unlock(saved);
}

#define FPROBE_TP(id) fprobe_tp_par_send((uint64_t)(uint16_t)(id) << 48, FPROBE_TP_PAR_SYMBOLS(16))
#define FPROBE_TP_VALUE(id, v)     fprobe_tp_par_send(((uint64_t)(uint16_t)(id) << 48) | ((uint64_t)(uint32_t)(v) << 16), FPROBE_TP_PAR_SYMBOLS(48))

/* ===================================================================================== */
#elif FPROBE_TP_BACKEND == FPROBE_TP_GPIO_SERIAL

#if !defined(FPROBE_TP_GPIO_BSRR) || !defined(FPROBE_TP_SER_CLK) || !defined(FPROBE_TP_SER_DAT)
#error "FPROBE_TP_GPIO_SERIAL needs FPROBE_TP_GPIO_BSRR, FPROBE_TP_SER_CLK, FPROBE_TP_SER_DAT"
#endif
#define FPROBE_TP_CLK (1u << FPROBE_TP_SER_CLK)
#define FPROBE_TP_DAT (1u << FPROBE_TP_SER_DAT)

static inline void fprobe_tp_gpio_idle(void) {
    FPROBE_TP_REG32(FPROBE_TP_GPIO_BSRR) = FPROBE_TP_CLK | FPROBE_TP_DAT;
}

/* 左詰めの w の上位 n ビット: (CLK = 0 と DAT を 1 回で) → (CLK = 1) */
static inline void fprobe_tp_ser_bits(uint32_t w, uint32_t n) {
    do {
        FPROBE_TP_REG32(FPROBE_TP_GPIO_BSRR) = (FPROBE_TP_CLK << 16) | ((int32_t)w < 0 ? FPROBE_TP_DAT : FPROBE_TP_DAT << 16);
        FPROBE_TP_GPIO_DELAY();
        FPROBE_TP_REG32(FPROBE_TP_GPIO_BSRR) = FPROBE_TP_CLK;                    /* 立ち上がりで DAT を取る */
        FPROBE_TP_GPIO_DELAY();
        w <<= 1;
    } while (--n);
}

/* 開始 → ID（16 ビット）[→ 値（32 ビット）] → 終わりの 0 → 終了 */
FPROBE_TP_FUNC void fprobe_tp_ser_send(uint32_t id, uint32_t value, int has_value) {
    uint32_t saved = fprobe_tp_lock();
    FPROBE_TP_REG32(FPROBE_TP_GPIO_BSRR) = FPROBE_TP_DAT << 16;                 /* 開始: CLK = 1 で DAT を 0 に */
    FPROBE_TP_GPIO_GAP();
    fprobe_tp_ser_bits(id << 16, 16u);
    if (has_value) {
        fprobe_tp_ser_bits(value, 32u);
    }
    fprobe_tp_ser_bits(0u, 1u);
    FPROBE_TP_GPIO_GAP();
    FPROBE_TP_REG32(FPROBE_TP_GPIO_BSRR) = FPROBE_TP_DAT;                        /* 終了: CLK = 1 で DAT を 1 に */
    fprobe_tp_unlock(saved);
}

#define FPROBE_TP(id) fprobe_tp_ser_send((uint32_t)(uint16_t)(id), 0u, 0)
#define FPROBE_TP_VALUE(id, v) fprobe_tp_ser_send((uint32_t)(uint16_t)(id), (uint32_t)(v), 1)

/* ===================================================================================== */
#elif FPROBE_TP_BACKEND == FPROBE_TP_ITM

#ifndef FPROBE_TP_ITM_PORT
#define FPROBE_TP_ITM_PORT 1
#endif
#define FPROBE_TP_ITM_STIM (0xE0000000u + 4u * FPROBE_TP_ITM_PORT)

static inline int fprobe_tp_itm_ready(void) {
    return (FPROBE_TP_REG32(0xE0000E80u) & 1u) && (FPROBE_TP_REG32(0xE0000E00u) & (1u << FPROBE_TP_ITM_PORT));
}

/* 値付きなら値（4 バイト）→ ID（2 バイト）。大きさで区別する。ITM・ポートが無効なら捨てる */
FPROBE_TP_FUNC void fprobe_tp_itm_send(uint32_t id, uint32_t value, int has_value) {
    uint32_t saved = fprobe_tp_lock();
    if (fprobe_tp_itm_ready()) {
        if (has_value) {
            while ((FPROBE_TP_REG32(FPROBE_TP_ITM_STIM) & 1u) == 0u) {
            }
            FPROBE_TP_REG32(FPROBE_TP_ITM_STIM) = value;
        }
        while ((FPROBE_TP_REG32(FPROBE_TP_ITM_STIM) & 1u) == 0u) {
        }
        *(volatile uint16_t *)(uintptr_t)FPROBE_TP_ITM_STIM = (uint16_t)id;
    }
    fprobe_tp_unlock(saved);
}

#define FPROBE_TP(id) fprobe_tp_itm_send((uint32_t)(id), 0u, 0)
#define FPROBE_TP_VALUE(id, v) fprobe_tp_itm_send((uint32_t)(id), (uint32_t)(v), 1)

/* ===================================================================================== */
#elif FPROBE_TP_BACKEND == FPROBE_TP_RTT

#ifndef FPROBE_TP_RTT_WRITE
#error "FPROBE_TP_RTT needs FPROBE_TP_RTT_WRITE(ptr, len), e.g. SEGGER_RTT_WriteSkipNoLock(1, ptr, len)"
#endif
#define FPROBE_TP_CYCCNT 0xE0001004u

/* DWT の CYCCNT を動かす（時刻の元）。起動時に 1 回呼ぶ */
static inline void fprobe_tp_rtt_init(void) {
    FPROBE_TP_REG32(0xE000EDFCu) |= 1u << 24;          /* DEMCR.TRCENA */
    FPROBE_TP_REG32(0xE0001000u) |= 1u;                /* DWT_CTRL.CYCCNTENA */
}

/* u32 CYCCNT, u16 ID, u16 flags（上位 8 ビット = 0xA5 の印、bit0 = 値付き）[, u32 値]（リトル・エンディアン） */
FPROBE_TP_FUNC void fprobe_tp_rtt_send(uint32_t id, uint32_t value, int has_value) {
    uint32_t rec[3];
    uint32_t saved = fprobe_tp_lock();
    rec[0] = FPROBE_TP_REG32(FPROBE_TP_CYCCNT);
    rec[1] = (id & 0xFFFFu) | ((0xA500u | (has_value ? 1u : 0u)) << 16);
    rec[2] = value;
    FPROBE_TP_RTT_WRITE(rec, has_value ? 12u : 8u);
    fprobe_tp_unlock(saved);
}

#define FPROBE_TP(id) fprobe_tp_rtt_send((uint32_t)(id), 0u, 0)
#define FPROBE_TP_VALUE(id, v) fprobe_tp_rtt_send((uint32_t)(id), (uint32_t)(v), 1)

/* ===================================================================================== */
#elif FPROBE_TP_BACKEND == FPROBE_TP_CUSTOM

void fprobe_tp_user_emit(uint32_t id);
void fprobe_tp_user_emit_value(uint32_t id, uint32_t value);
#define FPROBE_TP(id) fprobe_tp_user_emit((uint32_t)(id))
#define FPROBE_TP_VALUE(id, v) fprobe_tp_user_emit_value((uint32_t)(id), (uint32_t)(v))

#else
#error "unknown FPROBE_TP_BACKEND"
#endif

#endif /* FPROBE_TP_H */
