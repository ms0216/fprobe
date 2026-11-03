# Linux の全コア停止デバッグを、今あるデバッガの画面につなぐ（2026-11-03）

指示書: `docs/superpowers/specs/2026-11-03-cloud-sessions-plan.md` §J。前の設計: `2026-11-03-linux-halt-ai-tools-design.md`
（`linux.halt.debug.*` と `HaltDebugger`）。設計原則: CLAUDE.md の「画面は考えない層」「どの機能も人（画面）と AI（MCP）の
両方から同じに使え、同じに見える」。

進み具合: 第 1 段（294db556）・実機の直し（f39be408・ecfc7a3a）・第 2 段（この版。§9 に第 2 段で決めたことをまとめる。
§3・§4 の古い書き方より §9 が正）。

## 1. 目的と、今までの穴

`linux.halt.debug.*`（中身は `fprobe/linux_halt/debugger.py` の `HaltDebugger`）は、今あるデバッガの画面（ソース・
ブレークポイント・止まった場所・レジスタ・呼び出し履歴・変数・ウォッチ・逆アセンブル）と一度もつながっていなかった。
AI が MCP で Pi 2B のプロセスを止めて両スレッドを読んでも人の画面には何も出ず、人は画面からその操作ができなかった。

画面の共通の状態 `host/ui/src/lib/debugger.ts` の `Debugger` は、`session.status`・通知 `target.state` の target（state
`HALTED`・pc・file・line・run）と、マイコンのデバッガの API（下の表）だけを読む。この設計は、**画面を作り直さずに**、
Linux のプロセスに attach している間、デーモンのそれらの API が**同じ名前・同じ引数・同じ応答の形**で Linux のデバッグから
答えるようにする（作り方 a）。

## 2. 道の選び方（事実で）

- 「今の対象が Linux のプロセスか」は、`LinuxHaltService` が持つ `HaltSession` に開いた `HaltDebugger` があり、閉じて
  いないこと（`linux.halt.debug.attach` から `linux.halt.debug.close` / `linux.halt.close` まで）。推定ではなく持ち主の有無。
- その間、`target.status`（したがって `session.status` の target と通知 `target.state`）は `linux` 欄にその事実（pid・
  comm・今のスレッド・スレッドの一覧・最後の停止の種類・ELF・読み込み位置）を返す。画面はこの値で描くだけ（判断を持たない）。
- 実装の置き場所: `fprobe/linux_halt/view.py` の `LinuxDebugView`（`HaltDebugger` をマイコンと同じ API の形で見せる層）。
  各サービス（`probe_target.py`・`probe_debug.py`・`probe_breakpoints.py`・`probe_inspect.py`・`probe_execution.py`・
  `probe_elf_watch.py`）の該当メソッドの入口で、`_linux_view()`（`LinuxHaltService` が SHARED に出す）が層を返せば
  そこへ渡す。名前・引数は変えない。各サービスは `_linux_view` を NEEDS に並べる（probe_service の決まり）。
- 層は `HaltDebugger` の公開のメソッドだけを使う（足したもの: `use`・`current_tid`・`resolve`・`step_instructions`・
  `poll`・`status_facts`。§6）。

## 3. API ごとの答え方

「今のスレッド」は、最後の停止（`last_stop`）の tid がプロセスのスレッドならそれ、無ければ `Engine.default_tid()`
（対象のスレッドを走らせている最小番号のコア、無ければリーダー。規則であって判断ではない）。`target.status` の
`linux.tid` に出す。レジスタ・呼び出し履歴・変数・式・ステップはこのスレッドで。

| API | Linux のときの答え | 合わない欄 |
|---|---|---|
| `target.status` | state = `RUNNING`（全コアを走らせている）/ `HALTED`。止まっていれば今のスレッドの pc・where・function・file・line（ELF の外なら pc と where = 番地だけ）、`linux` 欄 | `target`= `"linux"`。halt_reason は停止の種類が同じ事実のときだけ（swbreak・hwbreak → breakpoint、watch → watchpoint、int・step・attach → request）。それ以外は欠く（`linux.stop_kind` に元の種類） |
| `target.halt` | 走っていれば `HaltDebugger.halt()`。止まっていれば何もしない（マイコンと同じく状態を返す） | `core` を渡したら断る |
| `target.resume` | `HaltDebugger.run(wait_s=0)`（再開だけ。待たない。すぐの当たりは拾う） | `core` を渡したら断る |
| `target.step` | 今のスレッドを 1 命令ずつ count 回（`step_instructions`）。途中で別の止まり（当たり・終了）なら打ち切り、steps_done | — |
| `debug.step` | mode over / into → `HaltDebugger.step`（今のスレッド。上限は §4.2）。out / until は断る（理由: `HaltDebugger` に無い） | `mask_interrupts` は DHCSR の話で Linux の道には当てはまらない（使わない。仕様の説明に書く）。runs は数えていないので欠く |
| `target.registers.read` | 今のスレッドの r0〜r12・sp・lr・pc・cpsr（`registers_from` = core / pt_regs は `linux.registers_from`） | names に無いレジスタは断る |
| `debug.backtrace` | 今のスレッドの CFI の巻き戻し（`Inspector`。load bias を通す） | — |
| `debug.frame_variables` | 今のスレッドの frame の引数・局所変数 | — |
| `debug.evaluate` | 今のスレッドの frame で C の式。`live = true` は断る（走らせたままプロセスのメモリを読む道は無い） | — |
| `debug.expression.check` | ELF の DWARF だけで調べる（番地は実行時 = リンク時 + load bias） | — |
| `debug.type_info` | ELF の DWARF だけ | — |
| `debug.symbol.info` | 場所 ↔ 実行時の番地（リンク時 + load bias） | — |
| `debug.disasm.around` | プロセスの仮想メモリ（置いた BKPT は元の命令で見せる）を ELF の $a / $t / $d で区切って読む。番地は実行時 | memory = `"target"`。 命令の中の名前（target_name・literal_name）はリンク時の番地で引く（PIE では ldr の値の名前は合わないことがある: §8） |
| `debug.breakpoint.set` | `HaltDebugger.break_insert(location, kind)`。kind は attach の `breakpoint_kind`（§4.3）。止まっているときだけ | condition・ignore_count・log・message を渡したら断る |
| `debug.breakpoint.clear` | location（置いたときの文字列か、同じ番地に解ける場所）を外す。省略で全部 | — |
| `debug.breakpoint.enable` | 無効 = 外して場所と種類を覚える、有効 = 同じ場所・種類で置き直す（マイコンと同じ） | — |
| `debug.breakpoint.list` | 置いたもの（kind = sw / hw）と無効にしたもの | watchpoints は `[]`（Linux の見張りは今は画面の道に無い） |
| `debug.source.list` | ELF の行表のソース（§5） | — |
| `debug.source.read` | 本文と命令のある行（§5） | — |
| `debug.elf.status` | path = attach の ELF、loaded_at = attach の時刻、file_changed = attach の後にファイルが変わったか | watching・auto_reload = false、interval_s = 0、target_image = `"unknown"`（プロセスの像と ELF の一致は `linux.halt.debug.status` の image の事実） |

失敗は例外（デーモンが誤りの応答にする）。マイコンの道と同じく、画面の自動の読みは誤りを黙って捨てる。

## 4. 実行・ステップ・ブレークポイントの上限と選び方

### 4.1 実行（Run）と、走っている間に画面が止まらないこと

デーモンは機器の操作を 1 本の作業スレッドで順に処理する（`api/arbiter.py`）。`linux.halt.debug.run(wait_s)` は wait_s 秒
その作業スレッドを使い、その間は画面・AI の呼び出し（停止のボタンも）が待たされる。そこで画面の道では長く待たない:

- `target.resume` = `run(wait_s=0)`: 再開して、すぐ返る（0 は「待たない」という明示の値で、暗黙の上限ではない）。
- 止まったことは、デーモンの見張り（`watch_s` ごとと、操作の後に `target.status` を読む）が拾う。走っている間の
  `target.status` は `HaltDebugger.poll()`（待たない 1 回の確かめ = `Engine.run(0)` の続き）を呼ぶ。当たりがあれば全コアを
  止めた状態（HALTED）を返し、無ければ RUNNING。対象外の当たり（別のプロセスが共有のブレークポイントに当たった）は
  `run` と同じく踏み越えて走らせ続ける（その数と中身は poll の `last_run.while_running`）。
- だから走っている間も作業スレッドは 1 回 20 ms 程度しか使われず、停止のボタン（`target.halt`）・他の読みはすぐ通る。
- 待つ時間の上限は無い（マイコンと同じ: 当たるか、人・AI が止めるまで走る）。暗黙の上限を作らないので、画面に入力欄は
  要らない。AI が上限つきで待ちたいときは従来どおり `linux.halt.debug.run(wait_s)`（明示）。
- poll は `HaltDebugger` の「止めたまま放置しない」見張り（max_halt_s）を数え直さない（画面を開いているだけで見張りが
  効かなくならない）。止まっている間の `target.status` は事実を読むだけ（`status_facts`）。

### 4.2 1 行のステップ（Step Over / Into）の上限

`linux.halt.debug.step` は `max_instructions`（命令の数）を使う側が明示する。画面の `debug.step` の引数は
`mode`・`location`・`timeout_s`・`mask_interrupts` で、命令の数の欄が無い。名前・引数を変えないため、Linux の道は
`timeout_s`（時間の上限。マイコンの `debug.step` と同じ引数・同じ意味）を上限にする: `HaltDebugger.step` に
`timeout_s`（任意）を足し、`max_instructions` を任意（None = 数の上限なし。時間の上限が要る）にする。時間を過ぎたら
その命令で止めて reason = `"timeout"`（`reached_new_line` = false と同じ事実）。命令の数を時間から推さない。

- 残る制約: 呼び出しの踏み越え（`Engine.run_to`）は時間の上限を持たない（Engine の今の作り。走ったまま返す道が
  `run_to` に無い）。眠る呼び出しを踏み越えると、戻るまで作業スレッドを使う（§8）。

### 4.3 ブレークポイントの種類

`linux.halt.debug.break.insert` は kind（sw / hw）を使う側が明示する。画面の `debug.breakpoint.set` は kind を渡さない。
Linux では sw（対象のページに BKPT。同じページを共有する別のプロセスも当たる）と hw（BRP の数に限り）は性質が違い、
どちらを使うかは使う側の判断なので、本体に既定の選び方を入れない。`linux.halt.debug.attach` に任意の
`breakpoint_kind`（sw / hw）を足し、画面の道（`debug.breakpoint.set`・有効に戻す）はそれを使う。渡していなければ
「attach の breakpoint_kind を渡していない」と断る。画面の attach の欄（`LinuxHaltPanel`）は空から選ぶ。

## 5. ソースの本文

Linux のアプリは対象の上でビルドされ、ソースが PC に無いことが多い（例: Pi の `/tmp/race2.c`）。

- **取り寄せは attach のとき（コアを止める前）だけ。** 全コアを止めている間の対象は ssh に答えない（2026-11-03 実機:
  止めてから取りに行き `ssh: connect ... Connection timed out`）。`fetch_from_target` を渡した attach は、ELF
  （`/proc/<pid>/exe`）を写すのと同じ時に、行表が指すソースのうちこの PC に無いものを、`linux.core.fetch` と同じ道
  （ssh の `cat`）で `<ELF と同じ所>/<ELF の名前>.sources/<対象の上の道>` に写す（`view.prefetch_sources`）。1 つの失敗で
  他をやめない。attach の結果の `sources_fetched` に、名前ごとの写し（target・path・local_path・bytes・sha256）か error。
- `debug.source.list` の項に `fetched`（写し）か `fetch_error`（取り寄せられなかった理由）。`exists` はこの PC の行表の
  場所にあるかの事実のまま。
- `debug.source.read` は、PC にあればそれ、無ければ attach の写し（`fetched` を添える）。止まっている間は取りに行かない。
  写しが無ければ理由を誤りにする（止まっている間は取れないので attach に fetch_from_target を渡す / attach のときに
  取り寄せられなかった理由）。
- 画面は `exists` か `fetched` がある項を開ける（`openFile` の条件。値は API の事実）。

## 6. HaltDebugger に足すもの

| メソッド | 中身 |
|---|---|
| `use(fn, *, halted)` | 外の読み手（view）が、API の 1 回の呼び出しとして fn を走らせる（`_call` と同じ: 見張りと交互、閉じていない・止まっていることの確かめ、止めたままの時間を数え直す） |
| `status_facts()` | 錠の中で事実だけを読む（数え直さない）: running・closed・今のスレッド・last_stop・threads_brief |
| `current_tid()` | §3 の規則 |
| `resolve(location)` | `break_insert` の場所の解き方（番地・`ファイル:行`・関数名 = 前置きの後）を切り出したもの（view の clear・enable が同じ番地を引く） |
| `step_instructions(tid, count)` | 1 命令ずつ count 回（途中の別の止まりで打ち切る）。止めの事実と steps_done |
| `poll()` | 走っていれば `Engine.run(0)` の 1 回。止まれば last_run・last_stop に入れて返す（while_running はその run を始めた再開からの増分）。走っていなければ None |
| `step(..., timeout_s=None)` | §4.2 |

## 7. 画面（考えない層のまま）

- `debugger.ts` の読み直しの合図に `linux.halt.debug.*` を足す（AI が MCP で操作しても人の画面が同じに変わる）:
  - ELF_METHODS（ソースの一覧を読み直す）: `linux.halt.debug.attach`・`linux.halt.debug.close`・`linux.halt.close`
  - BREAK_METHODS（ブレークポイントを読み直す）: 上の 3 つと `linux.halt.debug.break.insert`・`linux.halt.debug.break.remove`
  - 止まった場所・レジスタ・変数は今と同じく `target.state` の run・pc で読み直す。デーモンの RUN_METHODS（コアを動かし
    得る操作。終わるたびに run を進める）に `linux.halt.debug.attach`・`run`・`halt`・`step`・`close`・`linux.halt.close`
    を足す。
- ソースの項は `exists || fetched` なら開く。
- 下の帯（StatusLine）とツールバーに、`target.linux` があれば「Linux comm pid tid」を出す。`system.status` の mcu は
  `target.status`（linux 欄）と、addresses の語をプロセスの仮想メモリから（止まっているとき）返す。
- `debug.elf.status` は attach の ELF（ソースの画面の空の判定・ELF の名前）。
- ツールバーの止まった場所の横に、`target.linux` があれば「pid comm · tid」を出す（止まったスレッド。値は API のまま）。
- `LinuxHaltPanel` に attach の欄（pid・elf・skew_limit_ns・thread_id・kernel_isa・max_step_retries・bp_unmapped・
  max_halt_s・breakpoint_kind・fetch_from_target。既定なし: 空なら送らない）と close を足す（人も画面から AI と同じ
  attach ができる）。

## 8. 試験と残し

試験（TDD）:
1. Python（`tests/test_linux_halt_view.py`）: 本物のデーモンと Probe、2 スレッドの競合の模型（`RaceWorld`・`FakeCores`）で
   attach した状態で、§3 の API が Linux の事実を同じ形で返す（応答の照合 `api_result_check` を通る）。ソースを対象から
   取り寄せる（偽の ssh）。target.resume が待たずに返り、見張りの target.status が当たりを拾う。人（gui）と AI が同じ事実。
2. `HaltDebugger` の足したもの（`tests/linux_halt/test_debugger.py`）。
3. 画面（vitest `tests/debugger.test.ts`）: Linux の target と応答で、ソースの強調・印・変数が変わる。行のクリックで
   `debug.breakpoint.set`。AI の `linux.halt.debug.*` の activity で読み直す。

残し（この設計の外。主担当の判断）:
- 呼び出しの踏み越え（`Engine.run_to`）の時間の上限（§4.2）。
- 画面からスレッドを選ぶ（今のスレッドは規則で 1 つ。Threads のパネルは RTOS の rtos.tasks で、Linux の全スレッドは
  `LinuxHaltPanel`・`linux.halt.debug.threads`）。
- 走っている間のブレークポイントの置き換え（`HaltDebugger` は止まっているときだけ書く）、ウォッチポイント、ライブ ウォッチ。
- PIE の逆アセンブルの、命令の中の名前（リンク時の番地で引く）。Thumb のコードは M の版の解読器（capstone の MCLASS）で読む。


## 9. 第 2 段で決めたこと（2026-11-03。主担当の点検 A1・A2・C1・C2・C3・C5 を含む）

### 9.1 画面が呼ぶ API の残り（A1）
attach の間、次も `LinuxDebugView` が答え、マイコンの道へ落ちない: `target.resume`（`run(0)`。待たない）・`target.halt`・
`target.step`・`debug.step`（over / into / out / until + location = Run to Cursor）・`debug.evaluate`（live は断る）・
`debug.expression.check`・`debug.type_info`・`debug.symbol.info`（実行時の番地）・`debug.disasm.around`（プロセスの仮想メモリ。
置いた BKPT は元の命令）・`debug.breakpoint.enable`・`target.registers.write`・`debug.write_expression`。
使えないものは「Linux の…」の理由で断る: `target.reset`・`debug.run_until_halt`（時間切れで止める既定を持つ）・
`target.memory.view`（linux.halt.debug.memory）・`debug.watchpoint.*`・`watch.write`（走らせたまま書けない）。

### 9.2 上限は使う側が明示（暗黙の既定なし）
- `linux.halt.debug.step` に必須の `timeout_s`。`Engine.run_to`・`Engine.step` に deadline を足し、呼び出しの踏み越え・
  カーネルから戻るのを待つ間も効く。過ぎたら全コアを止め、一時的なブレークポイントを外して `timed_out = true`（止めたまま）。
- 画面の道（`debug.step`・`target.step`・Run to Cursor）は上限の引数を持たないので、attach の任意の
  `step_max_instructions`・`step_timeout_s` を使う（渡さなければ断る）。`debug.step` の `timeout_s`（マイコンの既定 5.0）・
  `mask_interrupts` は使わない。reason は step / timeout / max_instructions / breakpoint / watchpoint 等。

### 9.3 長い待ちで作業スレッド・見張りを塞がない（C1）
- `HaltDebugger.run(wait_s)` は `RUN_SLICE_S`（50 ms）ごとに錠を放す。止める要求（`request_halt`。Engine の interrupted）が
  立てば待ちの中で全コアを止めて返す（kind = int）。待つ間にほかの道（poll・halt）が止めればその止まりを返す。
- デーモン: `target.halt`・`linux.halt.debug.halt` は受けた時点で `backend.request_halt_now()` を呼ぶ（作業スレッドの順番を
  待たない。一時停止で保留される要求は除く）。見張りは `backend.target_status_unqueued()`（attach の間の状態。作業スレッドを
  通さない）を先に読む。だから AI の長い `linux.halt.debug.run` の間も、人の停止が効き、target.state（RUNNING）が配られる。

### 9.4 スレッド（A2）
- `linux.halt.debug.select(tid)`: 今のスレッドを選ぶ（次の止まりまで）。`target.status` の `linux.tid_from` が
  selected / last_stop / rule。RUN_METHODS に入れる（画面が読み直す）。
- Threads のパネル: `target.linux` があれば Linux のスレッド（`linux.halt.debug.threads` の値のまま）。行を押すと select。
- LinuxHaltPanel に attach の欄（必須の欄は空から。画面の道の種類・上限・取り寄せ元は書いたときだけ送る）と close。

### 9.5 止まっている間の linux.*（C2）
- `linux.connect` の ssh を `GuardedSsh` で包み、エージェントの要求にも同じ確かめを入れる: 対象が linux.halt で止まっていると
  分かれば（attach の `fetch_from_target` に渡した対象で、全コアを止めている間）、ssh・エージェントを叩かずに `TargetHalted`
  （事実の文）。止まった対象には ConnectTimeout（10 秒）まで塞いでから失敗していた。ほかの対象との対応は知らない（推さない）。
- `linux.disconnect` は対象の上の後始末を叩かず、`remote_cleanup_skipped` に理由。背景の sync・ring.watch は止まっている間は
  休み、走り出したら続ける（前は最初の失敗で黙って止まった）。

### 9.6 錠を長く持つ操作と max_halt_s（C3）
attach で全コアを止めている間、ロジアナの取り込み・流し（la.capture / collect / deep.capture / deep.collect / stream）は、
その上限（timeout_s・duration_s）が放置しない見張りの残り（`HaltDebugger.halt_budget_left`）より長ければ断る（見張りが錠を
取れず、全コアを止めたままになる）。事実の比較だけ。

### 9.7 閉じた後の古い値・AI が開くパネル
- 画面: target.state が DISCONNECTED になったら、レジスタ・呼び出し履歴・変数・逆アセンブル・ウォッチの値を空にする
  （マイコンの切断も同じ。走り出しただけなら今までどおり古い印付きで残す）。
- `ui.state.set` の `panel`（registry の id。仕様の enum と画面の試験で照らす）: 人の画面がそのパネルを開いて前に出す
  （View メニューと同じ）。状態の `panel = {id, seq}`、頼まれるたびに seq が増える。

### 9.8 試験の偽物（C5）
- 偽の取り寄せ（`_fetcher`）は、全コアを止めている間は実機と同じく ssh が答えない（Connection timed out）。
- 偽の ssh（`linux_fake_ssh.py`）と linux.halt を組んだ試験: 止まっている間は ssh を叩かない（記録に命令が残らない）。

### 9.9 鍵は 1 つ（2026-11-03 実機の行き詰まりの直し）
- 実機で行き詰まった: 作業スレッドは system.status（機器の錠を持つ）→ target.status → poll で HaltDebugger の Condition を
  待ち、作業スレッドを通さない読み（9.3）は Condition を持ったまま get_regs で機器の錠を待った（取る順番が逆）。放置しない
  見張りも同じ鍵で止まり、Pi の全コアが止まったまま残った。
- 決まり: HaltDebugger の Condition は**機器の錠そのもの**（HaltSession の lock = Probe の `_lock` の RLock）の上に作る。
  API の呼び出し・見張り・コアの 1 回ずつの操作・ほかのサービスの機器の操作は同じ 1 つの鍵を取る（順番の取り違えが
  起こり得ない）。鍵を取らない読みは `HaltDebugger.snapshot()`（鍵の中で作り直す、最後に分かった事実の写し）だけ。
- 作業スレッドを通さない読み（`LinuxDebugView.target_status_unqueued`）は、鍵がすぐ取れれば普通の読み（poll も）、取れ
  なければ待たずに写しから作る。
- 見張りは機器を使うので、機器の錠を持ち続ける操作の間は待つ（長いものは 9.6 で断る）。放されたらすぐ走らせる。
- 試験: 実機のような遅れ（2 ms）をコアの口に入れ、作業スレッドの target.status・system.status・作業スレッドを通さない読み・
  debug.run の待ち・停止・続行・呼び出し履歴を 3 秒同時に回し、時間内に終わる。錠を 1 秒持つ間も写しは読め、放せば見張りが
  走らせる。
- デーモンの見張りは、コアを動かし得る操作（RUN_METHODS）の後は、target.state の購読が無くても読み直す（実機: close の直後の
  session.status の target が HALTED のまま）。

### 9.10 操作の直後の状態・長い操作の間の状態の読み（2026-11-03 実機）
- コアを動かし得る操作（RUN_METHODS。linux.halt.debug.select も）は、応答の前に見張りの状態を読み直す: 直後の
  session.status がその操作の結果を返す（前は select の応答が 4459 なのに直後の target.linux.tid が 4458 だった）。
- 状態の読み（target.status・system.status）は、Linux のプロセスに attach している・attach が進行中の間、作業スレッドを
  通さない（デーモンの UNQUEUED_READS → backend.read_unqueued。機器の錠だけを短く取る）。attach の取り寄せ（約 4 秒）・
  長い run の後ろに並ばない。attach が進行中は target.state が `{state: DISCONNECTED, linux_attaching: {pid, since_unix_s}}`、
  target.status は待たずに NotConnected（attach が進行中の事実）、system.status の mcu は `connected: false` と linux_attaching。

### 9.11 残し
- SimCores の既定（`memory_needs_halt` = False・wait_event がすぐ返る）を実機に寄せること（点検 C5 の残り）。
- 対象の ssh のターゲットと linux.halt の対応は、attach の fetch_from_target のときだけ分かる（halt_all だけのときは知らない）。
- PIE の逆アセンブルの命令の中の名前（リンク時の番地で引く）・Thumb の解読器（M の版）。
