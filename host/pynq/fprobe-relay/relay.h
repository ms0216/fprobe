/* fprobe-relay: PC の UDP のデータグラムと PL の zynq_frame_link のフレームを 1 対 1 で受け渡す（PYNQ-Z2 の PS の Linux）。
 * 仕様: docs/superpowers/specs/2026-10-19-pynq-z2-port-design.md §5.1
 *
 * 線上の形式（Ethernet 版と同じ。host/fprobe/eth_link.py）: UDP 63920、ch(1) | seq(1) | payload(0..512)。
 * PC → PL の seq は捨てる。PL → PC の seq は全チャンネル共通の 8 ビットの通し番号。
 * PL の窓の読み書きは pl_ops で受ける（PYNQ では /dev/mem の写し、PC の試験では Python の模型）。
 * SPDX-License-Identifier: AGPL-3.0-only
 */
#ifndef FPROBE_RELAY_H
#define FPROBE_RELAY_H

#include <stddef.h>
#include <stdint.h>

#define RELAY_MAX_PAYLOAD 512
#define RELAY_LINK_ID 0x46504C4Bu      /* "FPLK" */
#define RELAY_LINK_VERSION 1u

enum {
    REG_ID = 0x000, REG_VERSION = 0x004, REG_RX_STATUS = 0x008, REG_RX_COMMIT = 0x00C,
    REG_TX_HEAD = 0x010, REG_TX_DONE = 0x014, REG_DROPS = 0x018, REG_MEM_BASE = 0x01C, REG_MEM_BYTES = 0x020,
    REG_MEM_ERRORS = 0x024, REG_RX_BUF = 0x400,
    REG_TX_BUF = 0x800
};

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

/* 0 = ID と VERSION が合う。負 = 合わない（受け渡しを始めない） */
int relay_check_design(const struct pl_ops *pl);

/* 0 = 渡した、1 = PL が busy（呼び手が待って繰り返す）、負 = 捨てた（st->dropped_bad を数える） */
int relay_from_pc(const struct pl_ops *pl, const uint8_t *dgram, size_t len, struct relay_stats *st);

/* PL に送るフレームがあれば、seq を付けたデータグラムを out に組み、その長さを返す。無ければ 0 */
size_t relay_to_pc(const struct pl_ops *pl, uint8_t *out, size_t cap, struct relay_stats *st);

/* 深いバッファの領域 [base, base + size) が使えるか: 0 でなく、/proc/iomem の中身 iomem（NUL 終わり）の
 * System RAM（Linux の使うメモリ）と重ならない。1 = 使える */
int relay_region_free(const char *iomem, uint32_t base, uint32_t size);

#define RELAY_DDR_END 0x20000000ull               /* PYNQ-Z2 の DDR（512 MB）の終わり */

/* 深いバッファの設定（/etc/fprobe/mem の base・size）を、PL が MEM_BASE から実際に使う大きさ mem_bytes（MEM_BYTES
 * レジスタ）で確かめる。0 = MEM_BASE に書いてよい、1 = 深いバッファの無い回路（書かない）、負 = 使えない
 * （-1 = 16 の倍数でない、-2 = 予約が PL の大きさより小さい、-3 = DDR の外、-4 = Linux のメモリと重なる） */
int relay_deep_region_ok(const char *iomem, uint32_t base, uint32_t size, uint32_t mem_bytes);

/* 正しいデータグラムを受けたら宛先をその送り主に替える */
void relay_peer_update(struct relay_peer *peer, uint32_t addr, uint16_t port);

#endif
