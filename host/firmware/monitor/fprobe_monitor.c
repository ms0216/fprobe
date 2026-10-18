// SPDX-License-Identifier: Apache-2.0
// fprobe のモニタ方式のターゲット側（DebugMon_Handler と箱）。説明は fprobe_monitor.h、仕様は
// docs/superpowers/specs/2026-10-18-u37-monitor-mode-design.md。
//
// 入口（アセンブラ）: 割り込まれた処理の r4〜r11（FP 付きの枠なら s16〜s31 も）・EXC_RETURN・MSP・PSP を箱に保存し、
//   モニタのスタックに切り替える。FP 付きの枠では、入口の FP 命令（vstmia）が遅延退避を起こし、s0〜s15・fpscr が枠に入る。
// 本体（C）: ハードが積んだ例外の枠を箱に写し、止まった理由（DFSR）を箱に写して消し、ホストの命令（GO / STEP）を待つ
//   （待つ間は on_poll）。命令が来たら、箱の SP の下に枠を積み直す（ホストが書いた値で）。
// 出口（アセンブラ）: 箱から r4〜r11（・s16〜s31）を書き戻し、MSP / PSP を積み直した枠にして例外から戻る。
//
// 割り込まれた処理のレジスタはすべて箱にあるので、ホストは箱を 1 回のブロック読みで読める。SP より下のメモリは
// モニタが使わない（モニタは自分のスタックで動く）ので、ホストは停止方式と同じくそこを使える（関数の呼び出しの引数）。
#include <stddef.h>
#include <stdint.h>

#include "fprobe_monitor.h"

#define XSTR(x) STR(x)
#define STR(x) #x

#define SHPR3_DEBUGMON (*(volatile uint8_t *)0xE000ED20u)   // SHPR3 の下位 8 ビット = 例外 12 の優先度
#define DFSR (*(volatile uint32_t *)0xE000ED30u)
#define DEMCR (*(volatile uint32_t *)0xE000EDFCu)
#define DEMCR_MON_PEND (1u << 17)
#define DEMCR_MON_STEP (1u << 18)
#define DFSR_ALL 0x1Fu
#define EXC_RETURN_PSP (1u << 2)
#define EXC_RETURN_BASIC_FRAME (1u << 4)
#define XPSR_ALIGNED (1u << 9)                  // 積んだ xpsr の bit9: 枠の整列のために 4 バイト詰めた
#define FRAME_WORDS_BASIC 8u
#define FRAME_WORDS_EXTENDED 26u
#define STACK_CANARY 0x4B54534Du                // "MSTK"（モニタのスタックの底）

_Static_assert(offsetof(struct fprobe_monitor, state) == 0x10, "box layout");
_Static_assert(offsetof(struct fprobe_monitor, seq_cmd) == 0x1C, "box layout");
_Static_assert(offsetof(struct fprobe_monitor, exc_return) == FPROBE_MONITOR_OFF_EXC_RETURN, "box layout");
_Static_assert(offsetof(struct fprobe_monitor, msp) == FPROBE_MONITOR_OFF_MSP, "box layout");
_Static_assert(offsetof(struct fprobe_monitor, psp) == FPROBE_MONITOR_OFF_PSP, "box layout");
_Static_assert(offsetof(struct fprobe_monitor, sp) == 0x4C, "box layout");
_Static_assert(offsetof(struct fprobe_monitor, frame) == 0x50, "box layout");
_Static_assert(offsetof(struct fprobe_monitor, r4_r11) == FPROBE_MONITOR_OFF_R4, "box layout");
_Static_assert(offsetof(struct fprobe_monitor, s16_s31) == FPROBE_MONITOR_OFF_S16, "box layout");
_Static_assert(offsetof(struct fprobe_monitor, exit_frame) == FPROBE_MONITOR_OFF_EXIT_FRAME, "box layout");
_Static_assert(sizeof(struct fprobe_monitor) == FPROBE_MONITOR_SIZE, "box layout");
_Static_assert(FPROBE_MONITOR_STACK_SIZE % 8 == 0 && FPROBE_MONITOR_STACK_SIZE >= 256, "monitor stack");

__attribute__((used)) struct fprobe_monitor fprobe_monitor;
__attribute__((used, aligned(8))) uint32_t fprobe_monitor_stack[FPROBE_MONITOR_STACK_SIZE / 4];

__attribute__((weak)) void fprobe_monitor_on_enter(void) {}
__attribute__((weak)) void fprobe_monitor_on_exit(void) {}
__attribute__((weak)) void fprobe_monitor_on_poll(void) {}

static inline void barrier(void) {
    __asm volatile("dsb" ::: "memory");
}

static inline uint32_t get_basepri(void) {
    uint32_t v;
    __asm volatile("mrs %0, basepri" : "=r"(v));
    return v;
}

static inline void set_basepri(uint32_t v) {
    __asm volatile("msr basepri, %0" ::"r"(v) : "memory");
}

static inline uint32_t get_primask(void) {
    uint32_t v;
    __asm volatile("mrs %0, primask" : "=r"(v));
    return v;
}

static inline void set_primask(uint32_t v) {
    __asm volatile("msr primask, %0" ::"r"(v) : "memory");
}

static inline uint32_t get_faultmask(void) {
    uint32_t v;
    __asm volatile("mrs %0, faultmask" : "=r"(v));
    return v;
}

static inline uint32_t get_control(void) {
    uint32_t v;
    __asm volatile("mrs %0, control" : "=r"(v));
    return v;
}

void fprobe_monitor_init(uint8_t priority) {
    struct fprobe_monitor *m = &fprobe_monitor;
    SHPR3_DEBUGMON = 0xFFu;                     // 実装された優先度のビットを測る（書けないビットは 0 で読める）
    m->prio_bits = SHPR3_DEBUGMON;
    SHPR3_DEBUGMON = priority;
    fprobe_monitor_stack[0] = STACK_CANARY;
    m->version = FPROBE_MONITOR_VERSION;
    m->size = sizeof *m;
    m->stack_size = FPROBE_MONITOR_STACK_SIZE;
#if defined(__ARM_FP)
    m->flags = FPROBE_MONITOR_FLAG_FPU;
#else
    m->flags = 0u;
#endif
    m->state = 0u;
    m->seq_ack = m->seq_cmd;
    m->step_forced = 0u;
    barrier();
    m->magic = FPROBE_MONITOR_MAGIC;            // 最後に書く: ホストはこれを見て使える箱と判断する
    barrier();
}

static uint32_t frame_words(const struct fprobe_monitor *m) {
    return (m->exc_return & EXC_RETURN_BASIC_FRAME) ? FRAME_WORDS_BASIC : FRAME_WORDS_EXTENDED;
}

static void check_stack(struct fprobe_monitor *m) {
    if (fprobe_monitor_stack[0] != STACK_CANARY) {
        m->flags |= FPROBE_MONITOR_FLAG_STACK_OVERFLOW;
    }
}

// ハードが積んだ枠を箱に写し、割り込まれた処理の SP を求める。
static void take_frame(struct fprobe_monitor *m) {
    const uint32_t *hw = (const uint32_t *)((m->exc_return & EXC_RETURN_PSP) ? m->psp : m->msp);
    uint32_t n = frame_words(m);
    for (uint32_t i = 0; i < n; i++) {
        m->frame[i] = hw[i];
    }
    m->sp = (uint32_t)hw + 4u * n + ((hw[7] & XPSR_ALIGNED) ? 4u : 0u);
    m->frame[7] &= ~XPSR_ALIGNED;
}

// 箱の SP の下に枠を積み直す（8 バイト境界。詰めたら xpsr の bit9）。出口のアセンブラが MSP / PSP をそこにする。
static void put_frame(struct fprobe_monitor *m) {
    uint32_t n = frame_words(m);
    uint32_t at = (m->sp & ~3u) - 4u * n;
    uint32_t xpsr = m->frame[7] & ~XPSR_ALIGNED;
    if (at & 4u) {
        at -= 4u;
        xpsr |= XPSR_ALIGNED;
    }
    uint32_t *out = (uint32_t *)at;
    for (uint32_t i = 0; i < n; i++) {
        out[i] = (i == 7u) ? xpsr : m->frame[i];
    }
    m->exit_frame = at;
}

// 割り込みを止める 1 命令: BASEPRI を DebugMon の 1 段下にし、DebugMon より低い割り込みに入らないようにする。
// DebugMon が最も低い段なら書かない（それより低い割り込みは無い）。すでにより強く止めていれば触らない。
static void mask_for_step(struct fprobe_monitor *m) {
    uint32_t bits = m->prio_bits;
    uint32_t level = (uint32_t)SHPR3_DEBUGMON + (bits & (0u - bits));
    if (bits == 0u || level > 0xFFu) {
        return;
    }
    uint32_t current = get_basepri();
    if (current != 0u && current <= level) {
        return;
    }
    m->step_basepri = current;
    m->step_forced = level;
    set_basepri(level);
}

// DebugMon_Handler の本体（入口のアセンブラから、モニタのスタックで呼ぶ）。
__attribute__((used)) void fprobe_monitor_service(void) {
    struct fprobe_monitor *m = &fprobe_monitor;
    uint32_t dfsr = DFSR & DFSR_ALL;
    DFSR = dfsr;                                // 1 を書いて消す（次に入ったときの理由のため）
    DEMCR &= ~(DEMCR_MON_STEP | DEMCR_MON_PEND); // ホストの停止の要求は、ほかの理由で入ったときもこれで済む
    if (m->step_forced != 0u) {                 // 割り込みを止める 1 命令の後: BASEPRI を戻す
        if (get_basepri() == m->step_forced) {  // 進めた命令が BASEPRI を書き替えたなら、その値を残す
            set_basepri(m->step_basepri);
        }
        m->step_forced = 0u;
    }
    take_frame(m);
    m->primask = get_primask();
    m->basepri = get_basepri();
    m->faultmask = get_faultmask();
    m->control = get_control();
    m->reason = dfsr;
    m->stops = m->stops + 1u;
    check_stack(m);
    barrier();
    m->state = 1u;
    barrier();
    fprobe_monitor_on_enter();

    uint32_t seq;
    uint32_t cmd;
    for (;;) {
        fprobe_monitor_on_poll();
        check_stack(m);
        seq = m->seq_cmd;
        if (seq == m->seq_ack) {
            continue;
        }
        barrier();
        cmd = m->cmd;
        if (cmd == FPROBE_MONITOR_CMD_GO || cmd == FPROBE_MONITOR_CMD_STEP) {
            break;
        }
        m->result = 1u;                         // 知らない命令: 断って待ち続ける
        barrier();
        m->seq_ack = seq;
    }

    uint32_t arg = m->arg;
    fprobe_monitor_on_exit();
    put_frame(m);
    set_basepri(m->basepri);                    // ホストが書いた値（DebugMon の中では効かない: 優先度は DebugMon のまま）
    if (cmd == FPROBE_MONITOR_CMD_STEP) {
        if (arg & FPROBE_MONITOR_STEP_MASK_INTERRUPTS) {
            mask_for_step(m);
        }
        DEMCR |= DEMCR_MON_STEP;                // 戻った先で 1 命令進めたら、また DebugMon に入る
    }
    m->result = 0u;
    m->state = 0u;
    barrier();
    m->seq_ack = seq;
    barrier();
    set_primask(m->primask & 1u);               // 最後に（PRIMASK = 1 でも例外からの戻りはできる）
}

// 入口と出口。頭の印（"FPMN" と箱の番地）はホストがベクタ表の DebugMon から箱を見つけるためのもの。
__attribute__((naked, used)) void FPROBE_MONITOR_HANDLER(void) {
    __asm volatile(
        "b.n    1f\n"
        ".p2align 2\n"
        ".word  " XSTR(FPROBE_MONITOR_MAGIC_WORD) "\n"
        ".word  fprobe_monitor\n"
        "1:\n"
        "movw   r0, #:lower16:fprobe_monitor\n"
        "movt   r0, #:upper16:fprobe_monitor\n"
        "str    lr, [r0, #" XSTR(FPROBE_MONITOR_OFF_EXC_RETURN) "]\n"
        "mrs    r1, msp\n"
        "str    r1, [r0, #" XSTR(FPROBE_MONITOR_OFF_MSP) "]\n"
        "mrs    r1, psp\n"
        "str    r1, [r0, #" XSTR(FPROBE_MONITOR_OFF_PSP) "]\n"
        "add    r1, r0, #" XSTR(FPROBE_MONITOR_OFF_R4) "\n"
        "stmia  r1, {r4-r11}\n"
#if defined(__ARM_FP)
        "tst    lr, #0x10\n"                    // bit4 = 0: FP 付きの枠
        "bne    2f\n"
        "add    r1, r0, #" XSTR(FPROBE_MONITOR_OFF_S16) "\n"
        "vstmia r1, {s16-s31}\n"                // FP 命令: 遅延退避で s0〜s15・fpscr が枠に入る
        "2:\n"
#endif
        "movw   r1, #:lower16:(fprobe_monitor_stack + " XSTR(FPROBE_MONITOR_STACK_SIZE) ")\n"
        "movt   r1, #:upper16:(fprobe_monitor_stack + " XSTR(FPROBE_MONITOR_STACK_SIZE) ")\n"
        "msr    msp, r1\n"                      // モニタのスタックへ（割り込まれた処理の SP の下を空ける）
        "push   {r0, lr}\n"
        "bl     fprobe_monitor_service\n"
        "pop    {r0, lr}\n"
        "add    r1, r0, #" XSTR(FPROBE_MONITOR_OFF_R4) "\n"
        "ldmia  r1, {r4-r11}\n"
#if defined(__ARM_FP)
        "tst    lr, #0x10\n"
        "bne    3f\n"
        "add    r1, r0, #" XSTR(FPROBE_MONITOR_OFF_S16) "\n"
        "vldmia r1, {s16-s31}\n"
        "3:\n"
#endif
        "ldr    r1, [r0, #" XSTR(FPROBE_MONITOR_OFF_EXIT_FRAME) "]\n"
        "tst    lr, #4\n"
        "bne    4f\n"
        "msr    msp, r1\n"                      // MSP の枠: MSP = 積み直した枠
        "bx     lr\n"
        "4:\n"
        "ldr    r2, [r0, #" XSTR(FPROBE_MONITOR_OFF_MSP) "]\n"
        "msr    msp, r2\n"                      // PSP の枠: MSP は入口の値に戻し、PSP = 積み直した枠
        "msr    psp, r1\n"
        "bx     lr\n");
}
