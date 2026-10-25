# 引き継ぎ（2026-10-25 夜 → 10-26 朝）

別のセッションで続けるための現状。事実だけ。

## 動いているもの
- **連続試験**（PC 上で切り離して実行中。開始 2026-10-25 23:47、終了 06:30）:
  - 結果: `build/soak/soak.jsonl`（1 行 = シナリオ 1 回）、要約: `build/soak/soak.jsonl.summary.txt`（15 分ごとに上書き）。
  - 止める: `build/soak/soak.stop` というファイルを作る（安全に止まって最終の要約を出す）。
  - 最初の起動（Primer の観測の代役が消えていて全部失敗）は `build/soak/soak.first-attempt.jsonl`。
  - 見る（失敗の一覧）: `python -c "import json;[print(d['scenario'],str(d.get('failures'))[:200]) for d in map(json.loads,open('build/soak/soak.jsonl',encoding='utf-8')) if d.get('scenario') not in (None,'resources') and not d.get('ok')]"`
  - 道具: `tools/soak/`（README あり）。シナリオ: 観測（obs_long/obs_reopen/obs_quiet/obs_count/obs_groups/obs_halt/chaos_obs）、ロジアナ（la/la_deep/la_obs_align）、SWD（swd_mem/sampler）、eventlog、ping。
- **PYNQ-Z2**: IP は固定 **<PYNQ の IP>**（`fprobe-ip.service`）。電源を入れるだけで PL が構成され（U-Boot が `/boot/fprobe.bit.bin` を `fpga load`）、`fprobe-boot.service` が印を置き、`fprobe-relay.service` が起動する。
  - **ソフトの `reboot` は fprobe の回路が載っている間は止まる**（`Reboot failed -- System halted`）。再起動は電源の入れ直しで。
  - 起動で IP が固定になることは、電源の入れ直しでは**未確認**（`fprobe-ip` は起動中に手動で適用して動作確認しただけ）。
  - 戻し方: `/boot/uEnv.txt.before-persist`（元の uEnv.txt）、`/boot/boot.scr.before-persist`。PL を自動で書かせない: uEnv.txt から `uenvcmd` と `fprobe_pl` の行を消す。
  - ビットストリーム: `gateware/build/fprobe_pynq_deep/top.bit`（印 `a036ff96d3d136bf`）。JTAG で書く: `python -m fprobe_gw program --board pynq_z2 --relay <top.bit>`（環境変数 FPROBE_PYNQ_HOST）。
- **Primer（観測の代役）**: SRAM に書いた構成 `obs_target_primer`。**電源を切ると消える**（フラッシュの標準の回路に戻る）。書き直し: `python -m fprobe_gw program gateware/build/obs_target_primer/top.fs`。消えると観測のシナリオが `ID 0x00000000` で全滅する。

## 配線（PYNQ ↔ Primer）
ヘッダ 15→J14（TCK）、16→J16（TMS）、18→M14（TDI）、22←M15（TDO）、20→J12 の GND、Primer の T12→PYNQ PMOD JB の 1 番（ロジアナ ch10）。Primer の J12 は番号でなく印刷のピン名でつなぐ。PYNQ の PMOD JA は Nucleo（SWD）。

## ブランチ
- 作業は `integ/round1`（`main` へは取り込んでいない）。約 150 コミット。`.claude/worktrees/` に担当ごとの作業場が多数ある（片づけ可）。
- 公開リポジトリ（ms0216/fprobe）は**未更新**（前回 4 回目まで）。5 回目は手順どおり（シリアル番号を伏せて書き出し、push は利用者）。

## 検証の状況
- ホストの全テスト: 約 3400 件通過。負荷で落ちることがある試験: `test_recording.py::test_a_million_events_...`、`test_probe_testpoint.py::test_itm_duration_makes_stop_wait`・`test_stream_with_a_duration_ends_by_itself`（単独では通る）。
- ゲートウェアの全テスト: 約 410 件通過（並列で約 20 分）。
- 実機: 観測の HIL 12 件通過。ロジアナと観測の時刻差 −9〜−14 ns、観測の時刻誤差は 15 構成で約 14 ns 以内。
- 未修正の不具合の再現試験が `xfail(strict)` で約 50 本残っている。台帳: `docs/reports/bugs/*.md`。

## 朝にやること（優先順）
1. 連続試験の要約と失敗の一覧を見る。観測の gap/lost/stuck、時刻の傾きのドリフト、ping の最大、RSS の増え方。失敗は台帳と照らして原因を調べる。
2. PYNQ を電源の入れ直しで起動し直し、固定 IP（<PYNQ の IP>）・PL の自動構成・中継の自動起動が揃うか確かめる。
3. 一過性だった HIL の失敗（書き込み直後の 1 回: `test_the_timeline_shows_named_fpga_signals_while_recording`）が連続試験で再発するか見る。
4. 利用者の FPGA（Xilinx 等）への観測 IP の組み込み（`docs/guide/fpga-analysis.md`）。鎖の位置は `jtag.scan` が観測の口を走査しない点に注意。
5. 設計判断が要るもの: イベント数 `EVENTS_WRITTEN` の 32 ビットの回り込み、`fprobe_pynq` の IP/ボードの電源順、API の長さ・個数の上限（キャンセルの仕組みが先）。
