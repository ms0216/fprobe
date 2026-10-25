# 実行制御（停止・再開・ステップ・ブレークポイント・故障の解読・セミホスティング）の不具合台帳

修正の経過は各項の「修正済み」を参照。再現試験: `host/tests/test_runctl_bugs.py`（すべて `xfail(strict=True)`）。
方法: 偽の Cortex-M（`tests/cpu_fake.FakeCpu`）に `Debugger` / `Probe` をつなぎ、状態モデルとの食い違いを調べた。
ランダムな操作列（ブレークポイントの設定・解除・有効無効・ステップ・実行・停止・リセットを 60 系列 × 40 操作）で、
機器のブレークポイントと帳簿（`breakpoint_list`）の一致も調べたが、この不変条件の破れは出なかった（失敗した enable の
取りこぼし BUG-RUNCTL-8 は別に確認）。

重さ: A = 静かに誤った結果 / B = 失敗が見える / C = 端の条件・まれ / D = 品質

| ID | 重さ | 要約 | 再現試験 | 確信度 |
|---|---|---|---|---|
| RUNCTL-1 | A | `Debugger.step` は止まった理由を読まず常に `reason="step"` | `test_step_reports_the_real_halt_reason` | 高 |
| RUNCTL-2 | B | `Debugger.step` / `target_step` はブレークポイントの上から進めない | `test_step_from_a_breakpoint_advances`, `test_target_step_from_a_breakpoint_advances` | 中（偽のコアの規則に依る） |
| RUNCTL-3 | A | `target_step(count)` は途中のベクタキャッチ等の停止後も進み続け、理由を失う | `test_target_step_stops_at_a_vector_catch` | 高 |
| RUNCTL-4 | A | `resume` の踏み越えがベクタキャッチで止まっても再開し、その停止を失う | `test_resume_keeps_a_vector_catch_hit_by_the_stepped_instruction` | 高 |
| RUNCTL-5 | B | `resume` の踏み越えが失敗するとブレークポイントを置き直さずに抜ける | `test_resume_restores_the_breakpoint_when_the_step_fails` | 高 |
| RUNCTL-6 | C | 時間切れの直前に別の理由で止まっていても `reason="timeout"` | `test_run_until_halt_timeout_does_not_hide_a_real_halt` | 中 |
| RUNCTL-7 | C | 同じ番地に大きさ・種類の違うウォッチポイントを置くと帳簿が上書きされ前の比較器が残る | `test_watchpoint_clear_frees_every_comparator` | 高 |
| RUNCTL-8 | B | `breakpoint_enable(True)` が比較器不足で失敗すると無効の帳簿からも消える | `test_enable_failure_keeps_the_disabled_breakpoint` | 高 |
| RUNCTL-9 | B | `target_disconnect` は片付けが失敗するとセッションを閉じずに抜ける | `test_disconnect_closes_the_session_even_if_cleanup_fails` | 高 |
| RUNCTL-10 | C | `fault.analyze` は Cortex-M1 (0xC21)・SC000 (0xC24) を ARMv6-M と認識しない | `test_analyze_knows_other_armv6m_parts` | 高（部品番号は ARM の表） |
| RUNCTL-11 | D | 同じ停止（DFSR の BKPT+VCATCH）の語がステップ系と `target_status` で違う | `test_one_halt_gets_one_name_in_every_path` | 高 |
| RUNCTL-12 | D | MMARVALID と BFARVALID が両方立つと `fault_address` は BFAR だけ | `test_both_fault_addresses_are_reported` | 高 |
| RUNCTL-13 | C | SYS_WRITE0 は 64 バイトずつ読むため、領域の終わり近くの正しい文字列で失敗 | `test_write0_near_the_end_of_memory` | 高 |

件数: A 3 / B 4 / C 4 / D 2（計 13）

## 詳細

### RUNCTL-1（A）`Debugger.step` の理由が固定
- **修正済み: `Debugger.step` は 1 命令の後に DFSR を読み、`dfsr_reason(stepping=True)` で理由を決める（None なら step）。halt_bits も添える（`Debugger._step_instruction`）。**
- 原因: `host/fprobe/debug.py:229-232`。`self.target.step()` の後 `_stop(start, "step")` で DFSR・`get_halt_reason` を見ない。
  ウォッチポイント・ベクタキャッチ・フォールトに入った 1 命令でも "step"。`resume`（同 208 行）は同じ状況を区別しているので不揃い。
- 直し方の案: step の後に DFSR を読み、`stepping.dfsr_reason(dfsr, stepping=True)` で理由を決める（None なら "step"）。halt_bits も添える。
- 影響: 利用者・AI が「ステップで止まった」と信じ、ウォッチポイントやフォールトの発生を見落とす。

### RUNCTL-2（B）ブレークポイントの上でのステップ
- **修正済み: `_step_instruction` が PC のブレークポイントを進める間だけ外し、finally で戻す（`Debugger.step` / `target_step` / `resume` で共有）。pyOCD の `CortexM.step` と `BreakpointManager.flush(is_step)`（venv の pyocd）は PC のブレークポイントを外さず、実機で FPB が再び当たるかは pyOCD のソースからは決まらない（未確認）。外して進めるのは実機がどちらでも誤りにならないので採用した。**
- 原因: `debug.py:229`（Debugger.step）と `probe_target.py:target_step`。`stepping.Runner._step_here`（stepping.py:126-135）は
  ブレークポイントを外して進めるが、直接の step は外さない。
- 影響: ブレークポイントで止まった直後の `target_step` が進まない（実機で FPB が先に当たる場合）。リポジトリの偽のコアが
  「実機では止まり直す」と定めている事実に基づく。pyOCD 実機での確認は未（HIL は走らせていない）。
- 直し方の案: step も resume と同様に pc のブレークポイントを外して進め、`finally` で戻す（Runner の手順を共有）。

### RUNCTL-3（A）`target_step(count)` が停止を踏み越える
- **修正済み: `target_step(count)` は `Debugger.step_instructions` で 1 命令ごとに DFSR を見て、step 以外の理由で打ち切る。返す状態に `steps_done`（実行した回数）。Cortex-M 以外（DFSR が無い）は従来どおり。**
- 原因: `probe_target.py` の `target_step` は `for _ in range(count): self.target.step()` で毎回の結果を見ない。
  2 回目の step でベクタキャッチ（HardFault の入口）で止まっても 3 回目がハンドラの中を進め、DFSR が上書きされて
  `halt_reason` は "request" になる。ウォッチポイントでも同様。
- 直し方の案: 各 step の後に DFSR を読み、step 以外の理由なら打ち切って返す（返す値に打ち切った回数）。count < 1 の扱いも決める。

### RUNCTL-4（A）`resume` の踏み越えで停止を失う
- **修正済み: `resume` の踏み越えの後、DFSR が通常のステップ以外（ベクタキャッチ・ウォッチポイント・BKPT・外部）なら再開せずその停止を残す。**
- 原因: `debug.py:208-209` はウォッチポイントだけ停止を残す。ベクタキャッチ（フォールト命令の上のブレークポイントの再開）・
  外部の停止要求・別のブレークポイントに着いた場合も、踏み越えの step 後に `self.target.resume()` で走り出す。
- 直し方の案: 踏み越えた後の停止の理由を DFSR で読み、HALTED（通常のステップ）以外なら再開しない。

### RUNCTL-5（B）`resume` の失敗後の後始末
- **修正済み: 踏み越えは `_step_instruction` の try/finally で、失敗しても必ずブレークポイントを置き直す。**
- 原因: `debug.py:205-207`、`remove_breakpoint` → `step` → `set_breakpoint` に try/finally が無い。`Runner._step_here` は finally で戻している。
- 影響: 転送エラー等の後、帳簿（`breakpoints`）は置いたままなのに機器には無い。以後その場所で止まらない。
- 直し方の案: try/finally で置き直す。

### RUNCTL-6（C）時間切れの競合
- **修正済み: 時間切れの `halt()` の前に状態を読み、既に HALTED ならその停止の理由で返す。**
- 原因: `debug.py:225-227`。`halt()` の後に停止の理由を読まず固定で "timeout"。最後のポーリングと `halt()` の間に別の理由で
  止まっていると理由が失われる（`Runner._wait` は同じ形）。
- 直し方の案: `halt()` の前に状態を読み直し、既に HALTED ならその理由で返す。

### RUNCTL-7（C）ウォッチポイントの帳簿
- **修正済み: 同じ番地の再設定は先に前の比較器を外し、新しいのが置けなければ前のを戻す（帳簿と機器を一致）。帳簿のキーは番地のまま（他の呼び手が番地で引くため）。**
- 原因: `debug.py:181-183`。`watches` が番地だけをキーにするので、同じ番地の別の大きさ・種類は前を上書きする。
  `remove_watchpoint` は最新の 1 つしか外さない。比較器（DWT は少数）が漏れ、`breakpoint_list` にも出ない。
- 直し方の案: キーを (番地, 大きさ, 種類) にする。同じ番地の再設定は先に前のを外す。

### RUNCTL-8（B）`breakpoint_enable(True)` の失敗
- **修正済み: `breakpoint_enable(True)` は置けてから無効の帳簿から消す。失敗なら無効のまま残り再試行できる。**
- 原因: `probe_breakpoints.py` の `breakpoint_enable`。`self._disabled.pop(address)` の後に `self.dbg.breakpoint(address)` を呼ぶ。
  置けない（比較器不足）と例外になり、`_disabled` からは既に消え、動作（condition 等）も捨てられる。
- 直し方の案: 置けたことを確かめてから `_disabled` から取り出す（または失敗時に戻す）。

### RUNCTL-9（B）`target_disconnect` の後始末
- **修正済み: 片付けの失敗は呼び手に伝えるが、`finally` で必ず接続を閉じて `_debug = None` にする。**
- 原因: `probe_target.py` の `target_disconnect`。`_monitor_detach()` / `_flash_bp_detach()` が例外を出すと `debug.close()` と
  `_debug = None` に届かず、接続が残る。以後の `target_connect` は再度 `target_disconnect` を呼んで同じ所で失敗しうる。
- 直し方の案: try/finally で必ず `close` し `_debug = None` にする。失敗は報告に添える。

### RUNCTL-10（C）ARMv6-M の部品番号
- **修正済み（一部）: Cortex-M1 (0xC21) を追加。**台帳の「SC000 = 0xC24」は誤り: 0xC24 は Cortex-M4（pyocd `core_ids.py`）。** よって 0xC24 は ARMv7-M のまま（試験で固定）。SC000 の部品番号は未確認のため入れていない。**
- 原因: `fault.py:_V6M_PARTNOS = {0xC20, 0xC60, 0xD20}`。Cortex-M1 (0xC21)、SC000 (0xC24) が無い。CFSR 等の無い部品で
  ARMv7-M のレジスタを読み、架空の故障原因を返す。（`tests/test_fault.py` の偽のコアの既定は 0xC24 だが、その試験は v7-M 前提の値を読んでいる。直すときは試験の部品番号も見直す）
- 直し方の案: 部品番号の表を ARM の定義に合わせる（アーキテクチャの事実なのでヒューリスティックではない）。

### RUNCTL-11（D）停止の語の優先順位
- 原因: `stepping.dfsr_reason`（vector_catch > watchpoint > breakpoint > external）と `fault.halt_reason`（breakpoint >
  watchpoint > vector_catch > external > request）の順が逆。docstring はどちらも「1 つ選ぶ」とだけ書く。halt_bits は添わる。
- 直し方の案: 選び方を 1 か所に集め、複数立っていれば halt_bits の全部を主に見せる（語は添え物）。

### RUNCTL-12（D）fault_address が 1 つ
- 原因: `fault.py:analyze`。MMARVALID の後に BFARVALID で同じキー `fault_address` を上書き。`registers` には両方あるので情報は残るが、
  解読された番地としては片方が落ちる。
- 直し方の案: `fault_addresses`（MMFAR・BFAR の別）を返す。

### RUNCTL-13（C）SYS_WRITE0 の先読み
- **修正済み: SYS_WRITE0 の NUL 探索は 64 バイトの読みが失敗したら半分ずつ縮めて 1 バイトまで読み直す。**
- 原因: `semihosting.py:_write0`（270-273 行付近）が NUL を探して 64 バイトずつ読む。領域末尾の近くの文字列は、
  終端が領域の中でも次の 64 バイトが領域外になり、読みが失敗する（実機では BusFault 相当のエラー）。
- 直し方の案: NUL まで 4 バイトずつ・または領域の境界で切って読む。

## 疑い（再現していない・試験なし）
- S1（D）`breakpoint_set`: `ignore_count` は条件が偽の当たりも数える（`_break_action` は `hits += 1` を条件評価より先に行う）。
  GDB は条件が真のときだけ無視を減らす。仕様の文言「最初の n 回は止まらない」とは合うが、組み合わせると期待と違いうる。
- S2（D）`location.startswith("0x")` の判定が大文字小文字・空白に弱い（`"0X100"` は記号の解決に回る）。`watchpoint` は
  `breakpoint_address` と違い `strip().lower()` しない。
- S3（D）`target_step(count)` の count < 1 は何も起きず状態だけを返す（負でも誤りにならない）。
- S4（C）`run_until_halt` は LOCKUP・SLEEPING を HALTED と区別しない。ロックアップしたコアは時間切れまで待ってから
  "timeout" と返し、ロックアップだった事実は Stop に載らない（`target_status` の `fatal` で別途見える可能性）。
- S5（C）`:semihosting-features` の BytesIO も `max_open` の数に入る。
- S6（D）`Debugger.load_to_ram`: 途中の書き込みが失敗した場合、リセットして止めた後の中途半端な RAM のまま残る（後始末なし）。
- S7（C）`Debugger.clear()` は途中の 1 件が失敗すると残りを外さずに抜ける。

## 調べた範囲
`fprobe/debug.py`（全体）、`probe_target.py`、`probe_execution.py`、`probe_debug.py`、`probe_breakpoints.py`、`fault.py`、
`stepping.py`（Runner・LineStepper の主要部）、`semihosting.py`（ホスト側の各操作）、`probe_observe._run_until_halt` /
`_service` / `_service_in_background`。偽のコア = `cpu_fake.FakeCpu` 上でのランダムな操作列。

## 調べていない範囲
`probe_safety` の見張り（fatal の判定）、coverage / profile ツール、`calling.py`（関数呼び出しの復元）、FPB / DWT の
本物の比較器の数の上限の挙動、Cortex-A・RISC-V の実行制御、monitor 方式、実機（pyOCD の実際の step / bp_manager の挙動。
RUNCTL-2 の確度に効く）。

## 未修正（理由）
- RUNCTL-11（語の優先順位）・RUNCTL-12（fault_address の形）: 出力の形・語の取り決めを変える（UI・利用側に波及）ため、設計の判断を待つ。
