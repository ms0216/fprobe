// テストポイント方式（F7）の試験用ファームウェアの起動と出力の設定（挿入しない）。
// Cortex-M4（Nucleo F401RE）の SRAM に読み込んで SRAM から実行する（Flash には書かない）。HSI 16 MHz のまま。
// 出力の方式はビルドの -D で選ぶ（build.py が 3 種の ELF を作る）:
//   GPIO 2 線: CLK = PA5（CN10-11、ロジアナ ch4）、DAT = PA9（CN10-21、ch5）— 今の配線のまま
//   GPIO 並列: D0..D2 = PC0..PC2（D2 が先頭の印）、STB = PC3 — J12 の ch1/ch5/ch6/ch4 をつなぎ替える
//   ITM: スティミュラス・ポート 1、SWO = PB3（ch6）、NRZ 2 Mbaud、ローカルのタイムスタンプ付き
#include <stdint.h>

#include "fprobe_tp.h"

extern uint32_t __bss_start, __bss_end, __stack_top;
void step(void);

#define REG32(addr) (*(volatile uint32_t *)(addr))
#define RCC_CR       0x40023800u
#define RCC_CFGR     0x40023808u
#define RCC_AHB1ENR  0x40023830u
#define GPIOA_BASE   0x40020000u
#define GPIOB_BASE   0x40020400u
#define GPIOC_BASE   0x40020800u
#define MODER        0x00u
#define OSPEEDR      0x08u
#define AFRL         0x20u

volatile uint32_t pause_cycles = 20000;   // step の間の休み（spin の回数）。デバッガで変えてよい

void spin(uint32_t n) {
    for (volatile uint32_t i = 0; i < n; i++) {
    }
}

static void gpio_output(uint32_t base, uint32_t pin) {
    REG32(base + MODER) = (REG32(base + MODER) & ~(3u << (2 * pin))) | (1u << (2 * pin));
    REG32(base + OSPEEDR) |= 3u << (2 * pin);
}

static void hsi(void) {
    REG32(RCC_CR) |= 1u;
    while ((REG32(RCC_CR) & 2u) == 0) {
    }
    REG32(RCC_CFGR) &= ~3u;
    while ((REG32(RCC_CFGR) & 0xCu) != 0) {
    }
}

#if FPROBE_TP_BACKEND == FPROBE_TP_ITM
#define CORE_HZ  16000000u
#define SWO_BAUD 2000000u

static void itm_setup(void) {
    REG32(RCC_AHB1ENR) |= 1u << 1;                                              // GPIOB
    REG32(GPIOB_BASE + MODER) = (REG32(GPIOB_BASE + MODER) & ~(3u << 6)) | (2u << 6);   // PB3 = 代替機能
    REG32(GPIOB_BASE + OSPEEDR) |= 3u << 6;
    REG32(GPIOB_BASE + AFRL) &= ~(0xFu << 12);                                  // AF0 = TRACESWO
    REG32(0xE0042004u) = (REG32(0xE0042004u) & ~(3u << 6)) | (1u << 5);         // DBGMCU: TRACE_IOEN、非同期
    REG32(0xE000EDFCu) |= 1u << 24;                                             // DEMCR.TRCENA
    REG32(0xE00400F0u) = 2u;                                                    // TPIU_SPPR = NRZ
    REG32(0xE0040010u) = CORE_HZ / SWO_BAUD - 1u;                               // TPIU_ACPR
    REG32(0xE0040304u) = 0x100u;                                                // TPIU_FFCR: フォーマッタ無効
    REG32(0xE0000FB0u) = 0xC5ACCE55u;                                           // ITM_LAR
    REG32(0xE0000E80u) = 0;
    REG32(0xE0001000u) = 1u | (1u << 10);                                       // DWT: CYCCNTENA、SYNCTAP
    REG32(0xE0000E00u) = 1u << FPROBE_TP_ITM_PORT;                              // ITM_TER
    REG32(0xE0000E80u) = (1u << 16) | (1u << 2) | (1u << 1) | 1u;               // TraceBusID 1、SYNC、TS、ITMENA
}
#endif

static void output_setup(void) {
#if FPROBE_TP_BACKEND == FPROBE_TP_GPIO_SERIAL
    REG32(RCC_AHB1ENR) |= 1u << 0;                                              // GPIOA
    fprobe_tp_gpio_idle();
    gpio_output(GPIOA_BASE, FPROBE_TP_SER_CLK);
    gpio_output(GPIOA_BASE, FPROBE_TP_SER_DAT);
#elif FPROBE_TP_BACKEND == FPROBE_TP_GPIO_PARALLEL
    REG32(RCC_AHB1ENR) |= 1u << 2;                                              // GPIOC
    fprobe_tp_gpio_idle();
    for (uint32_t pin = 0; pin < FPROBE_TP_PAR_WIDTH; pin++) {
        gpio_output(GPIOC_BASE, FPROBE_TP_PAR_SHIFT + pin);
    }
    gpio_output(GPIOC_BASE, FPROBE_TP_PAR_STROBE);
#elif FPROBE_TP_BACKEND == FPROBE_TP_ITM
    itm_setup();
#endif
}

__attribute__((noreturn)) void reset_handler(void) {
    for (uint32_t *p = &__bss_start; p < &__bss_end; p++) {
        *p = 0;
    }
    hsi();
    output_setup();
    for (;;) {
        step();
        spin(pause_cycles);
    }
}

__attribute__((noreturn)) void default_handler(void) {
    for (;;) {
    }
}

// ベクタ表（SRAM の先頭。デバッガが VTOR・SP・PC を設定して起動する）
__attribute__((section(".vectors"), used)) const void *const vectors[16] = {
    &__stack_top, (void *)reset_handler, (void *)default_handler, (void *)default_handler,
    (void *)default_handler, (void *)default_handler, (void *)default_handler, 0, 0, 0, 0,
    (void *)default_handler, (void *)default_handler, 0, (void *)default_handler, (void *)default_handler,
};
