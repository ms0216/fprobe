// fprobe のデバッグ機能（ブレークポイント・ウォッチポイント・シンボル・実行して待つ）の試験用ファームウェア。
// Cortex-M4（Nucleo F401RE）の SRAM に読み込んで SRAM から実行する（Flash には書かない）。
// 作り方: python host/firmware/sram_demo/build.py（zig cc。生成した sram_demo.elf は試験のためにリポジトリに置く）。
#include <stdint.h>

extern uint32_t __bss_start, __bss_end, __stack_top;

volatile uint32_t counter;              // tick のたびに増える
volatile uint32_t milestone;            // counter が 256 の倍数になるたびに書く（ウォッチポイントの的）
__attribute__((used)) uint32_t table[4] = {0x11111111, 0x22222222, 0x33333333, 0x44444444};   // 名前で読む試験の的

__attribute__((noinline)) void reached_milestone(uint32_t value) {
    milestone = value;                  // ブレークポイントの的（関数名）
}

__attribute__((noinline)) void tick(void) {
    counter++;
    if ((counter & 0xFF) == 0) {
        reached_milestone(counter);
    }
}

__attribute__((noreturn)) void reset_handler(void) {
    for (uint32_t *p = &__bss_start; p < &__bss_end; p++) {
        *p = 0;
    }
    for (;;) {
        tick();
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
