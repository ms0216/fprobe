# 不具合の台帳: API（デーモン・クライアント・MCP・仕様）の入口

検出専用で作り、2026-10-25 に BUG-API-001〜004・006・007・009〜012（部分）・014・015 を修正（各項の「修正済み」）。未修正: 005（設計判断）・013・S-API 群。再現試験は `host/tests/test_api_bugs.py`（すべて `xfail(strict=True)`。直したら xfail を外す）。
重さ: A = 静かに誤った結果 / B = 失敗が見える / C = 端の条件・まれ / D = 品質。2026-10-25 作成。

## 調べ方

- 仕様 `host/api/fprobe_api.json`（361 メソッド）の全メソッドを、実物の `Probe` をデーモン（`Daemon.handle`）の中で直接呼ぶ形で機械的に叩いた
  （約 3.1 万呼び出し × 未接続・偽の機器で接続済みの 2 通り）。引数は、必須欠落・型違い・範囲外（負・0・2^31・2^64+1・10^40・1e308・5e-324・NaN・±inf）・
  空/長い(20 万字)/NUL/孤立サロゲート/パス風の文字列・配列/辞書/入れ子・余計な引数・深い入れ子を 1 つずつ変えた。
  結果の `error.data.exception`（漏れた例外の型）、待ち（8 秒超）、`ok`（範囲外を黙って受けた）を集計した。
- 型違い・必須欠落・余計な引数・仕様にある範囲（minimum/maximum）は、jsonschema の検証で全て E_INVALID_PARAMS になり、漏れは無かった
  （余計な引数は全メソッドで additionalProperties:false）。**漏れは、仕様が範囲を書いていない値・仕様を通した後の実装**にあった。
- 追加で、DaemonClient/Arbiter/MCP の道具の生成を、実際の WebSocket（127.0.0.1 の空きポート、偽の機器）と単体で調べた。

## 見つかった不具合

### BUG-API-001（B）MCP の道具が、省略可能で既定値の無い引数を null で転送し、デーモンに断られる
- **修正済み: mcp_client の道具は、必須でなく仕様に既定値が無く null も許さない引数が省略（None）のときは転送しない。**
- 試験: `test_mcp_tool_with_only_required_args_is_valid_for_the_daemon[<メソッド>]`（watch.xy / watch.measure / rtos.backtrace / rtos.read / testpoint.instrument / testpoint.start）
- 原因: `host/fprobe/mcp_client.py` の `tool_function` が `default=schema.get("default")`（既定が無ければ None）で道具の署名を作り、MCP の SDK が省略した引数も None で渡す。
  仕様側で `"default"` が無く null を許さない引数（`watch.xy.max_dt_us`・`watch.measure.names`・`rtos.backtrace/read.port`・`testpoint.instrument.non_type_words`・`testpoint.start.guard_samples`）が E_INVALID_PARAMS になる。
- 直し方の案: 仕様で該当引数を `anyOf [..., null]` + `default: null` にする（実装の既定値と一致させる）。または道具で値が None の引数は転送しない。仕様全体に「省略可能なら null を許す」を検査する試験を足す。
- 影響: AI（MCP）が上記 6 つの道具を必須引数だけでは呼べない（毎回断られる）。画面や直接の API 利用者は影響なし。
- 確信度: 高（道具の転送された引数を仕様で検証して再現）。

### BUG-API-002（B）watch.set の `names` の既定値 null が自分の型（array）に反する
- **修正済み: 仕様の watch.set.names を anyOf [array, null]（既定 null）にした。**
- 試験: `test_watch_set_default_is_valid_for_its_own_schema`
- 原因: 仕様 `watch.set.names` = `{"type":"array", ..., "default": null}`（null を許さない）。MCP の道具が既定値 None を転送すると E_INVALID_PARAMS。
- 直し方の案: `anyOf [array, null]`。「既定値が自分のスキーマに合う」を全メソッドで検査する試験を足す（この 1 件だけが違反）。
- 影響: MCP の watch_set が式だけでは呼べない。確信度: 高。

### BUG-API-003（A）孤立サロゲートを含む引数の操作が、操作の記録に残らない
- **修正済み: 操作の記録は、孤立サロゲートで UTF-8 にできないときだけ バックスラッシュ u のエスケープ（ensure_ascii）で書く。さらにデーモンが孤立サロゲートの引数を E_INVALID_PARAMS で断る（BUG-API-014 と共通）。**
- 試験: `test_activity_log_keeps_an_operation_with_a_lone_surrogate_argument`
- 原因: `host/fprobe/api/activity.py:39` が `open(encoding="utf-8")` に `json.dumps(ensure_ascii=False)` を書く。JSON の `"\ud800"` は Python の `json.loads` が孤立サロゲートの str にするため UnicodeEncodeError。
  `daemon.py:_recorder.record`（future の完了コールバック）の中なので例外は握りつぶされ、記録・通知(activity)・時間軸への `_note` が全部欠ける。連番 `_seq` だけ進む。
- 直し方の案: 記録を `ensure_ascii=True` で書く（または `errors="backslashreplace"`）。コールバック全体を try で囲み、記録に失敗したら失敗の記録を別に残す。
- 影響: 操作の記録（AI の監査証跡）が静かに欠ける。AI が作る文字列で起き得る。確信度: 高。
- 備考: 同じ文字列で応答は返る（Starlette が処理する）ので利用者には見えない。

### BUG-API-004（C）購読の関数が例外を投げると、DaemonClient の受信が死ぬ
- **修正済み: DaemonClient の購読の関数の例外は捕まえてログに残し、受信を続ける。**
- 試験: `test_a_failing_subscriber_does_not_kill_the_client`
- 原因: `host/fprobe/api/client.py:_read` が `callback(...)` を try で囲まず、例外が外側の `except BaseException` に届いて `_lost` を立て、以後の `call` が全部 ConnectionError。
- 直し方の案: コールバックの例外は捕まえて（記録して）受信を続ける。
- 影響: 画面以外の利用者（スクリプト・MCP の Notices.add 等）のコールバックの不具合で接続全体が使えなくなる。確信度: 高。

### BUG-API-005（C・設計の疑い）人 2 人が一時停止したとき、先の人の再開で後の人の一時停止まで解ける
- 試験: `test_one_humans_resume_does_not_release_another_humans_pause`
- 原因: `arbiter.py` の `_paused_by` が 1 人分。pause は上書き、resume は誰が呼んでも全解除（持ち主の照合なし）。
- 直し方の案: 一時停止の持ち主を集合で持ち、全員が再開したら解く（または再開を持ち主だけに限る）。設計判断が先。
- 影響: 人が複数台の画面で操作するとき、一方の再開で他方の保護が外れる。確信度: 中（仕様としての意図は未確認）。

### BUG-API-006（D）method が配列・辞書だと内部の TypeError（unhashable）が E_INTERNAL で漏れる
- **修正済み: method が文字列でなければ E_INVALID_REQUEST（無いときは「method が無い」）。**
- 試験: `test_a_non_string_method_is_refused_as_invalid_request[...]`
- 原因: `daemon.py:handle`（`method not in self.spec.methods`）が型を確かめず辞書引きする。
- 直し方の案: `isinstance(method, str)` を確かめ、違えば E_INVALID_REQUEST。method が無いときの文言（`" は無い"`）も「method が無い」と分ける。
- 影響: 誤った呼び手に内部の例外の文言が見える。確信度: 高。

### BUG-API-007（C）偽とみなされる params（[]・0・""・false・null）が黙って {} 扱いになる
- **修正済み: params は null・省略だけ {} とみなし、[]・0・""・false は検証で E_INVALID_PARAMS。**
- 試験: `test_a_falsy_non_object_params_is_refused[...]`
- 原因: `daemon.py:handle` の `request.get("params") or {}`。真の非オブジェクト（`[1]`・`"x"`・`5`）は E_INVALID_PARAMS で断るのに、偽の値は通る（不揃い）。null は JSON-RPC では省略と同じなので許容、`[]`・`0`・`""`・`false` は断るべき。
- 直し方の案: `params = request.get("params", {})`、`None` だけ `{}`、それ以外は検証へ。
- 影響: 呼び手の取り違え（位置引数の配列 `[]` 等）が見逃される。確信度: 高。

### BUG-API-009（C）整数の引数に整数値の小数（3.0）が通り、実装に float のまま渡って内部の TypeError が漏れる
- **修正済み: 検証後、integer だけを受ける引数の整数値の小数（3.0）を int に直す（spec.py `_integral`）。number も受ける所は触らない。**
- 試験: `test_an_integral_float_for_an_integer_argument_does_not_leak_a_typeerror[target.step / target.dp.read]`
- 原因: jsonschema の `"type":"integer"` は 3.0 や 1e308 も整数として通す（draft 2020-12）。デーモンは `fn(**params)` で float のまま渡す（`daemon.py:_call`）。実装は `range(count)`・`register & 0xFC` などで TypeError。
  漏れた例: target.step count=1e308、target.dp.read/write register、debug.breakpoint.log limit。Python から `3.0` を出す JSON（例: `json.dumps(n/1)`）で起き得る。
- 直し方の案: 検証の後、integer の引数は整数値の float を int に直す（または拒否）を、デーモンの 1 か所で行う。
- 影響: 意図せず float を渡した呼び手が、E_DEVICE の TypeError の文言で混乱する。silent に float が通る箇所があれば値が丸められる疑い。確信度: 高（TypeError の再現）/ 中（silent 側は未確認）。

### BUG-API-010（B）swo.stop が未接続・未開始で素の AttributeError を漏らす
- **修正済み: swo_stop は未接続なら NotConnected。**
- 試験: `test_swo_stop_without_a_connection_is_not_connected`
- 原因: `host/fprobe/probe_swo_rx.py:47` の `swo_stop` が `self._debug.probe` を `self.target`（未接続検査）を通さず参照。`self._debug` が None。
- 直し方の案: `self.target` で接続を確かめ NotConnected にする（`swo_read` は既にそうしている）。同種（`_debug` を直に参照する他のメソッド）を grep で洗う。
- 影響: E_DEVICE の `AttributeError: 'NoneType' object has no attribute 'probe'`（E_NOT_CONNECTED が正しい）。確信度: 高。

### BUG-API-011（B・重い）NaN が number の引数に通り、rtt.read の timeout_s=NaN で作業スレッドが永久に塞がる
- **修正済み: デーモンは受信の JSON の NaN・Infinity・-Infinity を E_INVALID_PARAMS（要求の id 付き）で断る。DaemonClient は送信で `allow_nan=False`（ValueError、待ちの登録も残さない）。rtt_read は期限が NaN でも待たない。**
- 試験: `test_nan_timeout_does_not_block_the_worker_forever`（別 process で 4 秒）, `test_the_non_json_token_nan_is_refused_at_the_wire`
- 原因: Python の `json` は `NaN`/`Infinity` を出しも読みもする（クライアント `json.dumps`、デーモン `json.loads`、パラメータは `parse_constant` 未指定）。jsonschema の minimum/exclusiveMinimum 比較は NaN に対して常に「違反なし」。
  `probe_observe.py:rtt_read` は `remaining = deadline - now`（NaN）で `remaining <= 0` が偽、`Condition.wait(NaN)` に入って戻らない。作業スレッドは 1 本（Arbiter）なので、1 回の呼び出しで**デーモンの全操作が止まる**（read も含め機器の操作が全部待ちになる）。
- 直し方の案: 受信側で `json.loads(parse_constant=lambda t: raise ValueError)` として NaN/Infinity を E_PARSE にする。念のため時間の引数は有限であることを仕様に書く（`"minimum"`/`"maximum"` と `finite`）。クライアントも `json.dumps(allow_nan=False)`。
- 影響: 誤りまたは悪意のある 1 回の呼び出しでデーモンが応答不能（再起動しか無い）。他にも NaN で無限待ちになる時間の引数があり得る（同じ形）。確信度: 高。

### BUG-API-012（B/C）長さ・個数・上限・時間の引数に最小・最大が無く、負や桁外れが検証を通る
- **修正済み（部分）: 仕様に型・プロトコル由来の範囲を明示した（約 66 引数）。**未解決（設計判断が要る）**: 長さ・個数・時間の上限。`target.memory.read length=2^31`（上限は 32 ビットのアドレス空間 2^32 まで）・`target.step count`・`probe.csr.read count`（上限 65535 はプロトコル由来）は、上限内でも作業スレッドを長く塞げる。操作をキャンセルする仕組み（またはチャンク化）が無い限り、経験則の上限は入れない。timeout_s 等の時間も下限 0 だけで上限なし。
  - 追加した範囲: アドレス・レジスタ値・マスク・サイズ・語（`address`/`ram_address`/`vector_table`/`bottom`/`top`/`scratch`/`size`/`low`/`high`/`address_mask`/`data`/`data_mask`、target.*/xtrig.* の `value`、CSR の語）は 0〜2^32−1（ターゲットは 32 ビットのアドレス空間）。linux.* のアドレスは 64 ビットがあり得るので触れていない。
  - `probe.csr.read count` は 0〜65535・`probe.csr.write words` は maxItems 65535（要求の語数が 16 ビット。control.py encode_read/encode_write）。
  - 長さ（target.memory.read・target.flash.read/erase・target.speed_test・target.flash.benchmark の `*length`）は 0〜2^32（32 ビットのアドレス空間の大きさ）。
  - 時間（`*timeout_s`・`*_s`・`*_us`・`hold_us`・`filter_ns` 等）は minimum 0。`*rate_hz` は 0 より大（exclusiveMinimum）。`baud`・`frequency_hz`・`core_clock_hz` は minimum 1。
  - 個数・番号（`count`・`last`・`since`・`offset`・`index`・`channel`・`task`・`frame`・`core`・`ap`・`pretrigger`・`before/after_beats`・マスク類 等）は minimum 0。`debug.breakpoint.log limit` は minimum 1（0 は常に空で `more` が真のまま進まない）。`debug.watchpoint.set size` は minimum 1。`spi.transfer mode` は 0〜3（SPI の定義）。
  - 触れていない: 時刻（`t0`・`t1`・`t`・`at`）は負が意味を持つか未確認、`debug.call arguments`・`watch.write value`・`debug.variable.write value`（64 ビットや浮動小数があり得る）、`la.*` の `mask`/`value`/`rise`/`fall` の上限（チャンネル数は機器の申告）、`linux.*` の上限。**
- 試験: `test_nonsensical_counts_lengths_and_timeouts_are_refused_by_the_spec[...]`（15 件）
- 原因: 仕様に minimum/maximum が無い（約 40 引数）。結果:
  - 作業スレッドを長時間塞ぐ: `target.memory.read length=2^31`、`target.step count=2^31`、`probe.csr.read count=2^31`（接続済みで 8 秒超、実機でも実質終わらない。デーモンの全操作が待つ。キャンセルも無い）。
  - 黙って誤り: `debug.breakpoint.log limit=-1`（`found[:limit]` で末尾 1 件を落とし `more` は真）、`limit=0`（常に空で `more` 真 = 読み手が永久に進まない）。
  - CSR/レジスタの 32 ビット幅を超える値（`probe.csr.write` の address/words、`xtrig.set_match/set_inject`、`target.registers.write`）が検証を通り、実機では `struct.error`（`control.py:encode_write`）として E_DEVICE で漏れる。
  - 負の timeout_s（`debug.run_until_halt`・`uart.read`・`swo.read` 等）が通る。
- 直し方の案: 仕様の生成元（`fprobe_gw` ではなく API の仕様の記述）で、長さ・個数に minimum と実用上の maximum（例: 1 回の読みの上限）、アドレス/値に 0〜2^32-1 を書く。時間は exclusiveMinimum 0 / minimum 0。上限を超える読みは分割を利用者に求める（上限は使う側の明示ではなく仕様の安全弁として）。
- 影響: 作業スレッドを 1 回で塞げる（デーモン停止）、静かな読み落とし。確信度: 高（検証の通過と接続済みの待ちを再現）。

### BUG-API-013（D）「無い」が素の KeyError（引用符付きの文言）の E_DEVICE で返る
- 試験: `test_a_missing_recording_is_not_reported_as_a_device_error_with_a_raw_keyerror`
- 原因: `recording.open`（記録が無い）・`target.flash.algo.remove`・`hooks.remove`・`xtrig.route`（未知の源名）・`target.registers.read`（未知のレジスタ名）が KeyError を投げ、`str(KeyError)` は文言を引用符で包む（`KeyError: "記録 'x' が無い"`）。専用の E_NO_DATA / E_INVALID_PARAMS を使うべき。
- 直し方の案: 実装で ValueError/NoData に直す、または `_call` で KeyError を E_INVALID_PARAMS（文言は args[0]）に写す。
- 影響: 画面・AI が「機器の誤り」と「名前の誤り」を区別できない。確信度: 高。

### BUG-API-014（D）孤立サロゲートの text が rtt.write で UnicodeEncodeError として漏れる
- **修正済み: デーモンが孤立サロゲートを含む引数を一括して E_INVALID_PARAMS で断る。**
- 試験: `test_rtt_write_with_an_unencodable_text_is_an_invalid_params_error`
- 原因: `probe_observe.py:rtt_write` の `text.encode("utf-8")` が例外。同種は `text`・`data` を encode する他のメソッドにもあり得る（uart.write 等は未確認）。
- 直し方の案: デーモンで JSON 受信後に孤立サロゲートを拒否する（BUG-API-003 とまとめて 1 か所で E_INVALID_PARAMS）。
- 影響: E_DEVICE の内部例外の文言。確信度: 高。

### BUG-API-015（C）閉じた Arbiter への submit が永久に待つ
- **修正済み: 閉じた Arbiter への submit は RuntimeError を Future にセットして返す。**
- 試験: `test_submit_after_close_does_not_hang`
- 原因: `arbiter.py:close` が作業スレッドを止めた後も `submit` は `_ready` に積むだけで、実行も拒否もされない。
- 直し方の案: close 後の submit は例外（または Future に例外をセット）。
- 影響: デーモン終了中（lifespan の終わり・GDB の接続の残り）に来た要求が応答されず待つ。確信度: 高（単体）。実際に起き得るかは終了の順序次第。

## 疑い（試験なし・または再現が不十分）

- S-API-1（C）`DaemonClient.call` / `request_with` に待ち時間の上限が無い（`future.result()`）。デーモンが応答しないとき（BUG-API-011 のような停止、または保留 `hold_s` 300 秒）、呼び手が永久に待つ。MCP の ReconnectingClient も同じ。タイムアウトの設計が先。
- S-API-2（C）`Arbiter` の保留の期限切れは再開のときだけ見る（ponytail の注記あり）。一時停止が長く続くと、AI の要求は期限を過ぎても応答が返らない（利用者側は上の待ち上限が無いので永久に待つ）。
- S-API-3（C）`ActivityLog.read` は全行を `json.loads` する。途中の行が壊れる（電源断・書き込み途中）と `session.activity.list` が恒久的に失敗する。1 行ずつ try する案。
- S-API-4（D）`binary.encode_frame` は `response["result"]` に `sections` というキーがあれば黙って上書きする。孤立サロゲートを含む結果は `.encode()` で例外になり、`serve` の `try` の外なので応答が返らない可能性がある（`la.view` 等の実際のバイナリの結果で起き得るかは未確認）。
- S-API-5（D）`_call` の `except BaseException` が SystemExit/KeyboardInterrupt も機器の誤りとして返す（意図されたコメントあり）。`KeyboardInterrupt` を握る点は議論の余地。
- S-API-6（C）`jsonable` は set の要素が比較できない型（None と int 等）だと `sorted` で TypeError → E_INTERNAL「結果を返せない」。結果の型の契約が曖昧。
- S-API-7（D）`DaemonClient.request_with` は `json.dumps` が TypeError（NaN ではなく Path 等）を投げたとき、登録済みの `_pending` が残る（小さな漏れ）。

## 調べた範囲 / 調べていない範囲

- 調べた: 仕様の全 361 メソッド（`session.hello`/`session.subscribe`/`stream.serve`/`target.pack.install`/`target.pack.find`/`probe.design.switch`/`hooks.add`/`hooks.load`/`linux.*` は副作用（ネットワーク・書き込み・プロセス）の恐れがあるため値の網羅から除外。検証層では他と同じ検査が効く）。
  デーモンの要求の検査（method/params/source/quiet の型）、一時停止と再開の競合（4 スレッド×40 呼び出し + 切り替え）、同じクライアントの 16 スレッド並行 60 呼び出し、利用者の切断時の保留の取り消し、MCP の道具の生成（署名と転送される引数）、仕様の自己整合（既定値が自分の型に合うか、必須が properties にあるか）。
- 調べていない: 実機での挙動（機器に触れない方針）。偽の機器の制限で見えない所（未接続で先に NotConnected になるため、実装が接続後に行う引数検査は偽の接続済みの機器でだけ見た。偽の機器に無い機能は AttributeError になり、これは製品の不具合として数えていない）。
  `linux.*`（ssh/ネットワーク）、`stream.serve`、`target.pack.*`、`probe.design.switch` の値の網羅。LAN の認証（--lan のトークン・Origin/Host の検査）の網羅（既存の試験が覆う）。`place_outputs`（依頼文にあったが、リポジトリに該当する名前は無かった: `grep` で `host/fprobe` に `outputs.py` も `place_outputs` も無い）。大きな結果の返し方はバイナリの応答 `api/binary.py` を読んだのみで、実行時の大きさの網羅はしていない。
- 欠番: BUG-API-008（調査中に「孤立サロゲートを写す誤りの応答が送れず待ち続ける」と疑ったが、再現せず取り下げた）。
