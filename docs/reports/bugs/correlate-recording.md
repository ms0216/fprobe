# 不具合台帳: correlate / recording / timesync / 条件の解釈（検出専用。修正は未適用）

調べた日: 2026-10-25。土台: integ/round1（2952479c）。重さ: A = 静かに誤った結果 / B = 失敗が見える / C = 端の条件・まれ / D = 品質。
再現試験は xfail(strict) で `host/tests/test_*_bugs.py`。直ったら XPASS で失敗する = マークを外す合図。

## 調べて問題が無かったもの（差分・基準実装との突き合わせ。ランダム）
- correlate: count / judged / preceded / followed / coverage を素朴な全探索（窓の端 e−c=window を含む、同じ時刻の複数イベント、重複時刻）と
  MCU・FPGA（1 ビット・列挙）で 150 組一致。ピン（1 かたまり・端数の tps）の rose/fell/changed の回数も 80 組一致。
- transactions（by / counts / effect の群ごとの回数・割合）と durations（changes・share の和 = 1）を基準実装と 100 組一致。
- Recording と SegmentedRecording の events（t0/t1/streams/kinds/limit/overlapping/a）・count_events・kind_counts・nearest・density・span・distinct を
  6 つのランダム記録 × 40 問い合わせで一致。decompose（縮図の分解）は 4000 組で重なり・隙間なし。timeline の _bins は 3000 組で条件を満たす。
- ConditionWatcher: changed / rose / fell だけの条件は、ランダムに刻んだ範囲で一度に調べたのと同じ時刻。

## 台帳

### BUG-CORR-01（B）'=' の項の変数が範囲に値を持たないと IndexError
- 再現: `test_correlate_bugs.py::test_state_term_on_variable_without_values_counts_zero`
- 原因の推定: `host/fprobe/correlate.py` `_holding`（numpy 経路、L309-313）。`vals[np.where(valid, idx, 0)]` は vals が空だと添字 0 が範囲外。
  numpy 無し経路は同じ入力で空を返す（経路で結果が違う）。
- 直し方の案（未適用）: `if not len(st): keep[:] = False; continue`。
- 影響: sample.names にあるが範囲に読みが無い変数を '=' で使うと correlate / transactions / ConditionWatcher.scan が落ちる。
  recording_trigger は state=error で見張りが止まる（めったに起きない不具合を待つ用途で、最初の範囲に読みが無いだけで終わる）。確信度: 高。

### BUG-CORR-02（A）ConditionWatcher の '=' の項が、範囲の最初の値が持ち越しの値と同じだと落ちる
- 再現: `test_watcher_equals_term_uses_value_carried_from_previous_range`（一度に調べると [15]、刻むと []）
- 原因の推定: `_Variables.series` L165。持ち越し seed は「範囲の最初の値が seed と違うとき」だけ先頭に入れる（入れると偽の変化になるため）。
  しかし値が同じときも `at(t)` のために前の値が要る。範囲の最初のイベントより前の時刻で `at` が None になり '=' が偽。
  ランダム試験で '=' を含む条件 27/120 組が不一致、changed/rose/fell のみは 0。
- 直し方の案（未適用）: seed を「変化の列」と別に持ち（`_Series.carry`）、`at` だけが使い、`instants` は使わない。
- 影響: recording_trigger が『ある状態のとき〇〇が変化』の条件を、観測の流れの刻み方で見逃す（静かに。記録を止める action=stop も効かない）。確信度: 高。

### BUG-CORR-03（A）読めなかった語（ack != 1）の欠けが「最初の失敗〜最後の失敗」の 1 区間になる
- 再現: `test_two_failed_reads_do_not_blank_out_everything_between`（失敗 2 回だけなのに judged = 0、durations は全部 excluded、share = None）
- 原因の推定: `correlate.py` `_mcu` L192-193 `{"reason": "read_failed", "t0": failed[0], "t1": failed[-1], "count": ...}`。
  `_holes` は t1 > t0 の欠けを丸ごと穴にするので、間の成功した読みも穴扱い。警告文は「欠けが 1 件・990 ms」と出るが
  『間は読めている』は伝わらない（事実と違う）。
- 直し方の案（未適用）: 失敗ごとに 1 件（t0 = t1 = 失敗の時刻、または直前の成功した読み〜次の成功した読みを穴に）。
- 影響: ack エラーが記録の両端にあるだけで、その変数を使う判定が全部 judged 外になる。値は空（None）で出るため気づきにくい。確信度: 高。

### BUG-CORR-04（C）引用符で囲んだ名前の中の " and " も区切りになる
- 再現: `test_and_inside_quoted_name_is_not_a_separator`。原因: `parse_condition` L51（コードに ponytail の既知コメントあり）。
- 影響: " and " を含む名前・値（`state="A and B"`）が書けない。docstring は「空白・= を含む名前は "…" で囲む」と言うので約束と食い違う。確信度: 高。

### BUG-CORR-05（C）'=' の値の先頭 0 の十進（08）を拒否
- 再現: `test_equals_value_with_leading_zero_is_decimal`。原因: `_target` L270 `int(text, 0)`（Python は 0 始まりの十進を禁止）。
  obs_value.number も同じ（`int(x, 0)`）なので FPGA 側の一致（compile_match）も同様。
- 影響: ConditionError で見えるので小さい。確信度: 高（仕様次第で D）。

### BUG-CORR-06（C）同じ名前が複数の番地にあっても曖昧にならない（後ろの番地に決まる）
- 再現: `test_duplicate_mcu_name_is_ambiguous`。原因: `_Variables.__init__` L113 の辞書内包表記で後勝ち。
- 影響: 名前の曖昧さは mcu / fpga / pin の間だけ検査される。同じ種類の中の重複は黙って片方を捨てる（静かに誤った変数）。確信度: 高。

### BUG-CORR-07（C）pins と channel_names で同じ名前が別のチャンネルを指しても黙って後勝ち
- 再現: `test_pin_name_conflict_between_pins_and_channel_names_is_ambiguous`。原因: `_Variables.__init__` L119-120。確信度: 高。

### BUG-OBS-01（A）count_condition の rose / fell が列挙の名前を常に「オン」とみなす
- 再現: `test_obs_condition_bugs.py::test_rose_on_enum_signal_from_zero_value`
- 原因の推定: `obs_value.is_on` L23-28（列挙の名前は 0 以外の値の名前とみなす、とコメントにある）。値 0 の名前（IDLE）も「オン」になり、
  IDLE→BUSY が rose にならず、BUSY→IDLE が fell にならない。count_condition は信号の定義を持たないので数に直せない。
  correlate の `state rose`（値 0→1 で成り立つ）と同じ式の意味が食い違う。
- 直し方の案（未適用）: fpga_obs_read の items に生の値（数）も持たせ、条件の評価は数で行う。または count_condition に FpgaSignals を渡す。
- 影響: fpga_obs_count の rose / fell 回数が、列挙の信号で静かに少なく（または 0）出る。確信度: 高。

### BUG-OBS-02（A）count_condition の `state=1` が列挙の名前（BUSY）と一致しない
- 再現: `test_equals_number_matches_enum_name_like_compile_match`（compile_match は value=1 に翻訳、数えた回数は 0）
- 原因の推定: `obs_value.same` L31-36。cur = "BUSY"（文字）と want = "1"（数）は number("BUSY") が None で文字比較 → 偽。
- 影響: 同じ式を FPGA に書くと一致し、ホストで数えると 0 になる（逆に `state=BUSY` は両方一致）。docstring の「同じ意味」に反する。確信度: 高。

### BUG-TS-01（C）fit_parent が子の時刻が全部同じ組で ValueError
- 再現: `test_timesync_bugs.py::test_fit_parent_with_identical_local_times_is_none`。原因: `timesync.fit_parent` → `fit_line` の ValueError（obs_decoder.fit_sync は握りつぶす）。
- 影響: 2 組未満は None なのに、同じ時刻の 2 組は例外で、呼び手が落ちる。確信度: 高。

### BUG-TS-02（C）interpolator が端に同じ子の時刻の組があると 0 除算
- 再現: `test_interpolator_with_duplicate_pair_at_the_end_does_not_divide_by_zero`。原因: `timesync.interpolator` L74-77（x1 == x0）。確信度: 高。

### BUG-REC-01（C）distinct の覚えが、最後でない行を消すだけの replace_derived で古くなる
- 再現: `test_recording_bugs.py::test_distinct_is_not_stale_after_replace_derived_removes_a_middle_row`
- 原因の推定: `recording.py` `distinct` L361-373。鍵 = MAX(rowid) だけで、削除（replace_derived の DELETE）を検出しない。
- 影響: 規則の検査し直しで違反が 0 件になっても distinct("kind") に違反の種類が残る（画面の絞り込み候補）。確信度: 高。

## 修正済み（2026-10-25。再現試験は xfail を外して通常の試験にした）
- BUG-CORR-01: 修正済み: `_holding` の numpy 経路も、値を 1 件も持たない変数は『どの時刻も一致しない』（numpy 無しと同じ）。
- BUG-CORR-02: 修正済み: 持ち越しの値が範囲の最初の値と同じなら、最初の値の時刻を持ち越しの時刻に置き換える（変化には数えず、'=' の `at` が前の値を引ける）。
- BUG-CORR-03: 修正済み: 読めなかった語は続くまとまりごとに 1 件（t0 = 前の読めた時刻、t1 = 次の読めた時刻、count = 件数）。間の成功した読みは穴にならない。
  試験の期待値は judged == 8（窓に失敗の欠けが掛かる効果 2 件だけ外れる。台帳の『>= 9』は数え間違い）。
- BUG-CORR-04: 修正済み: 区切り（and / かつ / &&）は引用符の外だけ。
- BUG-CORR-05: 修正済み: `obs_value.number` が先頭 0 の十進を十進として読む（correlate と compile_match で共通）。
- BUG-CORR-06 / 07: 修正済み: `_Variables.dups` に記録し、同じ名前が別の番地・チャンネルを指すと resolve が ConditionError（自動の候補からは外す）。
- BUG-OBS-01 / 02: 修正済み: `count_condition(items, condition, signals=None)`。signals（信号の定義）を渡すと列挙の名前を定義の表（名前 -> 値）で
  数に直して比べる（IDLE = 0 は『オフ』、state=1 と state=BUSY は同じ値に一致）。fpga_obs_count は定義を渡す。3 つの意味（compile_match・
  count_condition・correlate）は『数で決まる』にそろった。signals を渡さない呼び出しは従来どおり名前を『オン』とみなす（表が無いので数に直せない）。
- BUG-TS-01: 修正済み: 子の時刻が 2 種類未満なら fit_parent は None。
- BUG-TS-02: 修正済み: interpolator は同じ子の時刻の組を親の時刻の平均 1 点にまとめてから内挿（違う子の時刻が 2 点未満なら ValueError）。
- BUG-REC-01: 修正済み: replace_derived が distinct の覚えを捨てる。

## 疑い（再現試験なし・未確認）
- S-01（C）`_pin`（correlate.py L228-265）: 範囲の頭 lo にちょうどある辺（端数の tps なら lo に丸まる辺も）は edges_between が排他的（s0 < e）なため
  『最初の値』に吸収され、変化に数えない（MCU / FPGA は『前の値が範囲に無いので数えない』設計だがピンは前の値が分かる）。
  逆に 端数の tps では hi に丸まる辺（時刻 == hi）が範囲に入る。範囲の端の数え方がソースで食い違う。
- S-02（C）`_pin`: チャンネル数が少ないかたまり（`channel >= chunk.channels`）は `pins_missing` の欠けに数えられず、値の無い区間が黙って「値が変わらない」になる。
- S-03（C）`_merge_same`（自動の候補）: 成立時刻が同じ候補を 1 つにするが、変数が違うと欠け（holes）が違うので judged / coverage は同じとは限らない。残るのは先頭の候補の数字。
- S-04（D）`transactions` の effect_lag_s: effect_offset_s は lag で前にずらした時刻で測る（本当の時刻との差から lag を引いた値）。
- S-05（C）`SegmentedRecording.nearest`: 同じ時刻の出来事が複数の分割にあるとき、Recording.nearest（rowid が後のもの）と選ぶものが違いうる（差分試験では同時刻の衝突が起きなかった）。
- S-06（D）`Recorder.__init__` は既存ディレクトリを開き直すと meta.json を渡された meta で上書きする（追記の再オープンの経路は製品に無いので影響なし）。
- 原則違反の疑い: correlate / recording / timesync に経験則の閾値・推定による判定は見つからなかった（`_Series.period` は中央値だが事実の報告のみ）。

## 調べていない範囲
- 実機・合成・HIL。probe_recording の MCP 周り（recording_start / stop の並行動作）と system_status の実機側。
- recording_long の間引き・容量による削除（decimate / max_bytes）後の読み、再オープン後の追記。timeline_rows の段の規則（classify / summarize）の中身。
- recording_stream / recording_sql / VCD 書き出し。numpy 無し経路の全般（_holding 以外）。
