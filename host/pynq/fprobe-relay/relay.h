/* fprobe-relay: PC の UDP のデータグラムと PL の zynq_frame_link のフレームを 1 対 1 で受け渡す（PYNQ-Z2 の PS の Linux）。
 * 仕様: docs/superpowers/specs/2026-10-19-pynq-z2-port-design.md §5.1
 *
 * 線上の形式（Ethernet 版と同じ。host/fprobe/eth_link.py）: UDP 63920、ch(1) | seq(1) | payload(0..512)。
 * PC → PL の seq は捨てる。PL → PC の seq は全チャンネル共通の 8 ビットの通し番号。
 *
 * まとめ送り（任意。PC が要求したときだけ）: PYNQ の UDP の送信は 1 パケットあたり約 35 µs（大きさによらない。実測）で、
 * 512 バイトのフレームを 1 つずつ送ると約 8 MB/s で頭打ちになる。PC が ch = RELAY_CH_BUNDLE のデータグラム
 * （ch | seq | 上限のバイト数 u16 LE。0 = まとめない）を送ると、PL からのバイト列のチャンネル（SWO・UART0・STREAM）のフレームが
 * 続けて溜まっているとき、1 つの UDP にまとめて送る:
 *     RELAY_CH_BUNDLE | seq（先頭のフレームの seq。中のフレームは seq が連続） | { ch(1) | 長さ(2, LE) | payload } × n
 * CONTROL・DAP のフレームは、まとめず待たせずに送る（応答の遅れを増やさない）。
 * 要求の無い PC・まとめられるフレームが 1 つだけのときは、従来のデータグラムのまま。
 * PL の窓の読み書きは pl_ops で受ける（PYNQ では /dev/mem の写し、PC の試験では Python の模型）。
 * SPDX-License-Identifier: AGPL-3.0-only
 */
#ifndef FPROBE_RELAY_H
#define FPROBE_RELAY_H

#include <stddef.h>
#include <stdint.h>

#include "window_regs.h"               /* 窓のレジスタの番地・ビット（生成物。fprobe_gw/ps_window.py が正） */

#define RELAY_MAX_PAYLOAD 512
#define RELAY_BUNDLE_POLLS 256       /* まとめ送り中、TX_DONE の後に次のフレームが TX_HEAD に出るまで待つ読みの上限（1 回 約 150 ns） */
#define RELAY_CH_BUNDLE 0x40           /* まとめ送りの要求・まとめたデータグラムの ch（PL のチャンネルは 0〜5） */
#define RELAY_LINK_VERSION_MIN 1u      /* 窓の版: 1〜3 はフレームの受け渡しだけに使う（PS 直読みは RELAY_LINK_VERSION_PS_STREAM 以上） */

struct pl_ops {
    uint32_t (*rd)(void *ctx, uint32_t addr);
    void (*wr)(void *ctx, uint32_t addr, uint32_t value);
    void *ctx;
};

struct relay_stats {
    uint32_t to_pl;        /* PL に渡したフレーム */
    uint32_t from_pl;      /* PC へ送ったデータグラム */
    uint32_t dropped_bad;  /* 形の悪いデータグラム（短い・長すぎる） */
    uint32_t pl_drops;     /* PL が捨てた COMMIT（DROPS レジスタの最後の値） */
    uint8_t seq;           /* 次に PC へ送る seq */
    uint16_t bundle_max;   /* まとめたデータグラムの上限（バイト）。0 = まとめない */
};

/* PC の宛先: 最後に正しいデータグラムを送ってきた送り主 */
struct relay_peer {
    uint32_t addr;         /* ネットワークのバイト順のまま */
    uint16_t port;
    int known;
};

/* 形の正しいデータグラム（ch と seq があり、payload が RELAY_MAX_PAYLOAD 以下）か。正しいものだけを PL に渡し、
 * その送り主を宛先にする */
int relay_dgram_ok(size_t len);

/* 送り主を PC の宛先にするデータグラムか（形が正しく、中継の状態の問い合わせ RELAY_CH_STATUS でないもの）。状態の問い合わせは
 * 返事を送り主へ返すだけで、宛先は変えない（`fprobe_gw pynq status` が別のソケットから問い合わせても、使っている PC の
 * 応答の宛先を奪わない） */
int relay_dgram_sets_peer(const uint8_t *dgram, size_t len);

/* 0 = ID と VERSION が合う。負 = 合わない（受け渡しを始めない） */
int relay_check_design(const struct pl_ops *pl);

/* 0 = 渡した、1 = PL が busy（呼び手が待って繰り返す）、2 = 中継の状態の問い合わせ（RELAY_CH_STATUS。PL に渡さない。
 * 呼び手が送り主へ relay_status_encode の返事を送る）、負 = 捨てた（st->dropped_bad を数える） */
int relay_from_pc(const struct pl_ops *pl, const uint8_t *dgram, size_t len, struct relay_stats *st);

/* PL に送るフレームがあれば、seq を付けたデータグラムを out に組み、その長さを返す。無ければ 0 */
size_t relay_to_pc(const struct pl_ops *pl, uint8_t *out, size_t cap, struct relay_stats *st);

/* 深いバッファの領域 [base, base + size) が使えるか: 0 でなく、/proc/iomem の中身 iomem（NUL 終わり）の
 * System RAM（Linux の使うメモリ）と重ならない。1 = 使える */
int relay_region_free(const char *iomem, uint32_t base, uint32_t size);

#define RELAY_DDR_END 0x20000000ull               /* PYNQ-Z2 の DDR（512 MB）の終わり */

/* 領域 [base, base + size) が、デバイスツリーの reserved-memory（dt_dir = /proc/device-tree/reserved-memory）の 1 つのノードの
 * reg に全体が含まれ、そのノードに no-map・reusable・compatible が無い: 1。カーネルは予約して使わず、/dev/mem は書き込み結合で
 * 写す（Linux の管理外の領域は最も遅い写像で、読みが約 125 MB/s。書き込み結合は約 230 MB/s。2026-10-31 実測）。
 * #address-cells・#size-cells が 1 でない・読めない・重ならない: 0 */
int relay_region_reserved(const char *dt_dir, uint32_t base, uint32_t size);

/* 深いバッファの設定（/etc/fprobe/mem の base・size）を、PL が MEM_BASE から実際に使う大きさ mem_bytes（MEM_BYTES
 * レジスタ）で確かめる。reserved は relay_region_reserved の結果（1 なら Linux のメモリと重なっても、予約されているので使える）。
 * 0 = MEM_BASE に書いてよい、1 = 深いバッファの無い回路（書かない）、負 = 使えない
 * （-1 = 16 の倍数でない、-2 = 予約が PL の大きさより小さい、-3 = DDR の外、-4 = Linux のメモリと重なる） */
int relay_deep_region_ok(const char *iomem, uint32_t base, uint32_t size, uint32_t mem_bytes, int reserved);

/* /proc/interrupts の中身 text（NUL 終わり）から、行の最後の名前が ifname と完全に同じ（直前は空白）割り込みの番号。無ければ -1 */
int relay_irq_of(const char *text, const char *ifname);

/* /proc/irq/N/effective_affinity_list の中身（"0" など）が、ちょうど 1 つの CPU 番号のとき、その番号。"0-1"・"0,1"・空・数字でないものは -1 */
int relay_single_cpu(const char *list);

/* CLOCK_MONOTONIC の秒とナノ秒を 64 ビットのナノ秒にする。ARM の long は 32 ビットなので、秒 × 10^9 を long で
 * 計算すると 2.1 秒で溢れる（経過時間の比べが狂い、待ちが効かず CPU を使い切る）。時刻の計算は必ずこれを通す */
int64_t relay_ns(int64_t sec, int64_t nsec);

/* 正しいデータグラムを受けたら宛先をその送り主に替える */
void relay_peer_update(struct relay_peer *peer, uint32_t addr, uint16_t port);

/* 読みと送信を別のスレッドに分けるためのリング（書くスレッドと読むスレッドが 1 つずつ。ロック無し）。
 * PL の窓の読み（語ごとの mmio。1 フレーム 約 34 µs）と UDP の送信（約 27 µs）を別のコアで同時に進める。
 * 書く側: relay_ring_slot で番地を得て（満杯なら NULL）、relay_to_pc などで中身を組み、relay_ring_commit で渡す。
 * 読む側: relay_ring_peek で先頭を見て（空なら NULL）、送り終えたら relay_ring_pop。 */
#define RELAY_RING_SLOTS 32u
#define RELAY_RING_SLOT_BYTES 16384u

struct relay_ring {
    uint32_t head;                                      /* 書く側だけが進める */
    uint32_t tail;                                      /* 読む側だけが進める */
    uint32_t len[RELAY_RING_SLOTS];
    uint8_t data[RELAY_RING_SLOTS][RELAY_RING_SLOT_BYTES];
};

size_t relay_ring_size(void);
uint32_t relay_ring_slots(void);
size_t relay_ring_slot_bytes(void);
void relay_ring_init(struct relay_ring *r);
uint8_t *relay_ring_slot(struct relay_ring *r);
void relay_ring_commit(struct relay_ring *r, size_t len);
const uint8_t *relay_ring_peek(struct relay_ring *r, size_t *len);
void relay_ring_pop(struct relay_ring *r);

/* 先頭から、読める（commit 済みの）スロットを最大 max 個、base・len の配列に並べて個数を返す（取り出さない。pop は 1 つずつ）。
 * TCP は、1 回の send が 16 KB 以上だと 6.5 KB のときより 1 MB あたりの時間が約 25 % 短い（PYNQ-Z2 実測 2026-10-31）ので、
 * 読めるスロットをまとめて 1 回の sendmsg にするために使う */
int relay_ring_peek_many(struct relay_ring *r, const uint8_t **base, size_t *len, int max);

/* Zynq の SLCR のレジスタ（ops は SLCR の窓）を 1 つ書く。書く前のロック状態を保つ: ロックされていれば、解除して書いて、ロックし直す。
 * 解除されていれば、解除のまま書く。Linux は起動時に SLCR を解除したままにし、reboot（PSS_RST_CTRL への書き込み）は鍵を書かずに行う。
 * ロックし直すと、その書き込みが無視されて reboot が「Reboot failed -- System halted」で止まる（PYNQ-Z2 で JTAG から確認 2026-10-31） */
void relay_slcr_write(const struct pl_ops *slcr, uint32_t reg, uint32_t value);

/* sysfs のファイル（napi_defer_hard_irqs など）に value を書く。書く前の値（改行を除く）を old に入れて返す（元に戻すため）。
 * 0 = 書いた、-1 = 読めない・書けない・old に入りきらない（入りきらないときは何も書かない） */
int relay_sysfs_set(const char *path, const char *value, char *old, size_t old_cap);

/* PS が DDR のリングを直接読むストリーム。PL（la_deep_buffer）が DDR のリングに書いた拍を、窓の位置のレジスタを見て
 * PS が DDR から読み（NEON の 64 バイトずつで実測 125 MB/s、glibc の memcpy は 68 MB/s。窓の語ごとの読みは約 15 MB/s）、PL が窓へ流すのと同じ形
 * （見出し 1 拍 + 中身。ロジアナ = 種類 1、イベント = 種類 2）の STREAM チャンネルのバイト列にして、UDP のデータグラムに詰める。
 * 読み終えた位置は窓に返す（PL はそれまでの拍を上書きしてよい）。
 * 公開される位置は、書き込みの応答が返った拍までに絞ってある（PL 側）ので、ここで読んでよい。
 *
 * 位置の約束（窓の版 4。docs/superpowers/specs/2026-11-03-ps-stream-position-design.md）:
 *   - STREAM_EPOCH を読むと、PL が 1 つの時点で揃えた組（流れの番号・状態・LA_WRITTEN・LA_WPTR・EV_WPOS・EV_START）が写され、
 *     以後の読みはその写し。状態の CURRENT = 0（ホストが次の流れを始めたのに PL がまだ前の流れを公開している）の間は何もしない。
 *   - 位置（LA_CONSUMED・EV_CONSUMED・STREAM_BEATS）の書きには、その値の流れの番号を POS_TAG で添える（ps_put。複写の道・
 *     ゼロコピーの道・つながりの終わりの全部がこれを通る）。窓は今の流れの番号でない書きを捨てる（STALE_WRITES）。
 *   - 付き始め（新しい番号・中継の起動）は、POS_TAG を書き、窓の位置を読み、STREAM_EPOCH を読み直して同じ番号・CURRENT なら
 *     その位置から続ける（読んでいる間に次の流れが始まっていない）。ロジアナのリングの読み位置は LA_WPTR と未読の数から決める
 *     （総数は 2^32 で折り返し、リングの拍数は 2 の冪でないので、総数 mod リングは使えない）。以後は読んだ分だけ進める。 */
#define RELAY_PS_CHUNK_MAX STREAM_PL_CHUNK_BEATS   /* 1 つのかたまりの中身の拍数の上限（la_deep_buffer の STREAM_CHUNK と同じ） */
#define RELAY_PS_LA_MIN 64u            /* ロジアナの拍はこれだけ溜まるか、古くなるか、書き込みが止まってから出す（LA_MIN と同じ） */
#define RELAY_PS_AGE_NS 100000         /* 溜まり始めてからこれだけ経ったら出す（la_deep_buffer の約 82 µs に合わせた） */

struct relay_ps {
    uint32_t la_ring, ev_ring;         /* ロジアナ・イベントのリングの拍数（DDR の領域の 15/16 と 1/16） */
    int attached;                      /* 今の流れ（epoch）に付いている */
    int active;                        /* 流れが動いている（PL が書いている・PS が読み終えていない）。呼び手は休まず待つ */
    uint32_t epoch;
    uint32_t tag;                      /* 最後に窓の POS_TAG に書いた番号（tag_valid のとき） */
    int tag_valid;
    uint32_t la_read, ev_read;         /* 流れの開始から読み終えた拍・イベントの数（32 ビットで折り返す） */
    uint32_t la_rpos;                  /* la_read の拍のロジアナのリングの位置 */
    uint32_t ev_start;                 /* この流れで最初に読むイベントの位置（流れの間は変わらない） */
    uint32_t bad_la, bad_ev;           /* ありえない位置（未読がリングより多い等）を見て読まなかった回数（A7。0 のはず） */
    uint32_t out_beats;                /* 流れに出すと決めた拍の総数（見出しを含む）。窓の STREAM_BEATS に書く（ホストの stream_beats） */
    int last_kind;                     /* 最後に出したかたまりの種類（1 = ロジアナ、2 = イベント） */
    /* 出せるほど溜まっていない未読を最初に見た時刻（無ければ -1）。ロジアナとイベントで別に持つ: 1 つだと、ロジアナが流れ続ける間は
     * 毎回ロジアナを出して時刻が消え、少しのイベント（欠けの記録）が流れの終わりまで出ない */
    int64_t la_first_seen_ns, ev_first_seen_ns;
    uint32_t stage_len, stage_pos;     /* 読み出したかたまり（見出し + 中身）と、データグラムに詰めた位置 */
    uint8_t stage[16 + RELAY_PS_CHUNK_MAX * 16];
};

size_t relay_ps_size(void);

/* TCP 用: 枠の無い STREAM のバイト列（データグラムの中身と同じ。見出し付きのかたまりの並び）を、out に cap バイトまで詰めて、
 * その長さを返す。かたまりを丸ごと入れられる間だけ読み出す（入れ物の残りがかたまりの最大より小さければ読まない）。
 * データグラムで出しかけた残りがあれば、それを先に出す（切り替えで順番を崩さない）。無ければ 0 */
size_t relay_ps_fill_bytes(struct relay_ps *ps, const struct pl_ops *pl, const uint8_t *ddr, uint8_t *out, size_t cap,
                           int64_t now_ns);

/* トリガ取り込みの読み出し（深いバッファ全量）を PS が DDR から行う。ホストが CSR の command.read_ps を打つと、PL は要求（領域・先頭・拍数）を
 * 世代つきで窓に出す。ここは、要求の拍を DDR から読んで、枠の無い STREAM のバイト列（リングの拍のまま。見出しは付けない）として out に詰め、
 * 最後まで詰めたら窓の RD_ACK に世代を返す（PL の status.reading が下がる）。書き込みは止まっているので、ストリームの「応答の来ていない書き」
 * の問題は無い。新しい要求が来たら、読みかけを捨ててそちらにする。 */
struct relay_rd {
    int busy;
    uint32_t epoch, region, pos, left;
};

size_t relay_rd_size(void);
void relay_rd_init(struct relay_rd *rd);

/* 要求の続きを out に cap バイトまで（16 の倍数）詰めて、その長さを返す。要求が無い・入れ物が 1 拍に満たないときは 0。
 * 形の悪い要求（先頭がリングの外・リングより多い・0 拍）は、何も詰めずに返事だけする（PL を待たせ続けない）。
 * geo は、リングの大きさ（relay_ps_init で決めたもの）を借りる */
size_t relay_rd_fill(struct relay_rd *rd, const struct pl_ops *pl, const uint8_t *ddr, const struct relay_ps *geo,
                     uint8_t *out, size_t cap);

/* 流れが動いている（PL が書いている、または PS が読み終えていない）か。呼び手は、動いている間は休まずに呼ぶ
 * （溜まるのを待つ時間の判定のため） */
int relay_ps_active(const struct relay_ps *ps);

/* mem_bytes（窓の MEM_BYTES）から、リングの大きさを決める。0 = 準備できた。負 = 深いバッファが無い・大きさが 2 の冪でない */
int relay_ps_init(struct relay_ps *ps, uint32_t mem_bytes);

/* ありえない位置を見て読まなかった回数（ロジアナ・イベント）。正しい PL と中継では 0 のまま（事実として状態の問い合わせで返す） */
uint32_t relay_ps_bad_la(const struct relay_ps *ps);
uint32_t relay_ps_bad_ev(const struct relay_ps *ps);

/* PS 直読みの流れのバイト列を 1 つのデータグラム（out。st->bundle_max があればまとめた形、無ければ 512 バイトまでの
 * 通常のフレーム）にして、その長さを返す。出すものが無ければ 0。ddr は MEM_BASE から mem_bytes の写し。
 * now_ns は溜まり始めの時刻の判定に使う（単調な時刻） */
size_t relay_ps_step(struct relay_ps *ps, const struct pl_ops *pl, const uint8_t *ddr, uint8_t *out, size_t cap,
                     struct relay_stats *st, int64_t now_ns);

/* ---- ゼロコピーの送信の道（TCP。/dev/fprobe_ring があるとき） -------------------------------------------------------
 * 設計: docs/superpowers/specs/2026-11-01-bandwidth-observation-design.md 1.7.4・1.7.9。
 * カーネルモジュール fprobe_ringmap（host/pynq/fprobe-ringmap）が、深いバッファのリングを struct page のある普通のページとして
 * 写す（ring）。かたまりの中身は、そのページを MSG_ZEROCOPY でそのまま TCP に渡す（CPU は中身を読まない・書かない）。
 * 見出し（1 拍）は記述の表（relay_zc の d[]）に置き、中身と iovec で並べる。
 *
 * 正しさの条件:
 *   - LA_CONSUMED・EV_CONSUMED は、そのかたまりの全バイトを運んだ呼び出しの完了通知（カーネルがページを手放した）が、
 *     流れの前から順にそろった後でだけ書く。STREAM_BEATS（出すと決めた拍の総数）は今の道と同じく、渡す前に書く。
 *   - 見出しは、そのかたまりを解放するまで書き換えない（記述の表の行は解放の後にだけ使い回す）。
 *   - 完了通知が「カーネルが写した」（copied。SO_EE_CODE_ZEROCOPY_COPIED）なら、写しはキャッシュありの写像を読むので、
 *     古い中身を送りうる。数えて（copied_calls・copied_bytes）、ゼロコピーをやめる: 送っていない残りは /dev/mem の写し
 *     （dev。キャッシュなし）から写して普通に送り、完了を待ち切ってから、今の複写の道（relay_ps_fill_bytes）に渡す。
 *   - リングの終わりをまたぐかたまりは 2 つに分ける（1 つのかたまりの中身は、リングの中で続いた範囲）。
 * OS に依存しない（ソケットは relay_sock_ops、完了通知は呼び手が relay_zc_complete で渡す）。PC の試験は
 * host/tests/test_pynq_relay_zc.py。 */
#define RELAY_ZC_CHUNK_MAX 4096u       /* ゼロコピーの道の 1 かたまりの中身の拍数の上限（64 KB）。ホストの STREAM_SEG_MAX_BEATS 以下 */
/* かたまりは、中身が RELAY_ZC_CHUNK_MAX 拍溜まるか、溜まり始めてから RELAY_ZC_AGE_NS 経つか、書き込みが止まったら出す
 * （ロジアナ・イベントとも）。溜めずに出すと、回りの速さ（1 回 約 0.2 ms）で 10 KB 前後のかたまりと呼び出しになり、見出しの断片・
 * 呼び出し・完了通知の数が増えて送る CPU が増えた（25 MS/s で 1 回 平均 10.5 KB、CPU1 sys 83 %。2026-11-02 実測、設計 1.7.9）。
 * 1 ms はホストの読みの間隔（LaStream.READ_INTERVAL_S 50 ms・READ_MIN 64 KB）より十分短く、流れの遅れとして見えない。
 * 溜める量・時間は送る効率だけを左右し、中身（欠け・順番）を左右しない */
#define RELAY_ZC_AGE_NS 1000000
#define RELAY_ZC_CALL_BYTES (256u << 10)   /* 1 回の sendmsg に並べるバイト数の上限 */
#define RELAY_ZC_IOV_MAX 64            /* 1 回の sendmsg の iovec の数の上限（かたまり 1 つで 2 つ） */
#define RELAY_ZC_DESCS 2048u           /* 送って完了を待つかたまりの数の上限（記述の表の行数） */
#define RELAY_ZC_CALLS 2048u           /* 完了を待つ呼び出しの数の上限 */
#define RELAY_ZC_BOUNCE_BYTES (64u << 10)  /* 写して普通に送るときの入れ物（copied の後・トリガ取り込みの読み出し） */

struct relay_iov {
    const void *base;
    size_t len;
};

#define RELAY_SEND_AGAIN (-1)          /* 送信バッファが埋まっている（EAGAIN） */
#define RELAY_SEND_NOBUFS (-2)         /* 完了通知の置き場が足りない（ENOBUFS。optmem_max） */
#define RELAY_SEND_BROKEN (-3)         /* つながりが壊れた */

struct relay_sock_ops {
    /* iov の n 個を 1 回で送る（zerocopy = 1 なら MSG_ZEROCOPY）。送れたバイト数（> 0）か RELAY_SEND_*。
     * zerocopy = 1 の呼び出しが 1 バイト以上送れたら、カーネルはその呼び出しに次の通し番号を付ける（失敗は番号を使わない） */
    int64_t (*send)(void *ctx, const struct relay_iov *iov, int n, int zerocopy);
    void *ctx;
};

/* ゼロコピーの道の数え（つながりごと。0 から）。名前の並びはホストの eth_link.RELAY_STATUS_FIELDS と同じ（試験で照らす） */
#define RELAY_ZC_STATS(X) \
    X(zc_calls)            /* MSG_ZEROCOPY で 1 バイト以上送れた呼び出し */ \
    X(zc_completed)        /* そのうち完了通知が来た呼び出し */ \
    X(zc_copied_calls)     /* そのうち完了通知が copied だった呼び出し */ \
    X(zc_copied_bytes)     /* その呼び出しが運んだバイト数 */ \
    X(zc_unknown_completions) /* 待っていない番号の完了通知 */ \
    X(zc_enobufs)          /* sendmsg の ENOBUFS */ \
    X(zc_eagain)           /* sendmsg の EAGAIN（ゼロコピーの道の中） */ \
    X(zc_bytes)            /* MSG_ZEROCOPY で渡したバイト数 */ \
    X(zc_bounce_bytes)     /* 写して普通に送ったバイト数（copied の後の残り・トリガ取り込みの読み出し） */ \
    X(zc_chunks)           /* 作ったかたまりの数 */ \
    X(zc_latency_count)    /* 完了までの時間を測った呼び出しの数 */ \
    X(zc_latency_sum_ns)   /* sendmsg が返ってから完了通知を受け取るまでの時間の合計 */ \
    X(zc_latency_max_ns)   /* 同、最大 */ \
    X(zc_outstanding_calls)    /* いま完了を待っている呼び出し */ \
    X(zc_outstanding_max)      /* 同、最大 */ \
    X(zc_la_held_beats)    /* いま渡したが CONSUMED に返していないロジアナの拍（PL はまだ上書きできない） */ \
    X(zc_la_held_max_beats) \
    X(zc_ev_held_beats)    /* 同、イベント */ \
    X(zc_ev_held_max_beats) \
    X(zc_desc_full)        /* 記述の表が満杯で、かたまりを作らなかった回数 */ \
    X(zc_calls_full)       /* 完了を待つ呼び出しが上限で、送らなかった回数 */ \
    X(zc_abandoned_chunks) /* つながりの終わりに、送り終えていない（完了していない・渡していない）まま捨てたかたまり */ \
    X(zc_abandoned_bytes)  /* 同、そのバイト数 */

struct relay_zc_stats {
#define RELAY_ZC_STAT_FIELD(name) uint64_t name;
    RELAY_ZC_STATS(RELAY_ZC_STAT_FIELD)
#undef RELAY_ZC_STAT_FIELD
};

enum { RELAY_ZC_RUN = 0, RELAY_ZC_DRAIN = 1, RELAY_ZC_DONE = 2 };

struct relay_zc_desc {
    uint8_t head[16];                  /* 見出しの 1 拍（解放まで書き換えない） */
    uint32_t epoch;                    /* 取ったときの流れ */
    uint32_t kind;                     /* 1 = ロジアナ、2 = イベント */
    uint32_t first;                    /* 中身の先頭: 深いバッファの領域の先頭からの拍 */
    uint32_t n;                        /* 中身の拍数 */
    uint32_t end_read;                 /* 解放したら CONSUMED に書く値（取った後の la_read / ev_read） */
    uint64_t end_off;                  /* TCP のバイト列での終わり（含まない） */
};

struct relay_zc_call {
    uint64_t start_off, end_off;       /* 運んだ TCP のバイト列の範囲 */
    int64_t t_ns;                      /* sendmsg が返った時刻 */
    uint8_t done, copied;
};

struct relay_zc {
    int state;                         /* RELAY_ZC_RUN / DRAIN（copied を見た。残りを写して送り、完了を待つ）/ DONE（渡せる） */
    uint32_t epoch;                    /* la_released・ev_released の流れ */
    int synced;                        /* epoch が決まっている */
    uint32_t la_released, ev_released; /* 最後に CONSUMED に書いた値 */
    uint32_t d_tail, d_send, d_head;   /* 記述の表: [d_tail, d_send) は送り始めた、[d_send, d_head) はまだ */
    uint32_t d_send_off;               /* d_send のかたまりの、ここまで送った（または写した）バイト */
    uint64_t taken_off;                /* 取ったかたまりの総バイト（次のかたまりの始まり） */
    uint64_t sent_off;                 /* カーネルが受け取った総バイト */
    uint32_t c_tail, c_head;           /* 完了を待つ呼び出しの番号: [c_tail, c_head)。番号 % RELAY_ZC_CALLS が行 */
    uint32_t bounce_pos, bounce_len;   /* 写した入れ物の送っていない範囲 */
    struct relay_zc_stats st;
    struct relay_zc_desc d[RELAY_ZC_DESCS];
    struct relay_zc_call c[RELAY_ZC_CALLS];
    uint8_t bounce[RELAY_ZC_BOUNCE_BYTES] __attribute__((aligned(16)));
};

size_t relay_zc_size(void);
uint32_t relay_zc_chunk_max(void);

/* つながりの始めに呼ぶ（数えも 0 に） */
void relay_zc_init(struct relay_zc *zc);

#define RELAY_ZC_MOVED 1               /* 何かを送った・解放した */
#define RELAY_ZC_BLOCKED 2             /* 送れなかった（EAGAIN・ENOBUFS・上限）。呼び手はソケットを待ってよい */

/* 1 回分を進める: 完了したかたまりを解放して CONSUMED を書き、新しいかたまりを取り、送る。rd は窓が版 3 のときの
 * トリガ取り込みの読み出し（NULL = 無し。今の道と同じく、要求があればストリームより先に、写して普通に送る）。
 * ring は /dev/fprobe_ring の写し（中身は読まない。番地を iovec に置くだけ）、dev は /dev/mem の写し（キャッシュなし。
 * 写すときだけ読む）。どちらも深いバッファの領域の先頭から。
 * 戻り値: RELAY_ZC_MOVED・RELAY_ZC_BLOCKED の和、負 = つながりが壊れた（呼び手が閉じる） */
int relay_zc_step(struct relay_zc *zc, struct relay_ps *ps, struct relay_rd *rd, const struct pl_ops *pl,
                  const struct relay_sock_ops *so, const uint8_t *ring, const uint8_t *dev, int64_t now_ns);

/* 完了通知（番号 lo..hi、copied = カーネルが写した）を受け取る。解放は次の relay_zc_step。新しく完了した呼び出しの数を返す */
uint32_t relay_zc_complete(struct relay_zc *zc, uint32_t lo, uint32_t hi, int copied, int64_t now_ns);

int relay_zc_state(const struct relay_zc *zc);
void relay_zc_get_stats(const struct relay_zc *zc, struct relay_zc_stats *out);

/* つながりが終わった: 取ったかたまりを全部解放する（今の道で tcp_ring の残りを捨てるのと同じ。流れが同じなら CONSUMED に
 * la_read・ev_read を書く）。完了していないかたまりのページを PL に返すので、呼び手は先につながりを打ち切る（SO_LINGER 0 で
 * close: カーネルは送信の列を捨て、そのページを生きている相手に送らない）。捨てたかたまりは zc_abandoned_* に数える（A8） */
void relay_zc_abandon(struct relay_zc *zc, struct relay_ps *ps, const struct pl_ops *pl);

/* デバイスツリーの reserved-memory（dt_dir）に fprobe_ring（名前が fprobe_ring か fprobe_ring@...）のノードがただ 1 つあり、
 * その reg（1 組）が [base, base + size) を含む: 1。fprobe_ringmap が写すのはこのノードなので、/dev/fprobe_ring の
 * 0 バイト目が base であることの確かめ。無い・2 つ以上・含まない・読めない: 0 */
int relay_ringmap_region_ok(const char *dt_dir, uint32_t base, uint32_t size);

/* ---- 中継の状態（PC が UDP で問い合わせる） -------------------------------------------------------------------------
 * PC → 中継: RELAY_CH_STATUS | 番号（2 バイトちょうど。PL には渡さない）。
 * 中継 → PC: RELAY_CH_STATUS | 番号 | 版(u16 LE) | zc_state(u8) | tcp_path(u8) | 数 n(u16 LE) | u64 LE × n
 * （値の名前の並びは RELAY_STATUS_FIELDS。番号は問い合わせの番号を写す（版 2。PC は自分の問い合わせの返事だけを受ける。遅れて
 * 届いた前の返事を次の問い合わせの返事にしない。A10）。PL のフレームの通し番号とは別） */
#define RELAY_CH_STATUS 0x41
#define RELAY_STATUS_VERSION 2u
enum { RELAY_ZCS_NONE = 0,             /* /dev/fprobe_ring が無い（モジュールが読み込まれていない）・PS 直読みが無い */
       RELAY_ZCS_READY = 1,            /* ゼロコピーの道を使える */
       RELAY_ZCS_STOPPED = 2,          /* copied を見たのでやめた（中継を起動し直すまで使わない） */
       RELAY_ZCS_UNUSABLE = 3 };       /* /dev/fprobe_ring はあるが使えない（領域が合わない・写せない） */
enum { RELAY_PATH_NONE = 0,            /* TCP がつながっていない */
       RELAY_PATH_COPY = 1,            /* 今の複写の道（DDR → tcp_ring → send） */
       RELAY_PATH_ZEROCOPY = 2,        /* ゼロコピーの道 */
       RELAY_PATH_ZC_DRAIN = 3 };      /* copied を見て、ゼロコピーで出した分の完了を待っている（その後は複写の道） */

#define RELAY_STATUS_FIELDS(X) \
    X(connections)         /* 中継の起動からの TCP のつながりの数 */ \
    X(zc_copied_total)     /* 中継の起動からの copied の呼び出しの数 */ \
    X(tcp_bytes)           /* このつながりで、複写の道で送ったバイト数 */ \
    X(tcp_calls) \
    X(tcp_eagain) \
    X(copy_ring_full)      /* 複写の道の tcp_ring が満杯で待った回数 */ \
    X(ps_bad_la_positions) /* PS 直読みで、ありえないロジアナの位置を見て読まなかった回数（relay_ps_bad_la） */ \
    X(ps_bad_ev_positions) /* 同、イベント */ \
    X(pl_stale_writes)     /* 窓が捨てた、前の流れの番号を添えた位置の書きの数（STALE_WRITES） */ \
    RELAY_ZC_STATS(X)

struct relay_status {
    uint8_t zc_state, tcp_path;
#define RELAY_STATUS_FIELD(name) uint64_t name;
    RELAY_STATUS_FIELDS(RELAY_STATUS_FIELD)
#undef RELAY_STATUS_FIELD
};

int relay_status_count(void);
const char *relay_status_field_name(int i);
/* 返事のデータグラムを out に組み、その長さを返す（入りきらなければ 0）。token は問い合わせの番号（dgram[1]） */
size_t relay_status_encode(const struct relay_status *s, uint8_t token, uint8_t *out, size_t cap);

/* ---- NIC の設定の控え（中継が終わり方に依らず元に戻すため。A5） -----------------------------------------------------------
 * 変える前の値を path（/run/fprobe/nic-saved）に「defer\nflush\n」で控える。起動時に控えがあれば（前の中継が戻さずに終わった）
 * その値に戻してから消す。0 = できた、-1 = できない */
int relay_nic_save(const char *path, const char *defer, const char *flush);
int relay_nic_load(const char *path, char *defer, size_t defer_cap, char *flush, size_t flush_cap);

#endif
