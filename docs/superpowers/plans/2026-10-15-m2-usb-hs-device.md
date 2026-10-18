# M2: VHDL USB 2.0 HS デバイス Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** USB3317 経由で Primer 20K を HS 複合デバイス（fprobe 拡張 IF + CDC-ACM）として Windows にドライバ不要で列挙させ、ループバックとスループット ≥ 30 MB/s を実機で確認する。

**Architecture:** 設計書 §2 の層構成（ulpi_link → usb_reset / usb_rx_packet / usb_tx_packet → usb_protocol → usb_ep_out / usb_ep_in / usb_control_ep、生成ディスクリプタ ROM、async_fifo）。各層を Python の PHY/ホストモデルで単体検証し、最後に列挙テストと HIL。

**Tech Stack:** VHDL-2008, GHDL 6 + cocotb 2.1, Gowin EDA 1.9.11.03, Python 3.13, pyusb + libusb-package（HIL）。

**Spec:** `docs/superpowers/specs/2026-10-15-m2-usb-hs-device-design.md`、技術根拠 `docs/reference/usb2-hs-device-on-ulpi.md`

## Global Constraints

- CLAUDE.md の VHDL 規約（VHDL-2008、ASCII 文字列、`S_` 状態名、同期アクティブハイ `rst`、ストリーム命名、全 entity に cocotb テスト）。
- 数値（PID、CRC、レジスタ値、時間定数）は技術メモの値に従い、テストベクタはメモの [calc] 値を使う。
- 時間定数は generic（既定値 = 実機用、テストでは短縮値）。実機値の定義はパッケージ `usb_timing_pkg` に一元化。
- `usb` ドメインと `sys` ドメインは `async_fifo` 以外で接続しない。
- 生成 VHDL（ディスクリプタ ROM）はコミットし、生成器との一致を pytest で検査する。
- 実機テストは Primer の SRAM のみに書き込む（安全ラッパ経由）。

## Review Focus

1. **ACK 喪失・CRC 異常・トグル不一致**の組み合わせで、データの重複・欠落・応答違反が起きない（ホストモデルで注入）。
2. **NXT がランダムに間引かれる**送受信で、バイト順・CRC・STP タイミングが崩れない。
3. **DIR がレジスタ操作や送信に割り込む**ときの再試行・中断処理。
4. **MPS 境界ちょうど・wLength より長い/短いディスクリプタ**での ZLP と長さ。
5. **チャープ不成立（FS ホスト）・HS 中のサスペンドとリセットの区別**。

---

## Tasks

### Task 1: USB パケットの Python モデル
- Create: `tests/models/usb_packets.py`: `pid_byte(pid)`, `crc5(value11)`, `crc16(payload)`, `token(pid, addr, endp)`, `sof(frame)`, `data_packet(pid, payload)`, `handshake(pid)`, `parse(bytes) -> Packet`、PID 定数。
- Tests: `tests/test_model_usb_packets.py`（メモ §4 のベクタ: `2D 00 10`, `69 15 EF`, `69 81 58`, `A5 10 2F`, CRC16 "123456789"=0xB4C8, `01 02 03 04`→`5E D4`, ZLP `C3 00 00`、受信剰余 0b01100 / 0xB001）

### Task 2: CRC パッケージ（VHDL）
- Create: `rtl/usb/usb_crc_pkg.vhd`: `crc5_next11(data11) return slv5`（トークン用一括）、`crc16_next(crc, byte) return slv16`（反射形、1 バイト更新）、定数 `CRC16_INIT`, `CRC16_RESIDUAL`（0xB001）、`crc16_final(crc) return slv16`。
- Tests: `tests/rtl/usb_crc_harness.vhd`（関数を entity に包む試験専用）+ `tests/tb/usb_crc_tb.py`（ランダム 200 ケースを Python モデルと照合）

### Task 3: ULPI PHY モデル（Python, cocotb）
- Create: `tests/models/ulpi_phy.py`: `UlpiPhy(dut_signals, *, nxt_throttle=rng)`。機能: 起動時 DIR high（T_START 短縮可）、レジスタファイル（0x00–0x03 ID、0x04/05/06 FC、0x0A–0x0C OTG、0x16–0x18 Scratch）、TX CMD 解読（REGW/REGR/TRANSMIT/NOPID）、送信バイトの取り込み（NXT 間引き、STP で完了・0xFF で中断）、RX CMD 生成（LineState・VBUS・RxEvent）、`async receive_from_host(bytes)`（RxActive・データ・NXT 間引き・RX CMD 終了）、`line_state` 設定、`inject_dir_during_*`（割り込み注入）、送信パケットのキュー。
- Tests: Task 4 の ulpi_link テストがモデルの検証を兼ねる（モデル単体は純 Python 部分のみ）。

### Task 4: ulpi_link
- Create: `rtl/usb/ulpi_link.vhd`（設計書 §2.2 の I/F。generic `STARTUP_CYCLES`）
- Tests: `tests/tb/ulpi_link_tb.py`: 起動（DIR high 中は何もしない → phy_ready）、Scratch の書き込み/読み返し、VID 読み出し、RX CMD → line_state/vbus、受信パケットのバイト列（NXT 間引き）、送信パケット（NXT 間引き・STP 位置）、ハンドシェイク送信、**レジスタ書き込み中の DIR 割り込み → 再試行**、**送信開始前の DIR → 待機**、**送信中の DIR → tx_aborted**、Idle 0x00 駆動、読み出し中の RX CMD 不解読。

### Task 5: usb_timing_pkg と usb_reset
- Create: `rtl/usb/usb_timing_pkg.vhd`（60 MHz 基準の実機定数）、`rtl/usb/usb_reset.vhd`（generic で各時間）
- Tests: `tests/tb/usb_reset_tb.py`（短縮時間）: FS 接続 → SE0 → チャープ K 送出 → ホスト K-J×3 → HS（FC=0x40 書き込み確認）、ホストが応答しない → FS(0x45)、HS 中 squelch 継続 → FS に戻して J → suspended、SE0 → 再リセット、VBUS 喪失 → bus_reset 保持、K/J グリッチは数えない。

### Task 6: usb_rx_packet
- Create: `rtl/usb/usb_rx_packet.vhd`
- Tests: `tests/tb/usb_rx_packet_tb.py`: トークン各種・アドレス/EP 抽出、CRC5 異常は無視、PID 検査異常は無視、4 バイト以上のトークン無視、SOF フレーム番号、データ 0/1/64/512 バイト（ストリーム + data_ok）、CRC16 異常 → data_ok=0、ハンドシェイク、RxError 中断。

### Task 7: usb_tx_packet
- Create: `rtl/usb/usb_tx_packet.vhd`
- Tests: `tests/tb/usb_tx_packet_tb.py`: ハンドシェイク各種、ZLP、1/64/512 バイトのデータ（PHY モデルが受けたバイト列を Python モデルでパースし CRC 一致）、NXT 間引き下での正しさ、送信中断時の done/aborted。

### Task 8: エンドポイントバッファ
- Create: `rtl/usb/usb_ep_out.vhd`（generic `DEPTH_BYTES`, `MPS`）, `rtl/usb/usb_ep_in.vhd`（generic `MPS`, `DEPTH_BYTES`, 短パケット送出タイムアウト `FLUSH_CYCLES`）
- Tests: OUT: 確定したパケットのみ出力、破棄は痕跡なし、space_for_packet の境界、連続パケット。IN: MPS で区切る、データ途切れで短パケット（FLUSH 後）、ZLP（ストリーム側の `last` が MPS 境界で来たとき）、retry で同一内容、ack で次へ、満杯時の背圧。

### Task 9: usb_protocol
- Create: `rtl/usb/usb_protocol.vhd`（generic でエンドポイント数・各 EP の種別・MPS）
- Tests（PHY モデル + パケットモデルで直接トランザクションを注入）: アドレス不一致無視、OUT ACK/NAK/NYET、トグル不一致は ACK + 破棄、PING、IN NAK/データ、ACK 喪失 → 同一 PID 再送、STALL（halt 中）、SETUP は常に ACK、応答開始が 24 サイクル以内、CRC 異常パケットに無応答。

### Task 10: ディスクリプタ生成
- Create: `fprobe_gw/usb/descriptors.py`（デバイス・構成・IAD・IF・EP・CDC 機能・文字列・BOS・MS OS 2.0 セット・DEVICE_QUALIFIER・OTHER_SPEED を Python で組み立てるビルダ）、`fprobe_gw/usb/fprobe_device.py`（M2 構成の定義）、`fprobe_gw/usb/vhdl_rom.py`（VHDL パッケージ出力）、生成物 `rtl/usb/generated/usb_descriptors_pkg.vhd`、生成 CLI `python -m fprobe_gw gen-usb`
- Tests: 各ディスクリプタのバイト列（メモ §6–8 の既知値）、wTotalLength・bNumInterfaces の自動計算、MS OS 2.0 の長さ整合、**コミット済み VHDL と生成結果の一致**。

### Task 11: usb_control_ep
- Create: `rtl/usb/usb_control_ep.vhd`
- Tests: GET_DESCRIPTOR（デバイス/構成/文字列/BOS/QUALIFIER/OTHER_SPEED、wLength 短・長・MPS 倍数で ZLP、ACK 喪失で同一チャンク再送）、SET_ADDRESS（ステータス ACK 後に適用）、SET/GET_CONFIGURATION、GET_STATUS、CLEAR_FEATURE(HALT)、GET/SET_INTERFACE、MS OS 2.0 ベンダー要求、CDC クラス要求の委譲（SET_LINE_CODING のデータ受け渡し、GET_LINE_CODING）、未対応要求 STALL と次の SETUP での解除。

### Task 12: async_fifo
- Create: `rtl/common/async_fifo.vhd`（グレイコード、generic WIDTH/DEPTH）
- Tests: 異なる周波数（60/100 MHz 相当）の書き込み/読み出しでランダムトラフィックの順序保持、満杯/空。

### Task 13: usb_device 統合と列挙テスト
- Create: `rtl/usb/usb_device.vhd`（M2 構成のエンドポイントをストリームで外に出す）、`tests/models/usb_host.py`
- Tests: `tests/tb/usb_device_tb.py`: リセット → HS → Windows と同順の列挙（GET_DESCRIPTOR(dev,64) → SET_ADDRESS → dev(18) → config(9) → config(全) → strings → BOS → MS OS 2.0 → SET_CONFIGURATION）、バルクループバック 1/511/512/513/4096 バイト、ACK 喪失・CRC 異常注入下のループバック一致。

### Task 14: Primer の USB 試験デザインと HIL
- Create: `rtl/boards/primer20k/primer20k_usb_top.vhd`（ULPI 双方向バッファ、反転クロック、F10 起動パルス、LED: HS 接続・構成済み・活動）、cst に ULPI ピン、SDC に入出力遅延、`designs.py` に `usbtest`
- Create: `tests/hil/test_usb_hw.py`（pyusb + libusb-package）: 列挙・スピード HS・ループバック・スループット ≥ 30 MB/s（IN/OUT）・CDC COM 出現
- 実行: `pytest gateware` → `build usbtest`（タイミング OK）→ `pytest gateware --hil`
