# M2: VHDL USB 2.0 High-Speed デバイススタック 設計

- 日付: 2026-10-15
- 親: `2026-10-15-fprobe-architecture-design.md` §5.1（UsbHsLink）
- 技術根拠: `docs/reference/usb2-hs-device-on-ulpi.md`（以下「メモ」。§番号はメモの節）

## 1. ゴールと完了条件

USB3317（ULPI）経由で Primer 20K を **USB 2.0 High-Speed の複合デバイス**として PC に認識させ、ホストリンクとして使える
エンドポイント群を提供する。

| 完了条件 | 測り方 |
|---|---|
| Windows がドライバ追加なしで **HS** として列挙する（MS OS 2.0 で WinUSB 自動割当て、CDC は usbser） | HIL: pyusb で VID/PID とスピードを確認、COM ポート出現 |
| バルク OUT→IN のループバックが任意長・任意パターンで一致 | HIL + シミュレーション |
| IN スループット ≥ 30 MB/s、OUT スループット ≥ 30 MB/s | HIL 計測（512 B × 多数の非同期転送） |
| CRC 異常・トグル不一致・ACK 喪失・NAK 時に仕様どおり振る舞う | シミュレーション（PHY モデルで注入） |
| 全 entity に cocotb テスト。合成でタイミング達成（usb 60 MHz・sys 100 MHz） | pytest / GowinBuilder |

## 2. 構成

```
            sys ドメイン (100 MHz)                │        usb ドメイン (ULPI CLKOUT 60 MHz, 反転して使用)
                                                  │
 機能ブロック ⇄ async_fifo ⇄──────────────────────┼──⇄ エンドポイントバッファ ⇄ usb_protocol ⇄ usb_rx_packet ⇄ ulpi_link ⇄ USB3317
 (M2 ではテスト用ループバック/パターン生成)          │        (usb_ep_out / usb_ep_in)   ↑            usb_tx_packet ⇄
                                                  │                      usb_control_ep (EP0) ─┘   usb_reset（チャープ）⇄ ulpi_link
                                                  │                      usb_descriptors_pkg（生成 ROM）
```

### 2.1 クロック
- `usb` ドメインは ULPI CLKOUT を**反転**したクロックで動かす（PHY の出力有効 1.5〜3.5 ns・入力セットアップ 5 ns に対し、
  両方向に約半周期の余裕。Primer 上での実績あり。メモ §1）。加えて SDC に入出力遅延を与え、ツールに検証させる。
- `sys` ドメインとの間は **async_fifo（グレイコードポインタ）のみ**で接続する。

### 2.2 エンティティと責務

| entity | 責務 | 主なインターフェース |
|---|---|---|
| `ulpi_link` | ULPI の物理プロトコル。バス所有、RX CMD 解読、受信バイト、送信パケット、レジスタ R/W、起動検出、送信中断検出 | PHY ピン（`data_i/data_o/data_oe` 分離）、`line_state/vbus_valid/rx_active/rx_error`、`rx_valid/rx_data`、`tx_valid/tx_ready/tx_data/tx_last`、`chirp_k`、`reg_*`、`phy_ready` |
| `usb_reset` | バスリセット検出・HS チャープ・サスペンド判定（メモ §3）。Function Control の値を決める | `line_state/vbus_valid` 入力、`reg_*` で PHY 設定、`chirp_k` 出力、`bus_reset`（パルス）・`high_speed`・`suspended` |
| `usb_crc_pkg` | CRC5 / CRC16 のバイト単位更新関数と検査定数 | 関数 |
| `usb_rx_packet` | PID 検査、トークン（CRC5）、SOF、データ（CRC16、2 バイト遅延、確定/破棄）、ハンドシェイク | `token_valid/token_pid/token_addr/token_endp`、`sof_valid/frame`、`data_valid/data_byte/data_pid` + `data_end/data_ok`、`handshake_valid/handshake_pid` |
| `usb_tx_packet` | ハンドシェイク送信、データパケット送信（PID + ペイロード + CRC16 付加） | `hs_send/hs_pid`、`data_send/data_pid` + ペイロードストリーム、`done` |
| `usb_protocol` | アドレス照合、エンドポイント振り分け、データトグル、SETUP/OUT/IN/PING への応答（ACK/NAK/NYET/STALL）、応答時間管理 | 上記パケット層と、エンドポイント I/F（下記）。`address`、`configured` |
| `usb_ep_out` | OUT エンドポイントのトランザクショナル FIFO（パケット単位の確定/破棄、空き容量申告） | protocol 側 I/F、ユーザ側ストリーム |
| `usb_ep_in` | IN エンドポイントのパケット化と再送バッファ（ACK まで保持、短パケット送出条件、ZLP） | ユーザ側ストリーム、protocol 側 I/F |
| `usb_control_ep` | EP0: 制御転送状態機械、標準要求、ディスクリプタ応答、MS OS 2.0、SET_ADDRESS/CONFIGURATION、クラス/ベンダー要求の外部委譲 | protocol 側 EP0 I/F、ディスクリプタ ROM、`request_*` 委譲 I/F |
| `usb_descriptors_pkg` | **Python から生成**するディスクリプタ ROM（コミットし、一致を pytest で検査） | 定数 |
| `async_fifo` | クロックドメイン間 FIFO | in/out ストリーム（別クロック） |
| `usb_device` | 上記を束ねた USB デバイス（ボード非依存） | PHY ピン、エンドポイントストリーム群 |

### 2.3 エンドポイント I/F（usb_protocol ⇄ バッファ）

- **OUT**: protocol は受信バイトを `wr_valid/wr_data` で流し、パケット末で `commit`（CRC 正常かつ受理）または `discard` を出す。
  バッファは `space_for_packet`（MPS 分の空きの有無）を常時申告し、protocol はそれで ACK/NAK/NYET を決める。
- **IN**: バッファは `packet_ready` と、送出時の `rd_*` ストリーム（長さ確定済みのパケット）を提供する。
  protocol は ACK 受信で `ack`（パケット解放）、ACK 無しで `retry`（同じパケットを再送可能に巻き戻し）を出す。
- トグルは protocol がエンドポイントごとに保持する（バッファはトグルを知らない）。

### 2.4 M2 で搭載する構成（試験用機能）

最終構成（仕様書 §5.1）のうち、M2 では次を有効にする。CMSIS-DAP IF は M4 で追加（ディスクリプタ生成系に 1 行足すだけになるよう設計する）。

| IF | 内容 | M2 での機能 |
|---|---|---|
| 0 | fprobe 拡張（ベンダー, WinUSB） EP1 OUT/IN, EP6 IN | EP1: OUT→IN ループバック（sys ドメイン経由）。EP6: 連番パターン生成（IN スループット計測） |
| 1–2 | CDC-ACM（IAD）EP5 通知 IN, EP4 OUT/IN | OUT→IN ループバック（M6 で UART ブリッジに置換） |

VID/PID は pid.codes の試験用 `1209:0001`。bcdUSB 0x0210。製品文字列 "fprobe"。

## 3. 検証方式

- **Python モデル**（`tests/models/`）
  - `usb_packets`: PID・CRC5・CRC16・トークン/データ/ハンドシェイクのエンコード/デコード（メモ §4 のテストベクタで自己検証）。
  - `ulpi_phy`: cocotb 上の USB3317 振る舞いモデル。DIR/NXT/DATA の駆動、レジスタファイル（VID/PID・Function Control・Scratch）、
    RX CMD 生成、ホスト→デバイスのパケット注入（RxActive・NXT の間引き）、デバイス送信の取り込み（NXT のランダムな間引き）、ラインステートの模擬（チャープ応答）、エラー注入。
  - `usb_host`: PHY モデル上のホスト。リセット・チャープ、制御転送（SETUP/DATA/STATUS）、バルク IN/OUT、タイムアウト判定、トグル管理。
- 層ごとの単体テストと、`usb_device` 全体の**列挙テスト**（実際の Windows と同じ順序の要求列）を cocotb で行う。
- 時間定数（チャープ 2 ms 等）は generic にし、シミュレーションでは短縮値を使う（実機値は既定値、テストで両方の妥当性を確認）。
- HIL: pyusb（libusb-package 同梱 DLL）で列挙・ループバック・スループットを確認。

## 4. 明示的な設計判断

1. ULPI クロックは反転して使う（実績重視）。ただし SDC の入出力遅延で余裕を検証可能にする。
2. RESETB（F10）は Ethernet PHY と共用のため、**起動時に 1 回だけ** 2 µs のリセットパルスを出し、以後は high に保つ（Ethernet を使う将来も同じ規則）。
3. HS 専用ではなく FS フォールバックも正しく動かす（チャープ失敗時、FS ハブ配下）。FS 時のバルク MPS は 64。
4. NYET を実装する（OUT 受理後に次の MPS 分の空きが無ければ NYET）。
5. SET_CONFIGURATION / SET_INTERFACE / CLEAR_FEATURE(HALT) でトグルを初期化。GET/SET_INTERFACE、GET_STATUS（HALT・自己給電ビット）を実装。
6. ディスクリプタは Python の単一定義から VHDL ROM を生成してコミットし、生成結果とコミット内容の一致を pytest で検査する。

## 5. 追記（2026-10-16）

- **起動時の切り離し**（usb_reset の S_DETACH、T_DETACH = 100 ms）: FPGA を書き込み直すと PHY のリセットは数 µs だけで、
  High-Speed で動いていたホストが切断に気づかず、古いアドレスで要求を続けることがあった（列挙がまれに失敗し、
  文字列記述子が読めないまま）。起動時は Function Control を 0x49（非駆動・プルアップ無し）にして 100 ms 保ってから
  FS で接続する。実機で書き込み → 認識 15/15 回、毎回約 1.1 秒。
- **出力レジスタの配置**: 出力イネーブルが組み合わせ（not dir）なので出力レジスタを I/O ブロックに詰められない。
  配置次第で出力の余裕（予算は配線に約 2.4 ns）が無くなるため、ボード定義（`ulpi_output_regions`）から
  ピンの隣の領域への配置制約（CST の GROUP / GRP_LOC）を生成する。
- **バス衝突の検査**: PHY モデルが、PHY の駆動中（DIR = 1 の 2 周期目以降）に link が出力イネーブルを立てていないことを確かめる。
