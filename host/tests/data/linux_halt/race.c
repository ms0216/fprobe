/* linux.halt.debug.* の試験の題材: 2 つのスレッドが同じ大域変数 counter を読み・足し・書く（データ競合）。
   模型 race_arm は実行しない（DWARF・行・番地だけ）。実機の版は -DRACE_PTHREAD（tests/linux_halt/hw/test_hw_debug.py）。
   作り方（再生成）: tests/data/linux_halt/build.sh */
volatile int counter;          /* 2 つのスレッドが足し損ねる大域変数 */
static int rounds = 1000;      /* 静的変数 */

__attribute__((noinline)) void add_one(int who)
{
    int before = counter;      /* 読む */
    int after = before + 1;    /* 足す */
    counter = after;           /* 書く（ここで他のスレッドの書き込みを消しうる） */
    (void)who;
}

__attribute__((noinline)) void *worker(void *arg)
{
    int who = (int)(long)arg;
    for (int i = 0; i < rounds; i++)
        add_one(who);
    return 0;
}

#ifdef RACE_PTHREAD
/* 実機の版（tests/linux_halt/hw/test_hw_debug.py）: worker を 2 つのスレッドで走らせ続ける */
#include <pthread.h>
int main(void)
{
    pthread_t t1, t2;
    rounds = 0x7fffffff;
    pthread_create(&t1, 0, worker, (void *)1);
    pthread_create(&t2, 0, worker, (void *)2);
    pthread_join(t1, 0);
    pthread_join(t2, 0);
    return counter;
}
#else
int main(void)
{
    worker((void *)1);         /* 試験の模型の版: 実行はしない（DWARF・行・番地だけを使う） */
    return counter;
}
#endif
