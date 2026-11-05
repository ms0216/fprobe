# 使い方の落とし穴（ヒント）

<!-- 自動生成ファイル。直接編集しないこと（python -m fprobe.api gen で再生成）。生成元: host/api/hints.json -->

使い方の落とし穴の記録。診断ではない: 症状に当てはまりうる『確かめる事実』と、それを読む API を並べるだけ（判断は使う側）。実機・アクティビティの失敗の事実から足す（2026-11-05〜）。session.hints で引く。 when があるヒントは、失敗の種類が合うときだけ失敗の応答の hint_ids に付く（code = invalid_params のとき validator = 検証の種類、device のとき exception = 例外の型）。

API では `session.hints`（`method` でそのメソッドに付いたヒント、`id` で 1 件）。失敗の応答の `data.hint_ids` にも id が出る。画面は Activity パネルでエラーの行を選ぶと出す。

## Pi 2B で linux.halt.open が JTAG-DP の IDCODE を見つけられない

`linux_halt.open.idcode_not_seen` — 付いたメソッド: `linux.halt.open`, `linux.halt.debug.attach`

**症状**: ArmDebugError: チェーンに Arm の JTAG-DP（IDCODE 0x0BA00477）が見えない（見えた装置: 0x974008EF など）。

**確かめる事実**:

- `jtag.sample_delay_scan` — 取り込みの遅れ 0〜7 のそれぞれで読めた IDCODE。期待の IDCODE が読める遅れがあるか
- `jtag.sample_delay_set` — 読み返した遅れ（readback）。設定した値と一致するか。FPGA を書き直すと 0 に戻る

**手順**:

1. 見えた値が期待の IDCODE を 1 ビットずらした値なら、TDO の取り込み時期のずれ（Pi 2B は 14.3 MHz 以上で起きる）
2. jtag.sample_delay_scan で遅れごとの読みを調べ、期待の IDCODE が読める遅れを jtag.sample_delay_set で設定してから linux.halt.open（今夜の実機では cycles=3）

関連: `docs/linux.md`

## linux.halt.debug.attach の前にすること

`linux_halt.attach.order` — 付いたメソッド: `linux.halt.debug.attach`, `linux.halt.open`

**症状**: linux.halt が開いていない（linux.halt.open）／ target.connect が残っていて linux.halt.open が断られる。

**確かめる事実**:

- `session.status` — target.state（DISCONNECTED でなければ MCU に接続中）、target.linux_halt_open（開いているか）

**手順**:

1. 順序: target.disconnect（MCU に接続中なら）→ jtag.sample_delay_set（Pi 2B）→ linux.halt.open → linux.halt.debug.attach
2. 引数はすべて明示（既定なし）。session.methods で name=linux.halt.open / linux.halt.debug.attach の引数と examples を読む
3. thread_id は "stack"（"contextidr" は Pi 2B で pid でなく ASID を返し、ブレークポイントが全部別のプロセス扱いになる）
4. linux.halt がすでに開いていると、もう一度 linux.halt.open は断られる（「もう開いている」）。linux.halt.status で開いているかを確かめ、開き直すなら先に linux.halt.close

関連: `docs/linux-halt.md`

## すぐ終わるプロセスに attach が間に合わない

`linux_halt.attach.short_lived_process` — 付いたメソッド: `linux.halt.debug.attach`

**症状**: ProcessNotFound（pid がカーネルのタスク一覧に無い）／ attach が数秒〜20 秒かかって失敗する。

**手順**:

1. attach は JTAG でタスクリストを辿るので、pid が無いと全部を辿り終わるまで返らない（約 22 秒）。先に ssh で /proc/<pid> があることを確かめる
2. 数秒で終わる/落ちるプログラムは、起動してすぐ SIGSTOP で止め、attach し、走らせる前に SIGCONT する（kill -STOP <pid> → attach → kill -CONT <pid>）
3. pid が無いときの文は『終了したか、pid の誤り』の両方を含む（どちらかは分からない）

関連: `docs/linux-halt.md`

## 全コアを止めている間は対象に ssh できない／長く止めるとリセットされる

`linux_halt.halted.no_ssh` — 付いたメソッド: `linux.halt.debug.halt`, `target.halt`, `linux.halt.halt_all`, `linux.connect`

**症状**: 停止中に ssh・linux.log.read などが通らない／止めたまま 12〜15 秒を超えるとボードが再起動する。

**確かめる事実**:

- `session.status` — target.state が HALTED か（止めている間は対象の Linux が止まっている）

**手順**:

1. 全コアが止まっている間は対象の Linux 自体が止まっている。停止中の linux.log.read は仕様どおり断られる
2. Pi 2B は BCM2835 のウォッチドッグが約 15 秒で効く。対象側で RuntimeWatchdogSec=0（/etc/systemd/system.conf.d/95-fprobe-nowdt.conf）にしてある場合のみ長く止められる。止めても 30 秒以内に target.resume か linux.halt.close で戻す

関連: `docs/linux-halt.md`

## target.resume は待たない（止まったことは別に見る）

`run.wait_after_resume` — 付いたメソッド: `target.resume`, `debug.run_until_halt`, `linux.halt.debug.run`

**症状**: target.resume の応答が null/状態だけで、止まったか分からない。

**確かめる事実**:

- `target.status` — state（RUNNING / HALTED）、止まっていれば pc・where・halt_reason（Linux は linux.stop_kind）

**手順**:

1. MCU: debug.run_until_halt（timeout_s を明示。時間切れでも止めて返る）。Linux のプロセス: linux.halt.debug.run（wait_s を明示。時間切れでも走らせたまま）。debug.run_until_halt は Linux では使えない
2. あるいは target.resume の後に target.status を繰り返し読む

関連: `docs/guide/mcp.md`

## 書き出し先（VCD・記録・スナップショット等）が許可の表に無い

`paths.write_permitted_dirs` — 付いたメソッド: `la.capture`, `la.export`, `recording.export`, `snapshot.save`, `hooks.save`, `linux.halt.profile.save`, `target.flash.read`

**症状**: E_NOT_ALLOWED（path_not_permitted）／ 書き出しが成功したのにファイルが見つからない。

**確かめる事実**:

- `session.permissions` — write に並ぶ場所（既定は <FPROBE_HOME>/out の下）。checked = false なら表で確かめていない

**手順**:

1. path を許可された場所（既定 ~/.fprobe/out の下）に置く。表は利用者が permissions.json に書く（AI からは変えられない）

関連: `docs/guide/mcp.md`

## 番地・速さなどの整数に '0x20000000' や 'fast' を渡した

`args.integer_not_string` — 付いたメソッド: `target.memory.read`, `target.memory.write`, `target.connect`, `debug.breakpoint.set`

**症状**: 型が違う（整数が必要。渡された値: '0x20000000'）。

**確かめる事実**:

- `session.methods` — name=<メソッド名> の params。各引数の type・description・examples

**付く条件**（失敗の種類）: code = invalid_params, validator = type

**手順**:

1. 整数の引数は JSON の数で渡す（0x20000000 は 536870912）。ただし debug.breakpoint.set の location は文字列（関数名・ファイル:行・"0x..."）
2. 単位は引数の description に書いてある（frequency_hz は Hz）

関連: `docs/guide/mcp.md`

## ブレークポイントを置けない（記号・行が無い）

`debug.breakpoint.symbol_or_line_missing` — 付いたメソッド: `debug.breakpoint.set`, `debug.evaluate`, `debug.backtrace`

**症状**: シンボル 'main' が ELF に無い／ file:line が ELF の行表に無い／ 記号表に無い変数。

**確かめる事実**:

- `debug.symbol.search` — 名前の一部から記号（関数・変数）を探す。件数 limit は自分で渡す。total > returned なら limit で切れている
- `debug.elf.status` — 読み込んでいる ELF のパス・build-id。取り違え（別のビルドの ELF）でないか
- `debug.symbol.info` — 記号の名前から アドレス・サイズ・種類（関数/変数）
- `debug.source.list` — ELF の行表にあるソースファイル

**手順**:

1. ELF を先に読む（debug.elf.load。未読込だと断られる）。Linux のプロセスは attach の elf と対象の実行ファイルの build-id が一致すること
2. 文に出る『前後の行』は行表の事実。その行にブレークポイントを置き直す
3. 関数名は式（debug.evaluate）では変数として引けない。変数は DWARF の変数、関数は debug.symbol.info

関連: `docs/guide/getting-started.md`

## main の終わりを越えて step すると libc の中で止まる

`step.out_of_main` — 付いたメソッド: `debug.step`

**症状**: step over / into が libc の番地（関数名なし）で止まる／ step out が『caller is not known』になる。

**確かめる事実**:

- `debug.backtrace` — 呼び出し履歴の段数と、巻き戻しが止まった理由（stop）。libc の CFI/シンボルを読んでいないと 1 段で終わる

**手順**:

1. ELF の外（共有ライブラリ）にはソース行が無いので、そこへ戻ったらそこで止まる（呼び出しでない戻りは LR まで走らない）
2. 共有ライブラリの関数名は attach の libraries（取り寄せたシンボル）が付く。stripped の内部関数は番地だけ

関連: `docs/linux-halt.md`

## watch.set は expressions（式の配列）を取る

`watch.expressions_list` — 付いたメソッド: `watch.set`

**症状**: 必須の引数が無い: expressions／ expression を渡して未知の引数と言われる。

**確かめる事実**:

- `session.methods` — name=watch.set の params（expressions は文字列の配列。例 ["s_ticks"]）

**付く条件**（失敗の種類）: code = invalid_params, validator = required / additionalProperties

**手順**:

1. watch.set(expressions=["s_ticks"], …)。状態は watch.status、値は watch.values

## 共有ライブラリ（libc・dlopen したライブラリ）の中にブレークポイントを置く

`linux_halt.breakpoint.shared_library` — 付いたメソッド: `debug.breakpoint.set`, `linux.halt.debug.break.insert`

**症状**: ファイル:行 が ELF の行表に無い／記号が ELF に無い／関数名で置けない（共有ライブラリの関数）。

**確かめる事実**:

- `linux.halt.debug.status` — library_symbols: attach のときに取り寄せた共有ライブラリのシンボル（読めたか・error）
- `debug.symbol.info` — attach の ELF（実行ファイル）の記号。共有ライブラリの記号は入っていない

**手順**:

1. 名前・ファイル:行 のブレークポイントは attach の ELF（実行ファイル）の記号と行表だけで解く。共有ライブラリの中の関数名は、呼び出し履歴・where の表示に付くだけ
2. 共有ライブラリの中の命令は、実行時の番地（"0x76dc02ec" の形）で置く。取り寄せたシンボルに無い内部の関数は、ARM か Thumb かが分からず断られる
3. ソースの行は共有ライブラリの行表が無いので使えない

関連: `docs/linux-halt.md`

## ブレークポイントを置いたのに止まらない（その行はもう通り過ぎている）

`linux_halt.breakpoint.line_already_passed` — 付いたメソッド: `debug.breakpoint.set`, `linux.halt.debug.break.insert`, `linux.halt.debug.run`, `target.resume`

**症状**: ブレークポイントは置けたのに、走らせても止まらない（RUNNING のまま）。

**確かめる事実**:

- `target.status` — 止まっているときの pc・where。attach したときにプロセスがどこまで進んでいたか
- `debug.breakpoint.list` — 置いたブレークポイントの番地・種類・有効か

**手順**:

1. attach はプロセスが走っている最中の所で全コアを止める。main の最初の行など、一度しか通らない行は、attach の時点でもう通り過ぎていると二度と当たらない
2. 繰り返し通る行（ループの中・周期的に呼ばれる関数）に置く。プログラムを最初から調べたいときは、起動してすぐ SIGSTOP で止め、attach・ブレークポイントを置いてから SIGCONT（linux_halt.attach.short_lived_process）

関連: `docs/linux-halt.md`

## step の reason が signal のとき、どのシグナルか

`linux_halt.step.signal_stop` — 付いたメソッド: `debug.step`, `linux.halt.debug.step`

**症状**: debug.step の応答が reason=signal で、次の step で「プロセスは終了している（killed by signal N）」になる。

**確かめる事実**:

- `target.status` — linux.stop_kind（signal / exit）と where（シグナルが来た場所）

**手順**:

1. reason=signal の応答は signal（番号）と signal_name（SIGSEGV 等）を持つ。シグナルが配送される直前で止まっていて、次に走らせると配送されて（致命的なら）プロセスが死ぬ
2. 死んだ後は同じ文で断られる。linux.halt.debug.close して、新しいプロセスに attach し直す

関連: `docs/linux-halt.md`

