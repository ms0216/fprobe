// fprobe の「既存のログ・トレースの形式の取り込み」（U14・U15）の実機試験用ファームウェア。
// Cortex-M4（Nucleo F401RE）の SRAM に読み込んで SRAM から実行する（Flash には書かない）。
// 作り方: python host/firmware/formats_demo/build.py（zig cc。生成した formats_demo.elf は試験のためにリポジトリに置く）。
// 仕様: docs/superpowers/specs/2026-10-18-u14-u15-trace-log-formats-design.md の 6 節。
//
// 各形式の書き手は、公開されている形式の説明・オープンソースの実装から書いた自作の最小実装（各ベンダーのソースは使わない）。
//   RTT 上り 0 "Terminal" : 100 ms ごとに "tick <ms>"、1 秒ごとに "pc=0x<systick_handler の番地>"（番地の解読の試験）
//   RTT 上り 1 "defmt"    : defmt（rzcobs）の "tick {=u32} seq={=u16}"（info）と、10 回に 1 回 "late {=i16} ms"（warn）
//   RTT 上り 2 "SysView"  : SystemView。下り "SysView" に 1（START）が来たら（または sysview_autostart）始める。
//                           2 つのタスク（worker・IDLE）を 10 ms ごとに切り替え、SysTick を割り込みとして記録する
//   RTT 上り 3 "Compact"  : 書式の文字列の番地 + 引数の語（compact）
//   ITM ポート 2（SWO）   : Trice（枠なし。ID 100 = "inf:seq %u\n"、til.json は試験が作る）
//   RAM: Event Recorder（EventRecorderInfo。RTX5 の ThreadSwitched と stdout）、Zephyr の CTF の RAM の backend
//        （ram_tracing。ts32_id8）、ThreadX の TraceX のバッファ（_tx_trace_header_ptr）
#include <stdint.h>

extern uint32_t __bss_start, __bss_end, __stack_top;

#define REG32(addr) (*(volatile uint32_t *)(addr))

#define ITM_STIM(n)  (0xE0000000u + 4u * (n))
#define ITM_TER      0xE0000E00u
#define ITM_TCR      0xE0000E80u
#define ITM_LAR      0xE0000FB0u
#define DWT_CTRL     0xE0001000u
#define DWT_CYCCNT   0xE0001004u
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

#define CORE_HZ  16000000u              // HSI
#define SWO_BAUD 2000000u
#define RAM_BASE 0x20000000u

// --- 観測用・設定（PC が読む・書く変数） --------------------------------------------------------

volatile uint32_t ticks;                // SysTick（1 ms）の回数
volatile uint32_t sequence;             // 10 ms ごとに増える通し番号
volatile uint32_t sysview_autostart;    // 0 以外: 下りのコマンドを待たずに SystemView を始める
volatile uint32_t sysview_running;

static inline uint32_t irq_save(void) {
    uint32_t primask;
    __asm volatile("mrs %0, primask\n cpsid i" : "=r"(primask)::"memory");
    return primask;
}

static inline void irq_restore(uint32_t primask) {
    __asm volatile("msr primask, %0" ::"r"(primask) : "memory");
}

static inline void memory_barrier(void) {
    __asm volatile("dmb" ::: "memory");
}

static inline uint32_t cycles(void) {
    return REG32(DWT_CYCCNT);
}

// --- RTT（自作の最小実装。rtt_demo と同じ） -----------------------------------------------------

typedef struct {
    const char *name;
    uint8_t *buffer;
    uint32_t size;
    volatile uint32_t wr_off;
    volatile uint32_t rd_off;
    uint32_t flags;
} rtt_buffer_t;

typedef struct {
    char id[16];
    int32_t max_up;
    int32_t max_down;
    rtt_buffer_t up[4];
    rtt_buffer_t down[2];
} rtt_control_block_t;

static uint8_t up0_buffer[512];
static uint8_t up1_buffer[1024];
static uint8_t up2_buffer[4096];
static uint8_t up3_buffer[256];
static uint8_t down0_buffer[16];
static uint8_t down1_buffer[8];

__attribute__((used, aligned(4))) rtt_control_block_t _SEGGER_RTT = {
    .id = "SEGGER RTT",
    .max_up = 4,
    .max_down = 2,
    .up = {
        {"Terminal", up0_buffer, sizeof up0_buffer, 0, 0, 0},
        {"defmt", up1_buffer, sizeof up1_buffer, 0, 0, 0},
        {"SysView", up2_buffer, sizeof up2_buffer, 0, 0, 0},
        {"Compact", up3_buffer, sizeof up3_buffer, 0, 0, 0},
    },
    .down = {
        {"Terminal", down0_buffer, sizeof down0_buffer, 0, 0, 0},
        {"SysView", down1_buffer, sizeof down1_buffer, 0, 0, 0},
    },
};

static uint32_t ring_free(const rtt_buffer_t *b) {
    uint32_t wr = b->wr_off, rd = b->rd_off;
    uint32_t used = wr >= rd ? wr - rd : b->size - rd + wr;
    return b->size - 1 - used;
}

// NO_BLOCK_SKIP: 全部入らなければ書かない（かたまりを分けない）
static uint32_t rtt_write(uint32_t channel, const void *data, uint32_t len) {
    rtt_buffer_t *b = &_SEGGER_RTT.up[channel];
    if (len > ring_free(b)) {
        return 0;
    }
    const uint8_t *p = data;
    uint32_t wr = b->wr_off;
    for (uint32_t i = 0; i < len; i++) {
        b->buffer[wr] = p[i];
        wr = wr + 1 == b->size ? 0 : wr + 1;
    }
    memory_barrier();
    b->wr_off = wr;
    return len;
}

static int rtt_getc(uint32_t channel) {
    rtt_buffer_t *b = &_SEGGER_RTT.down[channel];
    uint32_t rd = b->rd_off;
    if (rd == b->wr_off) {
        return -1;
    }
    memory_barrier();
    int c = b->buffer[rd];
    b->rd_off = rd + 1 == b->size ? 0 : rd + 1;
    return c;
}

// --- 文字 ----------------------------------------------------------------------------------------

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

static uint32_t append_hex8(char *out, uint32_t at, uint32_t value) {
    for (int shift = 28; shift >= 0; shift -= 4) {
        out[at++] = "0123456789abcdef"[(value >> shift) & 0xFu];
    }
    return at;
}

static void put_le(uint8_t *out, uint32_t value, uint32_t bytes) {
    for (uint32_t i = 0; i < bytes; i++) {
        out[i] = (uint8_t)(value >> (8u * i));
    }
}

// --- defmt（表は .defmt の区画のシンボル。番号 = シンボルの番地） ---------------------------------------

#define J(tag, data, n) "{\"package\":\"formats_demo\",\"tag\":\"defmt_" tag "\",\"data\":\"" data \
                        "\",\"disambiguator\":\"" #n "\",\"crate_name\":\"formats_demo\"}"
#define DEFMT_ENTRY(sec, name, json) \
    __attribute__((section(sec), used)) static const unsigned char name __asm__(json) = 0;

__attribute__((used)) const unsigned char defmt_version __asm__("_defmt_version_ = 4") = 0;
__attribute__((used)) const unsigned char defmt_encoding __asm__("_defmt_encoding_ = rzcobs") = 0;
DEFMT_ENTRY(".defmt.timestamp.1", defmt_timestamp, J("timestamp", "{=u32:us}", 1))

// rzCOBS（github.com/Dirbaio/rzcobs と同じ符号化）で 1 つのかたまりを上り 1 に書く（先頭のかたまりの前に 0x00）
static void defmt_send(const uint8_t *data, uint32_t len) {
    static uint32_t started;
    uint8_t out[64];
    uint32_t n = 0, run = 0, zeros = 0;
    if (!started) {
        out[n++] = 0;
        started = 1;
    }
    for (uint32_t i = 0; i < len; i++) {
        uint8_t b = data[i];
        if (run < 7) {
            if (b == 0) {
                zeros |= 1u << run;
            } else {
                out[n++] = b;
            }
            run++;
            if (run == 7 && zeros) {
                out[n++] = (uint8_t)zeros;
                run = zeros = 0;
            }
        } else if (b == 0) {
            out[n++] = (uint8_t)((run - 7u) | 0x80u);
            run = zeros = 0;
        } else {
            out[n++] = b;
            if (++run == 134) {
                out[n++] = 0xFF;
                run = zeros = 0;
            }
        }
    }
    if (run >= 1 && run <= 6) {
        out[n++] = (uint8_t)((zeros | (0xFFu << run)) & 0x7Fu);
    } else if (run >= 7) {
        out[n++] = (uint8_t)((run - 7u) | 0x80u);
    }
    out[n++] = 0;
    rtt_write(1, out, n);
}

static void defmt_tick(uint32_t now, uint32_t seq) {
    DEFMT_ENTRY(".defmt.info.1", DEFMT_LOG_STATEMENT, J("info", "tick {=u32} seq={=u16}", 10))
    uint8_t frame[12];
    put_le(frame, (uint16_t)(uintptr_t)&DEFMT_LOG_STATEMENT, 2);
    put_le(frame + 2, now * 1000u, 4);                      // 時刻（µs）
    put_le(frame + 6, now, 4);
    put_le(frame + 10, seq, 2);
    defmt_send(frame, sizeof frame);
}

static void defmt_late(uint32_t now, int16_t late) {
    DEFMT_ENTRY(".defmt.warn.1", DEFMT_LOG_STATEMENT, J("warn", "late {=i16} ms", 11))
    uint8_t frame[8];
    put_le(frame, (uint16_t)(uintptr_t)&DEFMT_LOG_STATEMENT, 2);
    put_le(frame + 2, now * 1000u, 4);
    put_le(frame + 6, (uint16_t)late, 2);
    defmt_send(frame, sizeof frame);
}

// --- SystemView（SEGGER_SYSVIEW.c の _SendPacket と同じ符号化） -------------------------------------

typedef struct {
    uint32_t dummy[4];                  // タスクの番地（ID）にするだけの実体
} fake_tcb_t;

fake_tcb_t task_worker, task_idle;
static uint32_t sv_last;                // 前のパケットの時刻（サイクル）
static fake_tcb_t *sv_current;

static uint32_t sv_varint(uint8_t *out, uint32_t n, uint32_t value) {
    while (value > 0x7Fu) {
        out[n++] = (uint8_t)(value | 0x80u);
        value >>= 7;
    }
    out[n++] = (uint8_t)value;
    return n;
}

static uint32_t sv_shrink(const void *p) {
    return ((uint32_t)p - RAM_BASE) >> 2;
}

// 1 つのパケット（ID < 24 は長さの欄なし）。payload は符号化済み。時刻の差を付けて書く（割り込みを止めて）
static void sv_packet(uint32_t id, const uint8_t *payload, uint32_t len) {
    uint8_t out[80];
    uint32_t primask = irq_save();
    uint32_t now = cycles();
    uint32_t n = sv_varint(out, 0, id);
    if (id >= 24) {
        n = sv_varint(out, n, len);
    }
    for (uint32_t i = 0; i < len; i++) {
        out[n++] = payload[i];
    }
    n = sv_varint(out, n, now - sv_last);
    sv_last = now;
    rtt_write(2, out, n);
    irq_restore(primask);
}

static void sv_task_info(fake_tcb_t *task, uint32_t prio, const char *name) {
    uint8_t p[48];
    uint32_t n = sv_varint(p, 0, sv_shrink(task));
    n = sv_varint(p, n, prio);
    uint32_t len = 0;
    while (name[len]) {
        len++;
    }
    p[n++] = (uint8_t)len;
    for (uint32_t i = 0; i < len; i++) {
        p[n++] = (uint8_t)name[i];
    }
    sv_packet(9, p, n);
}

static void sysview_start(void) {
    static const uint8_t sync[10] = {0};
    static const char desc[] = "N=formats_demo,O=demo,D=STM32F401";
    uint8_t p[64];
    uint32_t primask = irq_save();
    rtt_write(2, sync, sizeof sync);
    uint8_t start[2] = {10, 0};                             // TRACE_START（差 0）
    rtt_write(2, start, 2);
    uint32_t n = sv_varint(p, 0, CORE_HZ);
    n = sv_varint(p, n, CORE_HZ);
    n = sv_varint(p, n, RAM_BASE);
    n = sv_varint(p, n, 2);                                 // ID_SHIFT
    uint8_t head[2] = {24, (uint8_t)n};
    rtt_write(2, head, 2);
    rtt_write(2, p, n);
    uint8_t zero = 0;
    rtt_write(2, &zero, 1);                                 // INIT の差 0
    sv_last = cycles();
    n = sv_varint(p, 0, 12);                                // SYSTIME_CYCLES（絶対の時刻）
    n = sv_varint(p, n, sv_last);
    p[n++] = 0;
    rtt_write(2, p, n);
    irq_restore(primask);
    n = 0;
    p[n++] = (uint8_t)(sizeof desc - 1);
    for (uint32_t i = 0; i + 1 < sizeof desc; i++) {
        p[n++] = (uint8_t)desc[i];
    }
    sv_packet(14, p, n);
    sv_task_info(&task_worker, 2, "worker");
    sv_task_info(&task_idle, 0, "IDLE");
    sysview_running = 1;
}

static void sysview_switch(fake_tcb_t *next) {
    uint8_t p[8];
    if (sv_current) {
        sv_packet(5, 0, 0);                                 // TASK_STOP_EXEC
    }
    sv_packet(4, p, sv_varint(p, 0, sv_shrink(next)));      // TASK_START_EXEC
    sv_current = next;
}

static void sysview_print(uint32_t seq) {
    static const char fmt[] = "seq=%u";
    uint8_t p[24];
    uint32_t n = 0;
    p[n++] = (uint8_t)(sizeof fmt - 1);
    for (uint32_t i = 0; i + 1 < sizeof fmt; i++) {
        p[n++] = (uint8_t)fmt[i];
    }
    n = sv_varint(p, n, 0);                                 // Options（レベル LOG）
    n = sv_varint(p, n, 1);                                 // 引数の数
    n = sv_varint(p, n, seq);
    sv_packet(26, p, n);
}

// --- Event Recorder（CMSIS-View の EventRecorder.c のプロトコル 1.1 と同じ RAM の形） ------------------

#define ER_COUNT 16u
typedef struct {
    uint32_t ts, val1, val2, info;
} er_record_t;
typedef struct {
    uint8_t state, context;
    uint16_t info_crc;
    uint32_t record_index, records_written, records_dumped, ts_overflow, ts_freq, ts_last, init_count, signature;
} er_status_t;
typedef struct {
    uint8_t protocol_type, reserved;
    uint16_t protocol_version;
    uint32_t record_count;
    er_record_t *event_buffer;
    uint8_t *event_filter;
    er_status_t *event_status;
    uint8_t ts_source, reserved3[3];
} er_info_t;

static er_record_t er_buffer[ER_COUNT] __attribute__((aligned(16)));
static uint8_t er_filter[128];
static er_status_t er_status __attribute__((aligned(64)));
__attribute__((used)) const er_info_t EventRecorderInfo = {
    1, 0, 0x0101, ER_COUNT, er_buffer, er_filter, &er_status, 1, {0, 0, 0},
};

static void er_item(uint32_t info, uint32_t ts, uint32_t v1, uint32_t v2) {
    uint32_t i = er_status.record_index++;
    er_record_t *r = &er_buffer[i & (ER_COUNT - 1u)];
    uint32_t tbit = (r->info >> 31) ^ 1u;
    info |= ((i / ER_COUNT) & 0xFu) << 20 | 1u << 27;       // 周回・VALID
    info |= (ts >> 31) << 28 | (v1 >> 31) << 29 | (v2 >> 31) << 30 | tbit << 31;
    r->info = (r->info & 0x80000000u) | 1u << 26;            // LOCKED（書いている間）
    r->ts = (ts & 0x7FFFFFFFu) | tbit << 31;
    r->val1 = (v1 & 0x7FFFFFFFu) | tbit << 31;
    r->val2 = (v2 & 0x7FFFFFFFu) | tbit << 31;
    memory_barrier();
    r->info = info;
    er_status.records_written++;
    er_status.ts_last = ts;
}

static void er_record2(uint32_t id, uint32_t v1, uint32_t v2) {
    er_item(id | 1u << 24 | 1u << 25, ticks, v1, v2);       // FIRST・LAST
}

static void er_stdout(const char *s, uint32_t len) {
    while (len) {
        uint32_t n = len < 8u ? len : 8u;
        uint8_t b[8] = {0};
        for (uint32_t i = 0; i < n; i++) {
            b[i] = (uint8_t)s[i];
        }
        uint32_t v1 = b[0] | b[1] << 8 | b[2] << 16 | (uint32_t)b[3] << 24;
        uint32_t v2 = b[4] | b[5] << 8 | b[6] << 16 | (uint32_t)b[7] << 24;
        er_item(0xFE00u | ((n & 7u) << 16) | 1u << 24 | 1u << 25, ticks, v1, v2);
        s += n;
        len -= n;
    }
}

static void er_init(void) {
    er_status.signature = 0xE1A5276Bu;
    er_status.ts_freq = 1000u;                              // SysTick（1 ms）
    er_status.init_count = 1u;
    er_status.state = 1u;
    er_record2(0xFF00u, 1u, 1000u);                         // INIT
}

// --- Zephyr の CTF（RAM の backend。満ちたら止まる線形のバッファ。見出し u32 ns + u8 ID） ------------------

__attribute__((used)) uint8_t ram_tracing[2048];
static uint32_t ctf_pos;

static void ctf_record(uint8_t id, const uint8_t *payload, uint32_t len) {
    if (ctf_pos + 5u + len > sizeof ram_tracing) {
        return;
    }
    put_le(ram_tracing + ctf_pos, (uint32_t)(((uint64_t)cycles() * 125u) >> 1), 4);    // 16 MHz → ns（62.5 ns）
    ram_tracing[ctf_pos + 4] = id;
    for (uint32_t i = 0; i < len; i++) {
        ram_tracing[ctf_pos + 5 + i] = payload[i];
    }
    ctf_pos += 5u + len;
}

static void ctf_thread(uint8_t id, const fake_tcb_t *task, const char *name) {
    uint8_t p[24] = {0};
    put_le(p, (uint32_t)task, 4);
    for (uint32_t i = 0; name[i] && i < 19; i++) {
        p[4 + i] = (uint8_t)name[i];
    }
    ctf_record(id, p, sizeof p);
}

// --- ThreadX の TraceX（tx_trace_enable が作る形: 見出し・登録・32 バイトの出来事の循環） -------------------

#define TX_ENTRIES 32u
typedef struct {
    uint32_t id, mask, base, registry_start;
    uint16_t reserved1, name_size;
    uint32_t registry_end, buffer_start, buffer_end, buffer_current, reserved2, reserved3, reserved4;
} tx_header_t;
typedef struct {
    uint8_t available, type, reserved1, reserved2;
    uint32_t pointer, param1, param2;
    char name[32];
} tx_object_t;
typedef struct {
    uint32_t thread, priority, event, time, info[4];
} tx_event_t;
typedef struct {
    tx_header_t header;
    tx_object_t registry[2];
    tx_event_t events[TX_ENTRIES];
} tx_trace_t;

static tx_trace_t tx_trace;
__attribute__((used)) tx_header_t *_tx_trace_header_ptr;
static uint32_t tx_next;

static void tx_register(uint32_t i, const fake_tcb_t *task, uint32_t prio, const char *name) {
    tx_object_t *o = &tx_trace.registry[i];
    o->type = 1;                                            // thread
    o->reserved1 = (uint8_t)(0x80u | (prio >> 8));
    o->reserved2 = (uint8_t)prio;
    o->pointer = (uint32_t)task;
    o->param1 = (uint32_t)task;
    o->param2 = 512u;
    for (uint32_t k = 0; name[k] && k < 31; k++) {
        o->name[k] = name[k];
    }
}

static void tx_init(void) {
    tx_header_t *h = &tx_trace.header;
    h->id = 0x54585442u;
    h->mask = 0xFFFFFFFFu;
    h->base = (uint32_t)&tx_trace;
    h->registry_start = (uint32_t)&tx_trace.registry[0];
    h->name_size = 32;
    h->registry_end = (uint32_t)&tx_trace.registry[2];
    h->buffer_start = (uint32_t)&tx_trace.events[0];
    h->buffer_end = (uint32_t)&tx_trace.events[TX_ENTRIES];
    h->buffer_current = h->buffer_start;
    h->reserved2 = 0xAAAAAAAAu;
    h->reserved3 = 0xBBBBBBBBu;
    h->reserved4 = 0xCCCCCCCCu;
    tx_register(0, &task_worker, 2, "worker");
    tx_register(1, &task_idle, 31, "idle");
    _tx_trace_header_ptr = h;
}

static void tx_event(const fake_tcb_t *ctx, uint32_t event, uint32_t i1, uint32_t i2) {
    tx_event_t *e = &tx_trace.events[tx_next];
    e->priority = 0x80000000u;
    e->event = event;
    e->time = cycles();
    e->info[0] = i1;
    e->info[1] = i2;
    e->info[2] = e->info[3] = 0;
    memory_barrier();
    e->thread = (uint32_t)ctx;
    tx_next = tx_next + 1 == TX_ENTRIES ? 0 : tx_next + 1;
    tx_trace.header.buffer_current = (uint32_t)&tx_trace.events[tx_next];
}

// --- compact（書式の文字列の番地 + 引数の語）と Trice（ITM ポート 2、枠なし） -------------------------------

const char compact_fmt[] = "compact seq=%u t=%u ms";

static void compact_log(uint32_t seq, uint32_t now) {
    uint32_t words[3] = {(uint32_t)compact_fmt, seq, now};
    rtt_write(3, words, sizeof words);
}

static void itm_put8(uint32_t port, uint8_t value) {
    if (!(REG32(ITM_TCR) & 1u) || !(REG32(ITM_TER) & (1u << port))) {
        return;                         // 無効なら捨てる（待ち続けない）
    }
    while ((REG32(ITM_STIM(port)) & 1u) == 0) {
    }
    *(volatile uint8_t *)ITM_STIM(port) = value;
}

static void trice_seq(uint32_t seq) {
    static uint8_t cycle = 0xC0;
    uint8_t p[8];
    put_le(p, 0x4000u | 100u, 2);                           // セレクタ 01（時刻なし）・ID 100
    put_le(p + 2, 4u << 8 | cycle++, 2);                    // NC: 長さ 4・回の番号
    put_le(p + 4, seq, 4);
    for (uint32_t i = 0; i < sizeof p; i++) {
        itm_put8(2, p[i]);
    }
}

// HSI 16 MHz、PB3 = SWO、TPIU は NRZ 2 Mbaud（フォーマッタ無効）、ITM はポート 0〜2、DWT の CYCCNT
static void trace_setup(void) {
    REG32(RCC_CR) |= 1u;
    while ((REG32(RCC_CR) & 2u) == 0) {
    }
    REG32(RCC_CFGR) &= ~3u;
    while ((REG32(RCC_CFGR) & 0xCu) != 0) {
    }
    REG32(RCC_AHB1ENR) |= 1u << 1;
    REG32(GPIOB_MODER) = (REG32(GPIOB_MODER) & ~(3u << 6)) | (2u << 6);
    REG32(GPIOB_OSPEED) |= 3u << 6;
    REG32(GPIOB_AFRL) &= ~(0xFu << 12);
    REG32(DBGMCU_CR) = (REG32(DBGMCU_CR) & ~(3u << 6)) | (1u << 5);
    REG32(DEMCR) |= 1u << 24;
    REG32(TPIU_SPPR) = 2u;
    REG32(TPIU_ACPR) = CORE_HZ / SWO_BAUD - 1u;
    REG32(TPIU_FFCR) = 0x100u;
    REG32(ITM_LAR) = 0xC5ACCE55u;
    REG32(DWT_CTRL) |= 1u;                                  // CYCCNTENA
    REG32(ITM_TER) = 7u;
    REG32(ITM_TCR) = (1u << 16) | 1u;                       // TraceBusID = 1、ITMENA
}

// --- 本体 -------------------------------------------------------------------------------------

void systick_handler(void) {
    if (sysview_running) {
        uint8_t p[4];
        uint32_t n = sv_varint(p, 0, 15);                   // ISR_ENTER（SysTick = 例外 15）
        uint32_t now = cycles();
        uint8_t out[16];
        uint32_t k = sv_varint(out, 0, 2);
        for (uint32_t i = 0; i < n; i++) {
            out[k++] = p[i];
        }
        k = sv_varint(out, k, now - sv_last);
        sv_last = now;
        rtt_write(2, out, k);
    }
    ticks++;
    if (sysview_running) {
        uint32_t now = cycles();
        uint8_t out[8];
        uint32_t k = sv_varint(out, 0, 3);                  // ISR_EXIT
        k = sv_varint(out, k, now - sv_last);
        sv_last = now;
        rtt_write(2, out, k);
    }
}

static void every_10ms(uint32_t now) {
    uint32_t seq = sequence++;
    fake_tcb_t *next = (seq & 1u) ? &task_idle : &task_worker;
    const char *name = (seq & 1u) ? "idle" : "worker";
    if (sysview_running) {
        sysview_switch(next);
        if (seq % 10u == 0) {
            sysview_print(seq);
        }
    }
    er_record2(0xF219u, (uint32_t)next, 0);                 // RTX5 の ThreadSwitched
    ctf_thread(0x10, (seq & 1u) ? &task_worker : &task_idle, (seq & 1u) ? "worker" : "idle");
    ctf_thread(0x11, next, name);
    tx_event(next, 112u, 10u, 0);                           // THREAD_SLEEP（文脈 = 次のスレッド）
    defmt_tick(now, seq);
    if (seq % 10u == 9u) {
        defmt_late(now, (int16_t)-(int32_t)(seq % 7u));
    }
    compact_log(seq, now);
    trice_seq(seq);
}

static void every_100ms(uint32_t now) {
    char line[48];
    uint32_t n = append_u32(line, append(line, 0, "tick "), now);
    line[n++] = '\n';
    rtt_write(0, line, n);
    n = append_u32(line, append(line, 0, "seq "), sequence);
    line[n++] = '\n';
    er_stdout(line, n);
    if (now % 1000u < 100u) {
        n = append_hex8(line, append(line, 0, "pc=0x"), (uint32_t)systick_handler);
        line[n++] = '\n';
        rtt_write(0, line, n);
    }
}

__attribute__((noreturn)) void reset_handler(void) {
    for (uint32_t *p = &__bss_start; p < &__bss_end; p++) {
        *p = 0;
    }
    extern const void *const vectors[];
    REG32(SCB_VTOR) = (uint32_t)vectors;
    trace_setup();
    er_init();
    tx_init();
    static const uint8_t named[28] = {'b', 'o', 'o', 't', 0};
    ctf_record(0x62, named, sizeof named);                  // named_event "boot"（arg0・arg1 = 0）
    REG32(SYST_RVR) = CORE_HZ / 1000u - 1u;
    REG32(SYST_CVR) = 0;
    REG32(SYST_CSR) = 7u;
    uint32_t next10 = 0, next100 = 0;
    for (;;) {
        if (!sysview_running && (sysview_autostart || rtt_getc(1) == 1)) {
            sysview_start();
        }
        if (sysview_running && rtt_getc(1) == 2) {         // STOP
            sysview_running = 0;
        }
        uint32_t now = ticks;
        if ((int32_t)(now - next10) >= 0) {
            next10 = now + 10u;
            every_10ms(now);
        }
        if ((int32_t)(now - next100) >= 0) {
            next100 = now + 100u;
            every_100ms(now);
        }
    }
}

__attribute__((noreturn)) void default_handler(void) {
    for (;;) {
    }
}

__attribute__((section(".vectors"), used)) const void *const vectors[16] = {
    &__stack_top, (void *)reset_handler, (void *)default_handler, (void *)default_handler,
    (void *)default_handler, (void *)default_handler, (void *)default_handler, 0, 0, 0, 0,
    (void *)default_handler, (void *)default_handler, 0, (void *)default_handler, (void *)systick_handler,
};
