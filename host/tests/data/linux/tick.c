/* Linux のプローブの試験の題材: tick(i) を 10 ms ごとに n 回呼ぶ（tests/test_linux_*.py・HIL） */
#include <stdlib.h>
#include <time.h>
volatile int sink;
__attribute__((noinline)) int tick(int i) { sink += i; return i * 2; }
int main(int argc, char **argv) {
    int n = argc > 1 ? atoi(argv[1]) : 300;
    struct timespec d = {0, 10 * 1000 * 1000};
    for (int i = 0; i < n; i++) { tick(i); nanosleep(&d, 0); }
    return 0;
}
