// fprobe の逆アセンブル（fprobe.disasm）の試験用ファームウェア。実行はしない（試験は ELF を読むだけ）。
// Cortex-M0+（Thumb-1 中心。movw/movt が無いので 32 ビットの定数はリテラルプール = .text の中のデータになる）。
// 作り方: python host/firmware/disasm_demo/build.py（zig cc。生成した disasm_demo.elf は試験のためにリポジトリに置く）。
#include <stdint.h>

extern uint32_t __stack_top;

volatile uint32_t sink;

__attribute__((noinline)) uint32_t mix(uint32_t x) {
    sink = x ^ 0xDEADBEEF;                      // 定数はリテラルプールから読む
    return x * 0x12345679u + 0x0BADF00D;
}

__attribute__((noinline)) void store_peripheral(void) {
    *(volatile uint32_t *)0x40020000 = 0xA8000000;   // GPIOA の MODER（番地も値もリテラルプール）
}

__attribute__((noreturn)) void reset_handler(void) {
    uint32_t v = 1;
    for (;;) {
        v = mix(v);
        store_peripheral();
    }
}

__attribute__((section(".vectors"), used)) const void *const vectors[2] = {&__stack_top, (void *)reset_handler};
