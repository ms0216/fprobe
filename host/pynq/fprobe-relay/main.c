/* fprobe-relay: PYNQ-Z2 の PS の Linux で、UDP 63920 と PL の zynq_frame_link（0x40000000、4 KB）を受け渡す。
 *
 * 起動の順:
 *   1. fprobe の回路が載っている印 /run/fprobe/pl-design が無ければ終わる（PL を読まない）。PL の番地に応答する回路が
 *      無いのに読むと ARM が固まる。印は書き込みの道具（fprobe_gw の PynqLoader）が /usr/local/bin/fprobe-pl で置く。
 *   2. PS と PL の間のレベルシフタを有効にする（JTAG で書いた後は無効のままで、PL の信号が PS に届かない）。
 *   3. ID と VERSION を確かめる（relay_check_design）。合わなければ終わる。
 * 動いている間は 100 ms ごとに ID を確かめ、読めなくなったら終わる（systemd が起動し直す。印が無ければそこで止まる）。
 *
 * 深いバッファ（P3）: /etc/fprobe/mem に「物理番地 大きさ」（例: 0x1E000000 0x2000000）があれば、その領域が Linux の
 * メモリ（/proc/iomem の System RAM）と重ならないことを確かめて PL の MEM_BASE に書く（重なれば書かない = PL は使わない）。
 *
 * ゼロコピーの送信（TCP の PS 直読み）: /dev/fprobe_ring（カーネルモジュール host/pynq/fprobe-ringmap。起動時の読み込みは
 * fprobe_gw pynq ringmap が置く。中継のサービスは systemd-modules-load の後に起動する）があり、その領域が深いバッファと
 * 同じなら、TCP のホストがつないだとき、リングのページを MSG_ZEROCOPY でそのまま送る（relay.h の relay_zc。主のスレッドが受け持つ）。無ければ今の複写の道（読み取りスレッドが
 * DDR → tcp_ring に写し、主が送る）。どちらの道かは、起動とつながりの記録（stderr = journal）と、PC の状態の問い合わせ
 * （RELAY_CH_STATUS）の返事に出す。完了通知が copied なら、記録してゼロコピーをやめ、複写の道に戻す（起動し直すまで使わない）。
 *
 * fprobe-relay --enable: レベルシフタを有効にするだけ（--isolate の対）。
 * fprobe-relay --isolate: レベルシフタを無効にするだけ（PL を書き直す前に fprobe-pl before が呼ぶ）。シフタが有効なまま
 * PL を書き直す・PS7 を持たない回路でシフタを有効にすると、PL から PS への信号が定まらず Linux が固まった（2026-10-20）。
 * SPDX-License-Identifier: AGPL-3.0-only
 */
#define _GNU_SOURCE                                         /* sched_setaffinity */
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/errqueue.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/uio.h>
#include <time.h>
#include <unistd.h>

#include "relay.h"

#ifndef SO_ZEROCOPY
#define SO_ZEROCOPY 60
#endif
#ifndef MSG_ZEROCOPY
#define MSG_ZEROCOPY 0x4000000
#endif

#define PL_BASE 0x40000000u
#define SLCR_BASE 0xF8000000u
#define SLCR_LVL_SHFTR_EN 0x900u
#define MARKER "/run/fprobe/pl-design"
#define MEM_CONF "/etc/fprobe/mem"
#define UDP_PORT 63920
#define TCP_PORT 63921                 /* PS 直読みのストリームを TCP で受ける口（ホストがつないだときだけ。つながなければ UDP） */
#define SPIN_NS 2000000               /* 動きがあった後は 2 ms 窓を見続ける（DAP の往復を速くする） */
#define DRAIN_MAX 8                     /* 1 周に PC へ送るデータグラムの上限（リングから取り出す数） */

/* PL → PC は 2 つのスレッドで進める: 読み取りスレッドが PL の窓を読んでリングに積み（1 フレーム 約 34 µs）、
 * 主のスレッドがリングから UDP で送る（約 27 µs）。別のコアで同時に進むので、読みと送信の長い方が律速になる。
 * PC → PL（DAP・CONTROL の要求）と ID の確認は主のスレッド。 */
static struct relay_ring ring;
static struct relay_ring tcp_ring;                         /* TCP のバイト列用（読み取りスレッドが積み、主が send する） */
/* TCP の状態（読み取りスレッドと主が共有）: 0 = つながっていない、1 = つながっていて、読み取りスレッドが tcp_ring に積む、
 * 2 = 主が閉じる（主 → 読み取りスレッドへの依頼）、3 = 読み取りスレッドは積むのをやめた（主が tcp_ring を空にしてよい）、
 * 5 = 主がゼロコピーの道にする（主 → 読み取りスレッドへの依頼）、4 = ゼロコピーの道（読み取りスレッドは g_ps・g_rd・tcp_ring に
 * 触らない。主が持つ）。4 から 1 へは主が移す（copied の後、複写の道に戻す） */
static volatile int g_tcp;
#define TCP_ZC 4
#define TCP_ZC_REQUEST 5
/* TCP の時間の内訳（TCP が閉じたとき stderr に出す）。書くのはそれぞれ 1 つのスレッドだけ（A12）: 主の数えは主だけが読み書きする。
 * 読み取りスレッドの数えは読み取りスレッドだけが書き（__atomic で。ARM の 64 ビットの読み書きは 1 命令でない）、0 に戻さずに
 * 増やし続ける。主はつながりの始めの値を控え（g_prof_reader_base）、差をこのつながりの値とする */
struct prof_main {
    int64_t send_ns, send_bytes, send_calls, send_eagain;      /* send の中の時間・バイト・回数・EAGAIN */
};
struct prof_reader {
    int64_t fill_ns, fill_bytes, full_waits;                   /* DDR→tcp_ring の時間・バイト・満杯で待った回数 */
};
static struct prof_main g_prof;
static struct prof_reader g_prof_reader, g_prof_reader_base;

static void prof_add(int64_t *field, int64_t v)               /* 読み取りスレッドが自分の数えを増やす */
{
    __atomic_store_n(field, __atomic_load_n(field, __ATOMIC_RELAXED) + v, __ATOMIC_RELAXED);
}

/* 読み取りスレッドの数えの、このつながりの値（主が読む） */
static struct prof_reader prof_reader_now(void)
{
    struct prof_reader r = {
        __atomic_load_n(&g_prof_reader.fill_ns, __ATOMIC_RELAXED) - g_prof_reader_base.fill_ns,
        __atomic_load_n(&g_prof_reader.fill_bytes, __ATOMIC_RELAXED) - g_prof_reader_base.fill_bytes,
        __atomic_load_n(&g_prof_reader.full_waits, __ATOMIC_RELAXED) - g_prof_reader_base.full_waits,
    };
    return r;
}

static void prof_reader_mark(void)                             /* 主: つながりの始め */
{
    g_prof_reader_base.fill_ns = __atomic_load_n(&g_prof_reader.fill_ns, __ATOMIC_RELAXED);
    g_prof_reader_base.fill_bytes = __atomic_load_n(&g_prof_reader.fill_bytes, __ATOMIC_RELAXED);
    g_prof_reader_base.full_waits = __atomic_load_n(&g_prof_reader.full_waits, __ATOMIC_RELAXED);
}
static struct pl_ops g_pl;
static struct relay_stats rstats;                          /* 読み取りスレッドだけが触る（seq・from_pl・dropped_bad） */
static struct relay_ps g_ps;                               /* PS が DDR のリングを直接読むストリーム（relay.h）。読み取りスレッドだけが触る */
static struct relay_rd g_rd;                               /* トリガ取り込みの読み出しを PS が行う（窓の版 3）。読み取りスレッドだけが触る */
static int g_rd_on;                                        /* 窓が版 3 のとき 1（TCP がつながっている間だけ使う） */
static int g_ps_on;                                        /* 窓が版 2 で、深いバッファの DDR を読めるとき 1 */
static const uint8_t *g_ddr;                               /* 深いバッファの DDR の読み取り専用の写し */
static const uint8_t *g_zc_ring;                           /* 同じ領域の /dev/fprobe_ring の写し（中身は読まない。ゼロコピーの道） */
static int g_zc_state = RELAY_ZCS_NONE;                    /* relay.h の RELAY_ZCS_* */
static struct relay_zc g_zc;                               /* ゼロコピーの道（主のスレッドだけが触る） */
static uint64_t g_connections, g_zc_copied_total;          /* 起動からの TCP のつながりの数・copied の呼び出しの数 */
static volatile int64_t g_activity_ns;                     /* 最後に動きのあった時刻（どちらのスレッドからも更新） */
static pthread_mutex_t wake_mutex = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t wake_cond = PTHREAD_COND_INITIALIZER;

static volatile sig_atomic_t stop;
static void on_signal(int sig) { (void)sig; stop = 1; }

static uint32_t mmio_rd(void *ctx, uint32_t addr)
{
    return *(volatile uint32_t *)((uint8_t *)ctx + addr);
}

static void mmio_wr(void *ctx, uint32_t addr, uint32_t value)
{
    *(volatile uint32_t *)((uint8_t *)ctx + addr) = value;
}

static int64_t now_ns(void)
{
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return relay_ns(t.tv_sec, t.tv_nsec);
}

/* ファイル path を text に読む（NUL 終わり）。読めなければ 0 */
static int slurp(const char *path, char *text, size_t cap)
{
    FILE *f = fopen(path, "r");
    if (!f)
        return 0;
    size_t n = fread(text, 1, cap - 1, f);
    fclose(f);
    text[n] = 0;
    return n > 0 && n < cap - 1;
}

/* NIC（ifname）の割り込みが届く CPU。1 つに決まらない・読めないときは -1 */
static int nic_irq_cpu(const char *ifname)
{
    static char text[16384];
    char path[64], list[32];
    if (!slurp("/proc/interrupts", text, sizeof text))
        return -1;
    int irq = relay_irq_of(text, ifname);
    snprintf(path, sizeof path, "/proc/irq/%d/effective_affinity_list", irq);
    return irq < 0 || !slurp(path, list, sizeof list) ? -1 : relay_single_cpu(list);
}

static int g_cpu_reader = -1, g_cpu_main = -1;             /* スレッドを載せる CPU（-1 = 固定しない） */

/* 呼んだスレッドだけを cpu に固定する（cpu < 0 は何もしない）。失敗しても動きは変わらないので、理由を出して続ける */
static void pin_this_thread(int cpu, const char *who)
{
    if (cpu < 0)
        return;
    cpu_set_t set;
    CPU_ZERO(&set);
    CPU_SET(cpu, &set);
    if (sched_setaffinity(0, sizeof set, &set) != 0)
        fprintf(stderr, "fprobe-relay: %s を CPU %d に固定できない（%s）。固定せずに続ける\n", who, cpu, strerror(errno));
}

static void *reader_main(void *arg)
{
    struct relay_stats *shared = arg;                      /* 主のスレッドの st: bundle_max は PC の要求で更新される */
    pin_this_thread(g_cpu_reader, "読み取りスレッド");
    while (!stop) {
        uint8_t *slot = relay_ring_slot(&ring);
        if (!slot) {                                       /* 満杯: 送る側が追いつくのを待つ（PL の側は DDR が溜める） */
            sched_yield();
            continue;
        }
        rstats.bundle_max = __atomic_load_n(&shared->bundle_max, __ATOMIC_RELAXED);
        size_t m = relay_to_pc(&g_pl, slot, RELAY_RING_SLOT_BYTES, &rstats);
        if (m > 0) {                                       /* CONTROL・DAP の応答を先に */
            relay_ring_commit(&ring, m);
            __atomic_store_n(&g_activity_ns, now_ns(), __ATOMIC_RELAXED);
            continue;
        }
        int tcp = __atomic_load_n(&g_tcp, __ATOMIC_ACQUIRE);
        if (tcp == 2)                                      /* 主が TCP を閉じる: もう積まない（以後 tcp_ring を触るのは主だけ） */
            __atomic_store_n(&g_tcp, 3, __ATOMIC_RELEASE);
        if (tcp == TCP_ZC_REQUEST) {                       /* 主がゼロコピーの道にする: 以後 g_ps・g_rd は主が持つ */
            __atomic_store_n(&g_tcp, TCP_ZC, __ATOMIC_RELEASE);
            tcp = TCP_ZC;
        }
        if (tcp == TCP_ZC) {                               /* ゼロコピーの道: ストリームは主が送る。PL のフレームだけを見る */
            if (now_ns() - __atomic_load_n(&g_activity_ns, __ATOMIC_RELAXED) > SPIN_NS) {
                struct timespec until;
                clock_gettime(CLOCK_REALTIME, &until);
                until.tv_nsec += 1000000;
                if (until.tv_nsec >= 1000000000) {
                    until.tv_sec++;
                    until.tv_nsec -= 1000000000;
                }
                pthread_mutex_lock(&wake_mutex);
                pthread_cond_timedwait(&wake_cond, &wake_mutex, &until);
                pthread_mutex_unlock(&wake_mutex);
            }
            continue;
        }
        if (g_ps_on && tcp == 1) {                         /* TCP がつながっている: 枠なしのバイト列を tcp_ring へ */
            uint8_t *tslot = relay_ring_slot(&tcp_ring);
            if (!tslot)
                prof_add(&g_prof_reader.full_waits, 1);
            if (tslot) {
                int64_t f0 = now_ns();
                size_t n = g_rd_on ? relay_rd_fill(&g_rd, &g_pl, g_ddr, &g_ps, tslot, RELAY_RING_SLOT_BYTES) : 0;
                if (n == 0)                                /* 読み出しの要求が無ければ、ストリーム */
                    n = relay_ps_fill_bytes(&g_ps, &g_pl, g_ddr, tslot, RELAY_RING_SLOT_BYTES, now_ns());
                if (n > 0) {
                    prof_add(&g_prof_reader.fill_ns, now_ns() - f0);
                    prof_add(&g_prof_reader.fill_bytes, (int64_t)n);
                    relay_ring_commit(&tcp_ring, n);
                    __atomic_store_n(&g_activity_ns, now_ns(), __ATOMIC_RELAXED);
                    continue;
                }
            }
        } else if (g_ps_on) {                              /* TCP が無い: UDP のデータグラム */
            m = relay_ps_step(&g_ps, &g_pl, g_ddr, slot, RELAY_RING_SLOT_BYTES, &rstats, now_ns());
            if (m > 0) {
                relay_ring_commit(&ring, m);
                __atomic_store_n(&g_activity_ns, now_ns(), __ATOMIC_RELAXED);
                continue;
            }
        }
        if (g_ps_on && relay_ps_active(&g_ps)) {           /* 流れが動いている間は、溜まるのを待つ時間の判定のため休まない */
            sched_yield();
            continue;
        }
        if (now_ns() - __atomic_load_n(&g_activity_ns, __ATOMIC_RELAXED) > SPIN_NS) {
            /* 動きが無い間は 1 ms ごとに PL を見る（SWO・ストリーム）。PC の要求が来たら主が起こす */
            struct timespec until;
            clock_gettime(CLOCK_REALTIME, &until);
            until.tv_nsec += 1000000;
            if (until.tv_nsec >= 1000000000) {
                until.tv_sec++;
                until.tv_nsec -= 1000000000;
            }
            pthread_mutex_lock(&wake_mutex);
            pthread_cond_timedwait(&wake_cond, &wake_mutex, &until);
            pthread_mutex_unlock(&wake_mutex);
        }
    }
    return NULL;
}

/* TCP の待ち受け（PS 直読みが使えるときだけ）。開けなければ理由を出し、-1（TCP は使わない。ストリームは UDP のまま） */
static int tcp_listen(void)
{
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    int yes = 1;
    struct sockaddr_in me = { .sin_family = AF_INET, .sin_port = htons(TCP_PORT), .sin_addr.s_addr = INADDR_ANY };
    if (fd < 0)
        return -1;
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof yes);
    if (bind(fd, (struct sockaddr *)&me, sizeof me) != 0 || listen(fd, 1) != 0) {
        perror("fprobe-relay: TCP の待ち受けを開けない。ストリームは UDP のまま");
        close(fd);
        return -1;
    }
    fcntl(fd, F_SETFL, O_NONBLOCK);
    fprintf(stderr, "fprobe-relay: TCP %d でも PS 直読みのストリームを送れる（ホストがつないだときだけ）\n", TCP_PORT);
    return fd;
}

/* TCP のストリームが流れている間だけ、NIC の割り込みを遅らせて、ACK の受信をまとめて処理する（Linux 6.6 の NAPI の設定）。
 * 1 回 約 30 µs の ACK の処理（毎秒 約 1 万回）が CPU0 の約半分を占め、読みスレッド（CPU0）の読みが 19〜21 ms/MB で
 * 必要な 20 ms/MB の前後にあった。設定すると 25 MS/s（50 MB/s）の 60 秒が 9/9 通った（設定なしは 2/6。2026-10-31 実測）。
 * 普段の通信の遅れに影響させないため、つながっている間だけ入れ、閉じたら元の値に戻す */
#define NIC_NAME "eth0"
#define NIC_DEFER_PATH "/sys/class/net/" NIC_NAME "/napi_defer_hard_irqs"
#define NIC_FLUSH_PATH "/sys/class/net/" NIC_NAME "/gro_flush_timeout"
#define NIC_DEFER_HARD_IRQS "8"
#define NIC_GRO_FLUSH_NS "500000"
/* 変える前の値の控え。中継が戻さずに終わっても（SIGKILL・固まって systemd が止めた）、次の起動で戻す（A5） */
#define NIC_SAVED "/run/fprobe/nic-saved"
static char g_old_defer[32], g_old_flush[32];
static int g_nic_tuned;

static int nic_restore(const char *defer, const char *flush)
{
    char scratch[32];                                      /* 書く値（元の値）と、読み捨てる今の値を、同じ配列にしない */
    return relay_sysfs_set(NIC_DEFER_PATH, defer, scratch, sizeof scratch) == 0 &&
           relay_sysfs_set(NIC_FLUSH_PATH, flush, scratch, sizeof scratch) == 0 ? 0 : -1;
}

static void nic_untune(void)
{
    if (!g_nic_tuned)
        return;
    g_nic_tuned = 0;
    if (nic_restore(g_old_defer, g_old_flush) != 0)
        fprintf(stderr, "fprobe-relay: %s の割り込みの設定を元に戻せなかった（控え %s は残す）\n", NIC_NAME, NIC_SAVED);
    else
        remove(NIC_SAVED);
}

/* 起動時: 前の中継が戻さずに終わった控えがあれば、その値に戻す */
static void nic_recover(void)
{
    char defer[32], flush[32];
    if (relay_nic_load(NIC_SAVED, defer, sizeof defer, flush, sizeof flush) != 0)
        return;
    if (nic_restore(defer, flush) == 0) {
        remove(NIC_SAVED);
        fprintf(stderr, "fprobe-relay: 前の中継が戻さなかった %s の割り込みの設定を戻した（%s・%s）\n", NIC_NAME, defer, flush);
    } else {
        fprintf(stderr, "fprobe-relay: %s の割り込みの設定の控え %s を戻せない\n", NIC_NAME, NIC_SAVED);
    }
}

static void on_fatal(int sig)                              /* 落ちる前に戻す（sysfs の書きは数 µs。戻せなくても控えが残る） */
{
    nic_untune();
    signal(sig, SIG_DFL);
    raise(sig);
}

static void nic_tune(void)
{
    if (g_nic_tuned)
        return;
    if (relay_sysfs_set(NIC_DEFER_PATH, NIC_DEFER_HARD_IRQS, g_old_defer, sizeof g_old_defer) != 0 ||
        relay_sysfs_set(NIC_FLUSH_PATH, NIC_GRO_FLUSH_NS, g_old_flush, sizeof g_old_flush) != 0) {
        if (g_old_defer[0])                                /* 1 つ目だけ書けたなら戻す */
            relay_sysfs_set(NIC_DEFER_PATH, g_old_defer, g_old_flush, sizeof g_old_flush);
        fprintf(stderr, "fprobe-relay: %s の割り込みの設定を変えられない（root でない・カーネルが対応していない）。そのまま続ける\n",
                NIC_NAME);
        return;
    }
    if (relay_nic_save(NIC_SAVED, g_old_defer, g_old_flush) != 0)
        fprintf(stderr, "fprobe-relay: %s の元の値を %s に控えられない（中継が落ちたら戻らない）\n", NIC_NAME, NIC_SAVED);
    g_nic_tuned = 1;
    fprintf(stderr, "fprobe-relay: %s の割り込みを遅らせる（napi_defer_hard_irqs %s・gro_flush_timeout %s ns。元は %s・%s）\n",
            NIC_NAME, NIC_DEFER_HARD_IRQS, NIC_GRO_FLUSH_NS, g_old_defer, g_old_flush);
}

/* つながっている TCP を閉じる。読み取りスレッドが tcp_ring に積むのをやめた（g_tcp = 3）のを待ってから、tcp_ring を空にする */
static int g_zc_used;                                      /* このつながりでゼロコピーの道を使った（記録を出す） */

/* ゼロコピーの道の数えを記録に出す */
static void zc_report(const char *when)
{
    const struct relay_zc_stats *s = &g_zc.st;
    fprintf(stderr, "fprobe-relay: ゼロコピーの道（%s）: %.1f MB・呼び出し %llu・完了 %llu・copied %llu（%.1f MB）・ENOBUFS %llu・"
            "EAGAIN %llu・写して送った %.1f MB・かたまり %llu・完了までの時間 平均 %.0f µs 最大 %.0f µs・"
            "待つ呼び出しの最大 %llu・渡して返していない拍の最大 ロジアナ %llu イベント %llu・表が満杯 %llu・呼び出しが上限 %llu・"
            "送り終えずに捨てたかたまり %llu（%.1f MB）\n",
            when, (double)s->zc_bytes / 1e6, (unsigned long long)s->zc_calls, (unsigned long long)s->zc_completed,
            (unsigned long long)s->zc_copied_calls, (double)s->zc_copied_bytes / 1e6, (unsigned long long)s->zc_enobufs,
            (unsigned long long)s->zc_eagain, (double)s->zc_bounce_bytes / 1e6, (unsigned long long)s->zc_chunks,
            s->zc_latency_count ? (double)s->zc_latency_sum_ns / (double)s->zc_latency_count / 1e3 : 0.0,
            (double)s->zc_latency_max_ns / 1e3, (unsigned long long)s->zc_outstanding_max,
            (unsigned long long)s->zc_la_held_max_beats, (unsigned long long)s->zc_ev_held_max_beats,
            (unsigned long long)s->zc_desc_full, (unsigned long long)s->zc_calls_full,
            (unsigned long long)s->zc_abandoned_chunks, (double)s->zc_abandoned_bytes / 1e6);
}

static void tcp_close(int *cfd, size_t *off)
{
    int zc = __atomic_load_n(&g_tcp, __ATOMIC_ACQUIRE) == TCP_ZC;
    if (*cfd >= 0) {
        if (zc) {
            /* ゼロコピーの道: 打ち切る（RST。カーネルは送信の列を捨て、完了していないページを生きている相手に送らない）。
             * その後でなら、完了していないかたまりのページを PL に返してよい（A8） */
            struct linger abort_now = { 1, 0 };
            setsockopt(*cfd, SOL_SOCKET, SO_LINGER, &abort_now, sizeof abort_now);
        }
        close(*cfd);
    }
    *cfd = -1;
    if (zc)                                                /* g_ps は主が持っている間に。捨てたかたまりは数える */
        relay_zc_abandon(&g_zc, &g_ps, &g_pl);
    if (g_zc_used)
        zc_report("つながりの終わり");
    g_zc_used = 0;
    *off = 0;
    __atomic_store_n(&g_tcp, 2, __ATOMIC_RELEASE);
    while (!stop && __atomic_load_n(&g_tcp, __ATOMIC_ACQUIRE) != 3)
        sched_yield();
    size_t n;
    while (relay_ring_peek(&tcp_ring, &n))
        relay_ring_pop(&tcp_ring);
    struct prof_reader rp = prof_reader_now();
    if (g_prof.send_calls > 0)
        fprintf(stderr, "fprobe-relay: TCP の内訳: 送信 %.1f MB・%lld 回・send の中 %.0f ms・EAGAIN %lld／"
                "読み出し %.1f MB・%.0f ms・リング満杯 %lld 回\n", (double)g_prof.send_bytes / 1e6, (long long)g_prof.send_calls,
                (double)g_prof.send_ns / 1e6, (long long)g_prof.send_eagain,
                (double)rp.fill_bytes / 1e6, (double)rp.fill_ns / 1e6, (long long)rp.full_waits);
    memset(&g_prof, 0, sizeof g_prof);
    __atomic_store_n(&g_tcp, 0, __ATOMIC_RELEASE);
    nic_untune();
}

/* つながっている TCP の相手が閉じた（または壊れた）か。ホストはこの道で何も送らないので、読めるのは閉じた印（0）だけのはず。
 * 送って失敗するまで気づかないと、閉じた後の最初のバイト列が捨てられ、次に UDP に戻ったストリームが途中から始まる */
static int tcp_peer_closed(int fd)
{
    char c;
    ssize_t n = recv(fd, &c, 1, MSG_PEEK | MSG_DONTWAIT);
    return n == 0 || (n < 0 && errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR);
}

/* ホストがつないできたら、前のつながりを閉じて、これに替える（つながった時点で tcp_ring は空） */
static void tcp_accept(int lfd, int *cfd, size_t *off)
{
    int c = accept(lfd, NULL, NULL);
    if (c < 0)
        return;
    if (*cfd >= 0 || __atomic_load_n(&g_tcp, __ATOMIC_ACQUIRE) != 0)
        tcp_close(cfd, off);
    int yes = 1, big = 4 << 20;
    fcntl(c, F_SETFL, O_NONBLOCK);
    setsockopt(c, IPPROTO_TCP, TCP_NODELAY, &yes, sizeof yes);
    setsockopt(c, SOL_SOCKET, SO_SNDBUF, &big, sizeof big);
    *cfd = c;
    *off = 0;
    g_connections++;
    memset(&g_prof, 0, sizeof g_prof);
    prof_reader_mark();
    relay_zc_init(&g_zc);
    int zc = g_zc_state == RELAY_ZCS_READY;
    if (zc && setsockopt(c, SOL_SOCKET, SO_ZEROCOPY, &yes, sizeof yes) != 0) {
        fprintf(stderr, "fprobe-relay: SO_ZEROCOPY を立てられない（%s）。このつながりは複写の道で送る\n", strerror(errno));
        zc = 0;
    }
    if (zc) {                                              /* 読み取りスレッドから g_ps・g_rd を受け取る */
        __atomic_store_n(&g_tcp, TCP_ZC_REQUEST, __ATOMIC_RELEASE);
        while (!stop && __atomic_load_n(&g_tcp, __ATOMIC_ACQUIRE) != TCP_ZC)
            sched_yield();
        g_zc_used = 1;
    } else {
        __atomic_store_n(&g_tcp, 1, __ATOMIC_RELEASE);
    }
    nic_tune();
    fprintf(stderr, "fprobe-relay: TCP のホストがつながった。PS 直読みのストリームは TCP の%sで送る\n",
            zc ? "ゼロコピーの道" : "複写の道");
}

/* ゼロコピーの道のソケット（relay_sock_ops）。ctx は int のソケット */
static int64_t zc_sock_send(void *ctx, const struct relay_iov *iov, int n, int zerocopy)
{
    struct iovec v[RELAY_ZC_IOV_MAX];
    if (n > RELAY_ZC_IOV_MAX)
        n = RELAY_ZC_IOV_MAX;
    for (int i = 0; i < n; i++) {
        v[i].iov_base = (void *)iov[i].base;
        v[i].iov_len = iov[i].len;
    }
    struct msghdr msg = { .msg_iov = v, .msg_iovlen = (size_t)n };
    int64_t s0 = now_ns();
    ssize_t r = sendmsg(*(int *)ctx, &msg, MSG_NOSIGNAL | (zerocopy ? MSG_ZEROCOPY : 0));
    g_prof.send_ns += now_ns() - s0;
    if (r > 0)
        return r;
    if (r < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR))
        return RELAY_SEND_AGAIN;
    if (r < 0 && errno == ENOBUFS)
        return RELAY_SEND_NOBUFS;
    return RELAY_SEND_BROKEN;
}

/* 完了通知（MSG_ERRQUEUE の SO_EE_ORIGIN_ZEROCOPY）をすべて読み、relay_zc に渡す。copied なら記録する */
static void zc_completions(int fd)
{
    for (;;) {
        char control[256];
        struct msghdr m = { .msg_control = control, .msg_controllen = sizeof control };
        if (recvmsg(fd, &m, MSG_ERRQUEUE | MSG_DONTWAIT) < 0)
            return;
        for (struct cmsghdr *cm = CMSG_FIRSTHDR(&m); cm; cm = CMSG_NXTHDR(&m, cm)) {
            if (!(cm->cmsg_level == SOL_IP && cm->cmsg_type == IP_RECVERR))
                continue;
            struct sock_extended_err e;
            memcpy(&e, CMSG_DATA(cm), sizeof e);
            if (e.ee_errno != 0 || e.ee_origin != SO_EE_ORIGIN_ZEROCOPY)
                continue;
            int copied = (e.ee_code & SO_EE_CODE_ZEROCOPY_COPIED) != 0;
            int before = relay_zc_state(&g_zc);
            uint32_t fresh = relay_zc_complete(&g_zc, e.ee_info, e.ee_data, copied, now_ns());
            if (copied) {
                g_zc_copied_total += fresh;
                g_zc_state = RELAY_ZCS_STOPPED;
                if (before == RELAY_ZC_RUN)
                    fprintf(stderr, "fprobe-relay: 完了通知が copied（カーネルがゼロコピーをやめて写した。番号 %u..%u）。写しはキャッシュの"
                            "ある写像を読むので古い中身を送りえた。ゼロコピーをやめ、出した分の完了を待って複写の道に戻す（起動し直すまで使わない）\n",
                            e.ee_info, e.ee_data);
            }
        }
    }
}

#define TCP_IOV_MAX 8                                       /* 1 回の sendmsg にまとめるスロットの上限（約 6.5 KB × 8） */

/* tcp_ring のバイト列を 1 周に max 回まで sendmsg する（1 回に TCP_IOV_MAX スロットまで）。送れた分だけ進める（途中までなら次の周に続ける）。
 * 戻り値: 動きがあれば 1。つながりが壊れたら閉じる */
static int tcp_drain(int *cfd, size_t *off, int max)
{
    int active = 0;
    for (int k = 0; k < max && *cfd >= 0 && !stop; k++) {
        const uint8_t *base[TCP_IOV_MAX];
        size_t len[TCP_IOV_MAX];
        int count = relay_ring_peek_many(&tcp_ring, base, len, TCP_IOV_MAX);
        if (count == 0)
            break;
        struct iovec iov[TCP_IOV_MAX];
        size_t total = 0;
        for (int i = 0; i < count; i++) {
            size_t skip = i == 0 ? *off : 0;
            iov[i].iov_base = (void *)(base[i] + skip);
            iov[i].iov_len = len[i] - skip;
            total += iov[i].iov_len;
        }
        struct msghdr msg = { .msg_iov = iov, .msg_iovlen = (size_t)count };
        int64_t s0 = now_ns();
        ssize_t n = sendmsg(*cfd, &msg, MSG_NOSIGNAL);      /* 読めるスロットをまとめて 1 回で（16 KB 以上が速い） */
        g_prof.send_ns += now_ns() - s0;
        g_prof.send_calls++;
        if (n > 0) {
            g_prof.send_bytes += n;
            active = 1;
            size_t left = (size_t)n;
            for (int i = 0; i < count && left > 0; i++) {
                if (left >= iov[i].iov_len) {               /* このスロットは送りきった */
                    left -= iov[i].iov_len;
                    relay_ring_pop(&tcp_ring);
                    *off = 0;
                } else {                                    /* 途中まで: 続きは次の周に */
                    *off = (i == 0 ? *off : 0) + left;
                    left = 0;
                }
            }
            if ((size_t)n < total)
                break;                                      /* 送りきれない: 送信バッファが埋まった */
        } else if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR)) {
            g_prof.send_eagain++;
            break;
        } else {
            fprintf(stderr, "fprobe-relay: TCP が切れた（%s）。ストリームは UDP に戻る\n", n < 0 ? strerror(errno) : "相手が閉じた");
            tcp_close(cfd, off);
            break;
        }
    }
    return active;
}

static void *map(int fd, uint32_t base)
{
    void *p = mmap(NULL, 4096, PROT_READ | PROT_WRITE, MAP_SHARED, fd, base);
    return p == MAP_FAILED ? NULL : p;
}

/* 深いバッファの DDR の領域を PL に渡す。渡したら、その物理番地を返す（0 = 使わない） */
static uint32_t setup_deep_buffer(const struct pl_ops *pl)
{
    FILE *f = fopen(MEM_CONF, "r");
    if (!f)
        return 0;                                       /* 深いバッファを使わない */
    char text[64] = { 0 };
    size_t got = fread(text, 1, sizeof text - 1, f);
    fclose(f);
    text[got] = 0;
    char *end;
    unsigned long long base = strtoull(text, &end, 0);   /* 32 ビットの long で飽和させない */
    unsigned long long size = strtoull(end, &end, 0);
    static char iomem[16384];
    FILE *m = fopen("/proc/iomem", "r");
    size_t n = m ? fread(iomem, 1, sizeof iomem - 1, m) : 0;
    if (m)
        fclose(m);
    iomem[n] = 0;
    int truncated = n >= sizeof iomem - 1;                /* 後ろの System RAM を見落とすので、使えないとして断る */
    uint32_t mem_bytes = pl->rd(pl->ctx, REG_MEM_BYTES);  /* PL が MEM_BASE から実際に使う大きさ */
    int r = (base > 0xFFFFFFFFull || size > 0xFFFFFFFFull || n == 0 || truncated) ? -5
            : relay_deep_region_ok(iomem, (uint32_t)base, (uint32_t)size, mem_bytes,
                                   relay_region_reserved("/proc/device-tree/reserved-memory", (uint32_t)base, (uint32_t)size));
    if (r == 1) {
        fprintf(stderr, "fprobe-relay: この回路は深いバッファを持たない（MEM_BYTES = 0）\n");
        return 0;
    }
    if (r != 0) {
        fprintf(stderr, "fprobe-relay: %s の領域 0x%08llx + 0x%llx は PL の 0x%x バイトに使えない（理由 %d: -1 = 16 の"
                "倍数でない、-2 = 小さい、-3 = DDR の外、-4 = Linux のメモリと重なる、-5 = 書式・読めない）。"
                "深いバッファは使わない\n", MEM_CONF, base, size, mem_bytes, r);
        return 0;
    }
    pl->wr(pl->ctx, REG_MEM_BASE, (uint32_t)base);
    fprintf(stderr, "fprobe-relay: 深いバッファ 0x%08llx + 0x%x\n", base, mem_bytes);
    return (uint32_t)base;
}

#define RINGMAP_DEV "/dev/fprobe_ring"
#define RESERVED_DT "/proc/device-tree/reserved-memory"

/* /dev/fprobe_ring（fprobe_ringmap）があり、それが写すデバイスツリーのノードの領域が深いバッファ [base, base + mem_bytes) を
 * 含むなら、写してゼロコピーの道を使えるようにする。無い・使えないときは理由を出し、複写の道のまま */
static void setup_zerocopy(uint32_t base, uint32_t mem_bytes)
{
    if (access(RINGMAP_DEV, F_OK) != 0) {
        fprintf(stderr, "fprobe-relay: %s が無い（fprobe_ringmap が読み込まれていない）。TCP は複写の道で送る\n", RINGMAP_DEV);
        return;
    }
    g_zc_state = RELAY_ZCS_UNUSABLE;
    if (!relay_ringmap_region_ok(RESERVED_DT, base, mem_bytes)) {
        fprintf(stderr, "fprobe-relay: %s の fprobe_ring のノードが深いバッファ 0x%08x + 0x%x と合わない（無い・2 つ以上・"
                "含まない）。TCP は複写の道で送る\n", RESERVED_DT, base, mem_bytes);
        return;
    }
    int fd = open(RINGMAP_DEV, O_RDONLY);
    void *p = fd >= 0 ? mmap(NULL, mem_bytes, PROT_READ, MAP_SHARED, fd, 0) : MAP_FAILED;
    if (fd >= 0)
        close(fd);                                         /* 写しは閉じた後も残る */
    if (p == MAP_FAILED) {
        perror("fprobe-relay: /dev/fprobe_ring を写せない。TCP は複写の道で送る");
        return;
    }
    g_zc_ring = p;
    g_zc_state = RELAY_ZCS_READY;
    fprintf(stderr, "fprobe-relay: %s を写した（0x%08x + 0x%x）。TCP はゼロコピーの道で送る（かたまりの中身は最大 %u 拍）\n",
            RINGMAP_DEV, base, mem_bytes, RELAY_ZC_CHUNK_MAX);
}

/* 窓が版 2 で深いバッファがあれば、DDR の領域を読み取り専用で写し、PS 直読みのストリームを使えるようにする。
 * 使えない場合は理由を出し、窓経由のストリームのまま（PL の ps_read は CSR で 1 にされない限り 0） */
static void setup_ps_stream(const struct pl_ops *pl, uint32_t base)
{
    if (base == 0)
        return;
    if (pl->rd(pl->ctx, REG_VERSION) < RELAY_LINK_VERSION_PS_STREAM) {
        fprintf(stderr, "fprobe-relay: 窓の版 %u は PS 直読みの位置の約束（版 %u）を持たない。窓経由のストリームのまま\n",
                pl->rd(pl->ctx, REG_VERSION), RELAY_LINK_VERSION_PS_STREAM);
        return;
    }
    uint32_t mem_bytes = pl->rd(pl->ctx, REG_MEM_BYTES);
    if (relay_ps_init(&g_ps, mem_bytes) != 0) {
        fprintf(stderr, "fprobe-relay: 深いバッファの大きさ 0x%x が PS 直読みのリングに合わない。窓経由のストリームのまま\n",
                mem_bytes);
        return;
    }
    int fd = open("/dev/mem", O_RDONLY | O_SYNC);
    void *p = fd >= 0 ? mmap(NULL, mem_bytes, PROT_READ, MAP_SHARED, fd, base) : MAP_FAILED;
    if (p == MAP_FAILED) {
        perror("fprobe-relay: 深いバッファの DDR を読み取り専用に写せない。窓経由のストリームのまま");
        return;
    }
    g_ddr = p;
    g_ps_on = 1;
    relay_rd_init(&g_rd);
    g_rd_on = 1;
    fprintf(stderr, "fprobe-relay: PS 直読みのストリームを使える（DDR 0x%08x + 0x%x を読み取り専用に写した）\n", base, mem_bytes);
    setup_zerocopy(base, mem_bytes);
}

static int isolate(void)
{
    int mem = open("/dev/mem", O_RDWR | O_SYNC);
    void *slcr = mem < 0 ? NULL : map(mem, SLCR_BASE);
    if (!slcr) {
        perror("fprobe-relay --isolate");
        return 1;
    }
    struct pl_ops slcr_ops = { mmio_rd, mmio_wr, slcr };
    relay_slcr_write(&slcr_ops, SLCR_LVL_SHFTR_EN, 0);
    return 0;
}

/* fprobe-relay --enable: レベルシフタを有効にするだけ（--isolate の対。Zynq DUT のように PS が AXI で PL を触る回路を書いた後） */
static int enable_shifter(void)
{
    int mem = open("/dev/mem", O_RDWR | O_SYNC);
    void *slcr = mem < 0 ? NULL : map(mem, SLCR_BASE);
    if (!slcr) {
        perror("fprobe-relay --enable");
        return 1;
    }
    struct pl_ops slcr_ops = { mmio_rd, mmio_wr, slcr };
    relay_slcr_write(&slcr_ops, SLCR_LVL_SHFTR_EN, 0xF);
    return 0;
}

int main(int argc, char **argv)
{
    if (argc > 1 && strcmp(argv[1], "--isolate") == 0)
        return isolate();
    if (argc > 1 && strcmp(argv[1], "--enable") == 0)
        return enable_shifter();
    nic_recover();                                         /* 前の中継が戻さずに終わった NIC の設定 */
    atexit(nic_untune);                                    /* どの return・exit で終わっても、割り込みの設定を元に戻す */
    signal(SIGSEGV, on_fatal);
    signal(SIGBUS, on_fatal);
    signal(SIGABRT, on_fatal);
    if (access(MARKER, F_OK) != 0) {
        fprintf(stderr, "fprobe-relay: %s が無い（fprobe の回路が載っていない）。PL を読まずに終わる\n", MARKER);
        return 2;
    }
    int mem = open("/dev/mem", O_RDWR | O_SYNC);
    if (mem < 0) {
        perror("fprobe-relay: /dev/mem");
        return 1;
    }
    void *slcr = map(mem, SLCR_BASE);
    void *win = map(mem, PL_BASE);
    if (!slcr || !win) {
        perror("fprobe-relay: mmap");
        return 1;
    }
    struct pl_ops slcr_ops = { mmio_rd, mmio_wr, slcr };
    relay_slcr_write(&slcr_ops, SLCR_LVL_SHFTR_EN, 0xF);

    struct pl_ops pl = { mmio_rd, mmio_wr, win };
    if (relay_check_design(&pl) != 0) {
        fprintf(stderr, "fprobe-relay: PL の ID・版が zynq_frame_link でない（0x%08x）\n", mmio_rd(win, REG_ID));
        return 3;
    }

    setup_ps_stream(&pl, setup_deep_buffer(&pl));

    int sock = socket(AF_INET, SOCK_DGRAM, 0);
    struct sockaddr_in me = { .sin_family = AF_INET, .sin_port = htons(UDP_PORT), .sin_addr.s_addr = INADDR_ANY };
    int big = 4 << 20;
    setsockopt(sock, SOL_SOCKET, SO_RCVBUF, &big, sizeof big);
    setsockopt(sock, SOL_SOCKET, SO_SNDBUF, &big, sizeof big);
    if (sock < 0 || bind(sock, (struct sockaddr *)&me, sizeof me) != 0) {
        perror("fprobe-relay: UDP");
        return 1;
    }
    fcntl(sock, F_SETFL, O_NONBLOCK);
    signal(SIGTERM, on_signal);
    signal(SIGINT, on_signal);
    fprintf(stderr, "fprobe-relay: UDP %d ⇄ PL 0x%08x を受け渡す\n", UDP_PORT, PL_BASE);

    signal(SIGPIPE, SIG_IGN);
    int lfd = g_ps_on ? tcp_listen() : -1;                 /* TCP の待ち受け */
    int cfd = -1;                                          /* つながっているホスト（-1 = 無し） */
    size_t tcp_off = 0;                                    /* tcp_ring の先頭を、ここまで送った */
    int64_t last_accept = 0;
    struct relay_stats st = { 0 };
    struct relay_peer peer = { 0 };
    uint8_t in[2048];
    ssize_t pending = -1;                /* PL が busy で渡せていないデータグラムの長さ（-1 = 無い） */
    int64_t last_activity = now_ns(), last_check = last_activity;
    g_pl = pl;
    relay_ring_init(&ring);
    relay_ring_init(&tcp_ring);
    g_activity_ns = last_activity;
    /* 送るスレッド（主）が NIC の割り込みと同じ CPU に載ると遅い。実測（PYNQ-Z2、RAW 16 ch の垂れ流し、各 8 秒 5 回）:
     * 主 = 割り込みの CPU でない・読み取り = 割り込みの CPU なら 5 回とも約 48 MB/s、逆は 5 回とも約 36 MB/s で溢れた。
     * 2 コアで割り込みの CPU が分かるときだけ固定する（それ以外の組み合わせは測っていない） */
    int irq_cpu = nic_irq_cpu(NIC_NAME);
    if (irq_cpu >= 0 && irq_cpu < 2 && sysconf(_SC_NPROCESSORS_ONLN) == 2) {
        g_cpu_reader = irq_cpu;
        g_cpu_main = 1 - irq_cpu;
        fprintf(stderr, "fprobe-relay: eth0 の割り込みは CPU %d。読み取りスレッドをそこに、主を CPU %d に固定する\n", irq_cpu, g_cpu_main);
    } else {
        fprintf(stderr, "fprobe-relay: eth0 の割り込みの CPU が分からない、または CPU が 2 つでない。スレッドは固定しない\n");
    }
    pthread_t reader;
    if (pthread_create(&reader, NULL, reader_main, &st) != 0) {
        perror("fprobe-relay: スレッド");
        return 1;
    }
    pin_this_thread(g_cpu_main, "主のスレッド");

    struct sockaddr_in from;                               /* 最後に受けたデータグラムの送り主（状態の問い合わせの返事の宛先） */
    struct relay_sock_ops zc_ops = { zc_sock_send, &cfd };
    int zc_blocked = 0;                                    /* ゼロコピーの道が送れなかった（ソケットを待ってよい） */
    while (!stop) {
        int active = 0;
        if (pending < 0) {
            socklen_t fl = sizeof from;
            ssize_t n = recvfrom(sock, in, sizeof in, 0, (struct sockaddr *)&from, &fl);
            if (n >= 0) {
                pending = n;
                if (relay_dgram_sets_peer(in, (size_t)n))
                    relay_peer_update(&peer, from.sin_addr.s_addr, from.sin_port);
            }
        }
        if (pending >= 0) {
            int r = relay_from_pc(&pl, in, (size_t)pending, &st);
            if (r == 2) {                                  /* 中継の状態の問い合わせ: 送り主へ返す */
                struct relay_status rs;
                memset(&rs, 0, sizeof rs);
                int tcp = __atomic_load_n(&g_tcp, __ATOMIC_ACQUIRE);
                rs.zc_state = (uint8_t)g_zc_state;
                rs.tcp_path = cfd < 0 ? RELAY_PATH_NONE : tcp != TCP_ZC ? RELAY_PATH_COPY
                              : relay_zc_state(&g_zc) == RELAY_ZC_RUN ? RELAY_PATH_ZEROCOPY : RELAY_PATH_ZC_DRAIN;
                rs.connections = g_connections;
                rs.zc_copied_total = g_zc_copied_total;
                rs.tcp_bytes = (uint64_t)g_prof.send_bytes;
                rs.tcp_calls = (uint64_t)g_prof.send_calls;
                rs.tcp_eagain = (uint64_t)g_prof.send_eagain;
                rs.copy_ring_full = (uint64_t)prof_reader_now().full_waits;
                rs.ps_bad_la_positions = relay_ps_bad_la(&g_ps);       /* 32 ビットの読み（ARM で 1 命令） */
                rs.ps_bad_ev_positions = relay_ps_bad_ev(&g_ps);
                rs.pl_stale_writes = g_ps_on ? pl.rd(pl.ctx, REG_STALE_WRITES) : 0;
#define COPY_ZC_STAT(name) rs.name = g_zc.st.name;
                RELAY_ZC_STATS(COPY_ZC_STAT)
#undef COPY_ZC_STAT
                uint8_t reply[1024];
                size_t m = relay_status_encode(&rs, in[1], reply, sizeof reply);   /* 問い合わせの番号を写す */
                if (m > 0)
                    sendto(sock, reply, m, 0, (struct sockaddr *)&from, sizeof from);
            }
            if (r != 1) {
                pending = -1;
                active = 1;
                __atomic_store_n(&g_activity_ns, now_ns(), __ATOMIC_RELAXED);
                pthread_cond_signal(&wake_cond);           /* 応答を速く拾うため、読み取りスレッドを起こす */
            }
        }
        /* PL → PC は、リングのデータグラムを 1 周に DRAIN_MAX 個まで送る。多く出し続けられても、PC からの要求
         * （流しを止める CONTROL・DAP）の受け取りと ID の確認・終了の合図に戻る */
        for (int k = 0; k < DRAIN_MAX && !stop; k++) {
            size_t m;
            const uint8_t *d = relay_ring_peek(&ring, &m);
            if (!d)
                break;
            if (peer.known) {
                struct sockaddr_in to = { .sin_family = AF_INET, .sin_port = peer.port,
                                          .sin_addr.s_addr = peer.addr };
                sendto(sock, d, m, 0, (struct sockaddr *)&to, sizeof to);
            }
            relay_ring_pop(&ring);
            active = 1;
        }
        if (lfd >= 0) {
            int64_t ta = now_ns();
            if (ta - last_accept > 1000000) {              /* accept は 1 ms ごと（毎周の呼び出しを避ける） */
                last_accept = ta;
                if (cfd >= 0 && tcp_peer_closed(cfd)) {
                    fprintf(stderr, "fprobe-relay: TCP のホストが閉じた。ストリームは UDP に戻る\n");
                    tcp_close(&cfd, &tcp_off);
                }
                tcp_accept(lfd, &cfd, &tcp_off);
            }
            zc_blocked = 0;
            if (cfd >= 0 && __atomic_load_n(&g_tcp, __ATOMIC_ACQUIRE) == TCP_ZC) {
                zc_completions(cfd);
                int r = relay_zc_step(&g_zc, &g_ps, g_rd_on ? &g_rd : NULL, &g_pl, &zc_ops, g_zc_ring, g_ddr, now_ns());
                if (r < 0) {
                    fprintf(stderr, "fprobe-relay: TCP が切れた（ゼロコピーの道）。ストリームは UDP に戻る\n");
                    tcp_close(&cfd, &tcp_off);
                } else {
                    active |= (r & RELAY_ZC_MOVED) != 0;
                    zc_blocked = (r & RELAY_ZC_BLOCKED) != 0;
                    if (relay_zc_state(&g_zc) == RELAY_ZC_DONE) {
                        /* copied の後、出した分の完了がそろった: 複写の道に渡す（g_ps・g_rd を読み取りスレッドへ） */
                        zc_report("copied でやめた");
                        g_zc_used = 0;
                        __atomic_store_n(&g_tcp, 1, __ATOMIC_RELEASE);
                        fprintf(stderr, "fprobe-relay: ゼロコピーの道をやめ、このつながりの続きを複写の道で送る\n");
                    }
                }
            } else if (cfd >= 0 && tcp_drain(&cfd, &tcp_off, DRAIN_MAX)) {
                active = 1;
            }
        }
        int64_t t = now_ns();
        if (active)
            last_activity = t;
        if (t - last_check > 100000000) {
            last_check = t;
            if (relay_check_design(&pl) != 0) {
                fprintf(stderr, "fprobe-relay: PL の ID が読めなくなった。終わる\n");
                return 4;
            }
        }
        int64_t ra = __atomic_load_n(&g_activity_ns, __ATOMIC_RELAXED);
        if (ra > last_activity)
            last_activity = ra;
        int zc_on = cfd >= 0 && __atomic_load_n(&g_tcp, __ATOMIC_ACQUIRE) == TCP_ZC;
        if (pending < 0 && zc_on && relay_ps_active(&g_ps)) {
            /* ゼロコピーの道で流れが動いている: 溜まるのを待つ時間の判定のため休まない（複写の道の読み取りスレッドと同じ）。
             * 送れなかった（送信バッファ・完了通知の置き場）ときは、ソケットが書ける・完了通知が来るまで（最大 1 ms）待つ */
            if (zc_blocked) {
                struct pollfd p[2] = { { .fd = sock, .events = POLLIN }, { .fd = cfd, .events = POLLOUT } };
                poll(p, 2, 1);
            } else {
                sched_yield();
            }
        } else if (pending < 0 && t - last_activity > SPIN_NS) {
            /* 1 ms ごとに PL からのフレーム（SWO・ストリーム）も見る。ゼロコピーの道では完了通知（POLLERR）でも起きる */
            struct pollfd p[2] = { { .fd = sock, .events = POLLIN }, { .fd = cfd, .events = 0 } };
            poll(p, zc_on ? 2 : 1, 1);
        }
    }
    stop = 1;
    pthread_cond_signal(&wake_cond);
    pthread_join(reader, NULL);
    if (cfd >= 0)
        close(cfd);
    if (lfd >= 0)
        close(lfd);
    fprintf(stderr, "fprobe-relay: 終わる（PL へ %u、PC へ %u、捨てた %u）\n", st.to_pl, rstats.from_pl,
            st.dropped_bad + rstats.dropped_bad);
    return 0;
}
