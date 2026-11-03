# デーモンの受け口の権限: 実行・書き込みの許可の表と、既定のトークン（2026-11-03）

元: 安全のレビュー（`docs/reports/2026-11-03-cloud-E.md` §5）で直していなかった 4 件への利用者の方針（2026-11-03）。

1. 中継の乗っ取り — 今は直さない（中継の C と実機が要る）。方針だけを決める（§4）。
2. API からの命令の実行・任意の場所への書き込み — 利用者が設定ファイルで決める **許可の表** にする（§1）。
3. 127.0.0.1 でもトークンを既定で作り、0600 のファイルに置く。画面と MCP はそれを読む（§2）。
4. 画面の URL から合言葉を外す（cookie）。`--lan` の暗号化は SSH の転送の手順を書く（§3）。

## 1. 許可の表

### 1.1 何を守るか
API（WebSocket の JSON-RPC。画面・MCP・Python の利用者・GDB の monitor・フック）から渡す PC のパスのうち、
- **exec**: PC で実行するファイル（`la.decoder_load` の `path` = Python の解読器、`linux.memap.connect` の `xsdb`）
- **write**: PC に書くファイル・フォルダ（`la.capture`・`la.raw_to_vcd` 等の `vcd_path`、`la.stream` の `raw_path`、`la.export`・`recording.export`・`snapshot.save`・
  `target.flash.read`・`rules.states` の `dot_path`・`rules.report`・`hooks.save`・`stream.log_start`・`profile.export`・
  `testpoint.export`・`testpoint.instrument` の `out_dir`・`map_path`、`snapshot.export_core` の `out`、`linux.core.fetch` の
  `local_path`、`debug.semihosting.configure` の `file_root`（`writable` が真のときだけ））

読むだけのパス（ELF・SVD・規則集等）は対象にしない（今回の方針の外。報告に残す）。
デーモンの中の保存物（`~/.fprobe` の記録・名前の表・sysroot 等）は API のパスでないので対象外。
Python のライブラリ（`Probe` を直に使う）は利用者自身なので対象外: 確かめるのはデーモンの受け口だけ。

### 1.2 仕様の印
`host/api/fprobe_api.json` の引数のスキーマに `"x-path": "exec"` か `"x-path": "write"` を書く。条件つきのものは
`"x-path-when": "<真偽の引数の名前>"`（その引数が真のときだけ確かめる。省略時は仕様の既定値）。
`Method.paths` = {引数の名前: (access, when)}。値が null・省略なら確かめない（その引数を使わない呼び出し）。

### 1.3 表のファイル
`<FPROBE_HOME>/permissions.json`（既定 `~/.fprobe/permissions.json`）。デーモンの起動時に読む（`--permissions` で別の
ファイルを渡せる）。読み直しは起動し直し（API からは変えられない・読み直させられない）。
```json
{ "exec":  ["C:/Xilinx/Vivado/2024.2/bin/xsdb.bat", "C:/Users/me/fprobe/decoders"],
  "write": ["C:/Users/me/fprobe/out", "D:/work/captures"] }
```
- 各項は **絶対パス**（`~` は展開する）。相対・空・型の違いは起動の誤り（黙って捨てない）。
- 項がファイルならそのファイルだけ、フォルダならその下すべて（シンボリックリンクと `..` を解いた後で比べる）。
- ファイルが無ければ（初めての起動。利用者の方針 2026-11-03）、CLI のデーモンは `<FPROBE_HOME>/out` だけに書いてよく、実行は
  何も許さない表を、そのファイルとして作る（`<FPROBE_HOME>/out` も作る。`prepare_permissions`）。作ったことと場所を起動の出力と
  `session.permissions` の `created` に出す。既にある表は書き換えない（壊れた表も書き換えず起動の誤り）。既定の許可は、
  このファイルに書かれた形でだけ持つ（コードの中の暗黙の許可は持たない）。

### 1.4 確かめ方
デーモンが検証（仕様）の後、実装を呼ぶ前に、印のある引数の値を `Path(値).expanduser().resolve()` にし、表の項のどれかと
同じか、その下にあるかを見る。
- 合えば、**解いた後の絶対パス** を実装に渡す（確かめた後にリンクを差し替えられても、確かめたパスに書く）。
- 合わなければ `E_NOT_ALLOWED`。文に引数・パス・種類・表のファイルを書き、`data` = `{"reason": "path_not_permitted",
  "param", "path", "access", "permissions_file"}`。
- 画面・MCP・Python・GDB の monitor・フックのどれから来ても同じ（`Daemon._validate` の後の 1 か所）。

### 1.5 見える化
`session.permissions`（read）: `{"checked", "file", "loaded", "created", "exec": [...], "write": [...]}` を返す。人と AI が同じ表を
見る。変える API は作らない。

## 2. 既定のトークン

- `fprobe-daemon` は `--lan` の有無によらず合言葉を持つ。`--token` で渡したもの、無ければ `<FPROBE_HOME>/daemon_token` の
  ものを使い回し、無いか `--new-token` なら作る。ファイルは持ち主だけが読める（0600。Windows は既定の ACL）。
- 合言葉は標準出力に出さない（MCP が起動したデーモンの出力は `daemon.log` に残る）。代わりにファイルの場所を出す。
- `DaemonClient(token=None)` は `<FPROBE_HOME>/daemon_token` があればそれを使う（同じ利用者の Python・MCP・道具は
  そのまま動く。ほかの利用者はファイルを読めない）。`--lan` の別の PC からは今までどおり `--token`。
- `create_app(token=None)` はトークン無し（試験・偽の機器のため。CLI からは作らない）。
- 前の版の `lan_token` は使わない（消さない）。

## 3. 画面の合言葉は cookie

- `GET /login`: 合言葉を入れる形（ファイルの場所を書く）。`POST /login`（`application/x-www-form-urlencoded` の `token`）:
  合えば cookie `fprobe_session`（値は合言葉から導いた HMAC。HttpOnly・SameSite=Strict・Path=/・1 年）を付けて `/` へ。
  違えば 403 で同じ形。Origin を送る POST は Origin = Host のときだけ（ほかのページから送らせない）。
- `GET /`: 合言葉のあるデーモンで cookie が合わなければ `/login` へ送る。`/assets` は合言葉なしで配る（中身は公開の画面）。
- WebSocket: cookie が合えば、`session.hello` の `token` は要らない。合わなければ今までどおり hello の `token`。
- 画面は `#token=` を読まない。hello が `E_NOT_ALLOWED` なら `/login` へ移る。開発（`npm run dev`）は Vite が `/login` も
  デーモンへ流す。

### 3.1 LAN から使う（暗号化）
`--lan` は平文の HTTP・WebSocket。暗号化して使うときは `--lan` を使わず、SSH の転送で 127.0.0.1 につなぐ手順を
`docs/guide/remote.md` に書く。

## 4. 中継（今は直さない。方針）

- 受け付ける相手は、中継の起動のときに使う側が明示する（PC のアドレスとトークン）。**最後に送ってきた相手を暗黙に信じない**。
- 明示の外からのデータグラム・TCP は PL に渡さず、数えて状態の問い合わせに出す（黙って捨てない）。
- PC の側（`EthLink`）は同じトークンを最初のつなぎで送る。形は中継の C と一緒に設計し、PYNQ の実機で確かめる。

## 5. 試験
- 許可の表: 表の読み込み（絶対パス・`~`・相対は誤り・無いファイルは空）、フォルダの下・ファイルそのもの・`..`・リンクで
  外へ出るもの、デーモンの拒否（E_NOT_ALLOWED と data）・許可（解いたパスが実装に届く）・`x-path-when`、
  `session.permissions`、仕様の印が全部の書き込み・実行の引数にあること。
- トークン: CLI の合言葉の作成（0600・使い回し・`--new-token`）、`DaemonClient` が既定のファイルを読む。
- cookie: `/login` の成否・cookie の属性・他の Origin の POST を断る・`/` の転送・cookie のある WebSocket は hello の
  token なしで通る。
