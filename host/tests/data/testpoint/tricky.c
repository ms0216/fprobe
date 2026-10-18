/* テストポイントの挿入の試験: 難しい書き方を集めたソース（コメントの中の { return } は無視される） */
#include <stdint.h>
#define CHECK(x) do { if (!(x)) return -1; } while (0)
#define LOOP for (;;)
#define FOREACH(i, n) for (int i = 0; i < (n); i++)
#define TRACE(x)

typedef struct { int a, b; } pair_t;
struct bits { unsigned f : 3; };
static const char *msg = "{ not a block } /* nor a comment */ return 1;";
static const char brace = '}';
volatile int state;
int table[4];
pair_t pair;

// 早期 return、else if、括弧の無い子、マクロに隠れた return
static int early(int x) {
    if (x < 0) return -1;   // 早期 return
    if (x == 0)
        return 0;
    else if (x == 1) return 1;
    CHECK(x != 3);
    return x * 2;
}

int with_goto(int x) {
    int r = 0;
    if (x > 10) goto fail;
    r = x;
    state = r + 1;
    return r;
fail:
    state = -1;
    return -1;
}

void loops(int n) {
    int i = 0;
    while (i < n) i++;
    do { i--; } while (i > 0);
    for (int k = 0; k < n; k++) {
        if (k == 2) continue;
        if (k == 5) break;
        table[k & 3] = k;
    }
    FOREACH(j, n) { state = j; }
    switch (n) {
    case 1:
    case 2: state++; break;
    case 3: { state = 3; }
    default: break;
    }
    switch (n) case 7: state = 7;
    if (n > 100) LOOP { TRACE(n) break; }
}

pair_t make(int a) { pair_t p = { .a = a, .b = (int)sizeof(struct bits) }; pair.a = a; return p; }
unsigned bitf(struct bits *b) { return b->f; }
const char *str(void) { return msg[0] == brace ? "}" : "{"; }
static inline __attribute__((always_inline)) int sq(int v) { return v * v; }
int (*getter(void))(int) { return sq; }
void nothing(void) {}
void ret_void(int x) { if (x) return; state = 2; }
static void call_void(void) { return nothing(); }

int combined(void) {
#ifdef FEATURE
    state = 10;
#else
    state = 11;
#endif
    int s = ({ int t = 3; t * 2; });
    static int once = 5;
    once = once + 1;
    call_void();
    return s + sq(2) + (int)bitf(&(struct bits){ 5 }) + early(4) + with_goto(3) + make(1).a + getter()(2) + *str();
}
