# PYNQ-Z2 の P3（DDR の深いバッファ）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** PYNQ-Z2 のロジアナの深いバッファ（Primer の DDR3 と同じ `la_deep_buffer` 以降）を、PS の DDR の連続した領域に置く。

**Architecture:** ボード非依存の `axi_mem_port`（mem_port の 128 ビットの拍 ⇄ AXI3 の 64 ビットのマスタ、S_AXI_HP0）を
作り、ブロック設計で HP0 を外に出し、`fprobe_core` の mem_port につなぐ（mem_clk = sys）。領域の物理番地は PS の中継が
`zynq_frame_link` のレジスタ MEM_BASE に書く。書くまで mem_ready = 0（深いバッファは使えない）。

**Spec:** docs/superpowers/specs/2026-10-19-pynq-z2-port-design.md §4.3（第 2 段）・§5.3

## 利用者の判断を待つこと

- **DDR の連続した領域の確保の方法**（2026-10-20 に調べた: CMA は 128 MB で空き 55 MB。dma_heap・u-dma-buf は無い。PYNQ の
  libcma は /dev/xlnk が無く使えない）。候補: (a) u-dma-buf のカーネルモジュールを入れる（再起動不要。第三者のモジュール）、
  (b) 起動の設定（device tree の reserved-memory か `mem=`）で DDR の上端を予約する（再起動が要る）、(c) PYNQ の Python の
  `pynq.allocate`（PL の状態に触る恐れ）。決まるまで Task 3 の実機は止める。
- 大きさ: 32 MB（ケイパビリティ mem_bytes）。
- **(b) に決めた場合の手順**（用意済み。適用は利用者の立ち会いのもとで）: `host/pynq/uEnv.txt`（今の起動の引数 + `mem=480M`。
  boot.scr が SD の /boot/uEnv.txt を `env import -t` で取り込み、U-Boot が bootargs を /chosen に入れる）を /boot へ、
  `host/pynq/fprobe-mem`（`0x1E000000 0x2000000`）を /etc/fprobe/mem へ置いて再起動する。Linux は DDR の上端 32 MB を
  使わなくなり、中継が /proc/iomem で重ならないことを確かめてから MEM_BASE に書く。元に戻すには /boot/uEnv.txt を消す
  （起動しなくなったら SD を PC に挿して消す）。

## Global Constraints

- コア（`la_deep_buffer` 以降）は変えない。`axi_mem_port` はボード非依存（`rtl/mem/`）。Xilinx の IP は PS7 のブロック設計だけ。
- mem_port の約束（`rtl/mem/mem_test.vhd` の説明）: 1 コマンド = 1 拍、読みの結果は発行順に rd_valid（止められない）。
- AXI の応答は必ず受け取る（BREADY・RREADY を落とさない）。応答の誤り（SLVERR・DECERR）は数えて CSR で見えるようにする。

## Review Focus

1. 読みを続けて出しても、結果が発行順・欠け無しで返る（RREADY を止めない、出しっぱなしの数に上限）。
2. 書きと読みが混ざっても、書いた値を後の読みが返す（書きの B が返るまで同じ番地の読みを出さない、または全体で順を守る）。
3. MEM_BASE が 0 の間は AXI に何も出さない（PS の DDR の 0 番地を壊さない）。

---

### Task 1: `axi_mem_port`（ボード非依存）

**Files:** Create `gateware/rtl/mem/axi_mem_port.vhd`、`gateware/tests/tb/axi_mem_port_tb.py`、
`gateware/tests/test_axi_mem_port.py`、`gateware/tests/tb/axi3_slave_mem.py`（cocotb の AXI3 のスレーブのメモリの模型、
ランダムな READY の遅れ）

**Interfaces:** entity `axi_mem_port`（generic `ADDR_BITS`、`MAX_OUTSTANDING := 8`）。口: clk・rst、`base`（32 ビット、
バイト番地。0 なら止める）、mem_port（`cmd_*`・`rd_*`・`ready`）、AXI3 のマスタ `m_axi_*`（64 ビット、ID 0、
AWLEN/ARLEN = 1、SIZE = 3、INCR）、`errors`（誤りの応答の数）。

- [ ] 失敗する試験: `writes_then_reads_return_the_data_in_order`・`reads_are_pipelined_and_never_stall`・
      `write_then_read_of_the_same_beat_returns_the_new_value`・`nothing_is_issued_while_base_is_zero`・
      `error_responses_are_counted`
- [ ] 実装 → 通す → コミット `feat(mem): axi_mem_port（mem_port ⇄ AXI3 HP）`

### Task 2: 構成への組み込み（シミュレーションと合成まで）

- ps7_bd.tcl に S_AXI_HP0（64 ビット、ACLK = sys）を外に出す。`zynq_frame_link` に MEM_BASE（0x01C、VERSION 2）。
  トップで `axi_mem_port` を `fprobe_core` の mem_port に（MEM_ADDR_BITS は大きさから）。ケイパビリティ mem_bytes。
- 中継: 起動時に MEM_BASE を書く（確保の方法が決まるまでは書かない = 深いバッファは使えないと申告せず、mem_ready = 0）。
- ビルドしてタイミングを満たす。

### Task 3: 実機（確保の方法が決まった後）

- 確保 → MEM_BASE → `test_la_deep_hw` 相当（PYNQ 用）で XIAO の線を流し続けて取りこぼし無し。
- 用意済み（2026-10-20）: `python -m fprobe_gw pynq reserve`（uEnv.txt と /etc/fprobe/mem を置き、中継を止めシフタを切って
  再起動。`unreserve` で戻す）→ 起動を待つ → `FPROBE_PYNQ_HOST=192.168.77.2 pytest --hil gateware/tests/hil/test_pynq_deep_hw.py`
  （fprobe_pynq_deep を書き、深い取り込み RAW/RLE・流し続け・サンプラのイベント。予約がまだなら飛ばす。予約の前に流して
  飛ぶことは確かめた）。

## 進み具合と判断（2026-10-20）

- Task 1: 済（axi_mem_port、試験 6 件。書きと読みの順の条件を外すと試験が落ちることを確かめた）。
- Task 2: 済（構成 fprobe_pynq_deep・ps7_hp_bd・MEM_BASE / MEM_BYTES / MEM_ERRORS・中継の確かめ・uEnv.txt）。実機で書いて
  動く（MEM_BASE = 0 のまま）。Task 3 は予約（再起動）の後。
- レビュー（Important 2 件を直した）:
  - 中継は設定の大きさでなく、PL の MEM_BYTES（実際に使う大きさ）の範囲で確かめる。16 の倍数・DDR（512 MB）の中・
    Linux のメモリ（/proc/iomem）と重ならない（relay_deep_region_ok、試験）。32 ビットの long の飽和を避けて strtoull。
  - 外部メモリの準備（MEM_STATUS.READY）ができていなければ、DeepAnalyzer は始めずに理由を言って断る（試験）。
  - AXI の誤りの数を MEM_ERRORS で見せる。読みの誤りはどちらの語でも数える（試験）。
- Ruling: EventLog（イベントの記録）は準備の確かめを足さない — 書けないイベントは「記録できなかった数」に数えて捨て、
  待ち続けない見込み（未確認。Task 3 の実機で確かめる）— 誤りなら EventLog.start にも同じ確かめを足す。
- Ruling: VERSION は 1 のまま（MEM_BASE などは足しただけ。古い回路は読むと 0 = 深いバッファ無し）。
