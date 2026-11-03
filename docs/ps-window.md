<!-- 自動生成ファイル。直接編集しないこと（gateware で python -m fprobe_gw gen）。生成元: gateware/fprobe_gw/ps_window.py -->
# PYNQ の PS と PL の窓（zynq_frame_link）のレジスタ

AXI4-Lite、0x40000000 から 4 KB。窓の版 4。仕様: [2026-11-03-ps-stream-position-design.md](superpowers/specs/2026-11-03-ps-stream-position-design.md)。0x028〜0x0FC は la_ps_port（PS が DDR を直接読む流れ）が受け持つ。表に無い番地は、読みが 0、書きは無視（どの番地にも OKAY を返す）。

| 番地 | 名前 | 読み書き | 意味 |
|---|---|---|---|
| 0x000 | ID | r | 0x46504C4B（"FPLK"）。中継はこれを確かめてから受け渡しを始める |
| 0x004 | VERSION | r | 窓の版（4） |
| 0x008 | RX_STATUS | r | bit0 = busy（frame_mux が前のフレームを流し中。RX_BUF に書かない・COMMIT しない） |
| 0x00C | RX_COMMIT | w | [9:0] = 長さ（ch を含むバイト数）。書くと RX_BUF の 1 フレームを渡す |
| 0x010 | TX_HEAD | r | [9:0] = 送るフレームの長さ（ch を含む。無ければ 0） |
| 0x014 | TX_DONE | w | 何を書いても、今の送るフレームを読み終えた |
| 0x018 | DROPS | r | 捨てた COMMIT の数（busy の間・長さが範囲外） |
| 0x01C | MEM_BASE | rw | 深いバッファに使う DDR の領域の物理番地（中継が書く。リセットの後は 0 = 使えない） |
| 0x020 | MEM_BYTES | r | PL が MEM_BASE から使う大きさ（深いバッファの無い構成は 0） |
| 0x024 | MEM_ERRORS | r | 深いバッファの AXI の誤りの応答の数 |
| 0x02C | LA_WRITTEN | r | 写し: ロジアナの拍の書いた総数（流れの開始から。応答の来た書きまで。32 ビットで折り返す） |
| 0x030 | EV_WPOS | r | 写し: イベントのリングの次の書き位置（応答の来た書きまで）。PS が読む流れで書き込みが止まった後は、止めの命令が効いた時点の書き位置で頭打ち（それより後のイベントは流れに入れず、リングに残る。ps-stream-position-design §2.9） |
| 0x034 | EV_START | r | 写し: この流れで最初に読むイベントの位置（流れの間は変わらない） |
| 0x03C | STREAM_EPOCH | r | 読むと、PL が 1 つの受け渡しで揃えて公開した組（流れの番号・状態・LA_WRITTEN・LA_WPTR・EV_WPOS・EV_START）を写す（以後の写しの読みはこの時点の値）。値: [15:0] 流れの番号、[16] ACTIVE、[17] WRITING、[18] OVERRUN、[19] CURRENT |
| 0x040 | LA_CONSUMED | rw | PS が読み終えたロジアナの拍の総数（流れの開始で 0）。書きは POS_TAG が今の流れの番号のときだけ受ける（違えば捨てて STALE_WRITES） |
| 0x044 | EV_CONSUMED | rw | PS が読み終えたイベントの数（流れの開始で 0）。書きは LA_CONSUMED と同じ規則 |
| 0x048 | STREAM_BEATS | rw | PS が流れに出すと決めた拍の総数（見出し・イベントを含む。流れの開始で 0）。CSR la_deep.stream_beats になる。書きは LA_CONSUMED と同じ規則 |
| 0x04C | RD_EPOCH | r | 読むと、PS への読み出しの要求（RD_REGION・RD_FIRST・RD_COUNT）を写す。値は要求の通し番号 [7:0] |
| 0x050 | RD_REGION | r | 写し: 読む領域（0 = ロジアナのリング、1 = イベントのリング） |
| 0x054 | RD_FIRST | r | 写し: 領域の中の先頭の拍 |
| 0x058 | RD_COUNT | r | 写し: 拍数 |
| 0x05C | RD_ACK | rw | 読み終えた要求の通し番号 [7:0]。RD_EPOCH と同じになるまで、PL の reading は 1 |
| 0x060 | LA_WPTR | r | 写し: ロジアナのリングの次の書き位置（拍。LA_WRITTEN と同じ時点。付き始めの読み位置を決める） |
| 0x064 | POS_TAG | rw | 位置の書き（LA_CONSUMED・EV_CONSUMED・STREAM_BEATS）に添える流れの番号 [15:0]。書いたまま残る |
| 0x068 | STALE_WRITES | r | POS_TAG が今の流れの番号と違って捨てた位置の書きの数（電源投入から。飽和） |
| 0x400 | RX_BUF | w | 4 バイトずつ（リトルエンディアン）。バイト番地 0 = ch、1〜 = payload（1 KB） |
| 0x800 | TX_BUF | r | 4 バイトずつ（TX_HEAD の長さより後ろのバイトは 0。1 KB） |
