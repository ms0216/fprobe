/*
 * fprobe のターゲットのエージェント（組み込み Linux。仕様 docs/superpowers/specs/2026-10-22-linux-app-debug-design.md）。
 *
 * stdin / stdout の 1 行 1 つの JSON でデーモンとやりとりする（SSH の中。道 A）。
 *   要求 {"id":n,"op":"..."} → 返事 {"id":n,"ok":true,...} / {"id":n,"ok":false,"error":"..."}
 *   出来事 {"ev":"..."}
 * op:
 *   hello                              → version, clock, pid
 *   time                               → t_ns（CLOCK_MONOTONIC_RAW）
 *   trace_open {path} / trace_close    → trace_pipe の行を {"ev":"trace","line","rx_ns"} で流す
 *   bp_add {bp,pid,path,offset,thumb,length,regs:[..]} → address, threads
 *   bp_remove {bp}
 *   ring_open {phys, size, cached, linger} / ring_close（道 B: bp の出来事を物理メモリのリングにも書く。host/fprobe/linux/ring.py）
 *   watchdog {seconds}: この時間、入力（要求・ping）が無ければデーモンがいなくなったとみなす（linger のリングがあれば ptrace している
 *     プロセスが終わるまで続け、無ければ bp を戻して終わる）。ping: 何もせず ok（デーモンが生きている合図）
 *   quit
 * ptrace のブレークポイント（32 ビットの ARM）: 当たったら全スレッドを止め、元の命令に戻し、番地 + length に仮の BKPT を
 * 置いてそのスレッドだけ進め、仮に当たったら戻して元の BKPT を置き直し、全スレッドを続ける（止める方式なので振る舞いが変わる）。
 * 出来事 {"ev":"bp","bp","tid","t_ns","regs":{...}} の t_ns は、エージェントが止まりを知った時刻（命令の実行の時刻ではない）。
 */
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <math.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/ptrace.h>
#include <sys/types.h>
#include <sys/user.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define VERSION 1
#define MAX_BP 64
#define MAX_TID 256
#define LINE_MAX_LEN 8192

static const uint32_t ARM_BKPT = 0xe7f001f0u;     /* Linux の ARM のブレークポイント（未定義命令） */
static const uint16_t THUMB_BKPT = 0xde01u;
static const char *REG_NAMES[] = {"r0", "r1", "r2", "r3", "r4", "r5", "r6", "r7", "r8", "r9", "r10", "r11", "r12",
                                  "sp", "lr", "pc", "cpsr"};
#define NREGS 17

struct bp {
    int used, id, length, thumb;
    pid_t pid;
    unsigned long addr;
    uint32_t orig;                 /* 元の命令（Thumb なら下位 16 ビット。長さ 4 の Thumb-2 でも先頭の半語だけ替える） */
    uint32_t regmask;
    long hits;
};

static struct bp bps[MAX_BP];
static pid_t traced_pid;           /* ptrace している（1 つの）プロセス */
static int trace_fd = -1;

/* ---- 道 A（標準出力 = SSH）: 待たずに書く ----
 * 道 A が詰まる（ネットワークが落ちた）・切れると、書き込みで待たされて ptrace と道 B（リング）も止まる（PYNQ で起きた）。
 * 標準出力を O_NONBLOCK にし、行ごとに 1 回の write で書く（PIPE_BUF 以下の行は全部か何も書かれないかのどちらか）。
 * 書けない行は捨てて数え、次に書けたときに {"ev":"a_dropped","n":…} を先に出す。切れた（EPIPE）後は書かない。 */
static char out_line[65536];             /* 1 行（trace の行は 0x80 以上のバイトが 6 文字になるので長くなりうる） */
static size_t out_len;
static int out_overflow;                  /* 今の行が out_line に入りきらなかった（行ごと捨てる） */
static char out_pend[65536 + 64];          /* 途中までしか書けなかった行の残り（次に何か書く前に、まずこれを書き切る） */
static size_t out_pend_len, out_pend_off;
static uint64_t a_dropped;
static int out_dead;

/* 書けた分を返す（-1 = EAGAIN 等で 1 バイトも書けない。EPIPE なら out_dead） */
static ssize_t try_write(const char *p, size_t n) {
    size_t done = 0;
    while (done < n) {
        ssize_t w = write(1, p + done, n - done);
        if (w > 0) { done += (size_t)w; continue; }
        if (w < 0 && errno == EINTR) continue;
        if (w < 0 && errno == EPIPE) out_dead = 1;
        break;
    }
    return done ? (ssize_t)done : -1;
}

/* 途中までの行の残りを書く。書き切れたら 1 */
static int flush_pending(void) {
    if (out_pend_off == out_pend_len) return 1;
    if (out_dead) { out_pend_off = out_pend_len = 0; return 1; }
    ssize_t w = try_write(out_pend + out_pend_off, out_pend_len - out_pend_off);
    if (w > 0) out_pend_off += (size_t)w;
    if (out_pend_off == out_pend_len) { out_pend_off = out_pend_len = 0; return 1; }
    return 0;
}

/* 行を全部書くか、書けなかった残りを out_pend に置く（行が混ざらない）。1 バイトも書けなければ -1 */
static int put_whole(const char *p, size_t n) {
    ssize_t w = try_write(p, n);
    if (w < 0) return -1;
    if ((size_t)w < n) {
        memcpy(out_pend, p + w, n - (size_t)w);
        out_pend_len = n - (size_t)w;
        out_pend_off = 0;
    }
    return 0;
}

static void emit_line(const char *p, size_t n) {
    if (out_dead || !flush_pending()) { a_dropped++; return; }   /* 前の行の残りが書けない間は、この行を捨てて数える */
    if (a_dropped) {
        char note[64];
        int k = snprintf(note, sizeof note, "{\"ev\":\"a_dropped\",\"n\":%llu}\n", (unsigned long long)a_dropped);
        if (put_whole(note, (size_t)k)) { a_dropped++; return; }
        a_dropped = 0;
        if (!flush_pending()) { a_dropped++; return; }
    }
    if (put_whole(p, n)) a_dropped++;
}

static ssize_t out_write(void *cookie, const char *buf, size_t size) {
    (void)cookie;
    for (size_t i = 0; i < size; i++) {
        if (out_len < sizeof out_line) out_line[out_len++] = buf[i];
        else out_overflow = 1;
        if (buf[i] == '\n') {
            if (out_overflow) a_dropped++;                          /* 入りきらない行は、改行の無い行を出さずに捨てる */
            else emit_line(out_line, out_len);
            out_len = 0;
            out_overflow = 0;
        }
    }
    return (ssize_t)size;                                                /* stdio には全部書けたと返す（捨てた分は数えてある） */
}

static void out_init(void) {
    fcntl(1, F_SETFL, fcntl(1, F_GETFL) | O_NONBLOCK);
    cookie_io_functions_t io = {NULL, out_write, NULL, NULL};
    FILE *f = fopencookie(NULL, "w", io);
    if (f) {
        setvbuf(f, NULL, _IOLBF, 4096);
        stdout = f;
    }
}

static uint64_t now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC_RAW, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + ts.tv_nsec;
}

/* ---- 出力 ---- */
static void put_json_string(const char *s) {
    putchar('"');
    for (; *s; s++) {
        unsigned char c = (unsigned char)*s;
        if (c == '"' || c == '\\') printf("\\%c", c);
        else if (c == '\n') fputs("\\n", stdout);
        else if (c == '\t') fputs("\\t", stdout);
        else if (c < 0x20 || c >= 0x80) printf("\\u%04x", c);      /* 0x80 以上はバイトの値のまま（受け手が latin-1 で元のバイトに戻す） */
        else putchar(c);
    }
    putchar('"');
}

static void reply_error(long id, const char *fmt, ...) {
    char msg[512];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(msg, sizeof msg, fmt, ap);
    va_end(ap);
    printf("{\"id\":%ld,\"ok\":false,\"error\":", id);
    put_json_string(msg);
    puts("}");
    fflush(stdout);
}

static void event_error(const char *fmt, ...) {
    char msg[512];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(msg, sizeof msg, fmt, ap);
    va_end(ap);
    fputs("{\"ev\":\"error\",\"error\":", stdout);
    put_json_string(msg);
    puts("}");
    fflush(stdout);
}

/* ---- 決まった形の要求だけを読む小さな JSON の読み取り（キーは一意） ---- */
static const char *find_key(const char *s, const char *key) {
    char pat[64];
    snprintf(pat, sizeof pat, "\"%s\"", key);
    const char *p = strstr(s, pat);
    if (!p) return NULL;
    p += strlen(pat);
    while (*p == ' ' || *p == ':') p++;
    return p;
}

static int get_long(const char *s, const char *key, long *out) {
    const char *p = find_key(s, key);
    if (!p) return 0;
    if (!strncmp(p, "true", 4)) { *out = 1; return 1; }
    if (!strncmp(p, "false", 5)) { *out = 0; return 1; }
    char *end;
    *out = strtol(p, &end, 10);
    return end != p;
}

static int get_double(const char *s, const char *key, double *out) {
    const char *p = find_key(s, key);
    if (!p) return 0;
    char *end;
    *out = strtod(p, &end);
    return end != p;
}

static int get_string(const char *s, const char *key, char *out, size_t n) {
    const char *p = find_key(s, key);
    if (!p || *p != '"') return 0;
    size_t i = 0;
    for (p++; *p && *p != '"' && i + 1 < n; p++) {
        if (*p == '\\' && p[1]) p++;
        out[i++] = *p;
    }
    out[i] = 0;
    return *p == '"';
}

static uint32_t get_regmask(const char *s, int *bad) {
    const char *p = find_key(s, "regs");
    uint32_t mask = 0;
    *bad = 0;
    if (!p || *p != '[') return 0;
    for (p++; *p && *p != ']'; p++) {
        if (*p != '"') continue;
        const char *q = strchr(p + 1, '"');
        if (!q) break;
        int found = 0;
        for (int i = 0; i < NREGS; i++)
            if ((size_t)(q - p - 1) == strlen(REG_NAMES[i]) && !strncmp(p + 1, REG_NAMES[i], q - p - 1)) {
                mask |= 1u << i;
                found = 1;
            }
        if (!found) *bad = 1;
        p = q;
    }
    return mask;
}

/* ---- ptrace ---- */
static int peek(pid_t tid, unsigned long addr, uint32_t *word) {
    errno = 0;
    long v = ptrace(PTRACE_PEEKTEXT, tid, (void *)addr, 0);
    if (errno) return -1;
    *word = (uint32_t)v;
    return 0;
}

static int poke(pid_t tid, unsigned long addr, uint32_t word) {
    return ptrace(PTRACE_POKETEXT, tid, (void *)addr, (void *)(unsigned long)word);
}

/* 番地 addr に、Thumb なら 16 ビット、ARM なら 32 ビットを書き、元の値を old に返す（語の境界に合わせて読み書きする） */
static int swap_insn(pid_t tid, unsigned long addr, int thumb, uint32_t val, uint32_t *old) {
    unsigned long base = addr & ~3ul;
    uint32_t w;
    if (peek(tid, base, &w)) return -1;
    if (!thumb) {
        if (old) *old = w;
        return poke(tid, base, val);
    }
    int shift = (addr & 2) ? 16 : 0;
    if (old) *old = (w >> shift) & 0xffff;
    w = (w & ~(0xffffu << shift)) | ((val & 0xffff) << shift);
    return poke(tid, base, w);
}

/*
 * スレッドごとの状態（止まっているか・渡すシグナル・グループの止まりか・bp に当たって処理待ちか）。
 * stop_all は止まっていないスレッドだけを止めて待つ（止まっているスレッドを待つと waitpid が永久に戻らない）。
 * bp に当たったまま処理を待つスレッドは drain が処理し、最後に cont_all が止まっているものをすべて続ける。
 */
struct thr {
    pid_t tid;
    int stopped;                   /* ptrace の止まりを受け取り、まだ続けていない */
    int sig;                       /* 続けるときに渡すシグナル */
    int group;                     /* グループの止まり（SIGSTOP 等）: PTRACE_LISTEN で続ける（ジョブ制御を壊さない） */
    int queued;                    /* bp に当たって止まっている（drain が処理する） */
};

static struct thr thr[MAX_TID];
static int nthr;
static pid_t stray[MAX_TID];       /* まだ知らないスレッド・子の最初の止まり（fork の子の止まりが親の知らせより先に来る） */
static int nstray;

static struct thr *th(pid_t tid) {
    for (int i = 0; i < nthr; i++)
        if (thr[i].tid == tid) return &thr[i];
    return NULL;
}

static struct thr *add_thr(pid_t tid) {
    struct thr *t = th(tid);
    if (t || nthr >= MAX_TID) return t;
    thr[nthr] = (struct thr){tid, 0, 0, 0, 0};
    return &thr[nthr++];
}

static void drop_thr(pid_t tid) {
    struct thr *t = th(tid);
    if (t) *t = thr[--nthr];       /* 状態ごと入れ替える（シグナルを別のスレッドに渡さない） */
}

static pid_t any_stopped(void) {
    for (int i = 0; i < nthr; i++)
        if (thr[i].stopped) return thr[i].tid;
    return 0;
}

static int take_stray(pid_t pid) {
    for (int i = 0; i < nstray; i++)
        if (stray[i] == pid) { stray[i] = stray[--nstray]; return 1; }
    return 0;
}

static int tgid_of(pid_t tid) {
    char name[64], line[256];
    snprintf(name, sizeof name, "/proc/%d/status", tid);
    FILE *f = fopen(name, "r");
    int tgid = -1;
    if (!f) return -1;
    while (fgets(line, sizeof line, f))
        if (sscanf(line, "Tgid: %d", &tgid) == 1) break;
    fclose(f);
    return tgid;
}

static struct bp *bp_at(unsigned long pc) {
    for (int i = 0; i < MAX_BP; i++)
        if (bps[i].used && bps[i].addr == pc) return &bps[i];
    return NULL;
}

static int bp_count(void) {
    int n = 0;
    for (int i = 0; i < MAX_BP; i++) n += bps[i].used;
    return n;
}

static int get_regs(pid_t tid, struct user_regs *r) { return ptrace(PTRACE_GETREGS, tid, 0, r); }

static void handle_exit(pid_t tid, int status) {
    drop_thr(tid);
    if (tid == traced_pid) {
        printf("{\"ev\":\"exit\",\"pid\":%d,\"code\":%d,\"signal\":%d}\n", tid,
               WIFEXITED(status) ? WEXITSTATUS(status) : -1, WIFSIGNALED(status) ? WTERMSIG(status) : 0);
        fflush(stdout);
        for (int i = 0; i < MAX_BP; i++) bps[i].used = 0;
        nthr = 0;
        traced_pid = 0;
    }
}

/* fork / vfork の子: 自動で ptrace される。fork の子は BKPT の写った自分の写しを持つので元の命令に戻して外す
 * （戻さないと子が bp に当たって SIGTRAP で死ぬ）。vfork の子は親とメモリを共有するので書き換えずに外す
 * （exec か _exit までの間に関数を呼べば SIGTRAP で死ぬ。vfork の子にできることは決まりで限られる）。 */
static void handle_fork(pid_t parent, pid_t child, int vfork) {
    int status;
    if (!take_stray(child)) waitpid(child, &status, __WALL);
    if (!vfork)
        for (int i = 0; i < MAX_BP; i++)
            if (bps[i].used) swap_insn(child, bps[i].addr, bps[i].thumb, bps[i].orig, NULL);
    ptrace(PTRACE_DETACH, child, 0, 0);
    printf("{\"ev\":\"fork\",\"pid\":%d,\"child\":%d,\"vfork\":%s}\n", parent, child, vfork ? "true" : "false");
    fflush(stdout);
}

/* t の止まりの理由を覚える（status は waitpid の結果。止まっていること） */
static void note_stop(struct thr *t, int status) {
    int sig = WSTOPSIG(status), ev = status >> 16;
    t->stopped = 1;
    if (ev == PTRACE_EVENT_STOP) {
        t->group = sig == SIGSTOP || sig == SIGTSTP || sig == SIGTTIN || sig == SIGTTOU;
        return;
    }
    if (ev == PTRACE_EVENT_CLONE || ev == PTRACE_EVENT_FORK || ev == PTRACE_EVENT_VFORK) {
        unsigned long newpid;
        ptrace(PTRACE_GETEVENTMSG, t->tid, 0, &newpid);
        if (ev == PTRACE_EVENT_CLONE) add_thr((pid_t)newpid);
        else handle_fork(t->tid, (pid_t)newpid, ev == PTRACE_EVENT_VFORK);
        return;
    }
    if (ev) return;
    if (sig == SIGTRAP) {
        struct user_regs r;
        if (!get_regs(t->tid, &r) && bp_at(r.uregs[15])) { t->queued = 1; return; }
    }
    t->sig = sig;                                                    /* 自分のでないシグナルは続けるときに渡す */
}

/* t が止まるか終わるまで待つ */
static void wait_for(pid_t tid) {
    for (;;) {
        int status;
        pid_t got = waitpid(tid, &status, __WALL);
        if (got < 0) { drop_thr(tid); return; }
        if (WIFEXITED(status) || WIFSIGNALED(status)) { handle_exit(got, status); return; }
        if (!WIFSTOPPED(status)) continue;
        struct thr *t = th(got);
        if (t) note_stop(t, status);
        return;
    }
}

static void stop_all(pid_t except) {
    pid_t want[MAX_TID];
    int n = 0;
    for (int i = 0; i < nthr; i++)                                    /* 先に写す（待つ間に終わったスレッドで並びが変わる） */
        if (thr[i].tid != except && !thr[i].stopped) want[n++] = thr[i].tid;
    for (int i = 0; i < n; i++) ptrace(PTRACE_INTERRUPT, want[i], 0, 0);
    for (int i = 0; i < n; i++) {
        struct thr *t = th(want[i]);
        if (t && !t->stopped) wait_for(want[i]);
    }
}

static void resume(struct thr *t) {
    if (!t->stopped || t->queued) return;
    if (t->group) ptrace(PTRACE_LISTEN, t->tid, 0, 0);
    else ptrace(PTRACE_CONT, t->tid, 0, (void *)(long)t->sig);
    t->stopped = t->sig = t->group = 0;
}

static void cont_all(void) {
    for (int i = 0; i < nthr; i++) resume(&thr[i]);
}

/* ---- 道 B のリング（host/fprobe/linux/ring.py と同じ形）: /dev/mem で物理メモリに置き、bp の出来事を書く ---- */
#define RING_HEADER 64
#define RING_RECORD 48
static volatile uint8_t *ring;          /* NULL なら開いていない */
static size_t ring_len;
static uint32_t ring_cap;
static uint64_t ring_first;
static uint64_t ev_seq;                 /* bp の出来事の通しの番号（道 A の seq と道 B の記録の seq は同じ） */
static uint64_t watchdog_ns;             /* 0 以外: この時間、入力（要求・ping）が無ければデーモンがいなくなったとみなす */
static uint64_t last_input_ns;
static int ring_linger;                  /* SSH（標準入力）が切れても、ptrace しているプロセスが終わるまで続ける（ring_open の linger） */

static uint32_t crc32_of(const uint8_t *p, size_t n) {
    uint32_t c = 0xFFFFFFFFu;
    while (n--) {
        c ^= *p++;
        for (int k = 0; k < 8; k++) c = (c >> 1) ^ (0xEDB88320u & -(c & 1));
    }
    return ~c;
}

/* 4 バイト境界の語を 1 回の 32 ビットの書き込みで（バイトごとに書くと、読み手が途中の値を見る） */
static void put32(volatile uint8_t *p, uint32_t v) { *(volatile uint32_t *)(void *)p = v; }
static void put64(volatile uint8_t *p, uint64_t v) { put32(p, (uint32_t)v); put32(p + 4, (uint32_t)(v >> 32)); }

static void le32(uint8_t *p, uint32_t v) { for (int i = 0; i < 4; i++) p[i] = (uint8_t)(v >> (8 * i)); }

/* 頭（40 バイト + CRC）を作る。magic は書かない（開くときは最後に書く） */
static void ring_header(uint64_t head, int with_magic) {
    uint8_t h[40];
    le32(h, 0x42525046u), le32(h + 4, 1), le32(h + 8, RING_RECORD), le32(h + 12, ring_cap);
    le32(h + 16, (uint32_t)head), le32(h + 20, (uint32_t)(head >> 32)), le32(h + 24, 1), le32(h + 28, 0);
    le32(h + 32, (uint32_t)ring_first), le32(h + 36, (uint32_t)(ring_first >> 32));
    uint32_t crc = crc32_of(h, 40);
    for (int i = 4; i < 40; i += 4) put32(ring + i, (uint32_t)h[i] | h[i + 1] << 8 | h[i + 2] << 16 | (uint32_t)h[i + 3] << 24);
    put32(ring + 40, crc);
    __sync_synchronize();
    if (with_magic) put32(ring, 0x42525046u);                         /* 最後に magic（読み手は書きかけの頭を見ない） */
}

static void ring_write(uint64_t seq, uint64_t t, pid_t tid, int bp, const struct user_regs *r) {
    if (!ring) return;
    volatile uint8_t *rec = ring + RING_HEADER + (size_t)((seq - ring_first) % ring_cap) * RING_RECORD;
    uint8_t buf[RING_RECORD];
    static const int regs[5] = {0, 1, 2, 3, 14};                          /* r0〜r3・lr */
    put64(rec, ~0ull);                                                    /* 書きかけ */
    __sync_synchronize();
    memset(buf, 0, sizeof buf);
    for (int i = 0; i < 8; i++) buf[i] = (uint8_t)(seq >> (8 * i)), buf[8 + i] = (uint8_t)(t >> (8 * i));
    for (int i = 0; i < 4; i++) buf[16 + i] = (uint8_t)((uint32_t)tid >> (8 * i));
    buf[20] = (uint8_t)bp, buf[21] = (uint8_t)(bp >> 8), buf[22] = 1, buf[23] = 0;    /* kind = 1（bp） */
    for (int k = 0; k < 5; k++)
        for (int i = 0; i < 4; i++) buf[24 + 4 * k + i] = (uint8_t)((uint32_t)r->uregs[regs[k]] >> (8 * i));
    uint32_t crc = crc32_of(buf, 44);
    for (int i = 0; i < 4; i++) buf[44 + i] = (uint8_t)(crc >> (8 * i));
    for (int i = 8; i < RING_RECORD; i++) rec[i] = buf[i];
    __sync_synchronize();
    put64(rec, seq);
    __sync_synchronize();
    ring_header(seq + 1, 0);                                              /* head と頭の CRC */
}

static void report_hit(struct bp *b, pid_t tid, uint64_t t, struct user_regs *r) {
    b->hits++;
    uint64_t seq = ev_seq++;
    ring_write(seq, t, tid, b->id, r);
    printf("{\"ev\":\"bp\",\"seq\":%llu,\"bp\":%d,\"tid\":%d,\"t_ns\":%llu,\"regs\":{", (unsigned long long)seq, b->id, tid,
           (unsigned long long)t);
    int first = 1;
    for (int i = 0; i < NREGS; i++)
        if (b->regmask & (1u << i)) {
            printf("%s\"%s\":%lu", first ? "" : ",", REG_NAMES[i], (unsigned long)r->uregs[i]);
            first = 0;
        }
    puts("}}");
    fflush(stdout);
}

/* t が b に当たって止まっている: 記録し、ほかを止め、元の命令に戻して次に仮の BKPT を置き、t だけ 1 歩進めて置き直す。
 * t も含めて止まったままにする（drain の最後の cont_all が続ける）。1 歩の間に t に届いたシグナルは渡す
 * （そのハンドラの中の呼び出しは、bp を外している間なので取れない）。 */
static void step_over(struct bp *b, struct thr *t, uint64_t when) {
    struct user_regs r;
    pid_t tid = t->tid;
    if (get_regs(tid, &r)) { event_error("GETREGS %d: %s", tid, strerror(errno)); return; }
    report_hit(b, tid, when, &r);
    stop_all(tid);
    t = th(tid);                                                     /* 止める間に終わったスレッドで並びが変わる */
    if (!t) return;
    unsigned long next = b->addr + b->length;
    uint32_t next_orig;
    int thumb = b->thumb;
    swap_insn(tid, b->addr, thumb, b->orig, NULL);
    swap_insn(tid, next, thumb, thumb ? THUMB_BKPT : ARM_BKPT, &next_orig);
    int alive = 1;
    ptrace(PTRACE_CONT, tid, 0, 0);
    t->stopped = 0;
    for (;;) {
        int status;
        pid_t got = waitpid(tid, &status, __WALL);
        if (got < 0) { drop_thr(tid); alive = 0; break; }
        if (WIFEXITED(status) || WIFSIGNALED(status)) { handle_exit(got, status); alive = 0; break; }
        if (!WIFSTOPPED(status)) continue;
        int sig = WSTOPSIG(status), ev = status >> 16;
        if (sig == SIGTRAP && ev == 0 && !get_regs(tid, &r) && r.uregs[15] == next) break;
        if (ev == PTRACE_EVENT_CLONE || ev == PTRACE_EVENT_FORK || ev == PTRACE_EVENT_VFORK) {
            struct thr *me = th(tid);
            if (me) note_stop(me, status);
            sig = 0;
        } else if (ev || sig == SIGTRAP) {
            sig = 0;                                                  /* 前の INTERRUPT の残り等 */
        }
        ptrace(PTRACE_CONT, tid, 0, (void *)(long)sig);
    }
    pid_t writer = alive ? tid : any_stopped();
    if (alive) { t = th(tid); if (t) t->stopped = 1, t->queued = 0, t->sig = 0, t->group = 0; }
    if (writer && traced_pid) {
        swap_insn(writer, next, thumb, next_orig, NULL);
        if (b->used) swap_insn(writer, b->addr, thumb, thumb ? THUMB_BKPT : ARM_BKPT, NULL);
    }
}

/* bp に当たって待っているスレッドを処理し、止まっているものをすべて続ける */
static void drain(void) {
    for (;;) {
        struct thr *q = NULL;
        for (int i = 0; i < nthr; i++) if (thr[i].queued) { q = &thr[i]; break; }
        if (!q) break;
        q->queued = 0;
        struct user_regs r;
        struct bp *b = !get_regs(q->tid, &r) ? bp_at(r.uregs[15]) : NULL;
        if (b) step_over(b, q, now_ns());                            /* 外された bp なら元の命令から続けるだけ */
    }
    cont_all();
}

/* waitpid の知らせ（poll の合間に WNOHANG で回す） */
static void reap(void) {
    for (;;) {
        int status;
        pid_t got = waitpid(-1, &status, __WALL | WNOHANG);
        if (got <= 0) break;
        if (WIFEXITED(status) || WIFSIGNALED(status)) { handle_exit(got, status); continue; }
        if (!WIFSTOPPED(status)) continue;
        struct thr *t = th(got);
        if (!t) {
            if (traced_pid && tgid_of(got) == traced_pid) t = add_thr(got);   /* 親の CLONE の知らせより先に来た新しいスレッド */
            else { if (nstray < MAX_TID) stray[nstray++] = got; continue; }   /* fork の子（親の知らせで扱う） */
            if (!t) continue;
        }
        note_stop(t, status);
    }
}

static void detach_all(void) {
    stop_all(0);
    for (int i = 0; i < nthr; i++) ptrace(PTRACE_DETACH, thr[i].tid, 0, (void *)(long)thr[i].sig);
    nthr = 0;
    traced_pid = 0;
}

static int attach(pid_t pid) {
    if (traced_pid == pid) return 0;
    if (traced_pid) { errno = EBUSY; return -1; }
    char dir[64];
    snprintf(dir, sizeof dir, "/proc/%d/task", pid);
    DIR *d = opendir(dir);
    if (!d) return -1;
    struct dirent *e;
    nthr = 0;
    traced_pid = pid;
    long opts = PTRACE_O_TRACECLONE | PTRACE_O_TRACEFORK | PTRACE_O_TRACEVFORK;
    while ((e = readdir(d))) {
        pid_t tid = (pid_t)atoi(e->d_name);
        if (tid <= 0) continue;
        if (ptrace(PTRACE_SEIZE, tid, 0, (void *)opts)) {
            if (errno == ESRCH) continue;                                /* 読む間に終わったスレッド */
            int err = errno;
            closedir(d);
            detach_all();                                                /* つかんだ分を放す */
            errno = err;
            return -1;
        }
        add_thr(tid);
    }
    closedir(d);
    return 0;
}

/* すべての bp を元に戻して外す（quit・stdin の終わり） */
static void release_all(void) {
    if (!traced_pid) return;
    stop_all(0);
    pid_t w = any_stopped();
    for (int i = 0; i < MAX_BP; i++)
        if (bps[i].used) { if (w) swap_insn(w, bps[i].addr, bps[i].thumb, bps[i].orig, NULL); bps[i].used = 0; }
    for (int i = 0; i < nthr; i++) thr[i].queued = 0;
    if (traced_pid) detach_all();
}

/* /proc/<pid>/maps で path と offset から番地を出す */
static int resolve(pid_t pid, const char *path, unsigned long offset, unsigned long *addr) {
    char name[64], line[1024];
    snprintf(name, sizeof name, "/proc/%d/maps", pid);
    FILE *f = fopen(name, "r");
    if (!f) return -1;
    int found = 0;
    while (fgets(line, sizeof line, f)) {
        unsigned long lo, hi, off;
        char perms[8];
        int n = 0;
        if (sscanf(line, "%lx-%lx %7s %lx %*s %*s %n", &lo, &hi, perms, &off, &n) < 4 || !n) continue;
        char *p = line + n;
        p[strcspn(p, "\n")] = 0;
        if (strcmp(p, path) || perms[2] != 'x') continue;
        if (offset >= off && offset < off + (hi - lo)) { *addr = lo + (offset - off); found = 1; break; }
    }
    fclose(f);
    return found ? 0 : -1;
}

static void op_bp_add(long id, const char *s) {
    long bpid, pid, offset, thumb, length;
    char path[512];
    int bad;
    if (!get_long(s, "bp", &bpid) || !get_long(s, "pid", &pid) || !get_long(s, "offset", &offset) ||
        !get_long(s, "thumb", &thumb) || !get_long(s, "length", &length) || !get_string(s, "path", path, sizeof path)) {
        reply_error(id, "bp_add needs bp, pid, path, offset, thumb, length");
        return;
    }
    uint32_t mask = get_regmask(s, &bad);
    if (bad) { reply_error(id, "unknown register name in regs"); return; }
    if (length != 2 && length != 4) { reply_error(id, "length must be 2 or 4"); return; }
    unsigned long addr;
    if (resolve((pid_t)pid, path, (unsigned long)offset, &addr)) {
        reply_error(id, "%s offset 0x%lx is not mapped executable in pid %ld", path, offset, pid);
        return;
    }
    if (bp_at(addr)) { reply_error(id, "a breakpoint is already at 0x%lx", addr); return; }
    struct bp *b = NULL;
    for (int i = 0; i < MAX_BP; i++) if (!bps[i].used) { b = &bps[i]; break; }
    if (!b) { reply_error(id, "too many breakpoints (%d)", MAX_BP); return; }
    if (attach((pid_t)pid)) { reply_error(id, "ptrace attach %ld: %s", pid, strerror(errno)); return; }
    stop_all(0);
    uint32_t orig;
    pid_t w = any_stopped();
    if (!w || swap_insn(w, addr, (int)thumb, thumb ? THUMB_BKPT : ARM_BKPT, &orig)) {
        reply_error(id, "write breakpoint at 0x%lx: %s", addr, w ? strerror(errno) : "no stopped thread");
        if (!bp_count() && traced_pid) detach_all();
        return;                                                          /* 止めたものは drain が続ける */
    }
    *b = (struct bp){1, (int)bpid, (int)length, (int)thumb, (pid_t)pid, addr, orig, mask, 0};
    printf("{\"id\":%ld,\"ok\":true,\"address\":%lu,\"threads\":%d}\n", id, addr, nthr);
    fflush(stdout);
}

static void op_bp_remove(long id, const char *s) {
    long bpid;
    if (!get_long(s, "bp", &bpid)) { reply_error(id, "bp_remove needs bp"); return; }
    struct bp *b = NULL;
    for (int i = 0; i < MAX_BP; i++) if (bps[i].used && bps[i].id == bpid) b = &bps[i];
    if (!b) { reply_error(id, "no breakpoint %ld", bpid); return; }
    stop_all(0);
    pid_t w = any_stopped();
    if (w) swap_insn(w, b->addr, b->thumb, b->orig, NULL);
    long hits = b->hits;
    b->used = 0;
    if (!bp_count() && traced_pid) {
        for (int i = 0; i < nthr; i++) thr[i].queued = 0;            /* 当たって待つものは元の命令から続く */
        detach_all();
    }
    printf("{\"id\":%ld,\"ok\":true,\"hits\":%ld}\n", id, hits);
    fflush(stdout);
}

/* ring_open {phys, size, cached, linger}: /dev/mem の物理番地 phys から size バイトをリングにする（cached = false なら O_SYNC。
 * linger = 1 なら、SSH が切れても ptrace しているプロセスが終わるまでリングに書き続ける）。
 * 番地は使う側が明示する（カーネルの予約。Pi は device tree の reserved-memory、PYNQ は mem=）。 */
static void op_ring_open(long id, const char *s) {
    long phys, size, cached;
    if (!get_long(s, "phys", &phys) || !get_long(s, "size", &size) || !get_long(s, "cached", &cached)) {
        reply_error(id, "ring_open needs phys, size, cached");
        return;
    }
    if (size < RING_HEADER + RING_RECORD || (phys & 0xFFF)) { reply_error(id, "size too small or phys not page aligned"); return; }
    long linger = 0;
    get_long(s, "linger", &linger);
    int fd = open("/dev/mem", O_RDWR | (cached ? 0 : O_SYNC));
    if (fd < 0) { reply_error(id, "open /dev/mem: %s", strerror(errno)); return; }
    void *m = mmap(NULL, (size_t)size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, (off_t)(unsigned long)phys);
    close(fd);
    if (m == MAP_FAILED) { reply_error(id, "mmap /dev/mem 0x%lx: %s", phys, strerror(errno)); return; }
    if (ring) munmap((void *)ring, ring_len);
    ring = m, ring_len = (size_t)size;
    ring_cap = (uint32_t)((size - RING_HEADER) / RING_RECORD);
    ring_first = ev_seq;
    ring_linger = (int)linger;
    for (size_t i = 0; i < ring_len; i++) ring[i] = 0;
    __sync_synchronize();
    ring_header(ring_first, 1);
    printf("{\"id\":%ld,\"ok\":true,\"capacity\":%u,\"first\":%llu}\n", id, ring_cap, (unsigned long long)ring_first);
    fflush(stdout);
}

static void op_trace_open(long id, const char *s) {
    char path[512];
    if (!get_string(s, "path", path, sizeof path)) { reply_error(id, "trace_open needs path"); return; }
    if (trace_fd >= 0) close(trace_fd);
    trace_fd = open(path, O_RDONLY | O_NONBLOCK);
    if (trace_fd < 0) { reply_error(id, "open %s: %s", path, strerror(errno)); return; }
    printf("{\"id\":%ld,\"ok\":true}\n", id);
    fflush(stdout);
}

static char trace_buf[LINE_MAX_LEN];
static size_t trace_len;

static void pump_trace(void) {
    ssize_t n = read(trace_fd, trace_buf + trace_len, sizeof trace_buf - 1 - trace_len);
    if (n <= 0) return;
    uint64_t t = now_ns();
    trace_len += n;
    trace_buf[trace_len] = 0;
    char *start = trace_buf, *nl;
    while ((nl = strchr(start, '\n'))) {
        *nl = 0;
        fputs("{\"ev\":\"trace\",\"line\":", stdout);
        put_json_string(start);
        printf(",\"rx_ns\":%llu}\n", (unsigned long long)t);
        start = nl + 1;
    }
    trace_len -= start - trace_buf;
    memmove(trace_buf, start, trace_len);
    if (trace_len == sizeof trace_buf - 1) {                          /* 改行の無い長すぎる行: 捨てたことを知らせる */
        event_error("trace line longer than %d bytes dropped", LINE_MAX_LEN - 1);
        trace_len = 0;
    }
    fflush(stdout);
}

static int handle(const char *s) {
    long id = 0;
    char op[32];
    get_long(s, "id", &id);
    if (!get_string(s, "op", op, sizeof op)) { reply_error(id, "no op"); return 1; }
    if (!strcmp(op, "hello")) {
        printf("{\"id\":%ld,\"ok\":true,\"version\":%d,\"clock\":\"CLOCK_MONOTONIC_RAW\",\"pid\":%d}\n", id, VERSION,
               getpid());
    } else if (!strcmp(op, "time")) {
        printf("{\"id\":%ld,\"ok\":true,\"t_ns\":%llu}\n", id, (unsigned long long)now_ns());
    } else if (!strcmp(op, "trace_open")) {
        op_trace_open(id, s);
    } else if (!strcmp(op, "trace_close")) {
        if (trace_fd >= 0) close(trace_fd);
        trace_fd = -1;
        printf("{\"id\":%ld,\"ok\":true}\n", id);
    } else if (!strcmp(op, "watchdog")) {
        double sec;
        if (!get_double(s, "seconds", &sec) || !isfinite(sec) || sec < 1e-9 || sec > 1.8e10) {   /* 1 ns 以上・uint64 の ns に収まる */
            reply_error(id, "watchdog needs 1e-9 <= seconds <= 1.8e10"); return 1;
        }
        watchdog_ns = (uint64_t)(sec * 1e9);
        printf("{\"id\":%ld,\"ok\":true}\n", id);
    } else if (!strcmp(op, "ping")) {
        printf("{\"id\":%ld,\"ok\":true}\n", id);
    } else if (!strcmp(op, "ring_open")) {
        op_ring_open(id, s);
    } else if (!strcmp(op, "ring_close")) {
        if (ring) munmap((void *)ring, ring_len);
        ring = NULL;
        printf("{\"id\":%ld,\"ok\":true}\n", id);
    } else if (!strcmp(op, "bp_add")) {
        op_bp_add(id, s);
    } else if (!strcmp(op, "bp_remove")) {
        op_bp_remove(id, s);
    } else if (!strcmp(op, "quit")) {
        release_all();
        printf("{\"id\":%ld,\"ok\":true}\n", id);
        fflush(stdout);
        return 0;
    } else {
        reply_error(id, "unknown op %s", op);
    }
    fflush(stdout);
    return 1;
}

int main(void) {
    static char in[LINE_MAX_LEN];
    size_t len = 0;
    int in_open = 1;
    signal(SIGPIPE, SIG_IGN);
    signal(SIGHUP, SIG_IGN);                                           /* SSH が切れても続ける（linger のとき） */
    out_init();
    last_input_ns = now_ns();
    for (;;) {
        if (!in_open && !traced_pid) break;                             /* linger: ptrace していたプロセスが終わった */
        flush_pending();                                                /* 道 A の途中の行の残り */
        struct pollfd p[2] = {{in_open ? 0 : -1, POLLIN, 0}, {trace_fd, POLLIN, 0}};
        int np = trace_fd >= 0 ? 2 : 1;
        int r = poll(p, np, traced_pid ? 1 : 100);                     /* ptrace 中は waitpid を 1 ms ごとに見る */
        if (traced_pid) { reap(); drain(); }
        /* デーモンからの入力が途絶えた（古い接続）。stdin に読むものが待っていれば途絶えていない（ptrace の処理で読むのが遅れただけ） */
        if (in_open && watchdog_ns && !(r > 0 && (p[0].revents & (POLLIN | POLLHUP)))
            && now_ns() - last_input_ns > watchdog_ns) {
            fprintf(stderr, "fprobe_agent: watchdog: no input for %.3f s (%s)\n", watchdog_ns / 1e9,
                    ring && ring_linger && traced_pid ? "linger until the traced process exits" : "removing breakpoints and exiting");
            if (ring && ring_linger && traced_pid) { in_open = 0; continue; }   /* linger: ptrace しているプロセスが終わるまで続ける */
            break;                                                       /* bp を戻して終わる */
        }
        if (r <= 0) continue;
        if (np == 2 && (p[1].revents & POLLIN)) pump_trace();
        if (p[0].revents & (POLLIN | POLLHUP)) {
            ssize_t n = read(0, in + len, sizeof in - 1 - len);
            last_input_ns = now_ns();
            if (n <= 0) {                                                /* デーモン（SSH）が切れた */
                if (ring && ring_linger && traced_pid) { in_open = 0; continue; }
                break;
            }
            len += n;
            in[len] = 0;
            char *start = in, *nl;
            while ((nl = strchr(start, '\n'))) {
                *nl = 0;
                if (!handle(start)) goto out;
                if (traced_pid) drain();                                 /* 操作の間に止めたもの・当たったものを続ける */
                start = nl + 1;
            }
            len -= start - in;
            memmove(in, start, len);
        }
    }
out:
    release_all();
    return 0;
}
