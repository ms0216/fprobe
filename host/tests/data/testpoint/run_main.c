/* 試験の枠（挿入しない）: FPROBE_TP_CUSTOM の出口で ID（と値）を配列に記録し、最後に 1 行ずつ出す */
#include <stdint.h>
#include <stdio.h>

int fact(int n);
int classify(int v);
int find(const int *a, int n, int key);
int cleanup(int x);
void touch(int v);

static uint32_t log_id[4096], log_value[4096], log_has[4096];
static unsigned count;

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
    int a[3] = {5, 7, 9};
    int r = fact(3);
    r += classify(0);
    r += classify(1);
    r += classify(5);
    r += find(a, 3, 7);
    r += find(a, 3, 4);
    r += cleanup(4);
    r += cleanup(-1);
    touch(2);
    printf("result %d\n", r);
    for (unsigned i = 0; i < count; i++) {
        if (log_has[i]) {
            printf("%u=%u\n", (unsigned)log_id[i], (unsigned)log_value[i]);
        } else {
            printf("%u\n", (unsigned)log_id[i]);
        }
    }
    return 0;
}
