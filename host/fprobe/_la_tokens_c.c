/* ロジアナ RLE トークン形式の復号の C 版（任意の高速版）。仕様: docs/superpowers/specs/2026-11-07-la-rle2-design.md §3・§6.4。
 *
 * Python 版（fprobe/la_tokens.py の numpy / 純 Python）と結果が一致することが前提で、Python 側が ctypes で呼ぶ。
 * 無くても（ビルドしなくても）同じ結果で遅いだけ。誤り（形式の違反）はここでは報告せず、1 を返して Python 側に戻す
 * （Python 版が同じ文面の TokenError を出す）。したがってここは「正しい入力を速く」だけを受け持ち、判定は Python 版と同じ条件で
 * 違反を見つけたら必ず 1 を返す。
 *
 * ビルド: python tools/build_la_tokens_c.py（gcc / clang / cc / cl のどれか）。
 */
#include <stddef.h>
#include <stdint.h>

#ifdef _WIN32
#define LA_EXPORT __declspec(dllexport)
#else
#define LA_EXPORT __attribute__((visibility("default")))
#endif

#define LA_ABI_VERSION 1
#define BEAT_MASK 15u
#define TIME_LIMIT ((uint64_t)1 << 62)  /* これ以上の時刻は Python 版に任せる（64 ビットの桁あふれの扱いを一致させるため） */

typedef struct {
    uint64_t pad;            /* NOP の数 */
    uint64_t tokens;         /* NOP・REPEAT を除くトークンの数（REPEAT で繰り返した分も数える） */
    uint64_t repeats;        /* REPEAT トークンの数 */
    uint64_t dsum;           /* 出来事の間隔の和（時刻の進み） */
    uint64_t nout;           /* 書いた変化点の数 */
    uint32_t last;           /* 最後の値（mode 0 で previous を 0 とみなして累積した値 = VALUE があればその絶対値、無ければ反転の累積） */
    int32_t has_value;       /* VALUE があった */
    int32_t first_is_value;  /* 最初のトークンが VALUE */
    int32_t first_delta_zero;/* 最初のトークンの間隔が 0 */
} la_tok_res;

typedef struct {
    uint64_t d;
    uint32_t x;
    int special;             /* VALUE・REPEAT */
    uint64_t beat;
} la_tk;

typedef struct {
    int mode;                /* 0: 要約だけ（変化点を作らない）、1: 変化点を書く */
    uint64_t first, cap, nout, t, gt;
    uint32_t acc, pv;
    int has_pv, pending, sim, bad;
    uint16_t *ov;
    uint64_t *os;
} la_st;

static void close_group(la_st *s)
{
    if (!s->has_pv || s->acc != s->pv) {
        if (s->nout >= s->cap) {
            s->sim = 0;      /* 出力の容量が足りない: 以降は数えるだけ（呼び側が tokens の数で取り直す） */
            return;
        }
        s->ov[s->nout] = (uint16_t)s->acc;
        s->os[s->nout] = s->gt + s->first;
        s->nout++;
    }
    s->pv = s->acc;
    s->has_pv = 1;
}

/* 1 つの出来事を適用する。d = 間隔、x = 反転のビット、isval のとき x は絶対値 */
static void step(la_st *s, uint64_t d, uint32_t x, int isval)
{
    if (s->mode && d != 0 && s->pending)
        close_group(s);
    s->t += d;
    if (s->t >= TIME_LIMIT)
        s->bad = 1;
    if (isval)
        s->acc = x;
    else
        s->acc ^= x;
    if (s->mode && (d != 0 || !s->pending)) {
        s->gt = s->t;
        s->pending = 1;
    }
}

LA_EXPORT int la_tok_abi(void) { return LA_ABI_VERSION; }

/* 戻り値: 0 = 正常、1 = 形式の違反か未対応の範囲（Python 版に任せる）、2 = 出力の容量が足りない（res->tokens が必要な数の上限）。 */
LA_EXPORT int la_tok_run(const uint8_t *b, size_t n, int mode, uint64_t first, int has_prev, uint32_t previous,
                         int width, uint16_t *ov, uint64_t *os, uint64_t cap, la_tok_res *r)
{
    la_st s;
    la_tk p1 = {0, 0, 1, 0}, p2 = {0, 0, 1, 0};
    uint64_t raw = 0;
    size_t i = 0;

    r->pad = r->tokens = r->repeats = r->dsum = r->nout = 0;
    r->last = 0;
    r->has_value = r->first_is_value = r->first_delta_zero = 0;
    s.mode = mode;
    s.first = first;
    s.cap = cap;
    s.nout = 0;
    s.t = s.gt = 0;
    s.acc = has_prev ? (previous & 0xFFFFu) : 0;
    s.pv = s.acc;
    s.has_pv = has_prev ? 1 : 0;
    s.pending = 0;
    s.sim = 1;
    s.bad = 0;
    s.ov = ov;
    s.os = os;

    while (i < n) {
        uint8_t h = b[i];
        size_t j;
        unsigned dd, c, k;
        uint64_t d;
        uint32_t x;
        int isval = 0;

        if (h & 0x80u)
            return 1;                                   /* 頭の位置に尾（拍の先頭の尾を含む） */
        j = i + 1;
        while (j < n && (b[j] & 0x80u)) {
            if ((j & BEAT_MASK) == 0)
                return 1;                               /* 尾が拍の先頭に来る */
            j++;
        }
        k = (unsigned)(j - i - 1);
        dd = h & 7u;
        c = (unsigned)(h >> 3);
        if (dd != 7u) {
            if (k)
                return 1;                               /* 間隔の欄が 7 でない頭の後ろに尾 */
            if (width < 16 && (int)c >= width)
                return 1;
            d = dd;
            x = 1u << c;
        } else {
            uint64_t pay;
            unsigned m;
            if (k == 0) {                               /* NOP */
                r->pad++;
                i = j;
                continue;
            }
            if (k > 6u)
                return 1;
            pay = (uint64_t)(b[i + 1] & 0x3Fu);
            for (m = 2; m <= k; m++)
                pay |= (uint64_t)(b[i + m] & 0x7Fu) << (6u + 7u * (m - 2u));
            if (b[i + 1] & 0x40u) {                     /* 特殊 */
                if (c == 0u) {                          /* WAIT */
                    d = pay;
                    x = 0;
                } else if (c == 1u) {                   /* VALUE */
                    d = pay >> 16;
                    x = (uint32_t)(pay & 0xFFFFu);
                    if (width < 16 && (x >> width))
                        return 1;
                    isval = 1;
                    r->has_value = 1;
                } else if (c == 2u) {                   /* REPEAT */
                    unsigned bn = 1u + (unsigned)(pay & 1u);
                    uint64_t cnt = (pay >> 1) + 1u, total = cnt * bn, unit;
                    if (raw < bn || (bn == 2u ? p2.beat : p1.beat) != (i >> 4))
                        return 1;                       /* 同じ拍に直前の対象が B 個ない */
                    if (p1.special || p1.d == 0 || (bn == 2u && (p2.special || p2.d == 0)))
                        return 1;                       /* VALUE・REPEAT・間隔 0 は繰り返せない */
                    r->repeats++;
                    r->tokens += total;
                    unit = p1.d + (bn == 2u ? p2.d : 0);
                    if (mode == 0) {                    /* 要約: 展開せず回数を掛ける */
                        if (cnt > (TIME_LIMIT - s.t) / unit)
                            return 1;
                        s.t += unit * cnt;
                        if (cnt & 1u)
                            s.acc ^= p1.x ^ (bn == 2u ? p2.x : 0);
                    } else if (s.sim) {
                        if (s.nout + total > cap) {
                            s.sim = 0;                  /* 展開すると容量を超えうる: 数えるだけにして取り直し */
                        } else {
                            uint64_t q;
                            la_tk t1 = p1, t2 = p2;
                            for (q = 0; q < cnt; q++) {
                                if (bn == 2u)
                                    step(&s, t2.d, t2.x, 0);
                                step(&s, t1.d, t1.x, 0);
                            }
                        }
                    }
                    p2 = p1;
                    p1.d = 0;
                    p1.x = 0;
                    p1.special = 1;
                    p1.beat = i >> 4;
                    raw++;
                    i = j;
                    continue;
                } else {
                    return 1;                           /* 予約の種類 */
                }
            } else {                                    /* TOGGLE_LONG */
                if (width < 16 && (int)c >= width)
                    return 1;
                d = pay + 7u;
                x = 1u << c;
            }
        }
        if (raw == 0) {
            r->first_is_value = isval;
            r->first_delta_zero = (d == 0);
        }
        r->tokens++;
        if (mode == 0 || s.sim)
            step(&s, d, x, isval);
        p2 = p1;
        p1.d = d;
        p1.x = isval ? 0 : x;
        p1.special = isval;
        p1.beat = i >> 4;
        raw++;
        i = j;
    }
    if (s.bad)
        return 1;
    if (mode && s.sim && s.pending)
        close_group(&s);
    r->dsum = s.t;
    r->last = s.acc;
    r->nout = s.nout;
    if (mode && !s.sim)
        return 2;
    return 0;
}
