// テストポイント方式（F7）の試験用ファームウェアの本体。build.py が挿入ツールで写し（build/tp/demo.c）を作り、
// 写しの方をビルドする（このファイルは変えない）。関数・分岐・ループ・早期 return・再帰・goto・fallthrough を含む。
// 仕様: docs/superpowers/specs/2026-10-17-f7-testpoint-design.md の 6 節。
#include <stdint.h>

void spin(uint32_t n);                  // startup.c（挿入しない）

volatile uint32_t state;                // 書き込みを記録する変数（--var state）
volatile uint32_t run_count;            // step の回数
volatile uint32_t inject_delay;         // デバッガで 1 にすると classify が 1 回だけ遅くなる（long_call の確かめ）
volatile uint32_t sink;                 // 結果の捨て場（最適化で消えない）

// 再帰
uint32_t fib(uint32_t n) {
    if (n < 2) return n;
    return fib(n - 1) + fib(n - 2);
}

// 早期 return
int validate(int v) {
    if (v < 0) return -1;
    if (v > 100) {
        return -2;
    }
    return 0;
}

// switch・fallthrough・default
int classify(int v) {
    if (inject_delay) {
        inject_delay = 0;
        spin(2000);
    }
    switch (v % 5) {
    case 0:
        return 0;
    case 1:
    case 2:
        state = 1;
        /* fallthrough */
    case 3:
        return 3;
    default:
        break;
    }
    return 4;
}

// ループ・continue・break
uint32_t sum_until(const uint8_t *p, uint32_t n, uint8_t stop) {
    uint32_t s = 0;
    for (uint32_t i = 0; i < n; i++) {
        if (p[i] == 0) continue;
        if (p[i] == stop) break;
        s += p[i];
    }
    return s;
}

// goto のエラー処理
int parse(const char *s) {
    int value = 0;
    if (s == 0) goto error;
    while (*s) {
        if (*s < '0' || *s > '9') goto error;
        value = value * 10 + (*s - '0');
        s++;
    }
    state = (uint32_t)value;
    return value;
error:
    state = 0xFFFFFFFFu;
    return -1;
}

// 1 回分の仕事（void 関数の末尾の出口）
void step(void) {
    static const uint8_t data[] = {3, 0, 5, 9, 7};
    run_count++;
    sink = fib(run_count % 8);
    sink += (uint32_t)validate((int)(run_count % 150) - 20);
    sink += (uint32_t)classify((int)run_count);
    sink += sum_until(data, sizeof data, (uint8_t)(run_count % 10));
    sink += (uint32_t)parse((run_count & 1) ? "1234" : "12x");
}
