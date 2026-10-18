// fprobe のモニタ方式（U37）の試験用ファームウェア。Cortex-M4F（Nucleo F401RE）の SRAM に読み込んで SRAM から実行する。
// 作り方: python host/firmware/monitor_demo/build.py（zig cc。生成した monitor_demo.elf は試験のためにリポジトリに置く）。
//
// 優先度（数が小さいほど高い）:
//   SysTick  0x00  DebugMon より高い: 止まっている間も fast_ticks を数え続ける（モーター制御の割り込みの見立て）
//   DebugMon 0x80  fprobe のモニタ
//   PendSV   0xF0  DebugMon より低い: 止まっている間は slow_ticks が増えない（SysTick が 8 回ごとに保留にする）
//   main（スレッド）: 止まっている間は loop_count が増えない
// work は浮動小数を使う（FP 付きの例外の枠の試験）。filter は呼び出し履歴の試験（reset_handler → work → filter）。
#include <stdint.h>

#include "fprobe_monitor.h"

#define SHPR3 (*(volatile uint32_t *)0xE000ED20u)
#define ICSR (*(volatile uint32_t *)0xE000ED04u)
#define ICSR_PENDSVSET (1u << 28)
#define CPACR (*(volatile uint32_t *)0xE000ED88u)
#define SYST_CSR (*(volatile uint32_t *)0xE000E010u)
#define SYST_RVR (*(volatile uint32_t *)0xE000E014u)
#define SYST_CVR (*(volatile uint32_t *)0xE000E018u)
#define SYSTICK_HZ 1000u
#define CORE_HZ 16000000u                       // リセット後の HSI

#define PRIO_SYSTICK 0x00u
#define PRIO_DEBUGMON 0x80u
#define PRIO_PENDSV 0xF0u

extern uint32_t __bss_start, __bss_end, __stack_top;

volatile uint32_t fast_ticks;                   // SysTick（DebugMon より高い）
volatile uint32_t slow_ticks;                   // PendSV（DebugMon より低い）
volatile uint32_t loop_count;                   // main の周回
volatile uint32_t poll_count;                   // モニタの待ちの輪（on_poll）
volatile uint32_t enter_count;                  // 止まった回数（on_enter）
volatile uint32_t exit_count;                   // 再開・1 命令の回数（on_exit）
volatile float gain = 1.5f;
volatile float level;

void fprobe_monitor_on_enter(void) {
    enter_count++;
}

void fprobe_monitor_on_exit(void) {
    exit_count++;
}

void fprobe_monitor_on_poll(void) {
    poll_count++;                               // 実際のファームではここでウォッチドッグを蹴る
}

void SysTick_Handler(void) {
    fast_ticks++;
    if ((fast_ticks & 7u) == 0u) {
        ICSR = ICSR_PENDSVSET;
    }
}

void PendSV_Handler(void) {
    slow_ticks++;
}

__attribute__((noinline)) float filter(uint32_t n) {
    return level * 0.5f + gain * (float)(n & 7u);   // ブレークポイントの的（呼び出し履歴の深い所）
}

__attribute__((noinline)) void work(uint32_t n) {
    level = filter(n);                          // ブレークポイントの的（関数名）
    loop_count = n;
}

__attribute__((noinline)) static void enable_fpu(void) {
    CPACR |= 0xFu << 20;                        // CP10・CP11 を全権で
    __asm volatile("dsb\n isb" ::: "memory");
}

__attribute__((noreturn)) void reset_handler(void) {
    for (uint32_t *p = &__bss_start; p < &__bss_end; p++) {
        *p = 0;
    }
    enable_fpu();
    SHPR3 = (SHPR3 & 0x0000FFFFu) | (PRIO_SYSTICK << 24) | (PRIO_PENDSV << 16);
    fprobe_monitor_init(PRIO_DEBUGMON);
    SYST_RVR = CORE_HZ / SYSTICK_HZ - 1u;
    SYST_CVR = 0u;
    SYST_CSR = 7u;                              // コアのクロック・割り込み・有効
    for (uint32_t n = 0;; n++) {
        work(n);
    }
}

__attribute__((noreturn)) void default_handler(void) {
    for (;;) {
    }
}

// ベクタ表（SRAM の先頭。デバッガが VTOR・SP・PC を設定して起動する）。12 番が DebugMon（fprobe のモニタ）。
__attribute__((section(".vectors"), used)) const void *const vectors[16] = {
    &__stack_top, (void *)reset_handler, (void *)default_handler, (void *)default_handler,
    (void *)default_handler, (void *)default_handler, (void *)default_handler, 0, 0, 0, 0,
    (void *)default_handler, (void *)DebugMon_Handler, 0, (void *)PendSV_Handler, (void *)SysTick_Handler,
};
