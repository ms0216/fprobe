# USB 2.0 HS デバイス on ULPI（USB3317）— 実装のための技術メモ

M2（VHDL 自作 USB スタック）の設計根拠。2026-10-15 にサブエージェントが LUNA 0.2.3 のソース、
Sipeed 公式例（Primer 20K で動作実績あり）、USB3317 Rev C データシート（Microchip, DS Rev 2.1）を読んで作成し、
本書に整理した。出典表記: **[DS p.N]** データシート、**[LUNA]** LUNA の実装、**[spec]** USB 2.0 / ULPI 1.1 /
CDC 1.2 / MS OS 2.0 の知識（原典との照合は未実施、実装時に要確認）、**[calc]** スクリプトで計算・既知ベクタで確認。
ULPI クロック 60 MHz の 1 サイクル = 16.67 ns = HS 8 ビット時間。

## 1. ULPI リンク層

| 信号 | 向き | 備考 |
|---|---|---|
| CLK | PHY→link | 60 MHz |
| DIR | PHY→link | 1: PHY が DATA を駆動 / 0: link が駆動 |
| NXT | PHY→link | スロットル・バイト受理 |
| STP | link→PHY | |
| DATA[7:0] | 双方向 | link 側の出力イネーブル = not DIR（組み合わせ） |
| RESETB | link→PHY | アクティブロー。常に駆動すること（内部プルダウンなし）[DS p.11] |

I/O タイミング [DS p.15 Table 4.3]: PHY 入力（STP, DATA）セットアップ 5.0 ns / ホールド 0 ns、PHY 出力は CLK 立ち上がり後 1.5〜3.5 ns。

- **バスの向き切り替え**: DIR が変わるたびに 1 サイクルのターンアラウンド（誰も駆動しない）[DS p.36]。
  - DIR 立ち上がり: その周期で link は DATA を離す。最初の DIR=1 サイクルは無効データ。
    RX CMD とみなすのは「DIR が 2 サイクル以上連続して 1、かつ NXT=0」[LUNA]。
  - DIR 立ち下がり: ターンアラウンド後、link は **0x00（Idle）を必ず駆動**する。前の値が残ると TX CMD と誤解される [DS p.47]。
- **RX CMD**（DIR=1, NXT=0）[DS p.41]: [1:0] LineState（[0]=D+, [1]=D−。SE0=00, FS J=01, FS K=10。HS では 00=squelch）、
  [3:2] VBUS（00=SessEnd 未満 … 11=VbusValid）、[5:4] RxEvent（00 idle, 01 RxActive, 11 RxActive+RxError, 10 HostDisconnect）、[6] ID、[7] alt_int。
- DIR=1 かつ NXT=1 は受信データバイト。**DIR と NXT が同時に立つ = RxActive 開始**（RX CMD は来ない。そのサイクルはターンアラウンド）[DS p.44]。
  RxActive の解除は DIR 低下または RX CMD の bit4 の 1→0。
- **レジスタ読み出し中は RX CMD 解読を止める**（読み値も DIR=1, NXT=0 で来るため）。
- **TX CMD** [DS p.37]: 0x00 Idle、0x40 NOPID 送信、0x40|PID 送信、0x80|addr レジスタ書き込み、0xC0|addr 読み出し（addr=0x2F は拡張アドレス）。
- **パケット送信**: DIR=0 かつバス空きで 0x40|PID を駆動し NXT=1 まで保持 → 以後 NXT=1 の周期ごとに次のバイト（NXT=0 なら保持）→
  最後のバイトが受理された次の周期に STP=1 を 1 周期（DATA=0x00）。データ待ち中（NXT=0）に STP を出してはならない [DS p.43]。
  ハンドシェイクは TX CMD → NXT → STP だけ。中断は STP と同時に DATA=0xFF。
- **送信中に DIR が立った場合** [spec]: TX CMD 受理前なら未受理として DIR 低下後に再試行。パケット途中なら PHY が中断したとみなす（LUNA は未対応）。
- **レジスタ書き込み** [DS p.37-38]: T0 0x80|addr → NXT → データ → NXT で受理 → 次の周期 STP=1・DATA=0x00 → STP を下げる。
  途中で DIR が立ったら最初からやり直す。
- **レジスタ読み出し** [DS p.39]: T0 0xC0|addr → NXT → 次の周期 DIR=1・NXT=0（ターンアラウンド）→ 次の周期にデータ → DIR 低下（ターンアラウンド）→ Idle 駆動。
  [spec] DIR が NXT=1 と同時に立った場合は受信が割り込んだので破棄して再試行（LUNA は未対応）。
- **起動** [DS p.25-26, 46-47]: RESETB を 1 µs 以上 low でリセット。解除後 PHY は PLL ロックまで **DIR=1 を保持**（T_START 1.03〜2.28 ms）。
  STP が high の間は DIR=1 を保つので、link は「STP=1 で待機 → 解放 → DIR が数サイクル low を確認 → VID 読み出し成功」で準備完了とする。
  LUNA の 1 ms 固定待ちは T_START 最大値より短い。

### Primer 20K 固有（Sipeed 例より）
- RESETB は `F10`。**RTL8201F（Ethernet PHY）のリセットと共用**。Sipeed 例は常時 high（リセットしない）。
- Sipeed 例は ULPI クロックを**反転**して全回路を動かし、出力は組み合わせで出す。SDC に入出力遅延の制約は無い。
  入出力遅延（上記 Table 4.3）を制約して STA すると、立ち下がりから出した出力はクロック分配（〜5.8 ns）と
  論理・出力バッファを経て PHY の次の立ち上がりをまたぎ（11〜19 ns、遅いコーナー）、どの周期に取り込まれるかが
  遅延次第になる。**本プロジェクトは反転しない**: 入力は立ち下がりで取り込み（前後に約 8 ns の余裕）、
  出力は立ち上がりのレジスタから出す（PHY の次の立ち上がりのセットアップに収まる）。OE だけは not DIR の組み合わせ。
- ピン: stp K11, data[7:0] = R12 P13 R13 T14 H13 J12 H12 G11, nxt K13, dir K12, clk T15, rst F10。LVCMOS33, PULL NONE, 出力 DRIVE=8。

## 2. USB3317 レジスタ [DS p.52-58]

| レジスタ | 読み | 書き | セット | クリア | 初期値 |
|---|---|---|---|---|---|
| Vendor ID L/H | 0x00/0x01 | – | – | – | **0x24/0x04** |
| Product ID L/H | 0x02/0x03 | – | – | – | **0x06/0x00** |
| Function Control | 0x04 | 0x04 | 0x05 | 0x06 | 0x41 |
| Interface Control | 0x07 | 0x07 | 0x08 | 0x09 | 0x00 |
| OTG Control | 0x0A | 0x0A | 0x0B | 0x0C | 0x06 |
| Debug | 0x15 | – | – | – | [1:0]=LineState |
| Scratch | 0x16 | 0x16 | 0x17 | 0x18 | 0x00 |

- Function Control: [1:0] XcvrSelect（00 HS, 01 FS, 10 LS）、[2] TermSelect、[4:3] OpMode（00 通常, 01 非駆動, 10 NRZI・ビットスタッフ無効=チャープ用）、
  [5] Reset（自己クリア）、[6] SuspendM（アクティブロー、通常 1）。
- OTG Control: デバイスは DpPulldown/DmPulldown を外す → **0x00**。
- デバイスの終端モード: **FS 0x45**（Xcvr 01, Term 1, Op 00）、**チャープ 0x54**（00, 1, 10）、**HS 0x40**（00, 0, 00）。
- 自己診断: Scratch に書いて読み返す、VID/PID が 24 04 06 00。

## 3. バスリセット検出と HS ネゴシエーション（チャープ）

60 MHz での時間: 2.5 µs=150, 5 µs=300, 200 µs=12 000, 1 ms=60 000, 2 ms=120 000, 2.5 ms=150 000, 3 ms=180 000 サイクル [LUNA]。

| 状態 | 動作 |
|---|---|
| FS 待機（0x45, プルアップ ON） | SE0 が 5 µs 連続 → bus_reset（アドレス・コンフィグ解除）→ チャープ開始。FS J が 3 ms → サスペンド。VBUS 無し → リセット保持 |
| チャープ準備 | 0x54 を書く（TermSelect=1 のまま。外すとホストが切断とみなす）。書き込み完了を待つ |
| デバイスチャープ | NOPID 0x40 + 0x00 を連続送信（チャープ K）を **2 ms**。STP + 0xFF で終了 [spec] 1.0 ms 以上、リセット開始から 7 ms 以内に終える |
| ホストチャープ待ち | K（LineState 10）・J（01）がそれぞれ **2.5 µs 連続**して初めて有効。**K-J を 3 組**で HS 確定。2.5 ms 以内に揃わなければ FS |
| HS 確定 | 0x40 を書く → HS 動作 |
| HS 動作中 | 非 00 の LineState でタイマ解除（SOF が 125 µs ごと）。**squelch 3 ms** → FS（0x45）に戻して 200 µs 後に LineState を見る: J ならサスペンド、それ以外はリセット → チャープへ |
| サスペンド | K → レジューム（元が HS なら HS へ）。SE0 2.5 µs → リセット |

## 4. パケット層

- PID: OUT 0x1, IN 0x9, SOF 0x5, SETUP 0xD, DATA0 0x3, DATA1 0xB, DATA2 0x7, MDATA 0xF, ACK 0x2, NAK 0xA, STALL 0xE, NYET 0x6, PING 0x4。
  線上のバイト = `(~PID & 0xF) << 4 | PID`（SETUP 0x2D, IN 0x69, OUT 0xE1, SOF 0xA5, PING 0xB4, DATA0 0xC3, DATA1 0x4B, ACK 0xD2, NAK 0x5A, STALL 0x1E, NYET 0x96）。
  受信時は上位ニブル = 下位ニブルの反転を必ず確認し、不一致は無視。
- トークン: PID, `ADDR[6:0] | ENDP[0]<<7`, `ENDP[3:1] | CRC5<<3`。CRC5: 多項式 x⁵+x²+1、初期値 0x1F、11 ビットを LSB から、結果を反転して MSB 側から送る。
  受信時は 16 ビット全体を通した剰余 **0b01100** [calc]。テストベクタ: `2D 00 10`（SETUP addr0 ep0）、`69 15 EF`（IN addr 0x15 ep 0xE）、`69 81 58`、`A5 10 2F`（SOF 0x710）。
  4 バイト以上のトークンは無視。CRC 不一致も無視。
- SOF はアドレス照合しない。HS では 1 フレームに同じ番号の SOF が 8 回。
- データ: PID, ペイロード 0〜MPS, CRC16（下位バイトから）。CRC16 = CRC-16/USB: 多項式 0x8005（反射形 0xA001）、初期値 0xFFFF、LSB first、最終 XOR 0xFFFF。
  "123456789" → 0xB4C8、空 → 0x0000（ZLP は `C3 00 00` / `4B 00 00`）、`01 02 03 04` → CRC バイト `5E D4` [calc]。
  受信検査: 反射レジスタ（最終 XOR なし）にペイロード + CRC 2 バイトを通すと **0xB001**（仕様の 0x800D のビット反転）。
  **PID は CRC に含めない**。受信ペイロードは CRC 判定前に流れるので、受け手は**確定（commit）/破棄（discard）**できる必要がある。
- HS の MPS: EP0 64、バルク 512。

## 5. トランザクション層

- アドレス不一致のトークンは無視し、続くデータにも反応しない。リセット後のアドレスは 0。
- **応答時間**: HS ではパケット終了から 192 ビット時間（24 サイクル）以内に応答を開始。最短 1 サイクル待つ。
  ホストの応答待ちタイムアウトは 736〜816 ビット時間（92 サイクル〜）。PHY・リンクの遅延もこの予算から引かれるので、判断経路は数サイクルに収める。
- **SETUP**: DATA0・8 バイトで CRC 正常なら**必ず ACK**（NAK/STALL してはならない）。進行中の制御転送を中断する。
- **バルク OUT**: トグル一致・空きあり → 受理・ACK・トグル反転。**トグル不一致 → ACK するがデータは捨てる**（ホストが前回の ACK を取りこぼした）。
  空き無し → NAK・破棄。CRC 異常 → 無応答。
- **PING**（HS）: MPS 分の空きがあれば ACK、なければ NAK。[spec] OUT 受理後に次の MPS 分の空きが無ければ ACK の代わりに **NYET**（LUNA は未実装）。
- **IN**: データ無し → NAK。あり → DATAx を送り ACK を待つ。**トグル反転は ACK を受けたときだけ**。ACK 無しで次のトークンが来たら同じデータ・同じ PID で再送。
  転送がちょうど MPS の倍数で終わったら ZLP を付ける。
- **データトグル** [spec]: SETUP は DATA0、データ段は DATA1 から交互、ステータス段は DATA1。
  バルク/インタラプトは SET_CONFIGURATION、SET_INTERFACE、CLEAR_FEATURE(ENDPOINT_HALT) で DATA0 に戻す（LUNA は最後のみ）。
- **SET_ADDRESS** は**ステータス段の ZLP にホストが ACK した後**に適用。2 ms 以内に新アドレスで応答できること。

## 6. 制御転送

- SETUP 8 バイト（リトルエンディアン）: bmRequestType（[7] 方向 1=IN, [6:5] 種別 0 標準/1 クラス/2 ベンダー, [4:0] 宛先 0 デバイス/1 IF/2 EP）、bRequest、wValue、wIndex、wLength。
- 状態遷移: SETUP → wLength≠0 なら DATA_IN / DATA_OUT、0 なら STATUS_IN。DATA_IN 中の OUT/PING → STATUS_OUT。DATA_OUT 中の IN → STATUS_IN。
  STATUS_IN は DATA1 の ZLP（エラーなら STALL）、STATUS_OUT はホストの ZLP に ACK。未対応要求は最初のデータ／ステータス機会で STALL（次の SETUP で解除）。
- 標準要求: GET_STATUS 0, CLEAR_FEATURE 1, SET_FEATURE 3, SET_ADDRESS 5, GET_DESCRIPTOR 6, GET_CONFIGURATION 8, SET_CONFIGURATION 9, GET_INTERFACE 0x0A, SET_INTERFACE 0x0B。
  ディスクリプタ種別: DEVICE 1, CONFIG 2, STRING 3, INTERFACE 4, ENDPOINT 5, DEVICE_QUALIFIER 6, OTHER_SPEED_CONFIG 7, IAD 0x0B, BOS 0x0F, DEVICE_CAPABILITY 0x10。
  - GET_STATUS: EP 宛は bit0 に HALT、デバイス宛は bit0 自己給電・bit1 リモートウェイクアップ（LUNA は常に 0）。
  - GET/SET_INTERFACE を実装する（GET は 1 バイト 0、SET alt 0 は ACK + トグル初期化。LUNA は STALL）。
  - HS 対応デバイスは DEVICE_QUALIFIER（10 バイト）と OTHER_SPEED_CONFIGURATION に応答する。
  - String 0 は `04 03 09 04`、他は UTF-16LE（bLength = 2 + 2n）。
- 長いディスクリプタ: 1 パケット = min(wLength − 送信済み, MPS)。送信位置は**ホストの ACK でのみ進める**（ACK を落とされたら同じ塊を再送）。
  ディスクリプタが wLength より長ければちょうど wLength で終了、短くてかつ長さが MPS の倍数なら ZLP を付ける。

## 7. Windows のドライバ不要化（MS OS 2.0）[spec]

- デバイスディスクリプタの **bcdUSB を 0x0210** にする（0x0201 未満だと BOS を取りに来ない）。
- BOS: `05 0F wTotalLength bNumDeviceCaps`。プラットフォーム能力（28 バイト）: `1C 10 05 00` + UUID {D8DD60DF-4589-4CC7-9CD2-659D9E648A9F}
  = `DF 60 DD D8 89 45 C7 4C 9C D2 65 9D 9E 64 8A 9F` + `00 00 03 06`（Windows 8.1）+ 記述子セット長(2) + ベンダーコード(1) + `00`。
- 要求: bmRequestType **0xC0**, bRequest = ベンダーコード, wValue 0, **wIndex 0x0007**。GET_DESCRIPTOR と同じく複数パケット・wLength 尊重・ZLP 規則。
- 記述子セット（複合デバイス）: セットヘッダ `0A 00 00 00 00 00 03 06 wTotalLength` →
  構成サブセット `08 00 01 00 00 00 wSubsetLength`（構成インデックス 0）→ WinUSB にするインターフェースごとに
  機能サブセット `08 00 02 00 bFirstInterface 00 wSubsetLength` + 互換 ID `14 00 03 00 "WINUSB\0\0" + 8×00` +
  レジストリ属性（wLength 0x0084, 型 `04 00`, `07 00`=REG_MULTI_SZ, 名前長 `2A 00` "DeviceInterfaceGUIDs\0"(UTF-16LE), データ長 `50 00` "{GUID}\0\0"(UTF-16LE)）。
  CDC インターフェースには機能サブセットを付けない（usbser.sys が付く）。
- Windows は MS OS 記述子を VID/PID/bcdDevice ごとにキャッシュする（`HKLM\SYSTEM\CurrentControlSet\Control\usbflags\VVVVPPPPRRRR`）。変更したら bcdDevice を上げる。
- **CMSIS-DAP v2**: ベンダー IF（クラス 0xFF）の iInterface 文字列に "CMSIS-DAP" を含める。エンドポイント順は**バルク OUT、バルク IN、（任意）SWO 用バルク IN**。
  HS MPS 512。DeviceInterfaceGUID は慣例的に {CDB3B5AD-293B-4663-AA36-1AAE46463776}。製品文字列にも "CMSIS-DAP" を入れる。

## 8. CDC-ACM

- デバイスクラス EF/02/01（IAD 使用時）。IAD `08 0B first 02 02 02 01 iFunction`。
- 通信 IF（02/02/01）: Header `05 24 00 10 01`、Call Management `05 24 01 00 <data_if>`、ACM `04 24 02 02`、Union `05 24 06 <comm_if> <data_if>`、
  通知用インタラプト IN（MPS 8〜16）。データ IF（0x0A）: バルク IN/OUT 512。
- クラス要求: SET_LINE_CODING（0x21/0x20, 7 バイト: dwDTERate LE4, bCharFormat, bParityType, bDataBits）、GET_LINE_CODING（0xA1/0x21）、
  SET_CONTROL_LINE_STATE（0x21/0x22, wValue bit0 DTR, bit1 RTS）、SEND_BREAK（0x21/0x23）。

## 9. 新規実装が陥りやすい落とし穴

1. DIR 立ち上がりの周期はターンアラウンド。RX CMD は DIR 2 周期以上 + NXT=0。DIR と NXT 同時立ち上がりは RxActive 開始。
2. レジスタ読み出し中は RX CMD 解読を止める。DIR に割り込まれたレジスタ操作は再試行。
3. 送信を優先し排他にする。リンクがバスを持つ間は常に 0x00 を駆動。
4. PHY リセット後は T_START（最大 2.28 ms）を DIR low で確認して待つ。
5. チャープ中は TermSelect=1、書き込み完了後にチャープ開始、NOPID + 0x00、終了は STP + 0xFF、K/J は 2.5 µs 連続で判定。
6. HS のアイドルとリセットは同じに見える → 3 ms で FS に戻し 200 µs 後に判定。
7. PID は CRC に含めない。送信時は CRC 上位バイトを 1 バイト目送出前にラッチ。
8. 受信は 2 バイト遅延 + CRC 異常時のロールバック。壊れたパケットには決して応答しない。
9. トグル不一致の OUT は ACK して捨てる。IN のトグルは ACK でのみ反転、ACK 無しなら同じ内容を再送。
10. MPS 境界で終わる転送には ZLP。SET_ADDRESS はステータス ACK 後に適用。SETUP は常に ACK して制御状態を初期化。
11. 送信中は PID を固定（パケット途中で変えない）。
