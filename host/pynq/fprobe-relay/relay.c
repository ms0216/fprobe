/* fprobe-relay の受け渡しの中身（OS に依存しない。PC の試験は host/tests/test_pynq_relay.py）。
 * SPDX-License-Identifier: AGPL-3.0-only
 */
#include "relay.h"

#include <dirent.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#if defined(__ARM_NEON)
#include <arm_neon.h>
#endif

int relay_dgram_ok(size_t len)
{
    return len >= 2 && len - 2 <= RELAY_MAX_PAYLOAD;
}

int relay_dgram_sets_peer(const uint8_t *dgram, size_t len)
{
    return relay_dgram_ok(len) && dgram[0] != RELAY_CH_STATUS;
}

int relay_check_design(const struct pl_ops *pl)
{
    if (pl->rd(pl->ctx, REG_ID) != RELAY_LINK_ID)
        return -1;
    uint32_t version = pl->rd(pl->ctx, REG_VERSION);
    if (version < RELAY_LINK_VERSION_MIN || version > RELAY_LINK_VERSION_MAX)
        return -2;
    return 0;
}

int relay_from_pc(const struct pl_ops *pl, const uint8_t *dgram, size_t len, struct relay_stats *st)
{
    if (len >= 1 && dgram[0] == RELAY_CH_BUNDLE) {      /* まとめ送りの要求: ch | seq | 上限 u16 LE。PL には渡さない */
        if (len != 4) {
            st->dropped_bad++;
            return -1;
        }
        st->bundle_max = (uint16_t)(dgram[2] | dgram[3] << 8);
        return 0;
    }
    if (len >= 1 && dgram[0] == RELAY_CH_STATUS) {      /* 中継の状態の問い合わせ: ch | seq。PL には渡さない */
        if (len != 2) {
            st->dropped_bad++;
            return -1;
        }
        return 2;
    }
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

/* PL の TX_BUF の n バイト（ch + payload）を fr に読み、TX_DONE を書く。fr[0] = ch、fr[1..n-1] = payload */
static void read_frame(const struct pl_ops *pl, uint32_t n, uint8_t *fr)
{
    for (uint32_t i = 0; i < n; i += 4) {
        uint32_t word = pl->rd(pl->ctx, REG_TX_BUF + i);
        for (uint32_t k = 0; k < 4 && i + k < n; k++)
            fr[i + k] = (uint8_t)(word >> (8 * k));
    }
    pl->wr(pl->ctx, REG_TX_DONE, 1);
}

/* バイト列のチャンネル（SWO 2・UART0 3・STREAM 4）か。CONTROL・DAP は遅れが応答に出るので、まとめない */
static int bundleable(uint8_t ch)
{
    return ch >= 2 && ch <= 4;
}

#define BUNDLE_OVERHEAD 2u       /* RELAY_CH_BUNDLE | seq */
#define BUNDLE_FRAME_HEAD 3u     /* ch | 長さ(2) */

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
    uint8_t fr[RELAY_MAX_PAYLOAD + 1];
    read_frame(pl, n, fr);
    out[0] = fr[0];
    out[1] = st->seq++;
    memcpy(out + 2, fr + 1, n - 1);
    st->from_pl++;
    size_t limit = st->bundle_max < cap ? st->bundle_max : cap;
    if (limit == 0 || !bundleable(fr[0]))
        return (size_t)n + 1;
    /* 次のフレームも溜まっていて、まとめられ、入れ物に収まるなら、まとめる */
    size_t used = BUNDLE_OVERHEAD + BUNDLE_FRAME_HEAD + (n - 1);
    uint8_t first_ch = fr[0];
    uint16_t first_len = (uint16_t)(n - 1);
    int bundled = 0;
    for (;;) {
        /* TX_DONE の直後は、PL が次のフレームを窓に用意するまで TX_HEAD が 0（実機で確認）。少しだけ待つ */
        uint32_t m = 0;
        for (int w = 0; w < RELAY_BUNDLE_POLLS && m == 0; w++)
            m = pl->rd(pl->ctx, REG_TX_HEAD) & 0x3FFu;
        if (m == 0 || m > RELAY_MAX_PAYLOAD + 1 || used + BUNDLE_FRAME_HEAD + (m - 1) > limit)
            break;
        if (!bundleable((uint8_t)pl->rd(pl->ctx, REG_TX_BUF)))
            break;
        if (!bundled) {                              /* 先頭のフレームを、まとめた形（ch | 長さ | payload）に置き直す */
            memmove(out + BUNDLE_OVERHEAD + BUNDLE_FRAME_HEAD, out + 2, first_len);
            out[BUNDLE_OVERHEAD] = first_ch;
            out[BUNDLE_OVERHEAD + 1] = (uint8_t)first_len;
            out[BUNDLE_OVERHEAD + 2] = (uint8_t)(first_len >> 8);
            out[0] = RELAY_CH_BUNDLE;                /* out[1] は先頭のフレームの seq のまま */
            bundled = 1;
        }
        read_frame(pl, m, fr);
        out[used] = fr[0];
        out[used + 1] = (uint8_t)(m - 1);
        out[used + 2] = (uint8_t)((m - 1) >> 8);
        memcpy(out + used + BUNDLE_FRAME_HEAD, fr + 1, m - 1);
        used += BUNDLE_FRAME_HEAD + (m - 1);
        st->seq++;
        st->from_pl++;
    }
    return bundled ? used : (size_t)n + 1;
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

/* path のファイルを buf に最大 cap バイト読む。戻り値は読んだバイト数（開けなければ -1） */
static long read_file(const char *path, uint8_t *buf, size_t cap)
{
    FILE *f = fopen(path, "rb");
    if (!f)
        return -1;
    size_t n = fread(buf, 1, cap, f);
    fclose(f);
    return (long)n;
}

static uint32_t be32(const uint8_t *p)
{
    return (uint32_t)p[0] << 24 | (uint32_t)p[1] << 16 | (uint32_t)p[2] << 8 | p[3];
}

int relay_region_reserved(const char *dt_dir, uint32_t base, uint32_t size)
{
    if (base == 0 || size == 0)
        return 0;
    char path[512];
    uint8_t cells[4], reg[256];
    snprintf(path, sizeof path, "%s/#address-cells", dt_dir);
    if (read_file(path, cells, 4) != 4 || be32(cells) != 1)
        return 0;                                        /* 1 セル幅の番地・大きさだけ読む（2 セルは読み違えるので認めない） */
    snprintf(path, sizeof path, "%s/#size-cells", dt_dir);
    if (read_file(path, cells, 4) != 4 || be32(cells) != 1)
        return 0;
    DIR *d = opendir(dt_dir);
    if (!d)
        return 0;
    uint64_t end = (uint64_t)base + size;
    int found = 0;
    struct dirent *e;
    while (!found && (e = readdir(d)) != NULL) {
        if (e->d_name[0] == '.' || e->d_name[0] == '#' || strlen(e->d_name) > 200)
            continue;
        /* no-map（写像が無い）・reusable（Linux が借りる）・compatible（共有プールなど、ドライバが使う）は、Linux が触りうる */
        static const char *const not_free[] = { "no-map", "reusable", "compatible" };
        int touched = 0;
        for (size_t i = 0; i < sizeof not_free / sizeof *not_free; i++) {
            FILE *f;
            snprintf(path, sizeof path, "%s/%s/%s", dt_dir, e->d_name, not_free[i]);
            if ((f = fopen(path, "rb")) != NULL) {
                fclose(f);
                touched = 1;
            }
        }
        snprintf(path, sizeof path, "%s/%s/reg", dt_dir, e->d_name);
        long n = touched ? -1 : read_file(path, reg, sizeof reg);
        if (n <= 0 || n % 8 != 0)
            continue;                                    /* reg の無いもの（ただの特性）・組（番地 + 大きさ）の倍数でないもの */
        for (long k = 0; k < n; k += 8) {
            uint64_t lo = be32(reg + k), hi = lo + be32(reg + k + 4);
            if (lo <= base && end <= hi)
                found = 1;
        }
    }
    closedir(d);
    return found;
}

int relay_deep_region_ok(const char *iomem, uint32_t base, uint32_t size, uint32_t mem_bytes, int reserved)
{
    if (mem_bytes == 0)
        return 1;                                       /* 深いバッファの無い回路: MEM_BASE を書かない */
    if (base & 0xFu)
        return -1;                                      /* 拍（16 バイト）の境目でない（AXI のバーストが 4 KB をまたぐ） */
    if (size < mem_bytes)
        return -2;                                      /* 予約した大きさより PL が多く使う */
    if ((uint64_t)base + mem_bytes > RELAY_DDR_END)
        return -3;                                      /* DDR の外 */
    if (!reserved && !relay_region_free(iomem, base, mem_bytes))
        return -4;                                      /* Linux のメモリと重なる（reserved-memory に入っていない） */
    return 0;
}

int relay_irq_of(const char *text, const char *ifname)
{
    size_t k = strlen(ifname);
    for (const char *line = text; *line; ) {
        const char *next = strchr(line, '\n');
        size_t len = next ? (size_t)(next - line) : strlen(line);
        while (len && (line[len - 1] == ' ' || line[len - 1] == '\r'))
            len--;
        int num;
        if (k && len > k && memcmp(line + len - k, ifname, k) == 0 && (line[len - k - 1] == ' ' || line[len - k - 1] == '\t')
            && sscanf(line, " %d:", &num) == 1)
            return num;
        if (!next)
            break;
        line = next + 1;
    }
    return -1;
}

int relay_single_cpu(const char *list)
{
    size_t n = strlen(list);
    while (n && (list[n - 1] == '\n' || list[n - 1] == ' '))
        n--;
    if (n == 0 || n > 4)
        return -1;
    int cpu = 0;
    for (size_t i = 0; i < n; i++) {
        if (list[i] < '0' || list[i] > '9')
            return -1;                                   /* "0-1"・"0,1" などは 1 つに決まらない */
        cpu = cpu * 10 + (list[i] - '0');
    }
    return cpu;
}

size_t relay_ring_size(void)
{
    return sizeof(struct relay_ring);
}

uint32_t relay_ring_slots(void)
{
    return RELAY_RING_SLOTS;
}

size_t relay_ring_slot_bytes(void)
{
    return RELAY_RING_SLOT_BYTES;
}

void relay_ring_init(struct relay_ring *r)
{
    r->head = 0;
    r->tail = 0;
}

uint8_t *relay_ring_slot(struct relay_ring *r)
{
    uint32_t head = __atomic_load_n(&r->head, __ATOMIC_RELAXED);
    uint32_t tail = __atomic_load_n(&r->tail, __ATOMIC_ACQUIRE);
    return head - tail >= RELAY_RING_SLOTS ? NULL : r->data[head % RELAY_RING_SLOTS];
}

void relay_ring_commit(struct relay_ring *r, size_t len)
{
    uint32_t head = __atomic_load_n(&r->head, __ATOMIC_RELAXED);
    r->len[head % RELAY_RING_SLOTS] = (uint32_t)len;
    __atomic_store_n(&r->head, head + 1, __ATOMIC_RELEASE);
}

const uint8_t *relay_ring_peek(struct relay_ring *r, size_t *len)
{
    uint32_t tail = __atomic_load_n(&r->tail, __ATOMIC_RELAXED);
    uint32_t head = __atomic_load_n(&r->head, __ATOMIC_ACQUIRE);
    if (head == tail)
        return NULL;
    *len = r->len[tail % RELAY_RING_SLOTS];
    return r->data[tail % RELAY_RING_SLOTS];
}

int relay_sysfs_set(const char *path, const char *value, char *old, size_t old_cap)
{
    FILE *f = fopen(path, "r");
    if (!f)
        return -1;
    size_t n = fread(old, 1, old_cap ? old_cap - 1 : 0, f);
    int more = fgetc(f);                                 /* 入りきらない値を、切れたまま「元の値」にしない */
    fclose(f);
    if (old_cap == 0 || (more != EOF && more != '\n'))
        return -1;
    old[n] = 0;
    while (n > 0 && (old[n - 1] == '\n' || old[n - 1] == '\r'))
        old[--n] = 0;
    f = fopen(path, "w");
    if (!f)
        return -1;
    int ok = fputs(value, f) >= 0;
    return fclose(f) == 0 && ok ? 0 : -1;
}

#define SLCR_LOCK_REG 0x004u
#define SLCR_UNLOCK_REG 0x008u
#define SLCR_LOCKSTA_REG 0x00Cu

void relay_slcr_write(const struct pl_ops *slcr, uint32_t reg, uint32_t value)
{
    int locked = (int)(slcr->rd(slcr->ctx, SLCR_LOCKSTA_REG) & 1u);
    if (locked)
        slcr->wr(slcr->ctx, SLCR_UNLOCK_REG, 0xDF0D);
    slcr->wr(slcr->ctx, reg, value);
    if (locked)
        slcr->wr(slcr->ctx, SLCR_LOCK_REG, 0x767B);
}

int relay_ring_peek_many(struct relay_ring *r, const uint8_t **base, size_t *len, int max)
{
    uint32_t tail = __atomic_load_n(&r->tail, __ATOMIC_RELAXED);
    uint32_t head = __atomic_load_n(&r->head, __ATOMIC_ACQUIRE);
    int n = 0;
    for (; n < max && tail + (uint32_t)n != head; n++) {
        base[n] = r->data[(tail + (uint32_t)n) % RELAY_RING_SLOTS];
        len[n] = r->len[(tail + (uint32_t)n) % RELAY_RING_SLOTS];
    }
    return n;
}

void relay_ring_pop(struct relay_ring *r)
{
    uint32_t tail = __atomic_load_n(&r->tail, __ATOMIC_RELAXED);
    __atomic_store_n(&r->tail, tail + 1, __ATOMIC_RELEASE);
}

/* ---- PS が DDR のリングを直接読むストリーム ---------------------------------------------------------------------- */

#define BEAT_BYTES 16u                 /* STREAM_SEG_*・CH_STREAM は window_regs.h（生成物） */

size_t relay_ps_size(void)
{
    return sizeof(struct relay_ps);
}

int relay_ps_init(struct relay_ps *ps, uint32_t mem_bytes)
{
    uint32_t beats = mem_bytes / BEAT_BYTES;
    if (mem_bytes == 0 || mem_bytes % BEAT_BYTES != 0 || (beats & (beats - 1)) != 0 || beats < 16)
        return -1;
    memset(ps, 0, sizeof *ps);
    ps->ev_ring = beats / 16;                           /* la_deep_buffer: EV_SIZE = 2^(ADDR_BITS - 4)、LA_SIZE = 残り */
    ps->la_ring = beats - ps->ev_ring;
    ps->la_first_seen_ns = ps->ev_first_seen_ns = -1;
    return 0;
}

uint32_t relay_ps_bad_la(const struct relay_ps *ps)
{
    return ps->bad_la;
}

uint32_t relay_ps_bad_ev(const struct relay_ps *ps)
{
    return ps->bad_ev;
}

/* 位置を窓に書く（中継の位置の書きは全部ここを通る。A6）。epoch はその値の流れの番号。窓の POS_TAG が違えば先に書く
 * （窓は書きを 1 つずつ順に処理するので、POS_TAG → 位置は割り込まれない）。今の流れの番号でない書きは窓が捨てる */
static void ps_put(struct relay_ps *ps, const struct pl_ops *pl, uint32_t epoch, uint32_t reg, uint32_t value)
{
    if (!ps->tag_valid || ps->tag != epoch) {
        pl->wr(pl->ctx, REG_POS_TAG, epoch);
        ps->tag = epoch;
        ps->tag_valid = 1;
    }
    pl->wr(pl->ctx, reg, value);
}

/* 見出しの 1 拍: la_deep_buffer の hdr_lo（種類 | 拍数 << 16 | 位置 << 32）+ SEG_MAGIC "FPRBSEG\0" */
static void ps_header(uint8_t *p, uint32_t kind, uint32_t count, uint32_t pos)
{
    uint64_t lo = (uint64_t)kind | (uint64_t)count << 16 | (uint64_t)pos << 32;
    for (int i = 0; i < 8; i++)
        p[i] = (uint8_t)(lo >> (8 * i));
    memcpy(p + 8, "FPRBSEG", 7);
    p[15] = 0;
}

/* DDR の領域（/dev/mem の写し。キャッシュ無し）から n バイト（16 の倍数）を写す。ARM（NEON あり）は 64 バイトずつ
 * 128 ビットで読む: 実測（PYNQ-Z2、PL が動いていない間）で glibc の memcpy は 68 MB/s、NEON の 64 バイトずつは 125 MB/s。
 * 領域の番地は 16 バイトに揃っている（拍の単位）。キャッシュの無い領域への揃っていない読みは、アライメントの誤りになる */
static void copy_from_device(uint8_t *dst, const uint8_t *src, size_t n)
{
#if defined(__ARM_NEON)
    size_t i = 0;
    for (; i + 64 <= n; i += 64) {
        uint32x4_t a = vld1q_u32((const uint32_t *)(src + i));
        uint32x4_t b = vld1q_u32((const uint32_t *)(src + i + 16));
        uint32x4_t c = vld1q_u32((const uint32_t *)(src + i + 32));
        uint32x4_t d = vld1q_u32((const uint32_t *)(src + i + 48));
        vst1q_u32((uint32_t *)(dst + i), a);
        vst1q_u32((uint32_t *)(dst + i + 16), b);
        vst1q_u32((uint32_t *)(dst + i + 32), c);
        vst1q_u32((uint32_t *)(dst + i + 48), d);
    }
    for (; i + 16 <= n; i += 16)
        vst1q_u32((uint32_t *)(dst + i), vld1q_u32((const uint32_t *)(src + i)));
#else
    memcpy(dst, src, n);
#endif
}

/* リング（ddr の ring_base 拍目から ring 拍）の pos 拍目から n 拍を、折り返しを含めて dst に写す */
static void ps_copy(uint8_t *dst, const uint8_t *ddr, uint32_t ring_base, uint32_t ring, uint32_t pos, uint32_t n)
{
    uint32_t first = n < ring - pos ? n : ring - pos;
    copy_from_device(dst, ddr + (size_t)(ring_base + pos) * BEAT_BYTES, (size_t)first * BEAT_BYTES);
    if (n > first)
        copy_from_device(dst + (size_t)first * BEAT_BYTES, ddr + (size_t)ring_base * BEAT_BYTES,
                         (size_t)(n - first) * BEAT_BYTES);
}

/* 位置のレジスタの読み（1 回の読みで、続けて何かたまりか写せるように、値を持ち回る） */
struct ps_snap {
    uint32_t la_written, ev_wpos, state;
};

static void ps_detach(struct relay_ps *ps)
{
    ps->attached = 0;
    ps->la_first_seen_ns = ps->ev_first_seen_ns = -1;
}

/* 流れ epoch に付く: POS_TAG を書き、窓の位置を読み、STREAM_EPOCH を読み直して（写し直す）同じ番号・CURRENT なら付く
 * （読んでいる間に次の流れが始まっていない = 読んだ位置はこの流れのもの）。ロジアナのリングの読み位置は、写しの LA_WPTR から
 * 未読の数だけ戻った所。1 = 付いた（*state は読み直した値）、0 = 付かない（次の周でやり直す） */
static int ps_attach(struct relay_ps *ps, const struct pl_ops *pl, uint32_t epoch, uint32_t *state)
{
    if (!ps->tag_valid || ps->tag != epoch) {
        pl->wr(pl->ctx, REG_POS_TAG, epoch);
        ps->tag = epoch;
        ps->tag_valid = 1;
    }
    uint32_t la = pl->rd(pl->ctx, REG_LA_CONSUMED);
    uint32_t ev = pl->rd(pl->ctx, REG_EV_CONSUMED);
    uint32_t beats = pl->rd(pl->ctx, REG_STREAM_BEATS);
    uint32_t st = pl->rd(pl->ctx, REG_STREAM_EPOCH);
    if ((st & WIN_EPOCH_MASK) != epoch || !(st & WIN_STATE_CURRENT) || !(st & WIN_STATE_ACTIVE))
        return 0;
    uint32_t written = pl->rd(pl->ctx, REG_LA_WRITTEN);
    uint32_t wptr = pl->rd(pl->ctx, REG_LA_WPTR);
    uint32_t unread = written - la;                      /* 総数は 32 ビットで折り返す */
    if (unread > ps->la_ring || wptr >= ps->la_ring) {
        ps->bad_la++;                                    /* ありえない位置（A7）: 付かない */
        return 0;
    }
    ps->epoch = epoch;
    ps->la_read = la;
    ps->ev_read = ev;
    ps->out_beats = beats;
    ps->la_rpos = (wptr + ps->la_ring - unread) % ps->la_ring;
    ps->ev_start = pl->rd(pl->ctx, REG_EV_START) & (ps->ev_ring - 1);
    ps->attached = 1;
    ps->la_first_seen_ns = ps->ev_first_seen_ns = -1;
    ps->last_kind = STREAM_SEG_EVENTS;                   /* 最初はロジアナの拍から */
    *state = st;
    return 1;
}

/* 位置のレジスタを読む（STREAM_EPOCH の読みで写し、写しを読む）。1 = 読めた、0 = 読めない（流れが無い・前の流れ・付けない） */
static int ps_snapshot(struct relay_ps *ps, const struct pl_ops *pl, struct ps_snap *s)
{
    uint32_t st = pl->rd(pl->ctx, REG_STREAM_EPOCH);
    if (!(st & WIN_STATE_ACTIVE)) {
        ps_detach(ps);
        ps->active = 0;
        return 0;
    }
    ps->active = 1;                                      /* 流れが生きている: 呼び手は休まず見る */
    if (!(st & WIN_STATE_CURRENT)) {                     /* ホストは次の流れを始めた（PL はまだ前の流れを公開している） */
        ps_detach(ps);
        return 0;
    }
    uint32_t epoch = st & WIN_EPOCH_MASK;
    if ((!ps->attached || epoch != ps->epoch) && !ps_attach(ps, pl, epoch, &st)) {
        ps_detach(ps);
        return 0;
    }
    s->state = st;
    s->la_written = pl->rd(pl->ctx, REG_LA_WRITTEN);
    s->ev_wpos = pl->rd(pl->ctx, REG_EV_WPOS);
    __atomic_thread_fence(__ATOMIC_ACQUIRE);              /* 位置のレジスタを読んだ後に、DDR の拍を読む（先に読まない） */
    return 1;
}

/* 次のかたまり（ps_next が決める。写さない） */
struct ps_chunk {
    uint32_t kind;                     /* STREAM_SEG_LA / STREAM_SEG_EVENTS */
    uint32_t ring_base, ring, pos;     /* 中身: 領域の ring_base 拍目からの ring 拍のリングの、pos 拍目から */
    uint32_t n;                        /* 中身の拍数 */
};

/* かたまりの作り方（道ごと）: 中身の上限、リングの終わりで切るか、出す前に溜める量（ロジアナ・イベント）と、溜まらないときに
 * 出すまでの時間。書き込みが止まっていれば、溜まるのを待たない */
struct ps_rule {
    uint32_t max;
    int no_wrap;
    uint32_t la_min, ev_min;
    int64_t age_ns;
};

/* 今の複写の道（PL の la_deep_buffer の窓経由の流れと同じ作り） */
static const struct ps_rule copy_rule = { RELAY_PS_CHUNK_MAX, 0, RELAY_PS_LA_MIN, 64, RELAY_PS_AGE_NS };
/* ゼロコピーの道: かたまりごとの見出し・呼び出し・完了通知の数を減らすため、1 かたまり分溜めてから出す（relay.h） */
static const struct ps_rule zc_rule = { RELAY_ZC_CHUNK_MAX, 1, RELAY_ZC_CHUNK_MAX, RELAY_ZC_CHUNK_MAX, RELAY_ZC_AGE_NS };

/* snap の値で次のかたまりを決め、見出しを head に書き、読み終えた位置（la_read・ev_read）と出す拍の総数（out_beats）を進める
 * （レジスタには書かない・中身は読まない）。1 = 決めた、0 = まだ無い。
 * snap は古くなっていてよい（PL は書き進めるだけなので、未読は多めに見積もっても少なめにしかならない） */
static int ps_next(struct relay_ps *ps, const struct ps_snap *snap, int64_t now_ns, const struct ps_rule *rule,
                   uint8_t *head, struct ps_chunk *c)
{
    uint32_t max = rule->max;
    int no_wrap = rule->no_wrap;
    /* 公開された位置は、応答の来ていない書きを除いてあるため、前の値より小さくなることがある。その場合は未読なしとして扱う
     * （正しい未読は、ロジアナはリング以下、イベントはリングの 3/4 未満。PL は未読がリングに迫ると書き込みを止める） */
    uint32_t la_unread = snap->la_written - ps->la_read;   /* 総数は 32 ビットで折り返す */
    if (la_unread > ps->la_ring) {                       /* ありえない（PL は未読がリングに迫ると止める）: 読まずに数える（A7） */
        ps->bad_la++;
        la_unread = 0;
    }
    uint32_t ev_mask = ps->ev_ring - 1;
    uint32_t ev_pos = (ps->ev_start + ps->ev_read) & ev_mask;
    uint32_t ev_unread = (snap->ev_wpos - ev_pos) & ev_mask;
    if (ev_unread > ps->ev_ring / 4 * 3) {               /* ありえない（PL はイベントの未読がリングの半分で止める） */
        ps->bad_ev++;
        ev_unread = 0;
    }

    int writing = (snap->state & WIN_STATE_WRITING) != 0;
    uint32_t ev_min = ps->ev_ring / 4 < rule->ev_min ? ps->ev_ring / 4 : rule->ev_min;
    if (!la_unread)
        ps->la_first_seen_ns = -1;
    else if (ps->la_first_seen_ns < 0)
        ps->la_first_seen_ns = now_ns;
    if (!ev_unread)
        ps->ev_first_seen_ns = -1;
    else if (ps->ev_first_seen_ns < 0)
        ps->ev_first_seen_ns = now_ns;
    int la_aged = ps->la_first_seen_ns >= 0 && now_ns - ps->la_first_seen_ns >= rule->age_ns;
    int ev_aged = ps->ev_first_seen_ns >= 0 && now_ns - ps->ev_first_seen_ns >= rule->age_ns;
    int la_ready = la_unread != 0 && (!writing || la_unread >= rule->la_min || la_aged);
    int ev_ready = ev_unread != 0 && (!writing || ev_unread >= ev_min || ev_aged);
    int take_events = ev_ready && (!la_ready || ps->last_kind == STREAM_SEG_LA);
    int take_la = !take_events && la_ready;
    if (!take_events && !take_la)
        return 0;

    uint32_t n;
    if (take_la) {
        c->kind = STREAM_SEG_LA;
        c->ring_base = 0;
        c->ring = ps->la_ring;
        c->pos = ps->la_rpos;
        n = la_unread < max ? la_unread : max;
        ps_header(head, STREAM_SEG_LA, 0, ps->la_read);
    } else {
        c->kind = STREAM_SEG_EVENTS;
        c->ring_base = ps->la_ring;
        c->ring = ps->ev_ring;
        c->pos = ev_pos;
        n = ev_unread < max ? ev_unread : max;
        ps_header(head, STREAM_SEG_EVENTS, 0, ev_pos);
    }
    if (no_wrap && n > c->ring - c->pos)
        n = c->ring - c->pos;
    head[2] = (uint8_t)n;                               /* 見出しの拍数（[31:16]） */
    head[3] = (uint8_t)(n >> 8);
    c->n = n;
    if (take_la) {
        ps->la_read += n;
        ps->la_rpos = (ps->la_rpos + n) % ps->la_ring;
    } else {
        ps->ev_read += n;
    }
    ps->last_kind = (int)c->kind;
    ps->out_beats += 1 + n;
    if (take_la)                                        /* 出した側の時刻だけを消す（残りは次に見たときから数える） */
        ps->la_first_seen_ns = -1;
    else
        ps->ev_first_seen_ns = -1;
    return 1;
}

/* snap の値で次のかたまりを stage に写す（今の複写の道。レジスタには書かない）。1 = 写した、0 = まだ無い */
static int ps_take(struct relay_ps *ps, const struct ps_snap *snap, const uint8_t *ddr, int64_t now_ns,
                   int *la_dirty, int *ev_dirty)
{
    struct ps_chunk c;
    if (!ps_next(ps, snap, now_ns, &copy_rule, ps->stage, &c))
        return 0;
    ps_copy(ps->stage + BEAT_BYTES, ddr, c.ring_base, c.ring, c.pos, c.n);
    if (c.kind == STREAM_SEG_LA)
        *la_dirty = 1;
    else
        *ev_dirty = 1;
    ps->stage_len = BEAT_BYTES + c.n * BEAT_BYTES;
    ps->stage_pos = 0;
    return 1;
}

/* 写し終えた位置を窓に返す。PL はこの拍を上書きしてよい。ホストの LaStream は stream_beats で「必ず届く量」を知る。
 * かたまり全体（見出しを含む）を、データを渡す前に書く（呼び手は、これが返ってから渡す） */
static void ps_publish(struct relay_ps *ps, const struct pl_ops *pl, int la_dirty, int ev_dirty)
{
    if (la_dirty)
        ps_put(ps, pl, ps->epoch, REG_LA_CONSUMED, ps->la_read);
    if (ev_dirty)
        ps_put(ps, pl, ps->epoch, REG_EV_CONSUMED, ps->ev_read);
    if (la_dirty || ev_dirty)
        ps_put(ps, pl, ps->epoch, REG_STREAM_BEATS, ps->out_beats);
}

/* 次のかたまりを stage に読み出す。1 = 読んだ、0 = まだ無い */
static int ps_fill(struct relay_ps *ps, const struct pl_ops *pl, const uint8_t *ddr, int64_t now_ns)
{
    struct ps_snap snap;
    int la_dirty = 0, ev_dirty = 0;
    if (!ps_snapshot(ps, pl, &snap) || !ps_take(ps, &snap, ddr, now_ns, &la_dirty, &ev_dirty))
        return 0;
    ps_publish(ps, pl, la_dirty, ev_dirty);
    return 1;
}

size_t relay_ps_step(struct relay_ps *ps, const struct pl_ops *pl, const uint8_t *ddr, uint8_t *out, size_t cap,
                     struct relay_stats *st, int64_t now_ns)
{
    if (ps->stage_pos >= ps->stage_len) {
        ps->stage_pos = ps->stage_len = 0;
        if (!ps_fill(ps, pl, ddr, now_ns))
            return 0;
    }
    size_t left = ps->stage_len - ps->stage_pos;
    size_t limit = st->bundle_max < cap ? st->bundle_max : cap;
    if (limit == 0) {                                    /* まとめ送りの要求が無い: 512 バイトまでの通常のフレーム */
        size_t n = left < RELAY_MAX_PAYLOAD ? left : RELAY_MAX_PAYLOAD;
        if (cap < n + 2)
            return 0;
        out[0] = CH_STREAM;
        out[1] = st->seq++;
        memcpy(out + 2, ps->stage + ps->stage_pos, n);
        ps->stage_pos += (uint32_t)n;
        st->from_pl++;
        return n + 2;
    }
    if (limit < 6)
        return 0;
    size_t n = left < limit - 5 ? left : limit - 5;      /* まとめた形のフレーム 1 つ: BUNDLE | seq | ch | 長さ(2) | 中身 */
    out[0] = RELAY_CH_BUNDLE;
    out[1] = st->seq++;
    out[2] = CH_STREAM;
    out[3] = (uint8_t)n;
    out[4] = (uint8_t)(n >> 8);
    memcpy(out + 5, ps->stage + ps->stage_pos, n);
    ps->stage_pos += (uint32_t)n;
    st->from_pl++;
    return n + 5;
}

int relay_ps_active(const struct relay_ps *ps)
{
    return ps->active;
}

size_t relay_ps_fill_bytes(struct relay_ps *ps, const struct pl_ops *pl, const uint8_t *ddr, uint8_t *out, size_t cap,
                           int64_t now_ns)
{
    size_t used = 0;
    struct ps_snap snap;
    int have = 0, la_dirty = 0, ev_dirty = 0;
    for (;;) {
        if (ps->stage_pos < ps->stage_len) {             /* データグラムで出しかけた残り（切り替えの途中）も、順番を守って先に */
            size_t left = ps->stage_len - ps->stage_pos;
            if (cap - used < left)
                break;
            memcpy(out + used, ps->stage + ps->stage_pos, left);
            used += left;
            ps->stage_pos = ps->stage_len;
            continue;
        }
        ps->stage_pos = ps->stage_len = 0;
        if (cap - used < sizeof ps->stage)               /* 入りきらないかもしれないかたまりは、読み出さない（読み終えた位置を進めるため） */
            break;
        if (!have && !(have = ps_snapshot(ps, pl, &snap)))   /* 位置のレジスタは 1 回だけ読み、続けて写す（MMIO は 1 回 約 2〜3 µs） */
            break;
        if (!ps_take(ps, &snap, ddr, now_ns, &la_dirty, &ev_dirty))
            break;
    }
    ps_publish(ps, pl, la_dirty, ev_dirty);              /* 写し終えた位置は、まとめて 1 回 */
    return used;
}

size_t relay_rd_size(void)
{
    return sizeof(struct relay_rd);
}

void relay_rd_init(struct relay_rd *rd)
{
    memset(rd, 0, sizeof *rd);
}

size_t relay_rd_fill(struct relay_rd *rd, const struct pl_ops *pl, const uint8_t *ddr, const struct relay_ps *geo,
                     uint8_t *out, size_t cap)
{
    uint32_t epoch = pl->rd(pl->ctx, REG_RD_EPOCH) & 0xFFu;   /* 読むと要求の組（RD_REGION・RD_FIRST・RD_COUNT）が写される */
    if (rd->busy && epoch != rd->epoch)                  /* 新しい要求: 読みかけを捨てる */
        rd->busy = 0;
    if (!rd->busy) {
        if (epoch == (pl->rd(pl->ctx, REG_RD_ACK) & 0xFFu))
            return 0;                                    /* 返事済み = 要求は無い */
        uint32_t region = pl->rd(pl->ctx, REG_RD_REGION) & 1u;   /* 写し: RD_EPOCH を読んだ時点の要求（混ざらない） */
        uint32_t first = pl->rd(pl->ctx, REG_RD_FIRST);
        uint32_t count = pl->rd(pl->ctx, REG_RD_COUNT);
        uint32_t ring = region ? geo->ev_ring : geo->la_ring;
        if (first >= ring || count == 0 || count > ring) {   /* 形の悪い要求: 返事だけ（データは出さない） */
            pl->wr(pl->ctx, REG_RD_ACK, epoch);
            return 0;
        }
        rd->busy = 1;
        rd->epoch = epoch;
        rd->region = region;
        rd->pos = first;
        rd->left = count;
    }
    uint32_t ring = rd->region ? geo->ev_ring : geo->la_ring;
    uint32_t n = (uint32_t)(cap / BEAT_BYTES);
    if (n > rd->left)
        n = rd->left;
    if (n == 0)
        return 0;
    ps_copy(out, ddr, rd->region ? geo->la_ring : 0, ring, rd->pos, n);
    rd->pos = (rd->pos + n) % ring;
    rd->left -= n;
    if (rd->left == 0) {
        rd->busy = 0;
        pl->wr(pl->ctx, REG_RD_ACK, rd->epoch);          /* 最後まで詰めた: PL の reading が下がる */
    }
    return (size_t)n * BEAT_BYTES;
}

/* ---- ゼロコピーの送信の道（relay.h の説明） ------------------------------------------------------------------------ */

size_t relay_zc_size(void)
{
    return sizeof(struct relay_zc);
}

uint32_t relay_zc_chunk_max(void)
{
    return RELAY_ZC_CHUNK_MAX;
}

void relay_zc_init(struct relay_zc *zc)
{
    memset(zc, 0, offsetof(struct relay_zc, d));         /* 表と入れ物は、使う前に書くので消さない */
    zc->state = RELAY_ZC_RUN;
}

int relay_zc_state(const struct relay_zc *zc)
{
    return zc->state;
}

void relay_zc_get_stats(const struct relay_zc *zc, struct relay_zc_stats *out)
{
    *out = zc->st;
}

static uint32_t desc_bytes(const struct relay_zc_desc *d)
{
    return BEAT_BYTES * (1 + d->n);
}

/* 流れ（epoch）が変わった・付き始め: 解放の起点を、PL の読み終えた位置（ps_snapshot が la_read・ev_read に読んだ値）にする */
static void zc_sync(struct relay_zc *zc, const struct relay_ps *ps)
{
    if (!ps->attached || (zc->synced && zc->epoch == ps->epoch))
        return;
    zc->epoch = ps->epoch;
    zc->synced = 1;
    zc->la_released = ps->la_read;
    zc->ev_released = ps->ev_read;
}

static void zc_held(struct relay_zc *zc, const struct relay_ps *ps)
{
    if (!zc->synced || zc->epoch != ps->epoch)
        return;
    zc->st.zc_la_held_beats = (uint32_t)(ps->la_read - zc->la_released);
    zc->st.zc_ev_held_beats = (uint32_t)(ps->ev_read - zc->ev_released);
    if (zc->st.zc_la_held_beats > zc->st.zc_la_held_max_beats)
        zc->st.zc_la_held_max_beats = zc->st.zc_la_held_beats;
    if (zc->st.zc_ev_held_beats > zc->st.zc_ev_held_max_beats)
        zc->st.zc_ev_held_max_beats = zc->st.zc_ev_held_beats;
}

/* 流れの前から順に、全バイトが「完了した呼び出し」か「普通に送った（写した）」で運ばれたかたまりを解放し、CONSUMED を書く。
 * 解放してよい位置: 完了を待つ呼び出しがあれば、その最も古いものの始まり。無ければ、カーネルが受け取った所まで */
static int zc_release(struct relay_zc *zc, struct relay_ps *ps, const struct pl_ops *pl)
{
    uint64_t limit = zc->c_tail != zc->c_head ? zc->c[zc->c_tail % RELAY_ZC_CALLS].start_off : zc->sent_off;
    int la = 0, ev = 0, moved = 0;
    uint32_t la_end = 0, ev_end = 0;
    while (zc->d_tail != zc->d_send && zc->d[zc->d_tail % RELAY_ZC_DESCS].end_off <= limit) {
        const struct relay_zc_desc *d = &zc->d[zc->d_tail % RELAY_ZC_DESCS];
        if (zc->synced && d->epoch == zc->epoch) {       /* 前の流れのかたまりは、今の流れの位置を動かさない */
            if (d->kind == STREAM_SEG_LA) {
                la_end = d->end_read;
                la = 1;
            } else {
                ev_end = d->end_read;
                ev = 1;
            }
        }
        zc->d_tail++;
        moved = 1;
    }
    /* 位置は、その値の流れの番号を添えて書く（新しい流れが始まっていれば窓が捨てる）。中継がもう次の流れに付いていると分かって
     * いれば書かない（窓の STALE_WRITES を増やさない） */
    if ((la || ev) && ps->attached && ps->epoch == zc->epoch) {
        if (la) {
            ps_put(ps, pl, zc->epoch, REG_LA_CONSUMED, la_end);
            zc->la_released = la_end;
        }
        if (ev) {
            ps_put(ps, pl, zc->epoch, REG_EV_CONSUMED, ev_end);
            zc->ev_released = ev_end;
        }
    }
    zc_held(zc, ps);
    return moved;
}

uint32_t relay_zc_complete(struct relay_zc *zc, uint32_t lo, uint32_t hi, int copied, int64_t now_ns)
{
    uint32_t fresh = 0;
    uint64_t count = (uint64_t)(uint32_t)(hi - lo) + 1;  /* 番号は 32 ビットで折り返す（カーネルも同じ） */
    for (uint32_t id = zc->c_tail; id != zc->c_head; id++) {   /* 待っている呼び出しのうち、lo..hi に入るもの */
        struct relay_zc_call *c = &zc->c[id % RELAY_ZC_CALLS];
        if ((uint32_t)(id - lo) >= count || c->done)
            continue;
        c->done = 1;
        c->copied = copied != 0;
        fresh++;
        zc->st.zc_completed++;
        uint64_t lat = now_ns > c->t_ns ? (uint64_t)(now_ns - c->t_ns) : 0;
        zc->st.zc_latency_count++;
        zc->st.zc_latency_sum_ns += lat;
        if (lat > zc->st.zc_latency_max_ns)
            zc->st.zc_latency_max_ns = lat;
        if (copied) {
            zc->st.zc_copied_calls++;
            zc->st.zc_copied_bytes += c->end_off - c->start_off;
            if (zc->state == RELAY_ZC_RUN)
                zc->state = RELAY_ZC_DRAIN;              /* 黙って続けない: ゼロコピーをやめる */
        }
    }
    zc->st.zc_unknown_completions += count - fresh;       /* 待っていない（済んだ・知らない）番号 */
    while (zc->c_tail != zc->c_head && zc->c[zc->c_tail % RELAY_ZC_CALLS].done)
        zc->c_tail++;
    zc->st.zc_outstanding_calls = zc->c_head - zc->c_tail;
    return fresh;
}

/* d_send から、まだ送っていないかたまりのバイトを入れ物に写す（copied の後）。キャッシュなしの写し dev を拍の境で揃えて
 * 読むため、拍の境から写し、送る所（bounce_pos）を入れ物の中でずらす。写したら 1 */
static int zc_fill_bounce(struct relay_zc *zc, const uint8_t *dev)
{
    uint32_t used = 0, skip = zc->d_send_off % BEAT_BYTES;
    while (zc->d_send != zc->d_head && used < RELAY_ZC_BOUNCE_BYTES) {
        const struct relay_zc_desc *d = &zc->d[zc->d_send % RELAY_ZC_DESCS];
        uint32_t at = zc->d_send_off - zc->d_send_off % BEAT_BYTES;   /* かたまりの中の拍の境 */
        uint32_t total = desc_bytes(d);
        if (at < BEAT_BYTES) {                           /* 見出し */
            memcpy(zc->bounce + used, d->head, BEAT_BYTES);
            used += BEAT_BYTES;
            at = BEAT_BYTES;
        }
        uint32_t n = total - at;
        if (n > RELAY_ZC_BOUNCE_BYTES - used)
            n = RELAY_ZC_BOUNCE_BYTES - used;
        copy_from_device(zc->bounce + used, dev + (size_t)d->first * BEAT_BYTES + (at - BEAT_BYTES), n);
        used += n;
        at += n;
        if (at == total) {
            zc->d_send++;
            zc->d_send_off = 0;
        } else {
            zc->d_send_off = at;
        }
    }
    if (used == 0)
        return 0;
    zc->bounce_pos = skip;                                /* 最初の拍の、送り済みの頭は送らない */
    zc->bounce_len = used;
    return 1;
}

/* 入れ物の残りを普通に送る（カーネルが受け取った時点で写し終わっている）。戻り値は relay_zc_step と同じ形 */
static int zc_send_bounce(struct relay_zc *zc, const struct relay_sock_ops *so)
{
    struct relay_iov iov = { zc->bounce + zc->bounce_pos, zc->bounce_len - zc->bounce_pos };
    int64_t n = so->send(so->ctx, &iov, 1, 0);
    if (n == RELAY_SEND_BROKEN || n == 0)
        return -1;
    if (n < 0) {
        if (n == RELAY_SEND_NOBUFS)
            zc->st.zc_enobufs++;
        else
            zc->st.zc_eagain++;
        return RELAY_ZC_BLOCKED;
    }
    zc->bounce_pos += (uint32_t)n;
    zc->sent_off += (uint64_t)n;
    zc->st.zc_bounce_bytes += (uint64_t)n;
    if (zc->bounce_pos >= zc->bounce_len)
        zc->bounce_pos = zc->bounce_len = 0;
    return RELAY_ZC_MOVED;
}

/* 新しいかたまりを、送っていないバイトが RELAY_ZC_CALL_BYTES に届くまで取る（位置のレジスタは 1 回だけ読む）。
 * 取ったら STREAM_BEATS を書く（渡す前に。ホストの「必ず届く量」） */
static void zc_take(struct relay_zc *zc, struct relay_ps *ps, const struct pl_ops *pl, int64_t now_ns)
{
    struct ps_snap snap;
    if (zc->taken_off - zc->sent_off >= RELAY_ZC_CALL_BYTES || !ps_snapshot(ps, pl, &snap))
        return;
    zc_sync(zc, ps);
    int took = 0;
    while (zc->taken_off - zc->sent_off < RELAY_ZC_CALL_BYTES) {
        if (zc->d_head - zc->d_tail >= RELAY_ZC_DESCS) {
            zc->st.zc_desc_full++;
            break;
        }
        struct relay_zc_desc *d = &zc->d[zc->d_head % RELAY_ZC_DESCS];
        struct ps_chunk c;
        if (!ps_next(ps, &snap, now_ns, &zc_rule, d->head, &c))
            break;
        d->epoch = ps->epoch;
        d->kind = c.kind;
        d->first = c.ring_base + c.pos;
        d->n = c.n;
        d->end_read = c.kind == STREAM_SEG_LA ? ps->la_read : ps->ev_read;
        zc->taken_off += desc_bytes(d);
        d->end_off = zc->taken_off;
        zc->d_head++;
        zc->st.zc_chunks++;
        took = 1;
    }
    if (took)
        ps_put(ps, pl, ps->epoch, REG_STREAM_BEATS, ps->out_beats);
    zc_held(zc, ps);
}

/* d_send からのかたまりを iovec に並べて MSG_ZEROCOPY で 1 回送る */
static int zc_send(struct relay_zc *zc, const struct relay_sock_ops *so, const uint8_t *ring, int64_t now_ns)
{
    if (zc->d_send == zc->d_head)
        return 0;
    if (zc->c_head - zc->c_tail >= RELAY_ZC_CALLS) {
        zc->st.zc_calls_full++;
        return RELAY_ZC_BLOCKED;
    }
    struct relay_iov iov[RELAY_ZC_IOV_MAX];
    int n_iov = 0;
    uint64_t total = 0;
    uint32_t off = zc->d_send_off;
    for (uint32_t i = zc->d_send; i != zc->d_head && n_iov + 2 <= RELAY_ZC_IOV_MAX && total < RELAY_ZC_CALL_BYTES; i++) {
        const struct relay_zc_desc *d = &zc->d[i % RELAY_ZC_DESCS];
        if (off < BEAT_BYTES) {
            iov[n_iov].base = d->head + off;
            iov[n_iov].len = BEAT_BYTES - off;
            total += iov[n_iov++].len;
            off = BEAT_BYTES;
        }
        iov[n_iov].base = ring + (size_t)d->first * BEAT_BYTES + (off - BEAT_BYTES);
        iov[n_iov].len = desc_bytes(d) - off;
        total += iov[n_iov++].len;
        off = 0;
    }
    int64_t n = so->send(so->ctx, iov, n_iov, 1);
    if (n == RELAY_SEND_BROKEN || n == 0)
        return -1;
    if (n < 0) {
        if (n == RELAY_SEND_NOBUFS)
            zc->st.zc_enobufs++;
        else
            zc->st.zc_eagain++;
        return RELAY_ZC_BLOCKED;
    }
    struct relay_zc_call *c = &zc->c[zc->c_head % RELAY_ZC_CALLS];
    c->start_off = zc->sent_off;
    c->end_off = zc->sent_off + (uint64_t)n;
    c->t_ns = now_ns;
    c->done = c->copied = 0;
    zc->c_head++;
    zc->st.zc_calls++;
    zc->st.zc_bytes += (uint64_t)n;
    zc->st.zc_outstanding_calls = zc->c_head - zc->c_tail;
    if (zc->st.zc_outstanding_calls > zc->st.zc_outstanding_max)
        zc->st.zc_outstanding_max = zc->st.zc_outstanding_calls;
    zc->sent_off += (uint64_t)n;
    uint64_t left = (uint64_t)n;                          /* 送れた分だけ送る位置を進める（途中までなら次の回に続ける） */
    while (left > 0) {
        const struct relay_zc_desc *d = &zc->d[zc->d_send % RELAY_ZC_DESCS];
        uint32_t rest = desc_bytes(d) - zc->d_send_off;
        if (left >= rest) {
            left -= rest;
            zc->d_send++;
            zc->d_send_off = 0;
        } else {
            zc->d_send_off += (uint32_t)left;
            left = 0;
        }
    }
    return (uint64_t)n < total ? RELAY_ZC_MOVED | RELAY_ZC_BLOCKED : RELAY_ZC_MOVED;
}

int relay_zc_step(struct relay_zc *zc, struct relay_ps *ps, struct relay_rd *rd, const struct pl_ops *pl,
                  const struct relay_sock_ops *so, const uint8_t *ring, const uint8_t *dev, int64_t now_ns)
{
    int r = zc_release(zc, ps, pl) ? RELAY_ZC_MOVED : 0, s;
    if (zc->state == RELAY_ZC_DONE)
        return r;
    if (zc->bounce_pos < zc->bounce_len) {               /* 写した残りを先に（順番を崩さない） */
        s = zc_send_bounce(zc, so);
        return s < 0 ? s : r | s;
    }
    if (zc->state == RELAY_ZC_DRAIN) {                   /* copied の後: 送っていない残りは写して送り、完了を待ち切る */
        if (zc_fill_bounce(zc, dev)) {
            s = zc_send_bounce(zc, so);
            return s < 0 ? s : r | s;
        }
        if (zc->d_tail == zc->d_head && zc->c_tail == zc->c_head)
            zc->state = RELAY_ZC_DONE;                   /* 呼び手が今の複写の道に渡す */
        return r;
    }
    /* かたまりでないバイト（記述の表に載らない）は、送り始めたかたまりの後ろにだけ置き、流れの位置（taken_off）を進める */
    if (zc->d_send == zc->d_head && ps->stage_pos < ps->stage_len) {
        uint32_t left = ps->stage_len - ps->stage_pos;   /* UDP のデータグラムで出しかけたかたまりの残り（切り替えで順番を崩さない） */
        memcpy(zc->bounce, ps->stage + ps->stage_pos, left);
        ps->stage_pos = ps->stage_len = 0;
        zc->bounce_pos = 0;
        zc->bounce_len = left;
        zc->taken_off += left;
        s = zc_send_bounce(zc, so);
        return s < 0 ? s : r | s;
    }
    if (zc->d_send == zc->d_head && rd != NULL) {        /* トリガ取り込みの読み出し（今の道と同じく、ストリームより先に） */
        size_t n = relay_rd_fill(rd, pl, dev, ps, zc->bounce, RELAY_ZC_BOUNCE_BYTES);
        if (n > 0) {
            zc->bounce_pos = 0;
            zc->bounce_len = (uint32_t)n;
            zc->taken_off += n;
            s = zc_send_bounce(zc, so);
            return s < 0 ? s : r | s;
        }
    }
    zc_take(zc, ps, pl, now_ns);
    s = zc_send(zc, so, ring, now_ns);
    return s < 0 ? s : r | s;
}

void relay_zc_abandon(struct relay_zc *zc, struct relay_ps *ps, const struct pl_ops *pl)
{
    /* 送り終えていない（完了していない・渡していない）かたまりを数える（捨てた事実を残す。A8） */
    for (uint32_t i = zc->d_tail; i != zc->d_head; i++) {
        zc->st.zc_abandoned_chunks++;
        zc->st.zc_abandoned_bytes += desc_bytes(&zc->d[i % RELAY_ZC_DESCS]);
    }
    if (zc->synced && ps->attached && zc->epoch == ps->epoch) {
        if (ps->la_read != zc->la_released)
            ps_put(ps, pl, zc->epoch, REG_LA_CONSUMED, ps->la_read);
        if (ps->ev_read != zc->ev_released)
            ps_put(ps, pl, zc->epoch, REG_EV_CONSUMED, ps->ev_read);
    }
    zc->la_released = ps->la_read;
    zc->ev_released = ps->ev_read;
    zc->d_tail = zc->d_send = zc->d_head;
    zc->d_send_off = 0;
    zc->c_tail = zc->c_head;
    zc->bounce_pos = zc->bounce_len = 0;
    zc->st.zc_outstanding_calls = 0;
    zc->st.zc_la_held_beats = zc->st.zc_ev_held_beats = 0;
}

int relay_ringmap_region_ok(const char *dt_dir, uint32_t base, uint32_t size)
{
    if (size == 0)
        return 0;
    char path[512];
    uint8_t cells[4], reg[16];
    snprintf(path, sizeof path, "%s/#address-cells", dt_dir);
    if (read_file(path, cells, 4) != 4 || be32(cells) != 1)
        return 0;
    snprintf(path, sizeof path, "%s/#size-cells", dt_dir);
    if (read_file(path, cells, 4) != 4 || be32(cells) != 1)
        return 0;
    DIR *dir = opendir(dt_dir);
    if (!dir)
        return 0;
    int found = 0;
    long n = 0;
    struct dirent *e;
    while ((e = readdir(dir)) != NULL) {
        if (strlen(e->d_name) > 200 || strncmp(e->d_name, "fprobe_ring", 11) != 0 ||
            (e->d_name[11] != 0 && e->d_name[11] != '@'))
            continue;                                    /* fprobe_ringmap の of_node_name_eq と同じ: 名前は @ の前 */
        found++;
        snprintf(path, sizeof path, "%s/%s/reg", dt_dir, e->d_name);
        n = read_file(path, reg, sizeof reg);
    }
    closedir(dir);
    if (found != 1 || n != 8)                             /* ただ 1 つ、reg はちょうど 1 組（モジュールは 0 番の組を写す） */
        return 0;
    uint64_t lo = be32(reg), hi = lo + be32(reg + 4);
    return lo == base && (uint64_t)base + size <= hi;
}

/* ---- 中継の状態 ---------------------------------------------------------------------------------------------------- */

static const char *const status_names[] = {
#define RELAY_STATUS_NAME(name) #name,
    RELAY_STATUS_FIELDS(RELAY_STATUS_NAME)
#undef RELAY_STATUS_NAME
};

int relay_status_count(void)
{
    return (int)(sizeof status_names / sizeof *status_names);
}

const char *relay_status_field_name(int i)
{
    return i >= 0 && i < relay_status_count() ? status_names[i] : NULL;
}

size_t relay_status_encode(const struct relay_status *s, uint8_t token, uint8_t *out, size_t cap)
{
    const uint64_t values[] = {
#define RELAY_STATUS_VALUE(name) s->name,
        RELAY_STATUS_FIELDS(RELAY_STATUS_VALUE)
#undef RELAY_STATUS_VALUE
    };
    size_t count = sizeof values / sizeof *values, len = 8 + 8 * count;
    if (cap < len)
        return 0;
    out[0] = RELAY_CH_STATUS;
    out[1] = token;                                      /* 問い合わせの番号を写す（PC は自分の問い合わせの返事だけを受ける） */
    out[2] = (uint8_t)RELAY_STATUS_VERSION;
    out[3] = (uint8_t)(RELAY_STATUS_VERSION >> 8);
    out[4] = s->zc_state;
    out[5] = s->tcp_path;
    out[6] = (uint8_t)count;
    out[7] = (uint8_t)(count >> 8);
    for (size_t i = 0; i < count; i++)
        for (int b = 0; b < 8; b++)
            out[8 + 8 * i + (size_t)b] = (uint8_t)(values[i] >> (8 * b));
    return len;
}

/* ---- NIC の設定の控え ------------------------------------------------------------------------------------------------ */

int relay_nic_save(const char *path, const char *defer, const char *flush)
{
    char tmp[512];
    if (snprintf(tmp, sizeof tmp, "%s.tmp", path) >= (int)sizeof tmp)
        return -1;
    FILE *f = fopen(tmp, "wb");
    if (!f)
        return -1;
    int ok = fprintf(f, "%s\n%s\n", defer, flush) > 0;
    if (fclose(f) != 0 || !ok || rename(tmp, path) != 0) {   /* 書きかけの控えを残さない */
        remove(tmp);
        return -1;
    }
    return 0;
}

/* 1 行（改行まで）を out に写す。入りきらない・空なら -1 */
static int take_line(const char **p, char *out, size_t cap)
{
    const char *nl = strchr(*p, '\n');
    size_t n = nl ? (size_t)(nl - *p) : strlen(*p);
    if (n == 0 || n >= cap)
        return -1;
    memcpy(out, *p, n);
    out[n] = 0;
    *p += n + (nl != NULL);
    return 0;
}

int relay_nic_load(const char *path, char *defer, size_t defer_cap, char *flush, size_t flush_cap)
{
    char text[128];
    long n = read_file(path, (uint8_t *)text, sizeof text - 1);
    if (n <= 0)
        return -1;
    text[n] = 0;
    const char *p = text;
    return take_line(&p, defer, defer_cap) == 0 && take_line(&p, flush, flush_cap) == 0 ? 0 : -1;
}
