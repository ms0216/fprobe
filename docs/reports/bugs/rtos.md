# RTOS・ヒープ解析の不具合台帳（検出専用。修正はしていない）

領域: host/fprobe/rtos/、probe_rtos.py、heap.py。再現試験は host/tests/test_rtos_bugs.py（strict の xfail。直ると
XPASS で失敗して気づく）。分類: A = 静かに誤った結果、B = 失敗が見える、C = 端の条件・まれ、D = 品質。

調べた手法: 精読、Intervals.overlap・timeline_from_samples・timeline_from_events の乱数入力と基準モデル／不変条件
（区間の重なり・範囲・抜けとの排他・切り替えの数）の突き合わせ（違反なし）、同じ名前・優先度 0・短い読み・抜けをまたぐ
入力などの境界値。

## 再現済み

### BUG-RTOS-01 (A) run_summary は同じ名前の別タスクの一方を消す
- 再現: test_run_summary_same_name_tasks_both_kept
- 原因: rtos/analysis.py の run_summary 末尾 `out[task] = row`（per は handle ごとに集めるが、出力は名前をキーにして上書き）。
- 直し方の案: キーを handle にするか、重複を区別し、重複があることを事実として返す。
- 影響: 同じ名前のタスク（SMP のアイドル、同名のワーカー）の周期・実行時間が片方だけになり、人も AI も気づけない。
- 確信度: 再現済み

### BUG-RTOS-02 (A) load_series の tasks は同じ名前のタスクの割合を上書きで失う
- 再現: test_load_series_same_name_tasks_shares_add_up
- 原因: rtos/analysis.py の load_series `"tasks": {names[h]: v / covered ...}`（名前キーの辞書内包で後勝ち）。
- 直し方の案: handle をキーにするか、名前が重なるとき区別する。
- 影響: 窓ごとの内訳の合計が負荷より小さく見える（欠けた分の説明が無い）。
- 確信度: 再現済み

### BUG-RTOS-03 (A) execution_profile／Timeline.stats が名前で別タスクを束ねる
- 再現: test_execution_profile_same_name_tasks_separate_rows
- 原因: rtos/analysis.py execution_profile の `threads.setdefault(r.task, ...)`（handle・idle は先頭の run のもの）。
  同根: rtos/timeline.py Timeline.stats() の `per.setdefault(r.task, ...)`（handle は runs[0]）、analysis.job_summary も名前で束ねる。
- 直し方の案: handle キー（表示名は別に付ける）。
- 影響: 別タスクの CPU 時間が 1 行に混ざり、idle の印も先頭の run のもので決まる。
- 確信度: 再現済み

### BUG-RTOS-04 (A) inversions_from_ops が urgency 0 の待ち手を最低に扱う
- 再現: test_inversions_from_ops_order_minus1_priority0_is_top_waiter
- 原因: rtos/flow.py evaluate の `max(st.waiters, key=lambda w: urgency(w) or -math.inf)`。`or` が 0 を偽として -inf にする。
  order=-1（Zephyr・ThreadX・µC/OS の「小さい方が高い」）では優先度 0（最高）の待ち手が最低に扱われ、逆転の区間が落ちる。
- 直し方の案: None かどうかだけで分ける（`-inf if u is None else u`）。
- 影響: 現在のトレースフックは FreeRTOS（order=1）だけなので実害は出にくいが、他の RTOS にフックを足すと静かに誤る。
- 確信度: 再現済み

### BUG-RTOS-05 (C) 打ち切りのジョブの exec_time が blocked のジョブと定義が違う
- 再現: test_truncated_job_exec_time_same_definition_as_blocked
- 原因: rtos/analysis.py _jobs_from_events の末尾（truncated）が `own = sum(r.duration ...)`。release より前の時間を含み、割り込み
  （isr_iv）を引かない。blocked 側は release で切り、isr を引く。
- 直し方の案: blocked と同じ計算にそろえる。
- 影響: truncated は統計に入らないが、find_jobs／to_dict の結果で見える exec が過大。
- 確信度: 再現済み

### BUG-RTOS-06 (A) 抜けをまたぐジョブが blocked のまま統計に入る
- 再現: test_job_across_gap_is_truncated
- 原因: rtos/analysis.py _jobs_from_events は tl.gaps を見ない（Job の docstring は truncated = 抜け・観測の端で終わりが分からない）。
  抜けの間の出来事が欠けていても、前後の run をつないで 1 ジョブにして exec／wall／response を出す。
- 直し方の案: [release, 終わり] が tl.gaps と重なれば truncated にして統計から外す。
- 影響: ITM のあふれの後の応答時間が外れ値になり、最悪応答時間の判断を誤らせる。
- 確信度: 再現済み

### BUG-RTOS-07 (C) memory_series は複数のヒープの slot を 1 つに上書きする
- 再現: test_memory_series_two_heap_slots_kept
- 原因: rtos/analysis.py memory_series `out["heap"] = {...}`（heap の slot ごとに代入）。スタックは名前キーの辞書なのにヒープは
  1 つだけ。副次: 行が無いとき `points_width` が応答に無い（形が違う、D）。
- 直し方の案: heap を名前をキーにした辞書にする。
- 影響: 複数ヒープ（Zephyr の複数 k_heap、heap_5 の領域別）の一方が見えない。
- 確信度: 再現済み

### BUG-RTOS-08 (C) blocking の objects が同じ名前の別オブジェクトを束ねる
- 再現: test_blocking_same_name_objects_separate
- 原因: rtos/flow.py blocking `by_object.setdefault(w.on, ...)`（on はオブジェクト名）。同根: communication_flow の辺のキーが
  名前、queue_usage の出力キー `_object_name` も同名で上書き。
- 影響: 名前が重なるキュー・セマフォの統計が混ざる／消える。
- 確信度: 再現済み

### BUG-RTOS-09 (C) untouched_bytes は短い読みを要求の長さとして数える
- 再現: test_untouched_bytes_short_read_not_overcounted
- 原因: rtos/stackcheck.py untouched_bytes の `count += n`（実際に返ったバイト数でない）。
- 影響: 現在の読み手は短く返さず AccessError にするので実害は小さい（境界の保護）。
- 確信度: 再現済み

### BUG-RTOS-10 (D) AllocationTracker.report は top に負を受け付けて末尾を欠く
- 再現: test_report_negative_top_rejected
- 原因: heap.py report の `sorted(...)[:top]`。series_points／live_points は検証するが top は検証しない。
- 影響: top=-1 で最小の 1 件が黙って落ちる（truncated は True になるので気づける余地はある）。
- 確信度: 再現済み

## 疑い（再現していない）

- SUSPECT-RTOS-11 (C) rtos/freertos.py tasks(): pending ready リストの `where[handle] = "pending"` は add() と違い handle==0 を
  除かない。pvOwner が 0 の壊れた項目で番地 0 の TCB を読みに行く。再現には壊れた TCB の像が要り、組んでいない。
- SUSPECT-RTOS-12 (D) rtos/zephyr.py _stack(): stack_info が無いとき base=0 で check_stack に渡し、番地 0 付近を「スタック」として
  読む（fill が誤る可能性）。stack_info の無い構成の ELF で確認していない。
- SUSPECT-RTOS-13 (D) rtos/flow.py _dot(): ノード名のダブルクォートをエスケープしない（名前に " があると DOT が壊れる）。
- SUSPECT-RTOS-14 (C) rtos/freertos.py _stack(): 走行中のタスクの SP を psp で読む。MSP の ISR 内で止まったとき psp は実際の SP と
  違う。実機で未確認。
- SUSPECT-RTOS-15 (D) rtos/timeline.py timeline_from_events: 末尾が overflow／lost のとき長さ 0 の Gap(t1, t1) を作り、stats の
  gaps に数える。無害だが数が 1 多い。

## 調べていない範囲

- nuttx.py・threadx.py・ucos3.py・rtx5.py・zephyr.py の細部（精読は rtx5 の状態表と zephyr の状態判定のみ）、*_objects.py の
  各オブジェクトの読み、objects.py（型による探索）。
- probe_rtos.py の時間軸の開始・停止の処理（実機・サンプラが要る）、probe_heap 周り。
- heap.py の scan（FreeRTOS heap_4/5・nano・dlmalloc）の実メモリ像との突き合わせ（既存の heap_fakes に頼る。精読のみ）。
  AllocationTracker は精読のみ（乱数での基準モデル突き合わせはしていない）。
