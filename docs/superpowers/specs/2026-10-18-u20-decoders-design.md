# U20: 解読器の追加と積み重ね 設計

日付: 2026-10-18 / 単位 U20（[差分の分析](../../reports/2026-10-18-gap-analysis.md)）/ 前提: [F7 通信の解読](2026-10-17-f7-protocol-decode-design.md)

満たす競合の機能: WIN-06（CAN の解読）、SAL-01（標準の解析器の種類）、SAL-02（上位層の解析器）、SAL-07（解析器を足す口）、
SIG-01（積み重ねるデコーダ）、SIG-04（ファイル形式: sigrok の .sr）、DSV-02（デコーダの数と追加）、WVF-01（パラレルバス・CAN）。

## 構成

```
host/fprobe/decode/
  registry.py   登録簿（Decoder）・積み重ね（decode / decode_on）・利用者のファイルの読み込み（load_file）
  core.py       Annotation / Decoding（+ derive: 同じ時間軸の上の別の結果を作る）
  can.py        CAN / CAN FD           onewire.py  1-Wire        jtag.py   JTAG と ARM JTAG-DP（jtag_arm）
  i2s.py        I2S・左詰め・右詰め    parallel.py パラレルバス  manchester.py  Manchester
  spiflash.py   SPI の上の SPI Flash   eeprom.py   I2C の上の 24 系 EEPROM     modbus.py  UART の上の Modbus RTU
  synth.py      各プロトコルの波形の発生器（試験用）
host/fprobe/sigrok.py      .sr の書き出し・読み込み
host/fprobe/probe_decode.py  la_decoders・la_decoder_load・la_export・la_load（Probe の mixin）
```

## 登録簿と積み重ね

- 解読器は `Decoder(name, function, lower, summary, default_kinds, origin)`。`lower` が空なら `function(tr, **設定)`
  （波形を読む）、空でなければ `function(lower: Decoding, **設定)`（下の解読の結果を読む）。
- 名前で呼ぶ（`decode`、API の `la.decode`）ときは、**下の解読の設定を下の名前のキーに入れる**:
  `decode("modbus", tr, uart={"channel": 5, "baud": 9600, "parity": "even"}, gap_chars=2.5)`。何段でも重ねられる
  （例: 利用者の解読器 → modbus → uart）。結果の `params` にも下の設定が同じキーで入る（再現できる）。
  キーを下の名前にしたのは、1 つの解読器が複数の下を受け付けられ（`lower` は列）、どれを使ったかが設定から明らかなため。
- `la.decode` は下の解読も同じ覚え（キャッシュ）を通す: 上の解読を変えて試すとき、下は再計算しない。覚えの鍵に
  `Decoder` そのものを含めるので、利用者の解読器を読み直すと前の結果は使われない。
- `default_kinds`: `la.decode` で kinds を省いたときの既定（SWD は transfer、JTAG は ir・dr・reset、1-Wire はビット以外、
  Manchester は word・frame）。従来の「SWD だけ特別」をデータにした。
- 利用者の解読器: ファイルに `@decoder("名前", lower=("uart",))` を付けた関数を書き、`load_file` / `la.decoder_load` で
  読む。出どころ（ファイルの絶対パス）ごとに登録を持ち、読み直すとそのファイルの前の登録を置き換える。失敗したら
  登録簿は元のまま。組み込みの名前は上書きできない。バイトコードの写しは使わない（同じ秒の書き換えでも新しい内容）。
  利用者の解読器はデーモンの中で動く Python なので、デーモンの利用者が信頼できる前提（MCP の AI はすでに PC で
  コードを動かせる立場）。

## 各解読器の要点

| 名前 | 主な設定 | 注釈の種類 | 要点 |
|---|---|---|---|
| can | channel・bitrate・data_bitrate・sample_point・data_sample_point・invert | frame・sof・id・control・data・stuff_count・crc・ack/nack・eof・error | サンプル点で読み、次のサンプル点の前の変化で再同期。スタッフの除去と 6 連続の誤り。CRC-15 / FD の CRC-17・21（ISO: 初期値の最上位 1、ダイナミックスタッフを含めて計算、固定スタッフ、スタッフ数のグレイ符号とパリティ）。BRS のサンプル点〜CRC 区切りのサンプル点はデータのビットレート。SOF は劣勢 10.5 ビット以上の後。誤り: stuff・form・crc・stuff_count・ack・data_bitrate・truncated |
| onewire | channel・overdrive | reset・presence・bit・rom_command・rom・data | 0 の長さでビット / リセットを区別。リセット直後の 0 はプレゼンス。ROM コマンド（READ・MATCH・SKIP・SEARCH・ALARM・OD SKIP・OD MATCH・RESUME）、ROM の CRC-8、SEARCH の三つ組から ROM を組み立て、OD コマンドの後はオーバードライブ、標準のリセットで戻る |
| jtag | tck・tms・tdi・tdo・trst・initial_state・ir_names | state・reset・ir・dr | TCK の立ち上がりで TAP を進める。Pause を挟むシフトは Update まで 1 つ。初めの状態が分からなければ TMS = 1 の 5 回で同期。誤り: aborted・truncated |
| jtag_arm（jtag の上） | ir_bits・ir_offset・dr_offset | access・idcode | DPACC / APACC の 35 ビットを要求と応答に。読みの値は次の走査で返る（previous に元の要求）。WAIT は error="wait"。鎖の中の DP の位置を offset で |
| i2s | sck・ws・sd・format・word_bits・signed | left・right | i2s（1 クロック遅れ、WS = 0 が左）・left_justified・right_justified。最初の途中のスロットは読まない |
| parallel | data（列）・clock・edge・enable・enable_level・settle | word | クロックの辺の直前の値、またはクロック無しで値の変化ごと（settle より短い途中の値を捨てる） |
| manchester | channel・bitrate・convention・word_bits・msb_first | bit・word・frame・error | 長い間（1 ビット）の両端は中央の変化。最初の長い間から逆にたどって位相を決める（無ければ最初の変化を中央） |
| spiflash（spi の上） | address_bytes | command | 転送（CS）ごとに命令・アドレス（EN4B / EX4B を追う）・ダミー・データ。WEL を追い、許可の無い書き・消去は error="write_disabled" |
| eeprom24（i2c の上） | device・address_bytes・block_bits・page_size | write・read・poll | ランダム読み（アドレスだけの書き + 読み）をまとめる。内部のアドレスを追って現在のアドレスの読みにも番地。ページをまたぐ書きは error="page_wrap"。NACK は書き込みサイクル中のポーリング |
| modbus（uart の上） | gap_chars | request・response・exception・frame | 無音（既定 2.5 文字: 1.5 と 3.5 の中間）で区切る。CRC-16/MODBUS。要求と応答を形と直前の要求で見分け、応答に latency_s |

## sigrok の .sr

- 書き出し（`write_sr` / `la.export` format="sr"）: zip に `version`（"2"）・`metadata`（`[device 1]` に capturefile=logic-1・
  total probes・samplerate（libsigrok と同じ "100 MHz" の形）・probeN・unitsize）・データ `logic-1-1`, `logic-1-2` …
  （1 ファイル 2^20 サンプル）。変化点の表現から少しずつ展開して書く（長い捕捉もメモリに全部は持たない）。
  channels で書くチャンネルを選べる（probe1 から順に詰める）。名前は `names`、既定の名前は VCD と同じ（ch8 = probe_drives_swdio …）。
- 読み込み（`read_sr` / `la.load`）: 他のロジアナで取った波形を「最後の捕捉」にして、la.decode・la.measure・la.view で扱う。
  単一の `logic-1` の古い形も読む。トリガは先頭。
- PulseView で開けば sigrok の約 100 種のデコーダ（DSV-02）もそのまま使える。

## API

| メソッド | kind | 説明 |
|---|---|---|
| la.decode | read | protocol を enum から文字列に（登録された名前）。積み重ねの設定、既定の種類 |
| la.decoders | read | 解読器の一覧（名前・lower・params・default_kinds・origin） |
| la.decoder_load | control | 利用者の解読器のファイルを読み込む（デーモンの共有の状態を変えるので control） |
| la.export | read | 最後の捕捉を .sr / VCD に書く |
| la.load | control | .sr を最後の捕捉として読む（全員が見る捕捉を置き換えるので control） |

## 試験

- 合成した波形で往復（`host/tests/test_decode_can.py`・`_onewire`・`_jtag`・`_i2s_parallel_manchester`・`_stacked`・
  `_registry`、`test_sigrok.py`、`test_probe_decode.py`）。CRC は公開の検査値（CRC-15/CAN 0x059E、CRC-17/CAN-FD 0x04F03、
  CRC-21/CAN-FD 0x0ED841、CRC-16/MODBUS 0x4B37、1-Wire の AN27 の例 0xA2）で発生器と独立に確かめる。
- 実機: `gateware/tests/hil/test_decoders_hw.py`（JTAG と jtag_arm、USART1 の Modbus、.sr の往復）。

## 限界・今後

- CAN FD の「データの最後で 5 個続いたときのダイナミックスタッフ」は 1 個入れる解釈（ISO の図に従う）。実機の CAN FD での確認は未。
- Manchester で長い間の無いフレーム（同じビットの続き）は、最初の変化を中央とみなす（線の休止の値によっては 1 ビットずれる）。
- I2S の TDM・DSP 形式、LIN、USB の低層、SD カード（SPI の上）、SMBus/PMBus（I2C の上）等は未。登録簿に足すだけで載る。
- PulseView で .sr が開けることは手で確かめる（この PC に sigrok が無い）。
