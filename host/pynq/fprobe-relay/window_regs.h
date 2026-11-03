/* 自動生成ファイル。直接編集しないこと（ゲートウェア側で python -m fprobe_gw gen を実行して再生成）。
 * 生成元: gateware/fprobe_gw/ps_window.py（PYNQ の PS と PL の窓 zynq_frame_link のレジスタ表）。
 * 仕様: docs/superpowers/specs/2026-11-03-ps-stream-position-design.md。表: docs/ps-window.md
 * SPDX-License-Identifier: AGPL-3.0-only
 */
#ifndef FPROBE_WINDOW_REGS_H
#define FPROBE_WINDOW_REGS_H

#define RELAY_LINK_ID 0x46504C4Bu
#define RELAY_LINK_VERSION_MAX 4u
#define RELAY_LINK_VERSION_PS_STREAM 4u   /* PS 直読みの流れを使える最小の版 */
#define WIN_EPOCH_BITS 16u
#define WIN_EPOCH_MASK 0xFFFFu
#define WIN_STATE_ACTIVE (1u << 16)
#define WIN_STATE_WRITING (1u << 17)
#define WIN_STATE_OVERRUN (1u << 18)
#define WIN_STATE_CURRENT (1u << 19)

/* ストリームの見出し（fprobe_gw.csr.fprobe_map の STREAM_SEG_*。la_deep_buffer の SEG_*）とチャンネル */
#define STREAM_SEG_LA 1u
#define STREAM_SEG_EVENTS 2u
#define STREAM_SEG_MAX_BEATS 4096u
#define STREAM_PL_CHUNK_BEATS 256u
#define CH_STREAM 4u

enum {
    REG_ID = 0x000,            /* r: 0x46504C4B（"FPLK"）。中継はこれを確かめてから受け渡しを始める */
    REG_VERSION = 0x004,       /* r: 窓の版（4） */
    REG_RX_STATUS = 0x008,     /* r: bit0 = busy（frame_mux が前のフレームを流し中。RX_BUF に書かない・COMMIT しない） */
    REG_RX_COMMIT = 0x00C,     /* w: [9:0] = 長さ（ch を含むバイト数）。書くと RX_BUF の 1 フレームを渡す */
    REG_TX_HEAD = 0x010,       /* r: [9:0] = 送るフレームの長さ（ch を含む。無ければ 0） */
    REG_TX_DONE = 0x014,       /* w: 何を書いても、今の送るフレームを読み終えた */
    REG_DROPS = 0x018,         /* r: 捨てた COMMIT の数（busy の間・長さが範囲外） */
    REG_MEM_BASE = 0x01C,      /* rw: 深いバッファに使う DDR の領域の物理番地（中継が書く。リセットの後は 0 = 使えない） */
    REG_MEM_BYTES = 0x020,     /* r: PL が MEM_BASE から使う大きさ（深いバッファの無い構成は 0） */
    REG_MEM_ERRORS = 0x024,    /* r: 深いバッファの AXI の誤りの応答の数 */
    REG_LA_WRITTEN = 0x02C,    /* r: 写し: ロジアナの拍の書いた総数（流れの開始から。応答の来た書きまで。32 ビットで折り返す） */
    REG_EV_WPOS = 0x030,       /* r: 写し: イベントのリングの次の書き位置（応答の来た書きまで）。PS が読む流れで書き込みが止まった後は、止めの命令が効いた時点の書き位置で頭打ち（それより後のイベントは流れに入れず、リングに残る。ps-stream-position-design §2.9） */
    REG_EV_START = 0x034,      /* r: 写し: この流れで最初に読むイベントの位置（流れの間は変わらない） */
    REG_STREAM_EPOCH = 0x03C,  /* r: 読むと、PL が 1 つの受け渡しで揃えて公開した組（流れの番号・状態・LA_WRITTEN・LA_WPTR・EV_WPOS・EV_START）を写す（以後の写しの読みはこの時点の値）。値: [15:0] 流れの番号、[16] ACTIVE、[17] WRITING、[18] OVERRUN、[19] CURRENT */
    REG_LA_CONSUMED = 0x040,   /* rw: PS が読み終えたロジアナの拍の総数（流れの開始で 0）。書きは POS_TAG が今の流れの番号のときだけ受ける（違えば捨てて STALE_WRITES） */
    REG_EV_CONSUMED = 0x044,   /* rw: PS が読み終えたイベントの数（流れの開始で 0）。書きは LA_CONSUMED と同じ規則 */
    REG_STREAM_BEATS = 0x048,  /* rw: PS が流れに出すと決めた拍の総数（見出し・イベントを含む。流れの開始で 0）。CSR la_deep.stream_beats になる。書きは LA_CONSUMED と同じ規則 */
    REG_RD_EPOCH = 0x04C,      /* r: 読むと、PS への読み出しの要求（RD_REGION・RD_FIRST・RD_COUNT）を写す。値は要求の通し番号 [7:0] */
    REG_RD_REGION = 0x050,     /* r: 写し: 読む領域（0 = ロジアナのリング、1 = イベントのリング） */
    REG_RD_FIRST = 0x054,      /* r: 写し: 領域の中の先頭の拍 */
    REG_RD_COUNT = 0x058,      /* r: 写し: 拍数 */
    REG_RD_ACK = 0x05C,        /* rw: 読み終えた要求の通し番号 [7:0]。RD_EPOCH と同じになるまで、PL の reading は 1 */
    REG_LA_WPTR = 0x060,       /* r: 写し: ロジアナのリングの次の書き位置（拍。LA_WRITTEN と同じ時点。付き始めの読み位置を決める） */
    REG_POS_TAG = 0x064,       /* rw: 位置の書き（LA_CONSUMED・EV_CONSUMED・STREAM_BEATS）に添える流れの番号 [15:0]。書いたまま残る */
    REG_STALE_WRITES = 0x068,  /* r: POS_TAG が今の流れの番号と違って捨てた位置の書きの数（電源投入から。飽和） */
    REG_RX_BUF = 0x400,        /* w: 4 バイトずつ（リトルエンディアン）。バイト番地 0 = ch、1〜 = payload（1 KB） */
    REG_TX_BUF = 0x800,        /* r: 4 バイトずつ（TX_HEAD の長さより後ろのバイトは 0。1 KB） */
};

#endif
