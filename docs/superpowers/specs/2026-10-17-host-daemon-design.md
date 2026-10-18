# PC 側 第 2 段: デーモンと API（fprobe-daemon）設計

## 1. ゴールと完了条件

人間用の画面（GUI）と AI（MCP）が **同時に** 同じ fprobe を使い、AI の作業を人間が見て、必要なら一時停止して
交代できるようにする土台。画面そのもの（第 3〜5 段）はこの API の利用者として後から作る。

| 完了条件 | 検証 |
|---|---|
| デーモンが fprobe（USB / シリアル / Ethernet）を 1 つ開き、既存のライブラリの機能を API で出す | 単体（偽のデバイス）+ HIL |
| AI（fprobe-mcp）と別の利用者（試験のスクリプト）が同時につながり、どちらの操作も記録として流れる | HIL |
| 人間が一時停止すると AI の次の要求が保留され、再開で続きから処理される | 単体 |
| API の仕様（JSON Schema）1 つから、Python の受け口・MCP の道具・TypeScript の型・文書が生成され、一致を試験で確かめる | 単体 |
| 既定は localhost のみ。`--lan` のときだけ外に開き、トークンが無い接続を断る | 単体 |

非目標（後の段）: 画面（第 3〜5 段）、波形のバイナリ転送の最適化、利用者ごとの権限の細分化。

## 2. 背景と判断

- Windows の USB は 1 プロセスしか機器を開けないので、GUI と AI が同時に使うには機器を握るプロセスが要る。
- 中心は既存の Python のライブラリ（`fprobe`）。デーモンは新しいハードの処理を持たず、ライブラリへの薄い対応だけ。
  中心を後で Rust（probe-rs）に、画面を Tauri 等に移しても、API の仕様が同じなら利用者は変わらない。

| 判断 | 理由 | 誤っていた場合のコスト |
|---|---|---|
| JSON-RPC 2.0 over WebSocket（1 本で要求・応答・通知） | 双方向で記録を押し出せる。どの言語にもある。画面はブラウザから直接つなげる | 低（HTTP の口を足すのは容易） |
| 仕様の正は手書きの JSON Schema（`host/api/fprobe_api.json`） | CSR・ディスクリプタと同じ「単一の定義から生成」。言語に依らない | 低 |
| Starlette + uvicorn（MCP の依存で導入済み） | 依存を増やさない。WebSocket と静的ファイルの配信が 1 つで済む | 低 |
| 操作は 1 本の作業スレッドで順に実行 | 機器・pyOCD はスレッド安全でない。調停（一時停止）もここで行う | 低 |

## 3. 構成

```
 ブラウザ UI / fprobe-mcp / CLI / スクリプト
            │  JSON-RPC 2.0 over WebSocket（ws://127.0.0.1:7820/api）
 fprobe-daemon（Python）
   ├─ 接続の受け口: 認証（--lan のときトークン）、session.hello（名前・種類・API の版）
   ├─ 調停: 操作の要求を 1 本の作業スレッドへ。一時停止中は human 以外の操作を保留
   ├─ 記録: すべての操作を activity として保存（JSON Lines のファイル）し、購読者へ通知
   ├─ 機能: 仕様のメソッド → fprobe のライブラリ（Session・LogicAnalyzer・EventLog・Debugger 等）
   └─ 静的配信: 画面のファイル（第 3 段から）
```

- 機器は起動時の引数（`--serial` / `--port` / `--eth`）で 1 つ決める。デーモン 1 つ = 機器 1 つ。
- MCP（`fprobe-mcp`）はデーモンの利用者になる。デーモンにつながらなければ自分の子プロセスとして起動してから
  つなぐ（今の使い方 `claude mcp add fprobe -- fprobe-mcp --serial 0001` を変えない）。
- 既存の `mcp_server.Probe`（道具の実体）は、デーモンの「機能」部分に移す。MCP 側はメソッドを呼ぶだけになる。

## 4. API

メソッドは「分野.動作」（例: `target.halt`）。引数と結果は名前付き（JSON のオブジェクト）。仕様は
`host/api/fprobe_api.json` に、メソッドごとに `params`・`result` の JSON Schema・説明・種類（`read` = 誰でも
いつでも、`control` = 操作権が要る）を書く。第 1 段までの MCP の道具をすべてメソッドにする（名前の対応は仕様書の
付録として生成する）。

| 分野 | メソッド（例） |
|---|---|
| `session` | `hello`、`status`、`subscribe`（受け取る通知の種類）、`control.pause` / `control.resume` |
| `probe` | `info`、`csr.read` / `csr.write`、`voltage` |
| `target` | `connect` / `disconnect` / `status` / `halt` / `resume` / `step` / `reset`、`registers.read` / `write`、`memory.read` / `write`、`flash`、`program_ram` |
| `debug` | `elf.load`、`symbol.info`、`breakpoint.set` / `clear` / `list`、`watchpoint.set` / `clear`、`run_until_halt`、`variable.read` / `write`、`backtrace` |
| `la` | `capture`、`arm` / `collect`、`deep.capture` / `arm` / `collect`、`stream` |
| `events`・`xtrig`・`swo`・`uart` | 既存の道具と同じ |

通知（デーモン → 利用者、`subscribe` した種類だけ）:

| 通知 | 内容 |
|---|---|
| `activity` | 操作の記録: 番号、時刻、利用者（名前・種類）、メソッド、引数、結果の要約か誤り、所要時間、保留された時間 |
| `control` | 操作権: `running` / `paused`（誰が止めたか）、保留中の要求の数 |
| `target.state` | 実行中 / 停止（理由・場所）。デーモンが状態の変化を見たとき |
| `uart.data`・`swo.data` | 受け取ったバイト（UART の端末・SWO の表示用） |

誤りのコード（JSON-RPC の `error.code`）: 機器が無い・ターゲット未接続・操作権が無い（一時停止中に human 以外が
保留を拒んだ場合）・タイムアウト・API の版の不一致・引数が仕様に合わない・機器の誤り（`ControlError` 等）。

版: `session.hello` で利用者が `api_version` を送り、デーモンは合わなければ断る（CONTROL のプロトコル版と同じ）。

バイナリの応答（2026-10-18 追加）: 仕様で `"binary": true` のメソッド（`timeline.tiles`）は、成功の応答を同じ
WebSocket の**バイナリのフレーム**（magic `FPB1` + JSON-RPC の応答 + 8 バイト境界の型付きの区画）で返す。要求・
誤り・通知はテキストのまま。WebSocket でない利用者（GDB・フック）には `E_NOT_ALLOWED`、MCP の道具には出さない。
形の詳細は `docs/superpowers/specs/2026-10-18-timeline-tiles-design.md` §3。

## 5. 調停（一時停止・交代）

- 利用者は `hello` で種類（`human` / `ai` / `script`）を名乗る。
- `control.pause`（human だけが呼べる）で一時停止。以後、human 以外の `control` 種別の要求は保留（応答を待たせる）。
  `read` 種別（状態・記録・購読）はいつでも通る。human の操作は一時停止中も通る（交代）。
- `control.resume` で再開し、保留していた要求を受けた順に処理する。保留が長すぎる（MCP 側のタイムアウト）場合に
  備え、MCP の道具の待ち時間は保留中は延ばす（保留の通知を受けて待ち続ける）。
- 保留が 300 秒を超えた要求は、再開しても実行せず E_TIMEOUT で返す（呼び出し側が諦めた古い操作を遅れて実行しない）。

## 6. 記録

- 1 操作 = 1 件。`~/.fprobe/activity/<日時>.jsonl` に追記し、同じものを `activity` 通知で流す。
- 大きな結果（メモリの塊・波形）は要約（長さ・先頭）だけを記録する。波形はファイル（VCD）の場所を記録する。

## 7. 生成と試験

- `python -m fprobe.api gen`: 仕様から (a) Python のメソッドの表（受け口が引数を検証する）、(b) MCP の道具の定義、
  (c) TypeScript の型（第 3 段の画面用）、(d) 文書 `docs/api.md` を生成。試験で生成物と仕様の一致を確かめる。
- 単体: 偽の機器（`tests` の FpgaModel 等）でデーモンを起動し、2 つの利用者から同時に操作・一時停止・再開・記録・
  認証・版の不一致を確かめる。
- HIL: デーモン + MCP（Ethernet / USB）で、第 1 段のデバッグの試験と同じ手順が通ること、記録が流れること。

## 8. 段取り

1. 仕様（JSON Schema）と生成器、Python の受け口（引数の検証・誤りのコード）。
2. デーモン本体（接続・調停・記録・機能の対応）と単体試験。
3. MCP をデーモンの利用者に作り替え（道具は仕様から生成）。HIL。
4. 第 3 段（画面 v1）の仕様へ。
