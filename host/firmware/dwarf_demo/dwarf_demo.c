// fprobe の DWARF（型・値の解読・ローカル変数と引数・式・CFI による巻き戻し）の試験用ファームウェア。
// Cortex-M4（Nucleo F401RE）の SRAM に読み込んで SRAM から実行できる（sram_demo と同じ配置）。
// 作り方: python host/firmware/dwarf_demo/build.py（zig cc。-O0 と -O1、DWARF 5 と 4 の ELF を作り、試験のためにリポジトリに置く）。
// 試験は ELF の初期値（.data）を偽のメモリとして読むので、大域変数の初期値は試験の期待値でもある。
#include <stdbool.h>
#include <stdint.h>

extern uint32_t __bss_start, __bss_end, __stack_top;

typedef enum Mode { MODE_IDLE = 0, MODE_RUN = 1, MODE_FAULT = -2 } Mode;

struct Point {
    int16_t x;
    int16_t y;
};

struct Flags {
    unsigned ready : 1;
    unsigned mode : 3;
    signed level : 4;                   // 符号付きのビットフィールド
    unsigned wide : 12;
    uint8_t raw;
};

union Word {
    uint32_t u32;
    uint8_t bytes[4];
    float f;
};

typedef struct Sensor {
    char name[8];
    struct Point pos;
    Mode mode;
    struct Flags flags;
    union Word word;
    float gain;
    double scale;
    int32_t samples[2][3];              // 多次元の配列
    struct Sensor *next;                // 自分自身の型を指す
    const volatile uint32_t *reg;       // 修飾子付きの型を指す
    union {                             // 無名の共用体（メンバーは外側の名前で引く）
        uint16_t raw16;
        int8_t pair[2];
    };
} Sensor;

typedef int (*Callback)(int);

volatile int sink;                      // 最適化で消えないための書き込み先

__attribute__((noinline)) int square(int v) {
    return v * v;
}

Sensor sensors[2] = {
    {"alpha", {10, -20}, MODE_RUN, {1, 5, -3, 0x123, 0xAB}, {.u32 = 0x3F800000}, 1.5f, -2.25,
     {{1, 2, 3}, {-4, -5, -6}}, &sensors[1], (const volatile uint32_t *)0xE000E010, {.raw16 = 0xBEEF}},
    {"beta", {-1, 2}, MODE_FAULT, {0, 7, 7, 0xFFF, 0x01}, {.bytes = {1, 2, 3, 4}}, 0.5f, 1e10,
     {{100, 200, 300}, {400, 500, 600}}, 0, 0, {.raw16 = 0x0102}},
};
Sensor *head = &sensors[0];
const char greeting[] = "hello, fprobe";
const char *message = greeting;
int8_t negative = -5;
uint64_t big = 0x123456789ABCDEF0ull;
bool enabled = true;
uint16_t matrix[3][4] = {{0, 1, 2, 3}, {10, 11, 12, 13}, {20, 21, 22, 23}};
uint8_t ramp[100] = {[0] = 1, [1] = 2, [99] = 99};   // 大きな配列（先頭だけを出す試験）
Callback callback = square;
static uint32_t s_ticks = 7;            // ファイルの中だけの静的変数

__attribute__((noinline)) void probe_point(int marker) {
    sink = marker;                      // ブレークポイントの的（葉の関数）
}

__attribute__((noinline)) int leaf_sum(const Sensor *s, int depth) {
    int total = 0;
    for (int i = 0; i < 3; i++) {
        total += s->samples[depth & 1][i];
    }
    struct Point corner = {(int16_t)depth, (int16_t)-depth};
    float ratio = s->gain * (float)depth;
    probe_point(total + corner.x);
    return total + (int)ratio + corner.y;
}

__attribute__((noinline)) int walk(const Sensor *s, int depth) {
    static int visits;                  // 関数の中の静的変数
    visits++;
    if (depth > 0) {
        int deeper = walk(s->next ? s->next : s, depth - 1);   // 深い呼び出し（再帰）
        return deeper + 1;
    }
    return leaf_sum(s, depth);
}

void SysTick_Handler(void) {
    s_ticks++;
    probe_point(-1);                    // 例外の中から呼ぶ（例外のフレームを越える巻き戻しの試験）
}

__attribute__((noreturn)) void reset_handler(void) {
    // FPU（CP10・CP11）を有効にする。eabihf でビルドしているので、無効のまま浮動小数点の命令を使うと UsageFault（NOCP）
    *(volatile uint32_t *)0xE000ED88 |= 0xFu << 20;
    __asm volatile("dsb\n\tisb" ::: "memory");
    for (uint32_t *p = &__bss_start; p < &__bss_end; p++) {
        *p = 0;
    }
    for (;;) {
        sink = walk(head, 3) + callback(sink);
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
    (void *)default_handler, (void *)default_handler, 0, (void *)default_handler, (void *)SysTick_Handler,
};
