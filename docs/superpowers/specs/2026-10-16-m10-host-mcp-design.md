# M10: ホストライブラリ・CLI・MCP サーバ 設計

- 日付: 2026-10-16
- 親: `2026-10-15-fprobe-architecture-design.md` §9

## 1. ゴールと完了条件

| 完了条件 | 測り方 |
|---|---|
| 1 つのプロセスで pyOCD（デバッグ）と fprobe 拡張（ロジアナ・CSR）を同時に使える | HIL: `test_la_hw`（pyOCD の読み出し中の SWD を同じプロセスのロジアナで捕捉） |
| AI が MCP 経由で、接続・メモリ R/W・レジスタ・ステップ・ロジアナ捕捉・再開の一連の作業を完遂できる | HIL: `test_mcp_hw`（stdio でサーバを起動し MCP クライアントから呼ぶ） |
| CLI でケイパビリティ表示とロジアナ捕捉（VCD）ができる | 手動 |

## 2. 構成

- `fprobe.transport.find_device(serial)`: 機器の探索。`UsbTransport` は渡された `usb.core.Device` の拡張 I/F だけを使い、
  自分で開いた機器だけを後始末する。
- `fprobe.session.Session`: 機器を 1 度だけ開き、拡張 I/F を `Device` に、CMSIS-DAP v2 I/F を pyOCD に渡す
  （`PyUSBv2` の共有版。探索と dispose をしない）。`debug(**options)` が pyOCD の `Session` を返す。
- `fprobe.mcp_server`: 道具の実体は `Probe` クラス（MCP 非依存）。`build_server` がそのメソッドを MCP の道具として登録する。
  ロジアナは `la_arm` → ターゲット操作 → `la_collect` で、デバッグ操作と同時の捕捉ができる。結果はチャンネルごとの
  要約（初期値・エッジ数・トリガ基準のエッジ時刻）で返し、波形全体は VCD に書く。
- `fprobe.__main__`: CLI（`info`、`la`）。

## 3. 判断

| 判断 | 理由 | 誤っていた場合のコスト |
|---|---|---|
| pyOCD の USB バックエンドを継承して共有する（別プロセス・別ハンドルにしない） | Windows の libusb（WinUSB）は機器の全 I/F を 1 ハンドルで握る。共有が唯一の方法 | 中: pyOCD の内部に依存。版を上げたら HIL で確認 |
| MCP の道具は粒度の細かい操作にし、ロジアナの結果は要約で返す | AI は 8192 サンプルの生データを読めない。要約で判断し、必要なら VCD を別ツールで見る | 低 |
| UART は CDC-ACM の COM を pyserial で開く（同じ USB ハンドルを使わない） | CDC は OS のドライバ（usbser）が持つので libusb の排他と衝突しない | 低 |
| イベント（events_query）・クロストリガ（cross_trigger_set）の道具は M9 で足す | ゲートウェア側が未実装 | 低 |
