# API パネル（画面に全 API を入れる、段階 A）実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** MCP の道具と同じ範囲のすべての API を、画面の 1 つのパネルから引数を入れて呼べるようにする。

**Architecture:**
- 画面は `host/api/fprobe_api.json` を build 時に読み込み、引数の JSON Schema から入力の欄を作る（純粋な関数 `lib/apiform.ts`）。
- 呼び出しは今の `conn.call` で行う。利用者 human として Activity と記録に残る。
- 確かめは `confirmer` を使い、`irreversible` の印で赤くする。
- Activity の行から、同じ呼び出しを API パネルで開ける（`UiState.openApi`）。

**Tech Stack:** Svelte 5・TypeScript・vitest、Python（spec.py・pytest）。

**Spec:** docs/superpowers/specs/2026-10-22-ui-all-apis-design.md

## Global Constraints

- 範囲は MCP の道具と同じ: session.* と `binary: true` は出さない。
- 確かめ: read は無し、control は引数を見せて確かめ、`irreversible: true` は danger の確かめ。
- 「取り返しのつかない操作」は JSON の印だけで決める（名前から推測しない）。
- 画面の文言は i18n.ts の EN と JA の両方に。メソッドの summary は日本語のまま出す。

## Review Focus

1. 整数の欄に 16 進数・負の数・範囲外・小数を入れたとき: 送らずに欄の誤りを出す。
2. null を許す欄を空にしたとき: 送らない（null も送らない）。必須の欄を空にしたときは送らずに誤りを出す。
3. JSON の欄に壊れた JSON を入れたとき: 送らずに誤りを出す。
4. Activity の引数は省略されていることがある（長い値は要約）: 開いた欄にそのまま入るので、呼ぶ前の確かめで見える。
5. 呼び出しの誤り（デーモンが返す）: 結果の欄に誤りとして出す（画面は落ちない）。

---

### Task 1: 取り返しのつかない操作の印（`irreversible`）

**Files:** `host/api/fprobe_api.json`、`host/fprobe/api/spec.py`（`Method.irreversible: bool = False`）、試験 `host/tests/test_api_spec_irreversible.py`

- 印を付けるのは次の 5 つ: `target.flash`・`target.flash.erase`・`target.option_bytes.write`・`target.unlock`・`target.flash.algo.remove`。
  - `target.flash.algo.remove` は外した領域を戻すのに設定のやり直しが要るため。迷うなら付けない側に倒す、という判断は次の Ruling に書く。
- 試験:
  - 印の付いたメソッドは、どれも `kind == "control"`。
  - 上の 5 つにだけ付いている。
  - `load_spec` が読む。

### Task 2: 欄の作りと引数の組み立て（`host/ui/src/lib/apiform.ts`）

- `apiMethods(spec) -> ApiMethod[]`: session.* と binary を除き、名前の順。`{name, kind, summary, irreversible, params}`。
- `fieldsOf(params) -> Field[]`:
  - `Field = {name, kind: "integer"|"number"|"boolean"|"string"|"enum"|"json", nullable, required, options?, min?, max?, exclusiveMin?, description?, default?}`。
  - `anyOf [X, {type: null}]` と `type: [X, "null"]` は、X で nullable にする。
- `buildParams(fields, values: Record<string,string>) -> {params} | {errors: Record<string,string>}`:
  - 空は送らない。
  - integer は 10 進・0x の 16 進・負の数を受け、範囲を確かめる。
  - number も範囲を確かめる。
  - boolean は "true" / "false"。
  - enum は値の文字列から元の型へ戻す。
  - json は JSON.parse。
- `valuesOf(fields, params) -> Record<string,string>`: Activity の引数から欄の文字列へ。
- `confirmLevel(m) -> "none" | "confirm" | "danger"`。
- 試験は `host/ui/tests/apiform.test.ts`（上の Review Focus 1〜3 を含む）。

### Task 3: パネルと Activity からの入り口

**Files:**
- `host/ui/src/components/panels/ApiPanel.svelte`
- `registry.ts`（id `api`、group `tools`）
- `lib/ui.ts`（`openApi(method, params)`、スナップショット `api: {method, params, seq} | null`）
- `lib/context.ts`・`App.svelte`（`showApi`）
- `ActivityPanel.svelte`（詳細に「API パネルで開く」）
- `i18n.ts`
- 試験 `host/ui/tests/ui.test.ts`（既にあれば追記）: `openApi` で seq が増え、メソッドと引数が入る。

パネルの作り:
- 左: 名前の前半（`target`・`la` …）の木と検索。
- 右: summary・欄・「呼ぶ」・結果（JSON）・誤り。
- 呼ぶ前に `confirmLevel` で確かめる。確かめの文面には、メソッド名と組み立てた引数の JSON を入れる。

### Task 4: 確かめ

- `npm test`・`npm run check`・`npm run build`（dist は git で管理していないので、作れることだけ確かめる）、`pytest host -q`。
