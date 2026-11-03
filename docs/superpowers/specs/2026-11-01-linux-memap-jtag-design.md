# Linux 認識の「止めずに読む口」を fprobe の JTAG で開く（memap の JTAG 版）

## 1. なぜ

道 B の止めずに読む口（memap）は、今は Vivado の xsdb（hw_server）を子プロセスで使い、PYNQ の USB の JTAG から DAP の AHB-AP で物理メモリを読む
（`host/fprobe/linux/memap.py`、`XsdbMemAp`）。この道は、(1) PC に Vivado と PYNQ の USB ケーブルが要る、(2) PYNQ が independent の起動のとき使えない
（基板の USB の JTAG から DAP が見えなくなる）、(3) fprobe の「共通タイムスタンプ・クロストリガ」の外にある。

2026-10-31 に、PYNQ の PS の DAP を PJTAG（EMIO → RPi ヘッダ）で Primer の fprobe の JTAG につなぎ、**AHB-AP でシステムのメモリが止めずに読める**ことを
実機で確かめた（`2026-10-23-zynq-pjtag-design.md` の末尾）。`XsdbMemAp` のコメントにも「fprobe の FPGA の JTAG で読む口は別に作る」とある。これを作る。

## 2. ゴール / 非ゴール

- ゴール: `linux.memap.connect_jtag` で、fprobe の JTAG の口（Primer の J12）につないだ DAP の MEM-AP を、`read_words(address, count)` の口として開く。
  開いた口は `linux.ring.*` の `access=memap` でそのまま使える（読み手の口は `XsdbMemAp` と同じ: `info`・`read_words`・`close`）。
- 非ゴール: コアを止める道（APB-AP が見えないので未達）。書き込み。PYNQ 以外のターゲットの自動判定（接続する側が IDCODE と AP の番号を明示する）。

## 3. 形

- 引数はすべて使う側が明示する（既定の暗黙の値を持たない）: `target`（`linux.connect` の名前）、`chain_idcode`（チェーンにあるはずの DP の IDCODE。違うターゲットを読まない。
  Zynq-7000 は 0x4BA00477）、`ap`（MEM-AP の番号。Zynq の AHB-AP は 0）、`frequency_hz`。
- 手順: JTAG のチェーンを走査 → IDCODE が一致する装置を探す（無ければ、見えた装置を並べて誤りにする）→ `RawDap.start("jtag", …)` → `read_dpidr`（JTAG の初期化に要る）→
  `power_up`（デバッグ電源）→ AP の IDR を読み、MEM-AP でなければ誤り（IDR と見えた AP を並べる）。
- `read_words`: 物理番地から 32 ビット語を count 個。`RawDap.mem_read32_many` で 64 語ずつの 1 往復。誤り（FAULT・応答なし）は黙って 0 にせず、事実で断る。
- `info`: `via`（"probe_jtag"）・`chain_idcode`・`ap`・`ap_idr`・`frequency_hz`・`device_index`。
- 開いている間は、プローブの生の DAP（`RawDap`）をこの口が使う。ほかの低レベル操作（`jtag.scan`・`jtag.shift` 等）とは同時に使えない（`linux.memap.disconnect` で解放）。
  `target.connect`（pyOCD・Cortex-A）とも同時に使わない（ターゲットに接続中は断る）。

## 4. 確かめ方

- ホスト（偽の `RawDap`）: 64 語ごとの分割と順序、IDCODE 不一致の誤り、MEM-AP でない AP の誤り、読みの誤りが黙って通らないこと、`close` 後は使えないこと。
- 実機（`gateware/tests/hil/test_linux_memap_jtag_hw.py`。`FPROBE_ZYNQ_JTAG=1` で走る）: PYNQ の independent 起動 + Primer の `fprobe_jtag`。
  BOOT_MODE（0xF800025C）= 0xD と DEVCFG_CTRL（0xF8007000）の DAP_EN[2:0] = 7 が JTAG 越しに読め、PYNQ の SSH で読んだ値と一致する（2026-10-31 に手で確認済みの値）。
  リングの読み（`access=memap`）は PYNQ の `linux.agent` を動かして確かめる（次の段）。

## 5. 決めてほしいこと

1. API の名前（`linux.memap.connect_jtag`）と、`XsdbMemAp` の経路を残すか（残す案。xsdb は Vivado がある環境の選択肢）。
2. 生の DAP を占有する制約（3 章）でよいか。デーモンの別の接続が生の DAP を使うと断られる。
