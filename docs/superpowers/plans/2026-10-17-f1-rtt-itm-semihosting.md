# F1（担当 D）: ITM/DWT の解読・RTT・セミホスティング — 実装計画

仕様: [2026-10-17-f1-rtt-itm-semihosting-design.md](../specs/2026-10-17-f1-rtt-itm-semihosting-design.md)

試験: `cd host && ../.venv/Scripts/python -m pytest -q -p no:cacheprovider`（worktree では `PYTHONPATH=.` を付けて
worktree の `fprobe` を読む）。実機・FPGA のビルドには触れない。`fprobe_api.json`・`mcp_server.py`・`api/`・`ui/`・
`symbols.py`・`debug.py`・`la.py` は変えない。

## 段 1: ITM / DWT（`host/fprobe/itm.py`、`host/tests/test_itm.py`）

1. 試験を先に書く: 各パケットの種類（ソフトウェア 1/2/4、DWT の各識別子、ローカル・グローバルのタイムスタンプ、
   拡張、オーバーフロー、同期）、1 バイトずつ与えても同じ、途中から始まる・予約ヘッダからの回復。
2. `ItmDecoder`（待ち行列から完全なパケットを 1 つずつ取り出す。同期を失ったら同期パケットを探す）。
3. `Timestamper`（ローカルの差分の累積、グローバル 1/2 の組み合わせ）、`PortText`（ポートごとの行）。
4. commit。

## 段 2: RTT（`host/fprobe/rtt.py`、`host/tests/test_rtt.py`）

1. 試験: 偽のメモリ（読み書きを記録）と、ターゲットの模擬（3 つのモードで上りに書く、下りから読む）。
   - 開く（名前・番地・大きさ）、読み出し・折り返し・`RdOff` の更新、普通の場合の読み 1 回 + 書き 1 回、
     リングが記述子より前にあるときはまとめない、下りの書き込み（空き・折り返し・`WrOff` はデータの後）、
     壊れた値で `RttError`、走査（境目、偽の ID を読み飛ばす）。
2. 純粋関数 `ring_used` / `ring_free` / `ring_spans`、`find_control_block`、`Rtt`。
3. commit。

## 段 3: セミホスティング（`host/fprobe/semihosting.py`、`host/tests/test_semihosting.py`）

1. 試験: 偽のターゲット（レジスタ辞書 + メモリ）で各操作。BKPT でない停止は扱わない。ファイルの既定の拒否、
   `root` の下の許可、絶対パス・`..`・UNC・書き込みの拒否。終了（SYS_EXIT / SYS_EXIT_EXTENDED）。機能のファイル。
2. `SemihostingHost`、`FileAccess`、`service()`。
3. commit。

## 段 4: 試験用ファーム（`host/firmware/rtt_demo/`）

1. `rtt_demo.c`（自作の RTT・ITM の printf・TPIU/SWO の設定・SysTick・セミホスティング・下りの echo）、`link.ld`、`build.py`。
2. zig cc でビルドし、`rtt_demo.elf` を置く。
3. 試験: `_SEGGER_RTT` がシンボルから引ける、ELF の中身を偽のメモリに置いて走査・解析（チャンネル名・大きさ・モード）、
   ID の並びが 1 か所だけ、`semihosting_enabled` 等の変数がある。
4. commit。

## 段 5: 仕上げ

全試験（`pytest host`）を通し、仕様書の「組み込みの提案」を実装に合わせて見直す。
