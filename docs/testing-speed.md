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

## 速さの約束の試験は別に走らせる（`perf` の印、2026-11-03）

`perf_budget.within` で「この量をこの時間以内」を測る試験 7 件（`test_la_vcd_perf`・`test_recording_vcd_perf`・`test_recording` の 100 万件・
`test_waveform`・`test_decode_swd`・`test_decode_uart`）に `@pytest.mark.perf` を付け、**既定の `pytest` から外した**（`host/pyproject.toml` の
`addopts = "-m 'not perf'"`）。速さの約束だけを続けて走らせる:

```sh
cd host
pytest              # 機能の試験（perf を除く。約 4 分半）
pytest -m perf      # 速さの約束だけ（別のプロセス。約 12 秒）
```

- 理由: 全体の実行（5000 件超）の中では、ほかの試験の後始末（残ったスレッド・メモリ・ディスクの書き）と PC の混み具合が測りに混ざり、
  上限を超えて落ちることがあった（C と利用者の手元）。`perf_budget.within` の測り直し（最良の回で比べる）では、混みが長く続くと救えない。
  別のプロセスで続けて走らせると、ほかの試験と同時でも、直前の試験の後始末とも混ざらない。約束（上限）は緩めない。
- 機能の試験を `-n`（xdist）で並べる場合も、`pytest -m perf` は `-n` を付けずに走らせる（並べると測りが揺れる）。
- 一つの試験を指定するときも、`perf` は `-m perf` が要る（`pytest tests/test_waveform.py -m perf`。無いと「deselected」になる）。
- 付け忘れは `test_perf_budget.py::test_every_test_that_measures_a_budget_with_within_is_marked_perf` が落ちて分かる。
- 実測（負荷は CPU を回し続けるプロセスを並べた。4 コア）: 2 ファイル（`test_la_vcd_perf`・`test_recording_vcd_perf`）を単独で走らせて、
  負荷なし 0/20、負荷 4 本 0/20、負荷 12 本 **1/20** 失敗（3 件とも上限超え）。つまり、別に走らせても、走らせる間に PC が極端に混めば落ちる。
  perf の実行は、ほかの重い作業と同時にしないこと。
