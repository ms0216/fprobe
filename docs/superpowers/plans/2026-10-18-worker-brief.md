# 並行の作業担当への共通の指示（2026-10-18）

fprobe（FPGA デバッグプローブ。目標は J-Link 周辺の全ツールの上位互換）の作業の単位を 1 つ担当する人への共通の指示。
単位の内容と満たすべき競合の機能 ID は `docs/reports/2026-10-18-gap-analysis.md`、その機能の具体は
`docs/reports/2026-10-18-competitors-commercial.md`・`docs/reports/2026-10-18-competitors-vendor-oss.md` が正。

## 言語
やりとり・コメント・文書・commit メッセージ・報告はすべて日本語。

## 作り方
- 正しく美しい設計にする。場当たり的な実装・手抜きをしない。競合の機能 ID を一つずつ読み、それを満たす（上回る）こと。
- 既存の作りに倣う: Probe の道具は mixin（`host/fprobe/probe_*.py`。例 `probe_watch.py`・`probe_breakpoints.py`）にして
  `host/fprobe/mcp_server.py` の `Probe` に混ぜる。機器に触るのは必ず `self._lock` の中。
- API は `host/api/fprobe_api.json` が正。kind の基準: 機器・ターゲットを読むものは control（状態を変えない読みは
  `observes: true`）、機器に触らない読みは read。追加後に `.venv/Scripts/python -m fprobe.api gen` で生成物を更新。
  MCP の案内文（`mcp_server.py` の INSTRUCTIONS）に短く足す（道具名の実在検査あり）。
- すべてに試験（偽の機器）。TDD（試験を先に書き、失敗を見てから実装）。
  `.venv/Scripts/python -m pytest host -q -p no:cacheprovider` が通ること。worktree では `PYTHONPATH=host` を付けないと
  本流の `host/fprobe` を import するので注意。
- 実機で確かめるべき手順を、`gateware/tests/hil/test_<名前>_hw.py`（`pytestmark = pytest.mark.hil`、既存の
  `test_inspect_hw.py` に倣う）として書いておく（走らせない。走らせるのは本流の担当）。
- 小さく commit。末尾に `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`。

## 禁止
- 実機（USB・COM・Ethernet）に触る、`--hil` の試験、FPGA のビルド・書き込み、`python -m fprobe_gw gen`。
- `git checkout <file>`、ネットワーク設定の変更、D: ドライブへの書き込み。
- VHDL の変更（担当の単位がゲートウェアでない限り）。

## 報告（最後に日本語で）
commit の一覧、満たした競合の機能 ID と満たせなかった ID（理由）、追加した API の表、試験の結果、実機で確かめる手順、
未対応・注意。
