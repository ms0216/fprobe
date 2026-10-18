// fprobe のメモリ・スタックの使用量の解析（U26）の試験用ファームウェア。STM32 の Flash + SRAM の配置
// （Flash にコードとベクタ表、.data は Flash から SRAM へ写す、スタックは SRAM の末尾）。
// 呼び出しの深さ・再帰・関数ポインタ・可変長の確保（alloca）・末尾呼び出し・割り込みを含む。
// 作り方: python host/firmware/stack_demo/build.py（zig clang。-fstack-usage の .su も作る。試験は生成済みのものを使う）。
// zig clang には標準ヘッダの道が無いので型は自分で（Cortex-M: int は 32 ビット）
typedef unsigned int uint32_t;
typedef unsigned char uint8_t;

extern uint32_t _estack, _sidata, _sdata, _edata, _sbss, _ebss;

volatile uint32_t sink;
uint32_t data_table[16] = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16};   // .data（Flash に写しがある）
uint8_t big_buffer[1024];                                                         // .bss
const uint8_t lookup[64] = {3, 1, 4, 1, 5, 9, 2, 6};                              // .rodata

__attribute__((noinline)) uint32_t leaf(uint32_t x) {
    volatile uint32_t buf[8];
    buf[x & 7] = x;
    return buf[(x + 1) & 7];
}

__attribute__((noinline)) uint32_t middle(uint32_t x) {
    volatile uint32_t buf[16];
    buf[x & 15] = leaf(x);
    return buf[0] + leaf(x + 1);
}

// 再帰（呼び出しの後にも仕事があるので末尾の呼び出しにならない）
__attribute__((noinline)) uint32_t depth(uint32_t n) {
    volatile uint32_t pad[4];
    pad[n & 3] = n;
    if (n == 0) {
        return 0;
    }
    return depth(n - 1) + pad[0];
}

typedef uint32_t (*op_t)(uint32_t);

__attribute__((noinline)) uint32_t op_double(uint32_t x) { return 2 * x + lookup[x & 63]; }
__attribute__((noinline)) uint32_t op_inc(uint32_t x) { return x + 1; }

op_t const ops[2] = {op_double, op_inc};

// 関数ポインタの呼び出し（呼び先は静的には分からない）
__attribute__((noinline)) uint32_t apply(uint32_t i, uint32_t x) {
    return ops[i & 1](x) + 1;
}

// 可変長の確保（スタックの大きさは引数で決まる）
__attribute__((noinline)) uint32_t dynamic_buf(uint32_t n) {
    volatile uint8_t *p = __builtin_alloca(n + 1);
    p[0] = (uint8_t)n;
    return p[0];
}

// 末尾の呼び出し（b.w で middle へ。自分の枠は残らない）
__attribute__((noinline)) uint32_t tail(uint32_t x) {
    return middle(x ^ 3);
}

void SysTick_Handler(void) {
    sink += leaf(sink) + data_table[sink & 15];
}

__attribute__((noreturn)) void Default_Handler(void) {
    for (;;) {
    }
}

int main(void) {
    for (;;) {
        sink += middle(sink) + depth(sink & 7) + apply(sink, sink) + dynamic_buf(sink & 15) + tail(sink)
                + big_buffer[sink & 1023];
    }
}

__attribute__((noreturn)) void Reset_Handler(void) {
    uint32_t *src = &_sidata;
    for (uint32_t *p = &_sdata; p < &_edata; p++) {
        *p = *src++;
    }
    for (uint32_t *p = &_sbss; p < &_ebss; p++) {
        *p = 0;
    }
    main();
    for (;;) {
    }
}

__attribute__((section(".isr_vector"), used)) const void *const g_pfnVectors[16] = {
    &_estack, (void *)Reset_Handler, (void *)Default_Handler, (void *)Default_Handler,
    (void *)Default_Handler, (void *)Default_Handler, (void *)Default_Handler, 0, 0, 0, 0,
    (void *)Default_Handler, (void *)Default_Handler, 0, (void *)Default_Handler, (void *)SysTick_Handler,
};
