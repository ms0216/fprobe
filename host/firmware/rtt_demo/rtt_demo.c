// fprobe の「止めずに見る」機能（RTT・SWO の ITM / DWT・セミホスティング）の試験用ファームウェア。
// Cortex-M4（Nucleo F401RE）の SRAM に読み込んで SRAM から実行する（Flash には書かない）。
// 作り方: python host/firmware/rtt_demo/build.py（zig cc。生成した rtt_demo.elf は試験のためにリポジトリに置く）。
// 仕様: docs/superpowers/specs/2026-10-17-f1-rtt-itm-semihosting-design.md の 4 節。
//
// RTT は公開されているメモリ上の形式（制御ブロック・バッファの記述子）に従う自作の最小実装（SEGGER のソースは使わない）。
//   上り 0 "Terminal"（512 B、NO_BLOCK_SKIP）: 100 ms ごとに "tick <ms>"、下り 0 から来た行を "echo: <行>" で返す
//   上り 1 "Data"（256 B、NO_BLOCK_TRIM）  : 100 ms ごとに 32 ビットの通し番号（リトル・エンディアン）
//   下り 0 "Terminal"（64 B）
// SWO（ITM）: ポート 0 に "itm <ms>"、ポート 1 に 32 ビットの通し番号。SysTick（1 kHz）の例外トレース、
//   ローカルのタイムスタンプ、同期パケット。TPIU は NRZ・2 Mbaud（HSI 16 MHz / 8）・フォーマッタ無効、SWO は PB3。
// セミホスティング: semihosting_enabled を PC が 1 にしたときだけ 1 回呼ぶ（処理しないデバッガでは BKPT で止まるため）。
#include <stdint.h>

extern uint32_t __bss_start, __bss_end, __stack_top;

#define REG32(addr) (*(volatile uint32_t *)(addr))

// --- 設定（PC が書き換えてよい変数） -------------------------------------------------------------

#define TRACE_SETUP      (1u << 0)      // ITM・TPIU・SWO のピンをファーム自身が設定する
#define TRACE_TIMESTAMPS (1u << 1)      // ローカルのタイムスタンプ
#define TRACE_EXCEPTIONS (1u << 2)      // 例外トレース（DWT）
#define TRACE_PC_SAMPLES (1u << 3)      // PC サンプリング（1024 クロックごと。帯域を使う）
#define TRACE_SYNC       (1u << 4)      // 同期パケット（途中から受けても同期を取り直せる）

volatile uint32_t trace_config = TRACE_SETUP | TRACE_TIMESTAMPS | TRACE_EXCEPTIONS | TRACE_SYNC;
volatile uint32_t semihosting_enabled;  // 0 以外で、起動後に 1 回だけセミホスティングを呼ぶ
volatile uint32_t semihosting_exit;     // 0 以外で、セミホスティングの最後に SYS_EXIT_EXTENDED（終了コード = この値 - 1）

// --- 観測用（PC が読む変数） -------------------------------------------------------------------

volatile uint32_t ticks;                // SysTick（1 ms）の回数
volatile uint32_t sequence;             // 100 ms ごとに増える通し番号
volatile uint32_t echo_count;           // 下りから受けて返した行の数
volatile uint32_t semihosting_done;
volatile int32_t sh_clock;              // SYS_CLOCK の結果（センチ秒）
volatile int32_t sh_time;               // SYS_TIME の結果（1970 年からの秒）
volatile int32_t sh_write_result;       // SYS_WRITE の結果（書けなかったバイト数。0 で成功）

// --- RTT（自作の最小実装） --------------------------------------------------------------------

#define RTT_MODE_NO_BLOCK_SKIP      0u
#define RTT_MODE_NO_BLOCK_TRIM      1u
#define RTT_MODE_BLOCK_IF_FIFO_FULL 2u
#define RTT_MODE_MASK               3u

typedef struct {
    const char *name;
    uint8_t *buffer;
    uint32_t size;
    volatile uint32_t wr_off;           // 上り: ターゲットが書く / 下り: PC が書く
    volatile uint32_t rd_off;           // 上り: PC が書く / 下り: ターゲットが書く
    uint32_t flags;
} rtt_buffer_t;

typedef struct {
    char id[16];
    int32_t max_up;
    int32_t max_down;
    rtt_buffer_t up[2];
    rtt_buffer_t down[1];
} rtt_control_block_t;

static uint8_t up0_buffer[512];
static uint8_t up1_buffer[256];
static uint8_t down0_buffer[64];

// 静的に初期化する（.data）。SRAM に直接読み込むので、読み込んだ時点で制御ブロックが有効になる。
// ID の並びはここ 1 か所だけ（PC の走査が偽物を見つけないように、ほかに同じ並びを置かない）。
__attribute__((used, aligned(4))) rtt_control_block_t _SEGGER_RTT = {
    .id = "SEGGER RTT",
    .max_up = 2,
    .max_down = 1,
    .up = {
        {"Terminal", up0_buffer, sizeof up0_buffer, 0, 0, RTT_MODE_NO_BLOCK_SKIP},
        {"Data", up1_buffer, sizeof up1_buffer, 0, 0, RTT_MODE_NO_BLOCK_TRIM},
    },
    .down = {
        {"Terminal", down0_buffer, sizeof down0_buffer, 0, 0, RTT_MODE_NO_BLOCK_SKIP},
    },
};

static inline void memory_barrier(void) {
    __asm volatile("dmb" ::: "memory");
}

static uint32_t ring_free(const rtt_buffer_t *b) {
    uint32_t wr = b->wr_off, rd = b->rd_off;
    uint32_t used = wr >= rd ? wr - rd : b->size - rd + wr;
    return b->size - 1 - used;
}

// 入るだけ書く（データを書いてから、バリアを挟んで WrOff を進める）
static uint32_t ring_put(rtt_buffer_t *b, const uint8_t *data, uint32_t len) {
    uint32_t wr = b->wr_off;
    for (uint32_t i = 0; i < len; i++) {
        b->buffer[wr] = data[i];
        wr = wr + 1 == b->size ? 0 : wr + 1;
    }
    memory_barrier();
    b->wr_off = wr;
    return len;
}

// 上りに書く。モードに従い、書いたバイト数を返す。
uint32_t rtt_write(uint32_t channel, const void *data, uint32_t len) {
    rtt_buffer_t *b = &_SEGGER_RTT.up[channel];
    const uint8_t *p = data;
    uint32_t free = ring_free(b);
    switch (b->flags & RTT_MODE_MASK) {
    case RTT_MODE_NO_BLOCK_SKIP:
        return len <= free ? ring_put(b, p, len) : 0;
    case RTT_MODE_NO_BLOCK_TRIM:
        return ring_put(b, p, len < free ? len : free);
    default: {                          // BLOCK_IF_FIFO_FULL: 空くまで待つ
        uint32_t done = 0;
        while (done < len) {
            uint32_t n = ring_free(b);
            if (n > len - done) {
                n = len - done;
            }
            done += ring_put(b, p + done, n);
        }
        return done;
    }
    }
}

// 下りから読む（最大 max バイト）。データを読んでから、バリアを挟んで RdOff を進める。
uint32_t rtt_read(uint32_t channel, void *out, uint32_t max) {
    rtt_buffer_t *b = &_SEGGER_RTT.down[channel];
    uint8_t *p = out;
    uint32_t rd = b->rd_off, wr = b->wr_off, n = 0;
    memory_barrier();
    while (rd != wr && n < max) {
        p[n++] = b->buffer[rd];
        rd = rd + 1 == b->size ? 0 : rd + 1;
    }
    memory_barrier();
    b->rd_off = rd;
    return n;
}

// --- ITM（SWO） ----------------------------------------------------------------------------

#define ITM_STIM(n)  (0xE0000000u + 4u * (n))
#define ITM_TER      0xE0000E00u
#define ITM_TCR      0xE0000E80u
#define ITM_LAR      0xE0000FB0u
#define DWT_CTRL     0xE0001000u
#define DEMCR        0xE000EDFCu
#define TPIU_ACPR    0xE0040010u
#define TPIU_SPPR    0xE00400F0u
#define TPIU_FFCR    0xE0040304u
#define DBGMCU_CR    0xE0042004u
#define RCC_CR       0x40023800u
#define RCC_CFGR     0x40023808u
#define RCC_AHB1ENR  0x40023830u
#define GPIOB_MODER  0x40020400u
#define GPIOB_OSPEED 0x40020408u
#define GPIOB_AFRL   0x40020420u
#define SYST_CSR     0xE000E010u
#define SYST_RVR     0xE000E014u
#define SYST_CVR     0xE000E018u
#define SCB_VTOR     0xE000ED08u

#define CORE_HZ   16000000u             // HSI
#define SWO_BAUD  2000000u

static int itm_enabled(uint32_t port) {
    return (REG32(ITM_TCR) & 1u) && (REG32(ITM_TER) & (1u << port));
}

static void itm_put8(uint32_t port, uint8_t value) {
    if (!itm_enabled(port)) {
        return;                         // ITM が無効なら捨てる（待ち続けない）
    }
    while ((REG32(ITM_STIM(port)) & 1u) == 0) {
    }
    *(volatile uint8_t *)ITM_STIM(port) = value;
}

static void itm_put32(uint32_t port, uint32_t value) {
    if (!itm_enabled(port)) {
        return;
    }
    while ((REG32(ITM_STIM(port)) & 1u) == 0) {
    }
    REG32(ITM_STIM(port)) = value;
}

static void itm_print(uint32_t port, const char *s, uint32_t len) {
    for (uint32_t i = 0; i < len; i++) {
        itm_put8(port, (uint8_t)s[i]);
    }
}

// HSI 16 MHz に切り替え、PB3 を SWO に、TPIU を NRZ 2 Mbaud（フォーマッタ無効）に、ITM・DWT を trace_config のとおりに
static void trace_setup(uint32_t config) {
    REG32(RCC_CR) |= 1u;                                    // HSI を有効に
    while ((REG32(RCC_CR) & 2u) == 0) {
    }
    REG32(RCC_CFGR) &= ~3u;                                 // システム・クロック = HSI
    while ((REG32(RCC_CFGR) & 0xCu) != 0) {
    }
    REG32(RCC_AHB1ENR) |= 1u << 1;                          // GPIOB
    REG32(GPIOB_MODER) = (REG32(GPIOB_MODER) & ~(3u << 6)) | (2u << 6);      // PB3 = 代替機能
    REG32(GPIOB_OSPEED) |= 3u << 6;
    REG32(GPIOB_AFRL) &= ~(0xFu << 12);                     // AF0 = TRACESWO
    REG32(DBGMCU_CR) = (REG32(DBGMCU_CR) & ~(3u << 6)) | (1u << 5);         // TRACE_IOEN、非同期
    REG32(DEMCR) |= 1u << 24;                               // TRCENA
    REG32(TPIU_SPPR) = 2u;                                  // NRZ（UART 方式）
    REG32(TPIU_ACPR) = CORE_HZ / SWO_BAUD - 1u;
    REG32(TPIU_FFCR) = 0x100u;                              // フォーマッタ無効
    REG32(ITM_LAR) = 0xC5ACCE55u;
    REG32(ITM_TCR) = 0;
    uint32_t dwt = 1u;                                      // CYCCNTENA（同期パケット・PC サンプリングの元）
    if (config & TRACE_SYNC) {
        dwt |= 1u << 10;                                    // SYNCTAP = CYCCNT[24]（約 1 秒ごと）
    }
    if (config & TRACE_PC_SAMPLES) {
        dwt |= (15u << 1) | (1u << 12);                     // POSTPRESET = 15、CYCTAP = 0 → 1024 クロックごと
    }
    if (config & TRACE_EXCEPTIONS) {
        dwt |= 1u << 16;                                    // EXCTRCENA
    }
    REG32(DWT_CTRL) = dwt;
    uint32_t tcr = (1u << 16) | (1u << 3) | 1u;             // TraceBusID = 1、DWT の転送、ITMENA
    if (config & TRACE_TIMESTAMPS) {
        tcr |= 1u << 1;                                     // TSENA
    }
    if (config & TRACE_SYNC) {
        tcr |= 1u << 2;                                     // SYNCENA
    }
    REG32(ITM_TER) = 3u;                                    // ポート 0・1
    REG32(ITM_TCR) = tcr;
}

// --- セミホスティング --------------------------------------------------------------------------

#define SYS_OPEN          0x01
#define SYS_WRITE0        0x04
#define SYS_WRITE         0x05
#define SYS_CLOCK         0x10
#define SYS_TIME          0x11
#define SYS_EXIT_EXTENDED 0x20
#define ADP_STOPPED_APPLICATION_EXIT 0x20026u

static int32_t semihost(uint32_t op, const void *arg) {
    register uint32_t r0 __asm("r0") = op;
    register const void *r1 __asm("r1") = arg;
    __asm volatile("bkpt 0xAB" : "+r"(r0) : "r"(r1) : "memory");
    return (int32_t)r0;
}

static void semihosting_demo(void) {
    static const char hello[] = "hello from semihosting\n";
    static const char tt[] = ":tt";
    static const char via_write[] = "via SYS_WRITE\n";
    semihost(SYS_WRITE0, hello);
    uint32_t open_args[3] = {(uint32_t)tt, 4u, sizeof tt - 1u};             // モード 4 = "w"（標準出力）
    int32_t handle = semihost(SYS_OPEN, open_args);
    uint32_t write_args[3] = {(uint32_t)handle, (uint32_t)via_write, sizeof via_write - 1u};
    sh_write_result = semihost(SYS_WRITE, write_args);
    sh_clock = semihost(SYS_CLOCK, 0);
    sh_time = semihost(SYS_TIME, 0);
    semihosting_done = 1;
    if (semihosting_exit) {
        uint32_t exit_args[2] = {ADP_STOPPED_APPLICATION_EXIT, semihosting_exit - 1u};
        semihost(SYS_EXIT_EXTENDED, exit_args);
    }
}

// --- 文字列の組み立て ------------------------------------------------------------------------

static uint32_t append(char *out, uint32_t at, const char *s) {
    while (*s) {
        out[at++] = *s++;
    }
    return at;
}

static uint32_t append_u32(char *out, uint32_t at, uint32_t value) {
    char digits[10];
    uint32_t n = 0;
    do {
        digits[n++] = (char)('0' + value % 10u);
        value /= 10u;
    } while (value);
    while (n) {
        out[at++] = digits[--n];
    }
    return at;
}

// --- 本体 ----------------------------------------------------------------------------------

void systick_handler(void) {
    ticks++;
}

static void report(uint32_t now) {
    char line[32];
    uint32_t n = append_u32(line, append(line, 0, "tick "), now);
    line[n++] = '\n';
    rtt_write(0, line, n);
    uint32_t seq = sequence++;
    rtt_write(1, &seq, sizeof seq);
    n = append_u32(line, append(line, 0, "itm "), now);
    line[n++] = '\n';
    itm_print(0, line, n);
    itm_put32(1, seq);
}

static void echo(void) {
    static char pending[64];
    static uint32_t length;
    char c;
    while (rtt_read(0, &c, 1)) {
        if (c == '\n' || length == sizeof pending) {
            char line[80];
            uint32_t n = append(line, 0, "echo: ");
            for (uint32_t i = 0; i < length; i++) {
                line[n++] = pending[i];
            }
            line[n++] = '\n';
            rtt_write(0, line, n);
            echo_count++;
            length = 0;
            if (c == '\n') {
                continue;
            }
        }
        pending[length++] = c;
    }
}

__attribute__((noreturn)) void reset_handler(void) {
    for (uint32_t *p = &__bss_start; p < &__bss_end; p++) {
        *p = 0;
    }
    extern const void *const vectors[];
    REG32(SCB_VTOR) = (uint32_t)vectors;
    if (trace_config & TRACE_SETUP) {
        trace_setup(trace_config);
    }
    REG32(SYST_RVR) = CORE_HZ / 1000u - 1u;
    REG32(SYST_CVR) = 0;
    REG32(SYST_CSR) = 7u;                                   // コアのクロック、割り込み、有効
    uint32_t next = 0;
    for (;;) {
        uint32_t now = ticks;
        if ((int32_t)(now - next) >= 0) {
            next = now + 100u;
            report(now);
        }
        echo();
        if (semihosting_enabled && !semihosting_done) {
            semihosting_demo();
        }
    }
}

__attribute__((noreturn)) void default_handler(void) {
    for (;;) {
    }
}

// ベクタ表（SRAM の先頭。デバッガが VTOR・SP・PC を設定して起動し、ファームも VTOR を設定し直す）
__attribute__((section(".vectors"), used)) const void *const vectors[16] = {
    &__stack_top, (void *)reset_handler, (void *)default_handler, (void *)default_handler,
    (void *)default_handler, (void *)default_handler, (void *)default_handler, 0, 0, 0, 0,
    (void *)default_handler, (void *)default_handler, 0, (void *)default_handler, (void *)systick_handler,
};
