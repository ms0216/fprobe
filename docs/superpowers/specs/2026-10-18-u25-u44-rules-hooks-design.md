# U25・U44: 規則の検査・期待値の比較・故障注入の台本と、デバッグの出来事のフック（設計）

日付: 2026-10-18。台帳: `docs/reports/2026-10-18-gap-analysis.md` の U25・U44。
競合の機能: U25 = WIN-03, WR-03, TZ-08（状態遷移の図示）, FRT-04, DT-10。U44 = SEG-60, IAR-32。
前提: 共通の時間軸の記録（F3、`fprobe.recording`）、クロストリガ（M9、`fprobe.xtrig`）、デーモン（`fprobe.api.daemon`）。

## 1. U25-a: 規則の検査（`fprobe.rules`、道具 `rules.check`）

### 1.1 規則の書き方（宣言的。JSON か YAML。辞書でも渡せる）

```yaml
name: モータ制御の時間の規則
requirements:                       # 任意: 要件 ID → 文（証跡の対応表に出す）
  REQ-12: 制御周期は 1 ms ± 50 µs
rules:
  - id: ctrl-period                 # 必須・一意
    kind: period
    requirement: REQ-12             # 文字列か列
    event: {stream: testpoint, kind: enter, body: {function: ctrl_isr}}
    period_us: 1000
    tolerance_us: 50
```

条件（`event`・`start` 等）は `recording.find` と同じ（stream / kind / a / b / body）。時間の単位は µs（記録の
タイムベースで換算）。共通の欄: `id`・`description`・`requirement`・`severity`（error / warning。既定 error）・
`allow_empty`（一致が 0 件でも合格にする。既定 false = 判定できない `no_data`）。

| kind | 欄 | 違反 |
|---|---|---|
| `interval` | start・end・min_us・max_us | 開始から次の終了までが範囲の外 / 終了が来ないまま次の開始（`recording.intervals` と同じ組の規則） |
| `period` | event・period_us と tolerance_us、または min_us・max_us | 続く 2 回の間隔が範囲の外 |
| `sequence` | trigger・steps（条件の列）・within_us・forbid（条件の列） | trigger の後、steps が順に来ない（within_us 以内、省略時は次の trigger まで）/ その間に forbid が来た |
| `count` | event・min・max・window_us | 件数（window_us ごと、省略時は全体）が範囲の外 |
| `never` | event | 1 件ごと（例: 故障・あふれ） |
| `value` | event・field・min・max・equals | 値が範囲の外・違う |
| `states` | event・field・allowed（[from, to] の列）・labels・initial | 許されない遷移 |

`field`: `a`・`b`・`kind`・`body.<名前>`（入れ子は `.` でつなぐ）。

### 1.2 結果（期待値と実測の比較）

規則ごとに `verdict`（pass / fail / warn / no_data）・`expected`（書いた範囲）・`measured`（件数・最小・最大・
平均・ばらつき等、種類ごと）・`violations`（時刻 t・t_end・記録の最初からの µs・文・値。limit 件まで、
`violations_total`）。全体は `verdict`（fail > no_data > warn > pass の順で悪いもの）と件数。

`baseline`（別の記録の名前）を渡すと、同じ規則を基準の記録でも測り、実測の数値の差を規則ごとに付ける
（「前の版・よい版と比べて遅くなったか」）。

### 1.3 派生イベント（印）

`mark=true`（既定）なら違反を記録の stream `rule`・kind `violation`（t・t_end・body に ruleset・rule・
requirement・message・value）として書く。同じ規則集（名前）を検査し直すと、前の印を消して書き直す
（`Recording.replace_derived`）。記録中でも保存した記録でもよい（SQLite の WAL）。画面・Perfetto の書き出し・
`recording.find` でそのまま見える（WIN-03 の派生イベント）。

### 1.4 状態遷移図（`rules.states`、TZ-08・FRT-04）

条件に一致する出来事の field の値を状態とし、状態ごとの入った回数・滞在時間（最小・最大・平均・合計 µs）、
遷移ごとの回数と最初の時刻、Graphviz の dot（回数を辺に、滞在の平均を節に）を返す（path に書ける）。
ユーザイベント（U16）が無くても、テストポイント・サンプラの変数（stream sample の b）・RTT の行等の任意の
出来事から作れる。許される遷移の検査は規則 `states`。

### 1.5 証跡（`rules.report`、DT-10）

最後の `rules.check` と `inject.run` の結果を、Markdown か JSON の 1 つの文書にする: 作った時刻、記録（名前・
基板・設計・ELF とその SHA-256・時間の範囲）、規則集（名前・ファイル・SHA-256）、要件ごとの対応表（要件 →
規則・故障注入 → 判定）、規則ごとの期待値と実測、違反の一覧（時刻）、基準との比較、故障注入の試行ごとの結果。

## 2. U25-b: 故障注入の台本（`fprobe.inject`、道具 `inject.run`、WR-03）

クロストリガの割り込み書き込み（条件が成立した数 µs 以内に、ハードウェアが任意の番地へ 1 語書く）で、
「いつ・何を壊すか・どうなるべきか」を台本にして試行し、判定する。

```yaml
name: 制御の変数の破壊
trials:
  - id: mode-corrupt
    requirement: REQ-30
    when: {pattern: {rise: 8}}         # pattern / sequence / dap_match / host のどれか 1 つ
    write: {address: g_mode, value: 7} # address は数か ELF の変数名。flip（ビットの反転）も可
    timeout_s: 2.0                     # 条件の成立を待つ時間
    observe_s: 0.5                     # 注入の後、判定までの時間
    expect:
      - {state: running}
      - {expression: g_error_count, min: 1}
      - {memory: {address: 0x20000010, size: 4}, equals: 0}
      - {rules: [{id: no-fault, kind: never, event: {stream: swo, kind: exception}}]}
    restore: {write: [{address: g_mode, value: 0}], reset: null}
```

| when | 仕掛け |
|---|---|
| `pattern: {mask, value, rise, fall}` | 多段トリガの 1 段（ロジアナのピンの条件。ロジアナの捕捉は要らない） |
| `sequence: {stages, pattern_a, pattern_b}` | 多段トリガ（`xtrig.set_sequence` と同じ形） |
| `dap_match: {address, ...}` | ホストの DRW アクセスの番地一致（`xtrig.set_match` と同じ形） |
| `host: {delay_s}` | 仕掛けて delay_s 後にホストが発火 |

試行の手順: 書く値を決める（flip は仕掛ける時点の値を読んで xor。その後に変わる値には向かない、と結果に
previous を残す）→ 割り込み書き込みの中身を設定 → 源を割り込み書き込みにつなぐ（ロジアナへのつなぎは保つ）→
成立を待つ（割り込み書き込みの回数が増える）→ 切る → 記録中なら stream `inject`・kind `write` を
**ハードウェアの時刻**（割り込み書き込みが受理された時のタイムベース）で入れる → observe_s 待つ → 判定 →
restore。最後に割り込み書き込みの中身を既定（ターゲット停止）へ戻す（壊す書き込みを残さない）。

判定: `state`（running / halted）、`expression`（C の式。equals / not_equals / min / max）、`memory`
（番地・大きさ。同じ比較）、`rules`（注入の時刻から observe_s の間の記録に規則を当てる。記録が要る）。
試行の verdict: pass / fail / not_triggered（時間内に成立しない）/ inject_error（SWD の誤り等）/ error。

## 3. U44: デバッグの出来事のフック（`fprobe.hooks`、道具 `hooks.*`、SEG-60・IAR-32）

### 3.1 置き場所と実行

登録簿（`HookTable`）は Probe（`HookTools`）が持ち、**実行はデーモン**が行う。AI（MCP）・画面・GDB の操作は
すべてデーモンを通るので、誰の操作でも同じフックが走り、`hooks.list`・`hooks.log`・通知 `hook` で全員が同じ
ものを見る。ライブラリから Probe を直接使うときは走らない。

### 3.2 出来事

| event | 対象の操作（API / GDB） | before | after |
|---|---|---|---|
| connect | target.connect, gdb.attach | 操作の前 | 成功の後 |
| disconnect | target.disconnect | 〃 | 〃 |
| reset | target.reset, gdb.reset | 〃 | 〃 |
| flash | target.flash, target.program_ram, target.flash.erase, gdb.flash | 〃 | 〃 |
| resume | target.resume, debug.run_until_halt, gdb.continue | 〃 | 操作の後（run_until_halt は止まった後） |
| halt | target.halt, gdb.interrupt（before） | 操作の前 | **止まったことを見つけた後**（誰の操作でも・ブレークポイント・GDB・ステップ） |

halt の after は、コアを動かし得る操作の後にデーモンが状態を読んで見つける（応答の前に走るので、呼んだ側は
フックの結果を前提にできる）ほか、見張り（watch）が見つけた止まり（GDB・ブレークポイント）でも走る。同じ止まり
で 2 回は走らない（状態と run の番号が変わったときだけ）。

### 3.3 動作（action）

- `{"evaluate": "C の式"}` → `debug.evaluate`
- `{"mark": "文"}` → 記録の印（stream annotation・kind mark。記録していなければ何もしない）。文の `{event}`
  `{phase}` `{method}` `{client}` `{pc}` `{where}` は置き換える。
- `{"call": "メソッド名", "params": {...}}` → 任意の API（検証・調停・操作の記録は普通の呼び出しと同じ）。

動作の呼び出しは、きっかけの利用者と同じ役割（role）の利用者 `hook <id>`（via hook）として行う（人の一時停止中に
人の操作のフックは通り、AI の操作のフックは保留される）。見張りが見つけた止まりのフックは role script。
フックの中の操作はフックを起こさない（再帰しない）。

`required: true` の before フックの動作が失敗したら、操作をしない（誤りで返す。例: 書き込みの前に電圧を確かめる）。
実行のたびに結果（動作ごとの成否・値・誤り）を `hooks.log` の環（500 件）に残し、通知 `hook` で送り、記録中なら
stream `hook`・kind `run` に入れる。

### 3.4 道具

`hooks.add`（event・phase・actions・name・required・enabled）→ id、`hooks.remove`（id か all）、`hooks.list`、
`hooks.log`（last）、`hooks.save` / `hooks.load`（JSON の宣言。load は置き換えか追加）。
