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
 * fprobe-relay --enable: レベルシフタを有効にするだけ（--isolate の対）。
 * fprobe-relay --isolate: レベルシフタを無効にするだけ（PL を書き直す前に fprobe-pl before が呼ぶ）。シフタが有効なまま
 * PL を書き直す・PS7 を持たない回路でシフタを有効にすると、PL から PS への信号が定まらず Linux が固まった（2026-10-20）。
 * SPDX-License-Identifier: AGPL-3.0-only
 */
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#include "relay.h"

#define PL_BASE 0x40000000u
#define SLCR_BASE 0xF8000000u
#define SLCR_LOCK 0x004u
#define SLCR_UNLOCK 0x008u
#define SLCR_LVL_SHFTR_EN 0x900u
#define MARKER "/run/fprobe/pl-design"
#define MEM_CONF "/etc/fprobe/mem"
#define UDP_PORT 63920
#define SPIN_NS 2000000               /* 動きがあった後は 2 ms 窓を見続ける（DAP の往復を速くする） */
#define DRAIN_MAX 4                    /* 1 周に PC へ送るフレームの上限 */

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

static void *map(int fd, uint32_t base)
{
    void *p = mmap(NULL, 4096, PROT_READ | PROT_WRITE, MAP_SHARED, fd, base);
    return p == MAP_FAILED ? NULL : p;
}

static void setup_deep_buffer(const struct pl_ops *pl)
{
    FILE *f = fopen(MEM_CONF, "r");
    if (!f)
        return;                                         /* 深いバッファを使わない */
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
            : relay_deep_region_ok(iomem, (uint32_t)base, (uint32_t)size, mem_bytes);
    if (r == 1) {
        fprintf(stderr, "fprobe-relay: この回路は深いバッファを持たない（MEM_BYTES = 0）\n");
        return;
    }
    if (r != 0) {
        fprintf(stderr, "fprobe-relay: %s の領域 0x%08llx + 0x%llx は PL の 0x%x バイトに使えない（理由 %d: -1 = 16 の"
                "倍数でない、-2 = 小さい、-3 = DDR の外、-4 = Linux のメモリと重なる、-5 = 書式・読めない）。"
                "深いバッファは使わない\n", MEM_CONF, base, size, mem_bytes, r);
        return;
    }
    pl->wr(pl->ctx, REG_MEM_BASE, (uint32_t)base);
    fprintf(stderr, "fprobe-relay: 深いバッファ 0x%08llx + 0x%x\n", base, mem_bytes);
}

static int isolate(void)
{
    int mem = open("/dev/mem", O_RDWR | O_SYNC);
    void *slcr = mem < 0 ? NULL : map(mem, SLCR_BASE);
    if (!slcr) {
        perror("fprobe-relay --isolate");
        return 1;
    }
    mmio_wr(slcr, SLCR_UNLOCK, 0xDF0D);
    mmio_wr(slcr, SLCR_LVL_SHFTR_EN, 0);
    mmio_wr(slcr, SLCR_LOCK, 0x767B);
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
    mmio_wr(slcr, SLCR_UNLOCK, 0xDF0D);
    mmio_wr(slcr, SLCR_LVL_SHFTR_EN, 0xF);
    mmio_wr(slcr, SLCR_LOCK, 0x767B);
    return 0;
}

int main(int argc, char **argv)
{
    if (argc > 1 && strcmp(argv[1], "--isolate") == 0)
        return isolate();
    if (argc > 1 && strcmp(argv[1], "--enable") == 0)
        return enable_shifter();
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
    mmio_wr(slcr, SLCR_UNLOCK, 0xDF0D);
    mmio_wr(slcr, SLCR_LVL_SHFTR_EN, 0xF);
    mmio_wr(slcr, SLCR_LOCK, 0x767B);

    struct pl_ops pl = { mmio_rd, mmio_wr, win };
    if (relay_check_design(&pl) != 0) {
        fprintf(stderr, "fprobe-relay: PL の ID・版が zynq_frame_link でない（0x%08x）\n", mmio_rd(win, REG_ID));
        return 3;
    }

    setup_deep_buffer(&pl);

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

    struct relay_stats st = { 0 };
    struct relay_peer peer = { 0 };
    uint8_t in[2048], out[RELAY_MAX_PAYLOAD + 2];
    ssize_t pending = -1;                /* PL が busy で渡せていないデータグラムの長さ（-1 = 無い） */
    int64_t last_activity = now_ns(), last_check = last_activity;

    while (!stop) {
        int active = 0;
        if (pending < 0) {
            struct sockaddr_in from;
            socklen_t fl = sizeof from;
            ssize_t n = recvfrom(sock, in, sizeof in, 0, (struct sockaddr *)&from, &fl);
            if (n >= 0) {
                pending = n;
                if (relay_dgram_ok((size_t)n))
                    relay_peer_update(&peer, from.sin_addr.s_addr, from.sin_port);
            }
        }
        if (pending >= 0) {
            int r = relay_from_pc(&pl, in, (size_t)pending, &st);
            if (r != 1) {
                pending = -1;
                active = 1;
            }
        }
        /* PL → PC は 1 周に DRAIN_MAX フレームまで。PL が中継の汲み出しより速く出し続けても（SWO・ストリーム）、
         * PC からの要求（流しを止める CONTROL・DAP）の受け取りと ID の確認・終了の合図に戻る */
        size_t m;
        for (int k = 0; k < DRAIN_MAX && !stop && (m = relay_to_pc(&pl, out, sizeof out, &st)) > 0; k++) {
            if (peer.known) {
                struct sockaddr_in to = { .sin_family = AF_INET, .sin_port = peer.port,
                                          .sin_addr.s_addr = peer.addr };
                sendto(sock, out, m, 0, (struct sockaddr *)&to, sizeof to);
            }
            active = 1;
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
        if (pending < 0 && t - last_activity > SPIN_NS) {
            struct pollfd p = { .fd = sock, .events = POLLIN };
            poll(&p, 1, 1);              /* 1 ms ごとに PL からのフレーム（SWO・ストリーム）も見る */
        }
    }
    fprintf(stderr, "fprobe-relay: 終わる（PL へ %u、PC へ %u、捨てた %u）\n", st.to_pl, st.from_pl, st.dropped_bad);
    return 0;
}
