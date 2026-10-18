# M3 実装計画: コントロールプレーン

仕様: `docs/superpowers/specs/2026-10-15-m3-control-plane-design.md`。各タスクは TDD（失敗するテスト → 実装 → 全テスト）で進め、
台帳 `.superpowers/sdd/2026-10-15-m3-control-plane/progress.md` に完了・判断を記録する。

| # | タスク | 成果物 | テスト |
|---|---|---|---|
| 1 | CSR バスの振り分け | `rtl/core/csr_decoder.vhd` | 窓ごとの振り分け、未割り当ての err、待ちのあるスレーブ |
| 2 | レジスタ定義 DSL と生成器 | `fprobe_gw/csr/`（定義・VHDL バンク・マップ pkg・ホスト・Markdown）、`gen` コマンド | 生成物の一致、バンクの R/W・RO・CONST・strobe（cocotb） |
| 3 | タイムベース | `rtl/core/timebase.vhd` | 増加・リセット、スナップショット（バンク経由） |
| 4 | ケイパビリティ | `fprobe_gw/caps.py`（TLV と ROM 生成）、`rtl/core/caps_rom.vhd` | TLV の往復、ROM の読み出し・範囲外 0 |
| 5 | ホストライブラリ（プロトコル部） | `host/fprobe/{control,caps,device,transport}.py` | 偽トランスポートで符号化・分割・エラー・tag |
| 6 | control_plane | `rtl/core/control_plane.vhd` | ホストライブラリの符号化で全 op・全エラー・ストールに耐える |
| 7 | fprobe_core | `rtl/core/fprobe_core.vhd` | ホストライブラリの Device を cocotb 上のストリームにつないで端から端まで |
| 8 | 製品デザイン | `primer20k_fprobe_top`、デザイン登録 | ビルド・STA |
| 9 | 実機 | `tests/hil/test_fprobe_hw.py` | CSR R/W、ケイパビリティ、タイムベースの実時間整合 |
| 10 | 文書 | CLAUDE.md・全体設計の更新 | — |
