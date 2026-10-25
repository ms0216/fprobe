# 試験を速く回す

## 計測（2026-10-25、12 コア機で他の作業と同時）

- ゲートウェア `pytest gateware/tests --ignore=tests/hil`: 約 25 分。時間のほとんどは GHDL（mcode）の**シミュレーションの実行**。
  VHDL の解析（`ghdl -i/-m`）は 1 試験あたり 1〜2 秒で、同じソースの解析を共有しても全体は 3% 未満しか縮まない（試した・入れていない）。
  遅い順: `test_dap_sampler` 385 秒（16 件の cocotb テストを 1 プロセスで順に）、`test_obs_core` 148 秒、`test_bringup_uart_behaviour` 88 秒、
  `test_obs_engine[False]` 88 秒。この 4 つで全体の半分。
- ホスト `pytest host`: 約 5 分、3265 件。1 件が 1 秒を超えるものは 20 件ほどで、固定費は第三者ライブラリ（mcp・pyocd）の import（約 4 秒、
  「コアはプラグインを import しない」を確かめる別プロセスの 2 件が払う）。支配的な 1 点は無い。

## 使う: モジュール内のテストを複数プロセスに分ける

`run_cocotb(..., parallel=True)` は、1 つの cocotb モジュールの `@cocotb.test()` を 1 件ずつ（`with_previous` で指定したものは直前の件と一緒に）
別の GHDL プロセスで同時に走らせる。同時実行数は環境変数 `FPROBE_SIM_JOBS`（既定は CPU 数の半分、最大 8。`1` で従来どおり 1 プロセス）。

- 確かめる内容は同じ: 件数が合わなければ失敗、1 件でも失敗すれば失敗。ソースから数え切れないモジュール（`TestFactory` 等）は分けない。
- 前のテストが残した状態に頼るテストは分けると結果が変わる（`test_dap_sampler` の `jitter_...`）。`with_previous` で直前と同じプロセスにする。
- `parallel=True` を付けたのは長いモジュールだけ（`test_dap_processor` `test_obs_core` `test_obs_engine` `test_bringup_core` `test_la_*` `test_deep_axi` `test_fprobe_core` ほか）。

## pytest-xdist（入れていない。入れるかは判断）

`pip install pytest-xdist` のうえ `FPROBE_SIM_JOBS=1 pytest gateware/tests -n 6` とすると、試験の関数単位でも並列になる
（`test_fprobe_core` の 12 通りなど、モジュールを分けても並ばない分）。`-n` と `FPROBE_SIM_JOBS` を両方大きくするとコアを奪い合う。
ホストも `pytest host -n auto` で並べられるが、daemon・ポートを使う試験の衝突は未確認。
