"""PYNQ-Z2 の PS（中継 fprobe-relay）と PL の窓（zynq_frame_link、AXI4-Lite 4 KB）のレジスタ表（単一の定義元）。

仕様: docs/superpowers/specs/2026-10-19-pynq-z2-port-design.md §4.2（フレームの受け渡し）、
docs/superpowers/specs/2026-11-03-ps-stream-position-design.md（PS が DDR を直接読む流れの「位置の約束」。窓の版 4）。

ここから VHDL のパッケージ（rtl/core/generated/ps_window_pkg.vhd。zynq_frame_link と la_ps_port が使う）・中継の C の
ヘッダ（host/pynq/fprobe-relay/window_regs.h）・ホストの定数（host/fprobe/generated.py）・表（docs/ps-window.md）を作る
（`python -m fprobe_gw gen`）。番地の写しを手で持たない（前は relay.h と zynq_frame_link.vhd の二重。D4）。
"""

from dataclasses import dataclass

from . import caps
from .csr import fprobe_map

__all__ = ["EPOCH_BITS", "LINK_ID", "LINK_VERSION", "LINK_VERSION_PS_STREAM", "PORT_FIRST", "PORT_LAST", "REGS", "Reg",
           "STATE_BITS", "render_c_header", "render_host_constants", "render_markdown", "render_vhdl_package"]

LINK_ID = 0x46504C4B           # "FPLK"
# 窓の版。1: STREAM_* が無い。2: PS が DDR のリングを直接読む。3: トリガ取り込みの読み出しも PS が。4: 位置の約束を作り直した
# （流れの番号を書く側で添える POS_TAG、読みで写す STREAM_EPOCH、LA_WPTR）。中継は 1〜4 を受け、PS 直読みは 4 だけで使う
LINK_VERSION = 4
LINK_VERSION_PS_STREAM = 4     # PS 直読みの流れを使える最小の版（2・3 の約束は位置を取り違えうるので使わない）

EPOCH_BITS = 16                # 流れの番号（run の 0→1 ごとに 1 進む。la_deep_buffer の ps_epoch）の幅
# STREAM_EPOCH の読みの値のビット（[EPOCH_BITS - 1:0] は流れの番号）
STATE_BITS = {
    "ACTIVE": 16,              # 流れが生きている（PS が読む流れで、PS がまだ読み終えていない。書き込みが止まっても読み終えるまで 1）
    "WRITING": 17,             # PL が書いている
    "OVERRUN": 18,             # 溢れて書き込みを止めた
    "CURRENT": 19,             # この流れの番号が、位置のレジスタ（LA_CONSUMED 等）の番号と同じ（= ホストが始めた最新の流れ）。
                               # 0 なら公開された位置は前の流れのもので、POS_TAG にこの番号を書いても位置の書きは捨てられる
}

PORT_FIRST = 0x028             # この範囲（バイト番地）は la_ps_port が受け持つ（zynq_frame_link はそのまま渡す）
PORT_LAST = 0x0FC


@dataclass(frozen=True)
class Reg:
    name: str
    addr: int                  # バイト番地
    access: str                # "r"・"w"・"rw"
    doc: str


REGS: tuple[Reg, ...] = (
    Reg("ID", 0x000, "r", f'0x{LINK_ID:08X}（"FPLK"）。中継はこれを確かめてから受け渡しを始める'),
    Reg("VERSION", 0x004, "r", f"窓の版（{LINK_VERSION}）"),
    Reg("RX_STATUS", 0x008, "r", "bit0 = busy（frame_mux が前のフレームを流し中。RX_BUF に書かない・COMMIT しない）"),
    Reg("RX_COMMIT", 0x00C, "w", "[9:0] = 長さ（ch を含むバイト数）。書くと RX_BUF の 1 フレームを渡す"),
    Reg("TX_HEAD", 0x010, "r", "[9:0] = 送るフレームの長さ（ch を含む。無ければ 0）"),
    Reg("TX_DONE", 0x014, "w", "何を書いても、今の送るフレームを読み終えた"),
    Reg("DROPS", 0x018, "r", "捨てた COMMIT の数（busy の間・長さが範囲外）"),
    Reg("MEM_BASE", 0x01C, "rw", "深いバッファに使う DDR の領域の物理番地（中継が書く。リセットの後は 0 = 使えない）"),
    Reg("MEM_BYTES", 0x020, "r", "PL が MEM_BASE から使う大きさ（深いバッファの無い構成は 0）"),
    Reg("MEM_ERRORS", 0x024, "r", "深いバッファの AXI の誤りの応答の数"),
    # ---- PS が DDR のリングを直接読む流れ（la_ps_port。読みの値は、最後に STREAM_EPOCH を読んだ時点の写し） ----
    Reg("LA_WRITTEN", 0x02C, "r", "写し: ロジアナの拍の書いた総数（流れの開始から。応答の来た書きまで。32 ビットで折り返す）"),
    Reg("EV_WPOS", 0x030, "r", "写し: イベントのリングの次の書き位置（応答の来た書きまで）。PS が読む流れで書き込みが止まった後は、"
        "止めの命令が効いた時点の書き位置で頭打ち（それより後のイベントは流れに入れず、リングに残る。ps-stream-position-design §2.9）"),
    Reg("EV_START", 0x034, "r", "写し: この流れで最初に読むイベントの位置（流れの間は変わらない）"),
    Reg("STREAM_EPOCH", 0x03C, "r",
        "読むと、PL が 1 つの受け渡しで揃えて公開した組（流れの番号・状態・LA_WRITTEN・LA_WPTR・EV_WPOS・EV_START）を写す"
        f"（以後の写しの読みはこの時点の値）。値: [{EPOCH_BITS - 1}:0] 流れの番号、"
        + "、".join(f"[{b}] {n}" for n, b in STATE_BITS.items())),
    Reg("LA_CONSUMED", 0x040, "rw",
        "PS が読み終えたロジアナの拍の総数（流れの開始で 0）。書きは POS_TAG が今の流れの番号のときだけ受ける（違えば捨てて STALE_WRITES）"),
    Reg("EV_CONSUMED", 0x044, "rw", "PS が読み終えたイベントの数（流れの開始で 0）。書きは LA_CONSUMED と同じ規則"),
    Reg("STREAM_BEATS", 0x048, "rw",
        "PS が流れに出すと決めた拍の総数（見出し・イベントを含む。流れの開始で 0）。CSR la_deep.stream_beats になる。"
        "書きは LA_CONSUMED と同じ規則"),
    Reg("RD_EPOCH", 0x04C, "r", "読むと、PS への読み出しの要求（RD_REGION・RD_FIRST・RD_COUNT）を写す。値は要求の通し番号 [7:0]"),
    Reg("RD_REGION", 0x050, "r", "写し: 読む領域（0 = ロジアナのリング、1 = イベントのリング）"),
    Reg("RD_FIRST", 0x054, "r", "写し: 領域の中の先頭の拍"),
    Reg("RD_COUNT", 0x058, "r", "写し: 拍数"),
    Reg("RD_ACK", 0x05C, "rw", "読み終えた要求の通し番号 [7:0]。RD_EPOCH と同じになるまで、PL の reading は 1"),
    Reg("LA_WPTR", 0x060, "r", "写し: ロジアナのリングの次の書き位置（拍。LA_WRITTEN と同じ時点。付き始めの読み位置を決める）"),
    Reg("POS_TAG", 0x064, "rw",
        f"位置の書き（LA_CONSUMED・EV_CONSUMED・STREAM_BEATS）に添える流れの番号 [{EPOCH_BITS - 1}:0]。書いたまま残る"),
    Reg("STALE_WRITES", 0x068, "r", "POS_TAG が今の流れの番号と違って捨てた位置の書きの数（電源投入から。飽和）"),
    Reg("RX_BUF", 0x400, "w", "4 バイトずつ（リトルエンディアン）。バイト番地 0 = ch、1〜 = payload（1 KB）"),
    Reg("TX_BUF", 0x800, "r", "4 バイトずつ（TX_HEAD の長さより後ろのバイトは 0。1 KB）"),
)


def _check() -> None:
    names = [r.name for r in REGS]
    addrs = [r.addr for r in REGS]
    assert len(set(names)) == len(names) and len(set(addrs)) == len(addrs)
    assert all(a % 4 == 0 for a in addrs)
    assert max(STATE_BITS.values()) < 32 and min(STATE_BITS.values()) >= EPOCH_BITS


_check()


def render_vhdl_package() -> str:
    """rtl/core/generated/ps_window_pkg.vhd（番地は語の番号 = バイト番地 / 4）。"""
    lines = [
        "-- 自動生成ファイル。直接編集しないこと（python -m fprobe_gw gen で再生成）。",
        "-- 生成元: gateware/fprobe_gw/ps_window.py（PYNQ の PS と PL の窓のレジスタ表）",
        "-- 仕様: docs/superpowers/specs/2026-11-03-ps-stream-position-design.md",
        "library ieee;",
        "use ieee.std_logic_1164.all;",
        "",
        "package ps_window_pkg is",
        f"  constant WIN_LINK_ID      : std_logic_vector(31 downto 0) := x\"{LINK_ID:08X}\";",
        f"  constant WIN_LINK_VERSION : natural := {LINK_VERSION};",
        f"  constant WIN_EPOCH_BITS   : natural := {EPOCH_BITS};",
        *(f"  constant WIN_STATE_{n:<8} : natural := {b};" for n, b in STATE_BITS.items()),
        f"  constant WIN_PORT_FIRST   : natural := {PORT_FIRST // 4};   -- la_ps_port が受け持つ語の範囲",
        f"  constant WIN_PORT_LAST    : natural := {PORT_LAST // 4};",
        "  -- レジスタ（語の番号）",
        *(f"  constant WIN_{r.name:<13}: natural := {r.addr // 4};" for r in REGS),
        "end package;",
        "",
    ]
    return "\n".join(lines)


def render_c_header() -> str:
    """host/pynq/fprobe-relay/window_regs.h（番地はバイト）。"""
    lines = [
        "/* 自動生成ファイル。直接編集しないこと（ゲートウェア側で python -m fprobe_gw gen を実行して再生成）。",
        " * 生成元: gateware/fprobe_gw/ps_window.py（PYNQ の PS と PL の窓 zynq_frame_link のレジスタ表）。",
        " * 仕様: docs/superpowers/specs/2026-11-03-ps-stream-position-design.md。表: docs/ps-window.md",
        " * SPDX-License-Identifier: AGPL-3.0-only",
        " */",
        "#ifndef FPROBE_WINDOW_REGS_H",
        "#define FPROBE_WINDOW_REGS_H",
        "",
        f"#define RELAY_LINK_ID 0x{LINK_ID:08X}u",
        f"#define RELAY_LINK_VERSION_MAX {LINK_VERSION}u",
        f"#define RELAY_LINK_VERSION_PS_STREAM {LINK_VERSION_PS_STREAM}u   /* PS 直読みの流れを使える最小の版 */",
        f"#define WIN_EPOCH_BITS {EPOCH_BITS}u",
        f"#define WIN_EPOCH_MASK 0x{(1 << EPOCH_BITS) - 1:X}u",
        *(f"#define WIN_STATE_{n} (1u << {b})" for n, b in STATE_BITS.items()),
        "",
        "/* ストリームの見出し（fprobe_gw.csr.fprobe_map の STREAM_SEG_*。la_deep_buffer の SEG_*）とチャンネル */",
        f"#define STREAM_SEG_LA {fprobe_map.STREAM_SEG_LA}u",
        f"#define STREAM_SEG_EVENTS {fprobe_map.STREAM_SEG_EVENTS}u",
        f"#define STREAM_SEG_MAX_BEATS {fprobe_map.STREAM_SEG_MAX_BEATS}u",
        f"#define STREAM_PL_CHUNK_BEATS {fprobe_map.STREAM_PL_CHUNK_BEATS}u",
        f"#define CH_STREAM {caps.CH_CAPTURE}u",
        "",
        "enum {",
        *(f"    REG_{r.name} = 0x{r.addr:03X},{' ' * (14 - len(r.name))}/* {r.access}: {r.doc} */" for r in REGS),
        "};",
        "",
        "#endif",
        "",
    ]
    return "\n".join(lines)


def render_host_constants() -> dict[str, int]:
    """host/fprobe/generated.py の節（バイト番地と値）。"""
    out = {"WIN_LINK_ID": LINK_ID, "WIN_LINK_VERSION": LINK_VERSION, "WIN_LINK_VERSION_PS_STREAM": LINK_VERSION_PS_STREAM,
           "WIN_EPOCH_BITS": EPOCH_BITS}
    out.update({f"WIN_STATE_{n}": b for n, b in STATE_BITS.items()})
    out.update({f"WIN_{r.name}": r.addr for r in REGS})
    return out


def render_markdown() -> str:
    """docs/ps-window.md。"""
    lines = [
        "<!-- 自動生成ファイル。直接編集しないこと（gateware で python -m fprobe_gw gen）。生成元: gateware/fprobe_gw/ps_window.py -->",
        "# PYNQ の PS と PL の窓（zynq_frame_link）のレジスタ",
        "",
        f"AXI4-Lite、0x40000000 から 4 KB。窓の版 {LINK_VERSION}。仕様: "
        "[2026-11-03-ps-stream-position-design.md](superpowers/specs/2026-11-03-ps-stream-position-design.md)。"
        f"0x{PORT_FIRST:03X}〜0x{PORT_LAST:03X} は la_ps_port（PS が DDR を直接読む流れ）が受け持つ。"
        "表に無い番地は、読みが 0、書きは無視（どの番地にも OKAY を返す）。",
        "",
        "| 番地 | 名前 | 読み書き | 意味 |",
        "|---|---|---|---|",
        *(f"| 0x{r.addr:03X} | {r.name} | {r.access} | {r.doc} |" for r in REGS),
        "",
    ]
    return "\n".join(lines)
