# F4: 変数のライブ観測（FPGA の周期読み出し「DAP サンプラ」）設計

全体計画: [2026-10-17-feature-expansion-roadmap.md](2026-10-17-feature-expansion-roadmap.md)。記録: [F3](2026-10-17-f3-timeline-recording-design.md)。

## 1. ゴール

ターゲットを止めずに、変数（メモリの語）を **FPGA が自分で周期的に読み、読んだ瞬間のデバイスの時刻を付けて** イベント
記録に入れる。J-Scope（HSS）・Keil の Logic Analyzer・STM32CubeMonitor を上回る点:
- 時刻がデバイスのタイムベース（ホストの OS の揺れが無い。CubeMonitor の弱点）で、波形・DAP の転送・UART と同じ時間軸。
- ターゲットへのコードの組み込みが要らない（SWD でメモリを読むだけ）。
- ホスト（pyOCD 等）のデバッグと同時に使える（DAP の命令の境界で線を借り、ホストのキャッシュを戻す。クロストリガの
  割り込み書き込み（`swd_injector`）と同じ方式）。

| 完了条件 | 検証 |
|---|---|
| 最大 8 か所（各 1〜16 語の連続）を、指定した周期で読み、1 語ごとに時刻付きのイベント（種類 7）にする | cocotb（偽の SWD のターゲット）+ HIL |
| ホストの DAP の命令の途中には割り込まず、ホストがキャッシュする SELECT / CSW / TAR を戻す（ホストから見えない） | cocotb + HIL（pyOCD のデバッグと同時に） |
| WAIT は繰り返し、FAULT 等は中止して数え、ホストの次の転送を壊さない | cocotb |
| 周期の取りこぼし（前の読みが終わる前に次の周期）を数える | cocotb |
| ホスト: 変数名・式（F2 の DWARF）・番地で観測を設定し、記録（stream `sample`）と最新値で読める。API・MCP | 単体 + HIL |
| 実機: sram_demo の `counter` を 10 kHz で読み、値が単調に増え、時刻の間隔が周期どおり（ジッタはタイムベースの数周期以内） | HIL |

非目標: 書き込みの周期実行（後で）、条件付きの読み（クロストリガの事象で 1 回読む: 後で同じ部品に足す）。

## 2. 構成（ゲートウェア）

- 新しい部品 `dap_sampler`（`rtl/debug/`）。`swd_injector` と `swd_phy` の間に入るアービタ（直列。上流が使っていないときに
  線を借りる。上流の busy = `dap_busy or inj_busy`）。
- 1 回の読み（1 か所）の転送の並び（控え shadow_valid があるとき）:
  `SELECT ← ap（bank 0）` / `CSW ← 32 ビット・自動増加 single` / `TAR ← addr` / `DRW 読み × n`（ポスト。語 i の値は i+1 回目の
  DRW 読み、最後は `RDBUFF` で返る）/ `CSW ← 控え` / `TAR ← 控え` / `SELECT ← 控え`。
  控えが無いときは、`swd_injector` と同じく CSW・TAR を先に読んで保存する。
- 各語の値が返った転送の周期の時刻で、イベント（種類 7: [47:0] 時刻、[79:48] 番地、[111:80] 値、[114:112] ACK）を出す。
  イベントの調停（fprobe_core）に源を 1 つ足す（DAP・割り込みの次、バイトの前。1 件保持、間に合わなければ lost）。
- 周期: `period`（sys のクロック数）ごとに、有効なか所を順に読む。前の巡回が終わる前に次の周期が来たら `overruns` に数える。
- `enable = '0'`（SWD 未接続）・リセットで止まる。
- ケイパビリティの FEATURES に `SAMPLER`（bit 3）。

## 3. CSR（新しい窓 `sampler`、0x0C00）

| レジスタ | 内容 |
|---|---|
| `control` | bit0 run、bits[15:8] ap |
| `period` | 巡回の周期（sys のクロック数、0 は止める） |
| `slot_index` | 書くか所（0〜7） |
| `slot_addr` | 書くか所の番地（語の境界） |
| `slot_config` | bit0 有効、bits[11:8] 語数 − 1（1〜16 語） |
| `slot_write` | 1 で slot_addr・slot_config を か所 slot_index に書く（PULSE） |
| `rounds` | 巡回の回数 |
| `errors` | 中止した読みの数（FAULT・プロトコル異常・パリティ） |
| `overruns` | 間に合わなかった周期の数 |
| `last_ack` | 最後の ACK |

か所の表は分散 RAM に置き、1 か所ずつ書く（読み返せない）。巡回は か所を 1 つずつ読むので、全か所を FF で
並べる必要は無い。8 か所を FF で持つ版は Primer で論理 90%・配線が通らなかった（2026-10-17）。

## 4. ホスト

- `fprobe.sampler.Sampler(device)`: `configure(slots=[(address, words)], period_s, ap=0)`、`start()`、`stop()`、`status()`。
  周期の上限・下限（SWD の速さと語数から、1 巡回の時間を見積もって警告）。
- `fprobe.events`: 種類 7 を `Access(kind="sample", address, value)` に。`collector` は stream `sample`（a = 番地、b = 値）へ。
- `Probe` / API・MCP:
  - `watch.set`（観測する変数: 名前・式・"0x..."、rate_hz）。名前・式は F2 の DWARF で番地と大きさに直し、語の範囲に
    まとめる（構造体のメンバーも可）。値の解読（型）は読み出し側で行う。
  - `watch.stop`、`watch.status`、`watch.values`（各変数の最新値と時刻）、`watch.series`（イベント記録の最新の分から、
    変数 1 つの時系列）。長い時間は記録の `recording_find`（stream `sample`）。
  - 観測中は記録（F3）に `sample` が入るので、波形・DAP・UART と同じ時間軸で見られる。

## 5. 判断

| 判断 | 理由 | 誤っていた場合のコスト |
|---|---|---|
| `swd_injector` と同じく、DAP の命令の境界で線を借りてホストのキャッシュを戻す | 実機で実績がある方式。pyOCD 等から見えない | 低 |
| 新しい部品にし、`swd_injector` は変えない | 動いているクロストリガの時間的な性質を変えない。共通の「線を借りる」部分は後で 1 つにまとめられる（2 つの使い手が揃ってから） | 低（重複する状態遷移が 2 つある間の保守） |
| 時刻は値が返った転送の周期 | 「その値を読んだ瞬間」に最も近い | 低 |

## 6. 試験

- cocotb: 偽の SWD のターゲット（既存の tb の部品）で、控えあり・なしの転送の並び、語数 1 / 16、WAIT の繰り返し、FAULT の中止、
  ホストの命令との交互（境界でしか借りない）、周期の取りこぼし、イベントの内容（番地・値・時刻の順）。
- 単体（ホスト）: 設定の符号化、種類 7 の解読、変数 → 語の範囲、`watch.series`。
- HIL: sram_demo の `counter` を 10 kHz で、pyOCD のデバッグ（レジスタ・メモリの読み）と同時に。値の単調増加、時刻の間隔、
  ホストの読みが壊れないこと。
