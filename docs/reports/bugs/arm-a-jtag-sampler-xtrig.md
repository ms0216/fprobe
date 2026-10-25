# 不具合台帳: Cortex-A / ARM11 の JTAG 直接デバッグ・JTAG の層・サンプラ・クロストリガ・故障注入の台本・RTT

検出専用（製品コードは変更していない）。再現試験は `host/tests/test_arm_a_bugs.py` と
`host/tests/test_sampler_xtrig_inject_bugs.py`（すべて `xfail(strict=True)`。直ったら XPASS で失敗する）。
重さ: A = 静かに誤った結果を返す / B = 失敗が見える / C = 端の条件・まれ / D = 品質。

件数: A 0 / B 1 / C 9 / D 3（計 13）。

## 再現済み

### BUG-ARMA-01（B）ブレークポイントのある番地からの step が 1 命令も進まない
- 試験: `test_arm_a_bugs.py::test_v7_step_from_an_address_with_a_breakpoint_advances`
- 原因: `host/fprobe/arm_a_target.py` L92-93 `ArmATarget.step` → `host/fprobe/arm_debug.py` L202-227 `ArmDebugCore.step`。step は最後の組に mismatch を置いて再開するが、現在の PC に一致のブレークポイント（利用者が置いたもの）が有効なままなので、再開と同時にそのブレークポイントが成立して同じ番地で止まり直す。`Debugger.resume`（debug.py L200-208）は自分で外してから step するので resume は動くが、`Debugger.step()`（L229-232）・`step_line` は外さない。Cortex-M（pyOCD）では起きないので呼び側は気づかない。
- 直し方の案: `ArmATarget.step` が、PC に一致するブレークポイントの組があれば、step の間だけ `clear_breakpoint` し、終わったら置き直す（OpenOCD の cortex_a_step と同じ）。
- 影響: ブレークポイントで止まった直後に step しても PC が進まない（停止理由は "step" と出る）。ステップ実行・行ステップが止まっているように見える。
- 確信度: 中（試験の模型は「再開した命令の番地に BT=0 の有効なブレークポイントがあれば、進まずに止まる」という ARMv7 の挙動。実機の Pi 2 では未確認）。ARM11 も同じ構造（試験は v7 のみ）。
- 修正済み: `ArmATarget.step` は PC に一致のブレークポイントがあれば、進める間だけ外して置き直す（OpenOCD の cortex_a_step と同じ。ARMv7 の仕様どおり）。ARM11 は同じ構造だが未確認。

### BUG-ARMA-02（C）Cortex-A のブレークポイントの ARM / Thumb が、最後に止まったときの CPSR.T で決まる
- 試験: `test_arm_a_bugs.py::test_v7_breakpoint_encoding_follows_the_address_not_the_mode_at_the_last_halt`
- 原因: `arm_debug.py` L459-465 `ArmDebugCore.set_breakpoint(thumb=None)` が `self.thumb`（最後の `_enter` の CPSR.T）を使い、`ArmATarget.set_breakpoint`（arm_a_target.py L180-192）は `thumb` を渡さず番地の bit 0 も見ない。`_byte_select` の BAS が、置く番地の命令の種類でなく「今止まっているコードの種類」になる。
- 直し方の案: 番地の bit 0（pyOCD / ELF の Thumb 関数の慣習）か、呼び側が渡す引数で ARM / Thumb を決める。決められないときは断る（今の種類を暗黙に使わない）。
- 影響: Thumb で止まっている間に ARM の関数に置くと BAS=0b0011（上位の半語を見ない）、逆に ARM で止まっている間に Thumb に置くと 0b1111 になり、命令の種類によっては成立しない・または UNPREDICTABLE。ARM と Thumb が混在するコード（Pi の Linux/ブートローダ）で起きる。
- 確信度: 中（BAS の符号化は ARMv7 ARM のとおり。実機未確認）。
- 未修正: 番地の偶奇では ARM / Thumb を決められない（DWARF の行表の Thumb の番地は偶数）ので、番地の bit 0 の慣習に変えると Thumb の行のブレークポイントが静かに ARM になる。呼び側（debug.py）が命令の種類を渡す API の設計が要る。別担当の debug.py に触れるため保留。

### BUG-SAMP-01（C）plan_slots が 32 ビットの外の番地を断らない
- 試験: `test_plan_slots_rejects_addresses_outside_32_bits`
- 原因: `host/fprobe/sampler.py` L74-88 `plan_slots`。語の集合を作るだけで、`address + size` が 2^32 を超える・`address < 0` を検査しない。0xFFFFFFFC から 8 バイトで `[(0xFFFFFFFC, 1), (0x100000000, 1)]` を返す。
- 直し方の案: 範囲が `0 <= address` かつ `address + size <= 1 << 32` でなければ ValueError。
- 影響: か所の番地の 33 ビット目が CSR 書き込みで落ち（0x00000000 を読む）、別の番地を観測する。
- 確信度: 再現済み（まれな入力）。
- 修正済み: `plan_slots` は 32 ビットの外・負の番地を ValueError で断る。

### BUG-SAMP-02（D）swd_hz_for_rate が rate_hz <= 0 で ZeroDivisionError
- 試験: `test_swd_hz_for_rate_rejects_a_non_positive_rate`
- 原因: `sampler.py` L62-71。`needed = transfers * BITS * rate_hz` が 0 のとき `half_clock / needed`。
- 直し方の案: `rate_hz <= 0` は ValueError（使う側が直せる言葉で）。
- 影響: 道具の誤りが裸の ZeroDivisionError で出る。
- 確信度: 再現済み。
- 修正済み: `swd_hz_for_rate` は rate_hz <= 0 を ValueError で断る。

### BUG-SAMP-03（C）Sampler の ap の範囲検査が無い
- 試験: `test_sampler_ap_is_range_checked`
- 原因: `sampler.py` L100-119。`self.ap << 8` を CONTROL に足す。AP 欄は (8, 8) だけ。ap >= 256 は bit 16 以上＝ゆらぎの欄を書き換える。負は上位ビットを全部立てる。
- 直し方の案: `0 <= ap <= 255` を configure で検査。
- 影響: 取り込みの周期がゆらぐ・または意図しない AP の読み。
- 確信度: 再現済み。
- 修正済み: `Sampler.configure` が ap を 0〜255 の整数に限る（ValueError）。

### BUG-XTRIG-01（C）Pattern の mask / value / rise / fall に 16 ビットの検査が無い
- 試験: `test_pattern_wider_than_16_bits_is_rejected`
- 原因: `host/fprobe/xtrig.py` L128-129 `TriggerSequencer.configure` が `a.mask | a.value << 16`、`a.rise | a.fall << 16` を作る。mask の 17 ビット目が value の bit 0 に、value の 17 ビット目以上が語の外に出る。
- 直し方の案: `Pattern.__post_init__` で 4 欄とも 0〜0xFFFF、さらに `value & ~mask` を持つかは使う側の事実として検査しない（ただし範囲は検査）。
- 影響: ch16 以上を指定した条件が、別の ch の条件として黙って設定される。MCP の `xtrig_set_sequence` は dict をそのまま渡すので AI も踏む。
- 確信度: 再現済み。
- 修正済み: `Pattern` が 4 欄とも 0〜0xFFFF の整数（bool 除外）を検査し、外は ValueError。

### BUG-XTRIG-02（C）within_s が 0・負のとき 1 周期に丸めて黙って受け付ける
- 試験: `test_non_positive_within_is_rejected`
- 原因: `xtrig.py` L137 `0 if within_s is None else max(1, round(within_s * clock_hz))`。`within_s=0.0` は「制限なし（None と同じ 0）」でも「即時」でもなく 1 周期になる。負も同様。
- 直し方の案: `within_s <= 0` は ValueError（制限なしは None だけ）。
- 影響: 「0 秒以内」「負」の指定が、ほぼ成立しない条件として設定されるのに、成功が返る。
- 確信度: 再現済み。
- 修正済み: `within_s` は正の数だけ（制限なしは None）。0・負・NaN は ValueError。

### BUG-XTRIG-03（C）条件が空（Event(0)）の段を受け付ける
- 試験: `test_a_stage_without_any_condition_is_rejected`
- 原因: `xtrig.py` L115-142 `configure`。未使用の段を `Stage(Event(0))` で埋めているので、使う段の空も区別していない。`xtrig_set_sequence(stages=[{"when": []}])` も通る。
- 直し方の案: 使う段（`stages` の中）の `when` が空なら ValueError。
- 影響: その段は永久に成立せず、トリガが発火しないまま「仕掛けた」と返る。
- 確信度: 再現済み。
- 修正済み: 使う段の条件が空なら ValueError。

### BUG-XTRIG-04（D）pulse_ticks が負・0 秒を 1 周期に丸める
- 試験: `test_pulse_ticks_rejects_a_negative_duration`
- 原因: `xtrig.py` L235 `max(1, round(seconds * clock_hz))`。呼び側（`xtrig_output_set`）は pulse_us > 0 を検査するが、ライブラリ単体は検査しない。
- 直し方の案: seconds <= 0 は ValueError。
- 影響: ライブラリを直接使うと負の脈が 1 周期で通る。
- 確信度: 再現済み。
- 修正済み: `pulse_ticks` は seconds <= 0 を ValueError で断る。

### BUG-XTRIG-05（D）xtrig_route の未知の源の名前が KeyError
- 試験: `test_xtrig_route_unknown_source_is_a_value_error`
- 原因: `host/fprobe/probe_xtrig.py` L18-19 `Source[n.upper()]`。`xtrig_output_set`（probe_xtrig_out.py L77-82）は ValueError で源の一覧を言う。
- 直し方の案: 同じ変換を共通にして ValueError と一覧を言う。
- 影響: 綴りの誤りが `KeyError: 'LAA_TRIGGER'` で出て、何が使えるか分からない（AI が直しにくい）。
- 確信度: 再現済み。
- 修正済み: `xtrig_route` の未知の源は ValueError で一覧を言う。

### BUG-INJ-01（C）台本の sequence の段・dap_match の欄を読み込み時に検査しない
- 試験: `test_sequence_stage_fields_are_validated_when_the_script_is_read`
- 原因: `host/fprobe/inject.py` L185-195 `_parse_when`。段は `when` の列の中身だけ検査し、`count`（1〜65535 の整数）・`within_us`（正の数）を検査しない。`dap_match` の `on_write` / `on_read` も型を見ない。実行時に `TriggerSequencer.configure` が ValueError / TypeError を出し、試行が `error` になる。
- 直し方の案: `count` は 1〜65535 の整数、`within_us` は正の数（bool 除外）、`on_*` は bool として検査。
- 影響: 台本の検証（`ScriptError`）を通ったのに、試行の途中で error。繰り返しの多い台本で時間を無駄にする。
- 確信度: 再現済み。
- 修正済み: 台本の読み込みで sequence の count（1〜65535 の整数）・within_us（正の数）・dap_match の on_write / on_read（真偽値）を検査して ScriptError。

### BUG-INJ-02（C）注入の後に通信が切れると restore が実行されない
- 試験: `test_restore_runs_even_when_the_status_read_fails_after_the_injection`
- 原因: `inject.py` L291-323 `_run_trial`。`try` の中のどの例外も `verdict="error"` で `return out` し、`_restore`（L341）・注入の `note`（L336-340）を通らない。`host.fire()` や状態の読みの途中の失敗でも、注入が実行済みの可能性がある。
- 直し方の案: 例外のときも、注入が起きたか（`status.count` の変化）を読み直せる範囲で確かめ、`_restore` は常に試す（失敗は `restore_error`）。
- 影響: 注入が起きたのに restore の書き戻し・リセット・再開が実行されず、ターゲットが壊れた状態で残る（台本の `restore` は「壊した後に戻す」ためにある）。
- 確信度: 中（失敗の種類による。注入前の失敗では restore は不要）。
- 修正済み: 仕掛けた後に例外が出ても `_restore` を試す（失敗は restore_error）。注入の note は、注入が起きたか読めないので出さない。

### BUG-RTT-01（C）チャンネル名を常に 32 バイト読むので、領域の終わりの近くの名前で open() 全体が失敗する
- 試験: `test_open_survives_a_channel_name_near_the_end_of_the_region`
- 原因: `host/fprobe/rtt.py` L273-277 `_read_name`、`NAME_LIMIT = 32`。名前の文字列が短くても 32 バイトの読みが領域の外に出ると、メモリの読みが失敗し `open()` が例外になる。
- 直し方の案: 失敗したら名前を縮めて読み直す（NUL までは実際に読めた範囲）か、名前は読めなければ None にして open は続ける。
- 影響: 名前が RAM / フラッシュの最後の 32 バイト以内にあると RTT が使えない。
- 確信度: 中（pyOCD は割り当て領域の外を読めない前提）。
- 修正済み: 32 バイトの読みが失敗したら NUL まで 1 バイトずつ読み、読めた分を名前にする。

## 疑い（再現していない・確かめられなかったもの）

- **S1（ARMv7 の mismatch の組）**: `ArmDebugCore.step` は最後のブレークポイントの組（`breakpoint_count - 1`）に BT=0b0100（アドレスのミスマッチ）を置く。ARMv7 の BRP は番号の大きい `CTX_CMPs` 個が文脈対応で、その BT の取れる値が限られる。Cortex-A7（BRP 6・うち 2 が文脈対応）の最後の組が使えるかは ARM ARM / A7 TRM で確かめる（実機の step は動作確認済みの記述があるので低確信）。`arm_debug.py` L139・L206。
- **S2（JTAG-DP の書き込みの誤り）**: `raw_dap.py` L281-284 `write_ap`・`mem_write32` は JTAG で `_jtag_checked` を通さない。AP の書き込みの誤り（STICKYERR）は次の読みで見つかり、その読みの誤りとして報告される（原因の書き込みと別の操作になる）。読みが続かなければ見つからない。Cortex-A の ITR / DTR の書き込みは `mem_write32`。
- **S3（plan_slots の巨大な範囲）**: `sampler.py` L77 は範囲の全語を集合にする。`(0, 1 << 31)` のような範囲はメモリと時間を使い果たす（8 か所に収まらないと分かる前に）。か所の数・区間で数えれば足りる。
- **S4（PinOutputs の端子の欄）**: `XTRIG_OUT_CTRL0_PIN` は 2 ビット。`PinOutputs.configure` は `pin < len(self.pins)` しか見ないので、ケイパビリティが 5 本以上の端子を申告する構成では `pin << 28` が語の外に出る。現在の構成は 4 本まで。
- **S5（run_script の全体の判定）**: `RANK` で not_triggered / error / inject_error が同順位（3）。`worst` は最初のものを返すので、`[not_triggered, error]` は not_triggered になり error が全体の判定から消える（counts には出る）。
- **S6（inject_status / match_status の時刻）**: 状態・lo・hi を 1 回の連続読みで読むが、その間に次の注入が来ると lo / hi が別の注入のものになりうる。レジスタ側にラッチがあれば問題ない。
- **S7（probe_testpoint の開始の失敗）**: `_tp_start_gpio` は `deep.start` / `deep.stream` の後に `LiveAnalysis` や `TimeMap` が例外を出すと、イベント記録の一時停止（`_tp_pause_events(True)`）と捕捉を戻さない（`_tp_run` が無いので testpoint_stop でも戻せない）。現在の呼び出しでは例外が出る入力は見つからなかった。
- **S8（ARM11 の sticky の消し方）**: `Arm11Port.clear_sticky` は DSCR の sticky ビットを「0 を書いて消す」前提（コメントにも実機未確認とある）。ARM1176 の DSCR の sticky ビットの消し方が違えば、アボートの印が残り続け、以後のメモリ操作がすべてアボートの誤りになる。
- **S9（gpio_parallel の検査）**: ドキュメントは data_channels を 2 本以上と書くが、`probe_testpoint.py` L169 は空でないことしか見ない（ParallelDecoder 側の検査は未確認）。
- **S10（_tp_pull_itm の重複除去）**: 記録していないとき、前回の読みの途中で増えた分を次回も読む（`ring_seen = written` は読む前の値）。同じ時刻のバイトが複数あると `at_last` の数え方で、重複の除去が 1 バイトずれうる。時刻が全部異なる通常の SWO では起きない。

## 調べた範囲 / 調べていない範囲

調べた: `arm_a_target.py` / `arm_debug.py` / `armv7a_debug.py` / `arm11_debug.py`（命令の符号化・DSCR・BCR/WCR のビット・DCC の手順を精読）、`raw_dap.py`、`jtag_chain.py`（ランダムなチェーンの走査 300 通りで IDCODE・IR 長を突き合わせ、問題なし。`shift_device` の位置計算も精読）、`sampler.py`（`swd_hz_for_rate` / `round_estimate` を 2 万通りでランダム試験し、要求の速さを満たす最小の値であることを確認。`plan_slots` の境界）、`xtrig.py`（CSR の番地・欄の幅を `generated.py` と突き合わせ）、`probe_xtrig.py` / `probe_xtrig_out.py`、`inject.py`、`rtt.py`（高速経路は `rtt.py` の poll の推測読み・まとめ読み・wr_hint。`rtt_speed` という名前のファイルは無い）、`probe_testpoint.py`（読んだだけ。試験は未作成）。

調べていない: 実機（HIL）、ARM11 の実機での DSCR / スキャンチェーンの挙動、`testpoint/` の下の解析・デコーダの中身、`probe_window` の窓の読み、CMSIS-DAP の JTAG シーケンスの pyOCD 側の実装、ARM11 側の step / ブレークポイントの偽の機器による突き合わせ（ARMA-01 の ARM11 版）。
