/* Zynq DUT のアプリ: PL のサンプル生成 + FIFO を /dev/mem 越しに読む、fprobe が外から観測される側の Linux プロセス。
 * (docs/superpowers/specs/2026-10-24-zynq-dut-environment-design.md)
 *
 *   dut_app [--period <clk>] [--delay-us <n>] [--fault <n>] [--seconds <n>]
 *
 * 1 語ごとに dut_consume() を呼ぶ（Linux のプローブ linux.probe.add をこの関数に付ける。引数 = 取り出した語）。
 * --delay-us で 1 語ごとに待つと読み手が遅れ、FIFO が溢れる（PL の dbg[2] と OVERFLOWS で分かる）。
 * --fault N は PL が N 個に 1 個の生成を黙って捨てる（取り出した番号が飛ぶ）。
 * 終了時に "generated=%u consumed=%u overflows=%u" を 1 行出す（生成と溢れはこの実行の間の増分。PL のカウンタはクリアされない）。
 * ARM 命令でビルドする（-marm。uprobe の将来の制約を避ける）。
 * SPDX-License-Identifier: AGPL-3.0-only */
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

#include "dut_regs.h"

static volatile uint32_t *regs;
static uint32_t consumed;
static uint32_t last_seen;

__attribute__((noinline)) void dut_consume(uint32_t value)
{
    last_seen = value;      /* 受け取った番号（ptrace / uprobe のプローブがこの引数を読む） */
    consumed++;
}

static double now_s(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec * 1e-9;
}

static int usage(const char *argv0)
{
    fprintf(stderr, "usage: %s [--period <clk>] [--delay-us <n>] [--fault <n>] [--seconds <n>]\n", argv0);
    return 2;
}

/* 引数の整数。数字でない・範囲外は使い方を出して断る（strtoul は黙って 0 や飽和した値にする） */
static int parse_u32(const char *text, uint32_t max, uint32_t *out)
{
    char *end;
    errno = 0;
    unsigned long v = strtoul(text, &end, 0);
    if (end == text || *end || errno || v > max)
        return -1;
    *out = (uint32_t)v;
    return 0;
}

int main(int argc, char **argv)
{
    uint32_t period = 1000, fault = 0, delay_us = 0;
    double seconds = 5;
    for (int i = 1; i < argc; i++) {
        if (i + 1 >= argc)
            return usage(argv[0]);
        const char *name = argv[i], *value = argv[i + 1];
        i++;
        if (strcmp(name, "--period") == 0) {
            if (parse_u32(value, 0xFFFFFFFFu, &period) || period == 0)
                return usage(argv[0]);
        } else if (strcmp(name, "--delay-us") == 0) {
            if (parse_u32(value, 0xFFFFFFFFu, &delay_us))
                return usage(argv[0]);
        } else if (strcmp(name, "--fault") == 0) {
            if (parse_u32(value, 0xFFFFFFu, &fault))        /* DUT_REG_FAULT の N は 24 ビット（bits[31:8]） */
                return usage(argv[0]);
        } else if (strcmp(name, "--seconds") == 0) {
            char *end;
            seconds = strtod(value, &end);
            if (end == value || *end || !(seconds >= 0))
                return usage(argv[0]);
        } else {
            return usage(argv[0]);
        }
    }

    int mem = open("/dev/mem", O_RDWR | O_SYNC);
    if (mem < 0) {
        perror("dut_app: /dev/mem");
        return 1;
    }
    void *map = mmap(NULL, DUT_MAP_BYTES, PROT_READ | PROT_WRITE, MAP_SHARED, mem, DUT_BASE_ADDR);
    if (map == MAP_FAILED) {
        perror("dut_app: mmap");
        return 1;
    }
    regs = map;
    if (regs[DUT_REG_ID] != DUT_ID_VALUE) {      /* PL に DUT の回路が載っていない（読んだ値を出して断る） */
        fprintf(stderr, "dut_app: ID が違う（0x%08x）。zynq_dut の回路を書いたか確かめる\n", regs[DUT_REG_ID]);
        return 3;
    }

    regs[DUT_REG_CTRL] = 0;
    uint32_t generated0 = regs[DUT_REG_GENERATED], overflows0 = regs[DUT_REG_OVERFLOWS];
    while (regs[DUT_REG_DATA] != DUT_EMPTY)      /* 前の実行の残りを捨てる（取り出すだけ。dut_consume には渡さない） */
        ;
    regs[DUT_REG_PERIOD] = period;
    regs[DUT_REG_FAULT] = fault ? ((fault << 8) | 1u) : 0;
    regs[DUT_REG_CTRL] = 1;

    double end = now_s() + seconds;
    while (now_s() < end) {
        uint32_t v = regs[DUT_REG_DATA];
        if (v == DUT_EMPTY) {
            usleep(100);
            continue;
        }
        dut_consume(v);
        if (delay_us)
            usleep(delay_us);
    }

    regs[DUT_REG_CTRL] = 0;
    printf("generated=%u consumed=%u overflows=%u\n", regs[DUT_REG_GENERATED] - generated0, consumed,
           regs[DUT_REG_OVERFLOWS] - overflows0);
    return 0;
}
