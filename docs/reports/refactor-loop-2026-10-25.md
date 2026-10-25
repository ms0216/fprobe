# リファクタリングのループ（2026-10-25 17:29〜21:29）の効果

基準 = ループ開始時のコミット `2a0f24db`（ブランチ fix/fpga-obs-host）。「後」はループの最後に同じ方法で測って埋める。

## 方針
- 主ターゲットは PYNQ-Z2。面積削減と Primer への導入は目標にしない（後で Primer へ移すときの分割は別に考える。PYNQ 開発では、コアは 1 つ・機能は generic とケイパビリティ・部品は境界の明確なインターフェース・時刻は共通、を意識する）。
- 外から見た挙動（API・イベント・レジスタ・記録の形式）は変えない。変えたものは「挙動の差」に書く。

## 基準の数字（前）

| 指標 | 前 | 後 | 測り方 |
|---|---|---|---|
| ホスト Python の総行数（host/fprobe/**/*.py） | 69400 | | git ls-tree と wc -l |
| mcp_server.py の行数 | 2473 | | |
| fpga_obs.py の行数 | 261 | | |
| RTL の総行数（gateware/rtl/**/*.vhd） | 21871 | | |
| obs_jtag_engine.vhd の行数 | 667 | | |
| fprobe_core.vhd の行数 | 1395 | | |
| 観測 IP（target_ip/fprobe_obs/rtl）の総行数 | 1026 | | |
| ホストのテスト | 3094 件通過・2 スキップ・201 秒 | | pytest host |
| 観測のシミュレーション試験（gateware、-k obs） | 16 件・約 158 秒 | | |
| 実機の観測 HIL（PYNQ ↔ Primer 代役 + Nucleo） | 12 件通過 | | gateware/tests/hil/test_fpga_obs*_hw.py |
| FPGA 観測の復号の速さ（ホスト） | 約 22 万イベント/秒（30 万イベント 1.36 秒） | | docs/reports/bench_obs_decoder.py |
| PYNQ deep: Slice LUT | 9483 / 53200（17.83%） | | Vivado util.rpt |
| PYNQ deep: Slice Registers | 12285 / 106400（11.55%） | | |
| PYNQ deep: Block RAM Tile | 8 / 140 | | |
| PYNQ deep: sys クロックの Fmax（目標 100 MHz） | 106.542 MHz・Setup TNS 0 | | ビルドの Fmax 報告 |
| エンジンのスキャン時間（hp=4・64 語） | 191.0 µs（理論 189.9 µs）・5234 スキャン/秒 | | 実機 |
| 相関（26 万行、候補を自動） | 1.80 秒 | | 担当 D の計測 |

## 今日ここまでに見つけて直したバグ（ループの前）
engine: 失敗した open で前の ID が見える／ID の待ち上限ちょうどで届いた ID をタイムアウトにする／一致を有効にした直後に既成立の状態で成立／cmd_dropped が積めても消えない。
host: 時刻の折り返し・開き直しで復号器の状態を引き継ぐ・空や矛盾した条件を受ける・数を文字で比べる・0x0 が真・hidden を数えない・ir_len の範囲検査が無い・open 失敗でエンジンが残る。

## ループの結果（後）
（ループの最後に埋める）
