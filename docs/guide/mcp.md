# MCP（AI から fprobe を使う）

fprobe の全機能は 1 つの API に出ていて、MCP サーバ `fprobe-mcp` はその API のメソッドを AI の道具として出す薄いクライアントです。
人の画面と AI の道具は同じ API なので、人に見えて AI に見えないものはありません。

## 1. 登録する

インストール: `pip install -e host[mcp]`（[getting-started.md](getting-started.md)）。

Claude Code の例（Windows）:

```sh
claude mcp add fprobe -- <venv>\Scripts\fprobe-mcp.exe --eth 192.168.77.2      # Ethernet（PYNQ-Z2 の既定の IP）
claude mcp add fprobe -- <venv>\Scripts\fprobe-mcp.exe --serial <fprobe の USB シリアル>   # USB（Primer 等。複数台のとき）
```

`fprobe-mcp --help` に出る引数（実在するものだけ）:

| 引数 | 意味 |
|---|---|
| `--serial SERIAL` | fprobe の USB シリアル番号（複数台あるとき） |
| `--name NAME` | 個体名（`fprobe name set` で付けた名前）。`--serial` / `--port` / `--eth` の代わり |
| `--port PORT` | SerialLink（UART のホストリンク）の COM ポート。指定すると USB ではなくこれで開く |
| `--baud BAUD` | SerialLink のボーレート |
| `--eth ETH` | EthLink（UDP のホストリンク）のボードの IP アドレス。指定すると USB ではなくこれで開く |
| `--listen LISTEN` | fprobe-daemon のポート（既定の画面は `http://127.0.0.1:7820/`） |
| `--bitstreams BITSTREAMS` | 起動するデーモンに渡す: Primer の構成のビットストリームの置き場所（`<dir>/<design>/top.fs`） |
| `--token TOKEN` | 別の PC の（`--lan` の）fprobe-daemon の合言葉（同じ PC なら `~/.fprobe/daemon_token` を自動で読む） |

どのクライアントでも、stdio の MCP サーバとして `fprobe-mcp.exe <引数>` を登録すれば同じです。

## 2. デーモンとの関係

- `fprobe-mcp` は `fprobe-daemon` の利用者です。デーモンが無ければ `fprobe-mcp` が起動します（使い方は変わらない）。
  デーモンが機器（USB / UART / Ethernet）を 1 つ握り、API（JSON-RPC 2.0 over WebSocket、`ws://127.0.0.1:7820/api`）を出します。
- デーモンは MCP の終了後も常駐して機器を握り続けます。**そのため `fprobe` の CLI や Python の `Session.open*`、実機試験は機器を開けません。**
  それらを使うときは先にデーモンを止めてください。
- 人と AI は同時につながれます。画面（ブラウザ）を開くには最初に一度 `cd host/ui && npm install && npm run build`（Node.js 24）。
  デーモンが表示する `画面: http://127.0.0.1:7820/` を開く（MCP が起こしたデーモンでも同じ URL）。初めて開くときだけ合言葉を
  聞かれるので、`~/.fprobe/daemon_token` の中身を入れる（ブラウザが cookie で覚える。URL には載せない）。AI の操作は 1 件ずつ流れ、行を選ぶと引数と結果が見えます。
  すべての操作は `~/.fprobe/activity/*.jsonl` に記録されます。
- 人が「一時停止」（API は `session.control.pause`）すると AI の要求は保留され、人が画面からターゲットを操作できます。「再開」で AI に戻ります。
  300 秒を超えて保留された要求は、再開後に実行せず `E_TIMEOUT` で返ります。
- 機器を手で握らせるときの起動例: `fprobe-daemon --eth 192.168.77.2`。GDB サーバは `--gdb 3333`（127.0.0.1 だけ）。
- 合言葉: デーモンは 127.0.0.1 でも合言葉を持つ（`~/.fprobe/daemon_token`、持ち主だけが読める。無ければ作って使い回す。
  作り直すのは `--new-token`）。同じ PC・同じ利用者の `fprobe-mcp`・Python（`DaemonClient`）はこのファイルを自動で読む。
  合言葉は秘密として扱い、文書・履歴に貼らないこと。
- PC のファイルを実行する・書く呼び出し（解読器の読み込み・xsdb・VCD 等の書き出し）は、許可の表 `~/.fprobe/permissions.json`
  にある場所だけ通る（[remote.md](remote.md) の「許可の表」）。表は `session_permissions` で見える。AI からは変えられない。
- 別の PC から使うとき: 暗号化するなら `--lan` でなく SSH の転送で（[remote.md](remote.md)）。`--lan` は平文で、接続側は
  `fprobe-mcp --token <合言葉>`、画面は `/login` で合言葉を入れる。

## 3. 道具の名前と一覧

- 道具の名前は API のメソッド名の `.` を `_` にしたものです（`fpga_obs.open` → `fpga_obs_open`、`recording.la_stream.start` → `recording_la_stream_start`）。
- 名前と引数の一覧は [../api.md](../api.md)（生成物）で、仕様の正は `host/api/fprobe_api.json` です。
- **API（WebSocket の JSON-RPC）の利用者は、API 自身に聞けます: `session.methods`。** `prefix`（名前の先頭。`"target."`）で絞った一覧
  （name・kind。`with_summary = true` で summary も）、`name` で 1 つのメソッドの全部（summary・引数と応答の JSON Schema）。名前を当てずっぽうで
  試さない。無いメソッドを呼ぶと、同じ前半のメソッドを挙げて断る。MCP では全メソッドがもともと道具として並ぶ。
- 例外: セミホスティングのファイル操作（`debug.semihosting.configure` の `file_root`）は API からだけ指定でき、AI の道具には出ません。
- AI が最初に読む案内は `host/fprobe/mcp_instructions.py` の `INSTRUCTIONS` です。道具の使い方の流れだけを書いてあり、名前・引数の正は仕様です
  （`INSTRUCTIONS` に書かれた `名前` がすべて実在する道具であることは試験 `host/tests/test_api_mcp.py` が確かめます）。

## 4. FPGA 解析で最初に頼む指示文

`INSTRUCTIONS` には FPGA 解析の流れ（`fpga_obs_open` → `events_start` → `fpga_obs_signals_set` → `fpga_obs_read` / `fpga_obs_count` →
`recording_start` → `watch_set` → `recording_la_stream_start` → `recording_correlate`、成立の瞬間の MCU は `fpga_obs_trigger_set`）と、
**測った事実だけを返し原因は判定しない**こと、結果を読む前に欠け（`gaps`）を確かめることが書いてあります。通しの手順は [fpga-analysis.md](fpga-analysis.md)。

> `probe_info` で機能を確かめて、FPGA の観測（fpga_obs）が使えるなら観測を開いて。信号は <名前と幅> で定義して、
> MCU の <変数> も同時に記録しながら 30 秒動かし、`<結果>` が変わる直前に多い出来事を調べて。欠けがないかも先に確認して。

## 5. よくある失敗

| 症状 | 原因と対処 |
|---|---|
| ツール（`fprobe` CLI・pytest の実機試験・Python）が機器を開けない | デーモンが機器を握っている。デーモンを止める |
| MCP の道具が接続できない | 機器の指定がない / 違う（`--eth` の IP、`--serial`）。PC のアドレスが `192.168.77.1/24` か（PYNQ-Z2 の USB LAN 直結） |
| `fpga_obs_open` が使えない・失敗する | その構成に FPGA の観測が無い（Primer 等）可能性。`probe_info` の features を見る（無い構成での断られ方の文言は未確認） |
| `E_NOT_ALLOWED`（トークンが違う） | 別の PC からは `--token` を渡す。同じ PC なら `~/.fprobe/daemon_token` を読めるか（別の利用者・別の FPROBE_HOME） |
| `E_NOT_ALLOWED`（`path_not_permitted`） | 実行・書き出しのパスが許可の表に無い。`session_permissions` で表を見て、利用者が `~/.fprobe/permissions.json` に足し、デーモンを起動し直す |
| AI の要求が返ってこない | 人が一時停止中。画面で「再開」 |
| 道具の名前が違う | `.` を `_` にした名前。[../api.md](../api.md) で確かめる |
| Primer で `target_connect` が数十秒かかる | 要る線の構成でなければビットストリームを自動で書き替える（結果の `design_switch`。ロジアナ・記録の状態は消える） |

その他は [troubleshooting.md](troubleshooting.md)。
