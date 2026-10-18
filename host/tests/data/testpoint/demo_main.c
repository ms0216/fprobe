/* 試験の枠（挿入しない）: 試験用ファーム（host/firmware/testpoint_demo/src/demo.c）をホストで 20 回 step する */
#include <stdint.h>
#include <stdio.h>

void step(void);
extern volatile uint32_t inject_delay;

static uint32_t log_id[65536], log_value[65536], log_has[65536];
static unsigned count;

void spin(uint32_t n) {
    (void)n;
}

void fprobe_tp_user_emit(uint32_t id) {
    log_id[count] = id;
    log_has[count++] = 0;
}

void fprobe_tp_user_emit_value(uint32_t id, uint32_t value) {
    log_id[count] = id;
    log_value[count] = value;
    log_has[count++] = 1;
}

int main(void) {
    for (int i = 0; i < 20; i++) {
        step();
    }
    printf("result %u\n", count);
    for (unsigned i = 0; i < count; i++) {
        if (log_has[i]) {
            printf("%u=%u\n", (unsigned)log_id[i], (unsigned)log_value[i]);
        } else {
            printf("%u\n", (unsigned)log_id[i]);
        }
    }
    return 0;
}
