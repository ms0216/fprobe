/* 測定の道具（設計 1.7.8）: 1 本の TCP で、待ち受けてつながった相手へ決めた秒数だけ送り、速さ・完了通知・CPU 時間を 1 行で出す。
 * 1.7.6 の b2_send（合成の送り手）の iov・plain の並びと同じ送り方に、中身の出どころ（-m）を足したもの。中継には触れない。
 *
 *   zc_send -p 口 [-c CPU] [-t 秒] [-z] [-m mem|ring|devmem] [-l plain|iov] [-s 中身の B] [-b 1 回の B] [-o ずらす B]
 *           [-w 回す範囲の B] [-n 送る総 B]
 *   -z      MSG_ZEROCOPY（SO_ZEROCOPY を立て、完了通知を MSG_ERRQUEUE から数える）
 *   -m      mem = 利用者空間の普通のメモリ（aligned_alloc、0x5a で埋める。1.7.6 と同じ）
 *           ring = /dev/fprobe_ring（fprobe_ringmap）を読み取り専用で写したリング
 *           devmem = /dev/mem（O_SYNC）でリングの番地を写したもの（いまの中継と同じ写し方。対照）
 *           ring・devmem の番地と大きさは /etc/fprobe/mem（中継と同じ設定）から読む
 *   -l      plain = 見出しなしの続いた -b B（基準）
 *           iov = 見出し 16 B（別のバッファ）+ 中身 -s B を交互に並べた iovec を、1 回に -b/-s 組（いまの中継は -s 4096）
 *   -o      中身の先頭をページの境から -o B ずらす（リングの中のかたまりは一般にページに揃わない）
 *   -w      中身を読む範囲（既定 8 MB。この範囲を先頭から回る）
 *   -n      秒数でなく、この総量を 1 回だけ（plain、-o から続けて）送って終わる（中身の照合用。-w より大きくしない）
 * 出力: mode・bytes・MBps・calls・completions・copied（SO_EE_CODE_ZEROCOPY_COPIED の付いた呼び出しの数）・enobufs・
 *       err（送信が失敗したときの errno の名前。EFAULT 等）・送り手のプロセスの user/sys の ms/MB（getrusage）
 * SPDX-License-Identifier: AGPL-3.0-only
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/errqueue.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <sys/uio.h>
#include <time.h>
#include <unistd.h>
#ifndef SO_ZEROCOPY
#define SO_ZEROCOPY 60
#endif
#ifndef MSG_ZEROCOPY
#define MSG_ZEROCOPY 0x4000000
#endif

#define MAX_IOV 1024

static double now(void)
{
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec * 1e-9;
}

static double cpu_ms(const struct timeval *tv) { return tv->tv_sec * 1e3 + tv->tv_usec * 1e-3; }

static long long copied, completions;
static void reap(int fd)
{
    for (;;) {
        char ctl[128];
        struct msghdr m = { .msg_control = ctl, .msg_controllen = sizeof ctl };
        if (recvmsg(fd, &m, MSG_ERRQUEUE | MSG_DONTWAIT) < 0)
            return;
        for (struct cmsghdr *c = CMSG_FIRSTHDR(&m); c; c = CMSG_NXTHDR(&m, c)) {
            struct sock_extended_err *e = (void *)CMSG_DATA(c);
            if (e->ee_origin != SO_EE_ORIGIN_ZEROCOPY)
                continue;
            completions += (long long)e->ee_data - e->ee_info + 1;
            if (e->ee_code & SO_EE_CODE_ZEROCOPY_COPIED)
                copied += (long long)e->ee_data - e->ee_info + 1;
        }
    }
}

/* /etc/fprobe/mem（「物理番地 大きさ」）のリングを写す。devmem = 0 なら /dev/fprobe_ring、1 なら /dev/mem（O_SYNC） */
static uint8_t *map_ring(int devmem, size_t *size)
{
    FILE *f = fopen("/etc/fprobe/mem", "r");
    unsigned long long base = 0, sz = 0;
    if (!f || fscanf(f, "%lli %lli", &base, &sz) != 2) {
        fprintf(stderr, "zc_send: /etc/fprobe/mem が読めない\n");
        exit(1);
    }
    fclose(f);
    int fd = devmem ? open("/dev/mem", O_RDONLY | O_SYNC) : open("/dev/fprobe_ring", O_RDONLY);
    if (fd < 0) {
        perror(devmem ? "open /dev/mem" : "open /dev/fprobe_ring");
        exit(1);
    }
    void *p = mmap(NULL, sz, PROT_READ, MAP_SHARED, fd, devmem ? (off_t)base : 0);
    if (p == MAP_FAILED) {
        perror("mmap");
        exit(1);
    }
    close(fd);
    *size = sz;
    return p;
}

int main(int argc, char **argv)
{
    int port = 0, cpu = -1, zc = 0, opt;
    double secs = 10;
    const char *src = "mem", *layout = "plain";
    size_t seg = 4096, call = 256 * 1024, shift = 0, window = 8u << 20, once = 0;
    while ((opt = getopt(argc, argv, "p:c:t:zm:l:s:b:o:w:n:")) != -1) {
        switch (opt) {
        case 'p': port = atoi(optarg); break;
        case 'c': cpu = atoi(optarg); break;
        case 't': secs = atof(optarg); break;
        case 'z': zc = 1; break;
        case 'm': src = optarg; break;
        case 'l': layout = optarg; break;
        case 's': seg = strtoul(optarg, 0, 0); break;
        case 'b': call = strtoul(optarg, 0, 0); break;
        case 'o': shift = strtoul(optarg, 0, 0); break;
        case 'w': window = strtoul(optarg, 0, 0); break;
        case 'n': once = strtoul(optarg, 0, 0); break;
        default: fprintf(stderr, "usage: see the comment at the top of zc_send.c\n"); return 2;
        }
    }
    int io = !strcmp(layout, "iov");
    if (!port || (!io && strcmp(layout, "plain")) || seg == 0 || call == 0) {
        fprintf(stderr, "zc_send: -p と正しい -l・-s・-b が要る\n");
        return 2;
    }
    if (cpu >= 0) {
        cpu_set_t s;
        CPU_ZERO(&s);
        CPU_SET(cpu, &s);
        sched_setaffinity(0, sizeof s, &s);
    }

    uint8_t *buf;
    size_t have;
    if (!strcmp(src, "mem")) {
        have = window + shift;
        buf = aligned_alloc(4096, (have + 4095) / 4096 * 4096);
        memset(buf, 0x5a, have);
    } else if (!strcmp(src, "ring") || !strcmp(src, "devmem")) {
        buf = map_ring(!strcmp(src, "devmem"), &have);
    } else {
        fprintf(stderr, "zc_send: -m は mem・ring・devmem\n");
        return 2;
    }
    if (shift + window > have || (once && shift + once > have)) {
        fprintf(stderr, "zc_send: -o + -w（または -n）が中身の大きさ %zu を超える\n", have);
        return 2;
    }
    uint8_t *data = buf + shift;
    size_t per = io ? call / seg : 1;                    /* iov の 1 回の組の数 */
    if (per < 1)
        per = 1;
    if (per > MAX_IOV / 2)
        per = MAX_IOV / 2;
    if (io && seg > window) {
        fprintf(stderr, "zc_send: -s が -w より大きい\n");
        return 2;
    }
    static uint8_t hdr[16 * 1024];                       /* 見出し 16 B ×1024（中身は固定。受け手は見出しを見ない） */
    for (int i = 0; i < 1024; i++) {
        uint64_t lo = 1u | (uint64_t)(seg / 16) << 16;
        memcpy(hdr + 16 * i, &lo, 8);
        memcpy(hdr + 16 * i + 8, "FPRBSEG", 8);
    }

    int l = socket(AF_INET, SOCK_STREAM, 0), one = 1, big = 4 << 20;
    setsockopt(l, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
    struct sockaddr_in a = { .sin_family = AF_INET, .sin_port = htons(port) };
    if (bind(l, (void *)&a, sizeof a) || listen(l, 1)) {
        perror("bind");
        return 1;
    }
    int fd = accept(l, 0, 0);
    close(l);
    setsockopt(fd, SOL_SOCKET, SO_SNDBUF, &big, sizeof big);
    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);
    if (zc && setsockopt(fd, SOL_SOCKET, SO_ZEROCOPY, &one, sizeof one)) {
        perror("SO_ZEROCOPY");
        return 1;
    }

    static struct iovec iov[MAX_IOV], v[MAX_IOV];
    struct rusage r0, r1;
    getrusage(RUSAGE_SELF, &r0);
    double t0 = now(), t = t0;
    long long sent = 0, calls = 0;
    long enobufs = 0;
    size_t off = 0;
    int err = 0;
    while (once ? (size_t)sent < once : (t = now()) - t0 < secs) {
        int k = 0;
        if (once) {
            iov[0].iov_base = data + sent;
            iov[0].iov_len = once - (size_t)sent < call ? once - (size_t)sent : call;
            k = 1;
        } else if (io) {
            for (size_t s = 0; s < per; s++) {
                if (off + seg > window)
                    off = 0;
                iov[k].iov_base = hdr + 16 * (s % 1024);
                iov[k++].iov_len = 16;
                iov[k].iov_base = data + off;
                iov[k++].iov_len = seg;
                off += seg;
            }
        } else {
            if (off + call > window)
                off = 0;
            iov[0].iov_base = data + off;
            iov[0].iov_len = call;
            k = 1;
            off += call;
        }
        /* 全部送りきる（一部だけ送れたら残りを続けて送る） */
        int ki = 0;
        size_t skip = 0;
        while (ki < k) {
            int m = 0;
            for (int j = ki; j < k; j++, m++) {
                v[m] = iov[j];
                if (j == ki) {
                    v[m].iov_base = (char *)v[m].iov_base + skip;
                    v[m].iov_len -= skip;
                }
            }
            struct msghdr mh = { .msg_iov = v, .msg_iovlen = m };
            ssize_t n = sendmsg(fd, &mh, (zc ? MSG_ZEROCOPY : 0) | MSG_NOSIGNAL);
            if (n < 0) {
                if (errno == ENOBUFS) {                  /* 完了通知が optmem_max で詰まった: 読んでから送り直す */
                    enobufs++;
                    struct pollfd p = { fd, 0, 0 };
                    poll(&p, 1, 1);
                    reap(fd);
                    continue;
                }
                err = errno;
                goto out;
            }
            calls++;
            sent += n;
            size_t left = (size_t)n;
            while (ki < k) {
                size_t rem = iov[ki].iov_len - skip;
                if (left < rem) {
                    skip += left;
                    break;
                }
                left -= rem;
                skip = 0;
                ki++;
            }
        }
        if (zc)
            reap(fd);
    }
out:
    t = now();
    if (zc) {                                            /* 送り終えた後、残りの完了通知を最大 2 秒待って数える */
        shutdown(fd, SHUT_WR);
        double w0 = now();
        while (completions < calls && now() - w0 < 2) {
            struct pollfd p = { fd, 0, 0 };
            poll(&p, 1, 10);
            reap(fd);
        }
    }
    getrusage(RUSAGE_SELF, &r1);
    double mb = sent / 1e6;
    printf("src=%s layout=%s zc=%d seg=%zu per=%zu shift=%zu cpu=%d bytes=%lld secs=%.2f MBps=%.1f calls=%lld "
           "completions=%lld copied=%lld enobufs=%ld err=%s user_ms_per_MB=%.2f sys_ms_per_MB=%.2f\n",
           src, layout, zc, io ? seg : 0, per, shift, cpu, sent, t - t0, mb / (t - t0), calls, completions, copied,
           enobufs, err ? strerrorname_np(err) : "none",
           mb > 0 ? (cpu_ms(&r1.ru_utime) - cpu_ms(&r0.ru_utime)) / mb : 0,
           mb > 0 ? (cpu_ms(&r1.ru_stime) - cpu_ms(&r0.ru_stime)) / mb : 0);
    close(fd);
    return 0;
}
