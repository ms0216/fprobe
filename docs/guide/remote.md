# 別の PC から使う・許可の表

仕様: [../superpowers/specs/2026-11-03-daemon-access-design.md](../superpowers/specs/2026-11-03-daemon-access-design.md)

## 1. 合言葉

`fprobe-daemon` は、127.0.0.1 だけで開くときも合言葉を持ちます。

- 場所: `~/.fprobe/daemon_token`（`FPROBE_HOME` を変えたらその下）。持ち主だけが読めます（POSIX は 0600、Windows は
  利用者のフォルダの既定の権限）。無ければ起動のときに作り、以後は使い回します。作り直すのは `--new-token`。
- 同じ PC・同じ利用者の `fprobe-mcp`・Python（`DaemonClient`）は、このファイルを自動で読みます。
- 画面（ブラウザ）は、初めて開くと `/login` で合言葉を聞きます。入れると HttpOnly の cookie になり、URL には載りません。
  合言葉を作り直したら、もう一度聞かれます。
- デーモンは合言葉を画面の出力に出しません（`fprobe-mcp` が起こしたデーモンの出力は `~/.fprobe/daemon.log` に残るため）。

## 2. 別の PC から、暗号化して使う（SSH の転送）

`--lan` は平文の HTTP・WebSocket です。合言葉も中身も LAN に見えます。別の PC から使うときは、`--lan` を使わずに
SSH の転送でデーモンの 127.0.0.1 につなぎます。

1. fprobe をつないだ PC（以下「機器の PC」）で OpenSSH サーバを動かす（Windows: 設定 → システム → オプション機能 →
   「OpenSSH サーバー」、`sshd` サービスを開始）。デーモンは普段どおり `--lan` なしで起動する。
2. 使う側の PC から転送をつなぐ:

   ```bash
   ssh -N -L 7820:127.0.0.1:7820 <利用者>@<機器の PC>
   ```

   GDB・流れの口も使うなら `-L 3333:127.0.0.1:3333` 等を足す。
3. 使う側の PC のブラウザで `http://127.0.0.1:7820/` を開き、合言葉（機器の PC の `~/.fprobe/daemon_token`）を入れる。
   使う側の `fprobe-mcp` は `--token <合言葉>` を渡す（`--listen 7820` のまま。デーモンを起こしに行かないよう、転送を
   先につなぐ）。

使う側の PC から機器の PC へ、合言葉のファイルを写す必要はありません（画面は 1 回入れるだけ。MCP は `--token`）。

## 3. 許可の表

API（画面・MCP・Python・GDB の monitor・フック）から、PC で **実行する** ファイルと、PC に **書く** 場所は、
利用者が `~/.fprobe/permissions.json` に書いたものだけ通ります。表に無いものは `E_NOT_ALLOWED`
（`data.reason` = `path_not_permitted`、どの引数・どのパス・どの表か）で断られます。表は API から変えられません
（`session.permissions` で見えるだけ）。直したらデーモンを起動し直します。

```json
{
  "exec": ["C:/Xilinx/Vivado/2024.2/bin/xsdb.bat", "C:/Users/me/fprobe/decoders"],
  "write": ["C:/Users/me/fprobe/out", "D:/work/captures"]
}
```

- 各項は絶対パス（`~` は使えます）。ファイルならそのファイルだけ、フォルダならその下すべて。リンクと `..` は解いてから
  比べます。
- **exec**: `la.decoder_load` の解読器（Python）、`linux.memap.connect` の `xsdb`。
- **write**: ロジアナの `vcd_path`（`la.raw_to_vcd` も）と `la.stream` の `raw_path`、`la.export`・`recording.export`・`snapshot.save`・`snapshot.export_core` の `out`・
  `target.flash.read`・`rules.states` の `dot_path`・`rules.report`・`hooks.save`・`stream.log_start`・`profile.export`・
  `testpoint.export`・`testpoint.instrument` の `out_dir` と `map_path`・`linux.core.fetch` の `local_path`、
  `debug.semihosting.configure` の `file_root`（`writable` のときだけ）。
- **初めての起動**: 表のファイルが無いと、デーモンは `~/.fprobe/out` だけに書いてよく、実行は何も許さない表を
  `~/.fprobe/permissions.json` として作ります（`~/.fprobe/out` も作ります）。作ったことと場所は、起動の出力
  （「許可の表を作った: …」）と `session.permissions` の `created: true` に出ます。書き出しは、まず `~/.fprobe/out` の下に
  書けば通ります。ほかの場所・解読器・xsdb を使うなら、このファイルに足して起動し直します。
- 既にある表は書き換えません（項目を全部消した表も、そのまま「何も許さない」表として使います）。形が違えばデーモンは
  起動しません（どこが違うかを出します）。
- 別の場所の表を使うなら `fprobe-daemon --permissions <ファイル>`。
- 読むだけのファイル（ELF・SVD・規則集等）は表の対象外です。
- デーモンの起動の引数（`--stream-log` 等）は利用者が書くものなので、表を通りません。
