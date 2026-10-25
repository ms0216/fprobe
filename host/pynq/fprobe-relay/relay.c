/* fprobe-relay の受け渡しの中身（OS に依存しない。PC の試験は host/tests/test_pynq_relay.py）。
 * SPDX-License-Identifier: AGPL-3.0-only
 */
#include "relay.h"

#include <stdlib.h>
#include <string.h>

int relay_dgram_ok(size_t len)
{
    return len >= 2 && len - 2 <= RELAY_MAX_PAYLOAD;
}

int relay_check_design(const struct pl_ops *pl)
{
    if (pl->rd(pl->ctx, REG_ID) != RELAY_LINK_ID)
        return -1;
    if (pl->rd(pl->ctx, REG_VERSION) != RELAY_LINK_VERSION)
        return -2;
    return 0;
}

int relay_from_pc(const struct pl_ops *pl, const uint8_t *dgram, size_t len, struct relay_stats *st)
{
    /* ch | seq | payload。ch と seq が無い・payload が長すぎるものは捨てる */
    if (!relay_dgram_ok(len)) {
        st->dropped_bad++;
        return -1;
    }
    if (pl->rd(pl->ctx, REG_RX_STATUS) & 1u)
        return 1;
    size_t frame_len = len - 1;                 /* ch + payload */
    for (size_t i = 0; i < frame_len; i += 4) {
        uint32_t word = 0;
        for (size_t k = 0; k < 4 && i + k < frame_len; k++) {
            /* フレームの i + k 番目のバイト: 0 は ch（dgram[0]）、1 以降は payload（dgram[2] から） */
            size_t j = i + k;
            uint8_t b = j == 0 ? dgram[0] : dgram[j + 1];
            word |= (uint32_t)b << (8 * k);
        }
        pl->wr(pl->ctx, REG_RX_BUF + (uint32_t)i, word);
    }
    pl->wr(pl->ctx, REG_RX_COMMIT, (uint32_t)frame_len);
    st->to_pl++;
    return 0;
}

size_t relay_to_pc(const struct pl_ops *pl, uint8_t *out, size_t cap, struct relay_stats *st)
{
    uint32_t n = pl->rd(pl->ctx, REG_TX_HEAD) & 0x3FFu;
    if (n == 0)
        return 0;
    if (n > RELAY_MAX_PAYLOAD + 1 || cap < (size_t)n + 1) {
        /* PL が範囲外の長さを出した（起きないはず）。読まずに捨てて先へ進める */
        pl->wr(pl->ctx, REG_TX_DONE, 1);
        st->dropped_bad++;
        return 0;
    }
    for (uint32_t i = 0; i < n; i += 4) {
        uint32_t word = pl->rd(pl->ctx, REG_TX_BUF + i);
        for (uint32_t k = 0; k < 4 && i + k < n; k++) {
            uint32_t j = i + k;
            uint8_t b = (uint8_t)(word >> (8 * k));
            if (j == 0)
                out[0] = b;
            else
                out[j + 1] = b;
        }
    }
    pl->wr(pl->ctx, REG_TX_DONE, 1);
    out[1] = st->seq++;
    st->from_pl++;
    return (size_t)n + 1;
}

int64_t relay_ns(int64_t sec, int64_t nsec)
{
    return sec * 1000000000 + nsec;
}

void relay_peer_update(struct relay_peer *peer, uint32_t addr, uint16_t port)
{
    peer->addr = addr;
    peer->port = port;
    peer->known = 1;
}

int relay_region_free(const char *iomem, uint32_t base, uint32_t size)
{
    if (base == 0 || size == 0 || (uint64_t)base + size > 0x100000000ull)
        return 0;
    uint64_t end = (uint64_t)base + size;               /* 含まない */
    for (const char *line = iomem; *line; ) {
        const char *next = strchr(line, '\n');
        if (line[0] != ' ' && strstr(line, "System RAM") && (!next || strstr(line, "System RAM") < next)) {
            char *dash;
            uint64_t lo = strtoull(line, &dash, 16);
            uint64_t hi = strtoull(dash + 1, NULL, 16) + 1;  /* /proc/iomem は終わりを含む */
            if (lo < end && base < hi)
                return 0;
        }
        if (!next)
            break;
        line = next + 1;
    }
    return 1;
}

int relay_deep_region_ok(const char *iomem, uint32_t base, uint32_t size, uint32_t mem_bytes)
{
    if (mem_bytes == 0)
        return 1;                                       /* 深いバッファの無い回路: MEM_BASE を書かない */
    if (base & 0xFu)
        return -1;                                      /* 拍（16 バイト）の境目でない（AXI のバーストが 4 KB をまたぐ） */
    if (size < mem_bytes)
        return -2;                                      /* 予約した大きさより PL が多く使う */
    if ((uint64_t)base + mem_bytes > RELAY_DDR_END)
        return -3;                                      /* DDR の外 */
    if (!relay_region_free(iomem, base, mem_bytes))
        return -4;                                      /* Linux のメモリと重なる */
    return 0;
}
