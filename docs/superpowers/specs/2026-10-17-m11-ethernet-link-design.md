# M11 第 3 段: EthernetLink（UDP のホストリンク）設計

## 1. ゴールと完了条件

全体設計の「将来の第 2 ホストリンク（リモート / AI 用）」。Tang Primer 20K Dock の RTL8201F（10/100、RMII）で、
USB と同じ論理チャンネルを UDP で運ぶ。PC 側は OS 標準のソケットだけで使える（ドライバ・Npcap 不要）。

| 完了条件 | 検証 |
|---|---|
| RMII の MAC（受信: プリアンブル・FCS の検査、送信: プリアンブル・FCS・フレーム間隔）が 100 Mb/s で動く | cocotb |
| ARP に答え、ICMP echo（ping）に答え、宛先ポートの UDP を論理チャンネルへ流す。IP・UDP のヘッダ検査 | cocotb |
| 構成 `fprobe_eth`（Primer 20K、USB の代わりに Ethernet）で、ホストからケイパビリティ・CSR・pyOCD のデバッグ・ロジアナ・UART が使える | HIL（LAN ケーブルで PC と直結） |

v1 は USB との同時使用はしない（構成でどちらか 1 本。ホストリンクの抽象どおり、コアは 1 本のリンクにつながる）。

## 2. 線上の形式

```
Ethernet II | IPv4（オプションなし、分割なし）| UDP（宛先ポート 0xF9B0 = 63920）| ch(1) | seq(1) | payload(0..512)
```

- 論理チャンネル `ch` と payload の意味は SerialLink（§2 of m11-serial-link）と同じ: 0 CONTROL、1 DAP、2 SWO、3 UART0、
  4 STREAM、5 LINE_CODING。CRC は Ethernet の FCS が受け持ち、区切りはデータグラムが受け持つ（COBS は不要）。
- `seq`: FPGA → PC のデータグラムに付ける通し番号（全チャンネル共通、8 ビットで一周）。PC は飛びを数え、
  バイト列のチャンネル（SWO・UART0・STREAM）の欠けを「黙って欠けない」ように報告する（UDP は落ちうる）。
  PC → FPGA の seq は使わない（0。CONTROL・DAP は PC が応答を待ってから次を送る）。
- payload の上限は SerialLink と同じ 512（CONTROL は上限のデータグラムを続け、上限未満で終える。DAP は 1 パケット）。
- 宛先: FPGA は、最後に受けた正しい UDP の送り主（MAC・IP・ポート）へ送る（PC が先に 1 つ送って結びつける）。
- アドレス: MAC と IP はボードのジェネリック（既定 MAC `02:66:70:72:00:01`、IP `192.168.77.2`）。
  PC は同じサブネットのアドレスを持つ（直結なら PC の LAN アダプタに静的に設定）。

## 3. 構成

- `rtl/hostlink/frame_mux.vhd`（SerialLink から切り出し）: 論理チャンネル ↔ フレーム（ch・payload・last）。
  チャンネルの選択（CONTROL > DAP > UART0 > SWO > STREAM）、パケットの組み立て、UART0 の FIFO、LINE_CODING。
  SerialLink と EthernetLink の両方がこれを使う（両者の違いは「フレームをどう運ぶか」だけ）。
- `rtl/hostlink/serial_link.vhd`: frame_mux + COBS/CRC/UART。
- `rtl/eth/rmii_mac.vhd`: RMII（50 MHz の REF_CLK のドメイン）↔ バイトのフレーム（valid/ready/data/last、受信は
  FCS の正誤を最後に示す）。sys とは async_fifo で渡す。
- `rtl/eth/udp_stack.vhd`（sys）: 受信の振り分け（ARP 要求 → 応答、ICMP echo → 応答、自分宛ての UDP → frame_mux）、
  送信の組み立て（IP のヘッダのチェックサム、UDP のチェックサムは 0 = 省略）。
- `rtl/hostlink/eth_link.vhd`: frame_mux + udp_stack + rmii_mac（コア側の I/F は serial_link と同じ）。
- ボード層: `primer20k_fprobe_eth_top`、ピンは `boards/primer20k.py` の資源グループ `eth`。
- ホスト: `fprobe.eth_link.EthLink`（SerialLink と同じチャンネルの受け渡し。フレームの読み書きだけ UDP に）、
  `Session.open_eth(host)`、MCP の `--eth`。

## 4. 判断

| 判断 | 理由 | 誤っていた場合のコスト |
|---|---|---|
| UDP（TCP は持たない） | 回路が小さい（ARP・IPv4・UDP の固定ヘッダ）。PC は標準ソケット | 中（遠いネットワーク越しで落ちが多ければ再送層を足す） |
| 欠けは通し番号で検出して報告（再送しない） | 直結 / LAN では落ちはまれ。CSR の読みに副作用があるので盲目的な再送はしない（SerialLink と同じ） | 低 |
| アドレスは静的（DHCP なし） | 直結が主な使い方。DHCP の回路と待ちが要らない | 低（後で DHCP か、PC からの割り当てを足せる） |
| channel の多重化を frame_mux に切り出す | 2 つのリンクで同じものを持たない | 低 |
| v1 は USB と排他 | 同時に 2 本をコアにつなぐ調停は、使い方が決まってから | 低 |

## 5. ピン（Primer 20K Dock）

出典: Dock 3714 の回路図 P005 Ethernet、Sipeed の TangPrimer-20K-example（Ethernet/verilog_UDP の .cst）、
LiteX の Dock 定義（extensions/sipeed.py）。3 つで一致。

| 信号 | ピン | 備考 |
|---|---|---|
| REF_CLK（50 MHz、PHY → FPGA） | A9 | RXD3/CLK_CTL の strap が GND（R63）= PHY の TXC から出力。25 MHz の水晶は PHY 側 |
| CRS_DV | M6 | |
| RXD[0], RXD[1] | F15, C9 | |
| TX_EN | E16 | |
| TXD[0], TXD[1] | D16, E14 | |
| PHY リセット（負論理） | F10 | USB3317 の RESET と共用（primer20k_ulpi_reset と同じパルスを起動時に 1 回） |
| MDC, MDIO | F14, F16 | v1 では使わない（基板のプルアップは未実装。使うなら FPGA の内部プルアップ） |

- PHY アドレス 1（LED0/PHYAD0 = 1、LED1/PHYAD1 = 0）。モード: RMII（strap）。割り込み線なし。
- v1 は PHY の既定の設定（自動ネゴシエーション）で 100 Mb/s 全二重を前提にする。10 Mb/s にはならない前提
  （RMII の 10 Mb/s はジビットを 10 周期ずつ繰り返すので、rmii_rx / rmii_tx が対応していない）。
- RMII のタイミング（RMII 1.2）: PHY の出力は REF_CLK の立ち上がりから 2〜14 ns、PHY の入力はセットアップ 4 ns・
  ホールド 2 ns。SDC の入出力遅延として制約する（`Board.eth_io_delays`）。
