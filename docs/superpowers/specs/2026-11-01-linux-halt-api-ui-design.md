# JTAG で全コアを止める Linux のデバッグを API と画面へ（`linux.halt.*`）

日付: 2026-11-01。対象: `fprobe.linux_halt`（docs/linux-halt.md）。

## 1. 要求（利用者、2026-11-01）

AI の操作と人間の操作は一対一。一方ができる操作は他方もできる。いまの `fprobe.linux_halt`（CTI の同時停止 `--cti-*`、FPGA の
停止の見張り、FPGA の時刻での上限 `--fpga-request-times`、`status`・`recover`・`rescue`）は CLI だけで、デーモンの API にも画面にも
無い。`linux.memap.connect_jtag` は API にあるが画面に無い。

守ること（CLAUDE.md）: 画面は考えない層（API の応答を描くだけ）、人と AI の操作・見えるものを一対一、ヒューリスティック禁止、
既定の暗黙の値を持たない。

## 2. 層と、二重の実装をしない形

```
CLI  python -m fprobe.linux_halt {attach, ps, threads, halt, resume, status, recover, rescue}
        │  引数 → HaltOptions（同じ名前）→ HaltSession の操作 1 つ → 閉じる
        ▼
fprobe.linux_halt.service   HaltOptions（設定・check）／open_backend／HaltSession（操作の中身）
        ▲
        │  linux_halt_*（Probe の mixin fprobe/probe_linux_halt.py。占有・排他の事実を足すだけ）
API  linux.halt.*（デーモン → MCP の道具・画面）
```

- `HaltOptions` は CLI の共通の引数と API の `linux.halt.open` の引数が同じ名前（`backend`・`jtag_khz`・`offsets`・…・`watch_slots`）。
  組の欠け・組み合わせの誤り（CTI の 5 つは全部か無し、見張りの 2 つは両方か無し、CTI と見張り・FPGA の時刻は併用しない等）は
  `HaltOptions.check()` が CLI と API で同じ文で断る。画面は同じ判断を持たない（送って、デーモンの文を出す）。
- 各操作の中身は `HaltSession` だけにあり、CLI のコマンドは「開く → 操作 → 閉じる」の薄い包み。
- 試験で一対一を固定する: API の `linux.halt.open` の引数の集合 = `HaltOptions` の欄 = CLI の共通の引数（機器の選び方
  `--probe-serial`・`--probe-eth` を除く）。`linux.halt.gdb.serve` の引数 = `attach` の引数（端末への表示 `-v` を除く）。

**主担当の判断**: CLI の `--probe-serial`・`--probe-eth` は API に無い。デーモンは起動時に機器を握る（`fprobe-daemon --serial / --eth`）ので、
それが同じ役目。OpenOCD の経路（`backend=openocd`）も API に入れる（中身が同じなので費用が小さく、CLI と一対一になる）。

## 3. API

| API | CLI | 中身（HaltSession） |
|---|---|---|
| `linux.halt.open`（共通の引数をすべて明示） | 各コマンドの共通の引数 | `HaltSession.open` |
| `linux.halt.close` | 各コマンドの終わり | `close` |
| `linux.halt.status`（read） | `status` | `status` |
| `linux.halt.halt_all` | `halt`（新） | `halt_all` |
| `linux.halt.resume_all` | `resume`（新） | `resume_all` |
| `linux.halt.processes` | `ps` | `processes` |
| `linux.halt.threads {pid}` | `threads --pid`（新） | `threads` |
| `linux.halt.recover` | `recover` | `recover` |
| `linux.halt.rescue` | `rescue` | `rescue` |
| `linux.halt.gdb.serve {pid, port, skew_limit_ns, thread_id, kernel_isa, max_step_retries, bp_unmapped, log?}` | `attach` | `gdb_serve` |
| `linux.halt.gdb.status`（read） | （attach の端末の表示） | `gdb_status` |
| `linux.halt.gdb.stop` | attach の Ctrl-C・Ctrl-Break | `gdb_stop` |

- 引数はすべて使う側が明示する。必須は `backend`〜`journal` と `fpga_request_times`（CLI の `--fpga-request-times` は付けないと false だが、
  API は true / false を明示させる。画面も選ぶまで送らない）。上限は ns の整数か `"unmeasured"`（測っていない、の明示。既定ではない）。
  `log`（パケットのログのファイル）だけは省ける: 省くと書かない（値を補わない）。
- 応答は事実だけ。停止の報告は `StopReport` をそのまま `{method, cores_halted, skew_upper_bound_ns, basis}`（上限が分からなければ null。0 ではない）。
  基準との比較は Engine の `skew_verdict`（使う側が渡した `skew_limit_ns` との比較）だけで、新しい判断を足さない。
- `processes`・`threads`・`recover` の止め方は規則で決める（推定しない）: 全コアが止まっていれば止め直さずに読み、走らせない
  （`stop = null`・`resumed = false`。`halt_all` で止めた世界をそのまま読む）。そうでなければ全コアを止めて読み、全コアを走らせる
  （`stop` = その停止の報告。CLI の ps・recover と同じ）。全部は止まらなければ止まったコアを走らせて断る。
- 並びは決めない: プロセスはカーネルのタスク一覧の順、スレッドはスレッド一覧の順（そのまま返す）。
- `status` はコアを止めない: 開いた事実（バックエンド・コアの数・HW の bp/wp の数・debug_ap・discovery・CTI・見張り・FPGA の時刻の有無）、
  渡した設定、占有、停止デバッグを立てているか、ジャーナルの事実、各コアの running・残っている bp/wp、gdb のサーバの事実。
  gdb のサーバが動いている間と、OpenOCD の経路では、コアを読まない（`cores = null` と理由 `cores_unread`）。

## 4. デーモンでの占有と排他

- `backend=fprobe` で開いている間、プローブの生の DAP をこの口が使う（`Session.dap_access` を 1 つ開いて `ArmACores` に渡す）。
  **主担当の判断**: 次を断る（誤りの文に理由と「linux.halt.close してから」）。応答の `occupies` に同じ一覧を事実として出す。
  - `target.connect`（pyOCD・Cortex-A の接続は同じ接続口を取り合う）
  - 線を直接動かす操作（`Probe._raw` を通るもの: `jtag.scan`・`probe.dp/ap.*`・`probe.pins.*` 等）
  - `linux.memap.connect_jtag`（`_raw` を通る）。逆に、止めずに読む口の JTAG が開いていれば `linux.halt.open` を断る。
  - 構成の書き替え（`probe.design.switch`・接続の自動の書き替え）: FPGA が替わると開いた DAP が消える。
  - ターゲットに接続中（pyOCD）は `linux.halt.open` を断る（同じ理由）。
- `backend=openocd` はプローブの DAP を使わないので、何も断らない（`occupies.probe_dap = false`）。
- コアの操作は 1 回ずつ機器の錠（`Probe._lock`）を取る（`service._Locked` が `CoreAccess` の各メソッドを包む）。gdb のスレッドと、
  調停の作業スレッドで動く他の操作（ロジアナ・CSR 等）が、同じ USB の口を混ぜない。gdb の「続行」の待ちは 1 回 20 ms まで
  （`Engine._wait` の `wait_event(0.02)`）なので、その合間に他の操作が通る（ロジアナとデバッガを同時に使う）。
- **主担当の判断**: gdb のサーバが動いている間（listening / serving）、`halt_all`・`resume_all`・`processes`・`threads`・`recover`・`rescue`・
  2 つ目の `gdb.serve` は断る（`Busy`）。理由: all-stop の Engine はコアの状態を持っていて、外から動かすと gdb の見ている世界と食い違う。
  FPGA の停止の見張りの間はホストが JTAG を使わない約束（docs/linux-halt.md）。`status` はコアを読まずに gdb の事実を返す。
  gdb の続行を止めたいときは gdb の ^C、gdb ごと終えるときは `linux.halt.gdb.stop`。
- 開いた口はデーモンのもの（WebSocket の利用者が切れても閉じない。`linux.connect` と同じ）。開いた利用者は `opened_by` に事実として出す。

## 5. gdb のサーバ（デーモンの中の RSP）

- `gdb.serve` は CLI の attach と同じ順で、失敗しうる所を応答の前に済ませる: ポートを開く（127.0.0.1 だけ。0 なら OS が選び、応答の
  `port` が開いたポート）→ 全コアを止めてプロセスを読む → ジャーナルの BKPT を戻す → Engine を作る → 全コアを走らせる。
  その後、別のスレッドで gdb を待ち、つないだら全コアを止めて（`attach_stop`）RSP に応える。
- 状態: `listening`（待ち）→ `serving`（gdb がつないだ）→ `ended`。終わり方 `ended_by`: `detach`（gdb の D・k）/ `disconnect`（gdb が
  切れた）/ `stop`（gdb.stop・close・デーモンの終了）/ `error`（`error` に文）。
- `gdb.stop` は止める印を立て、gdb の接続を shutdown する。RSP の読みの待ち・実行中の ^C の確かめが印を見て抜ける（ソケットの
  shutdown が別のスレッドの select を起こすことには頼らない。Windows では起きなかった）。

## 6. 強制終了・切断のときの後始末（BKPT を残さない）

| 起きたこと | 後始末 |
|---|---|
| gdb が detach・切断 | gdb のスレッドの finally: Engine.close（BKPT・HW の bp/wp を外す、キャッシュの一貫、全コアを走らせる）→ 停止デバッグを切る。失敗は `cleanup_errors` に事実で残す |
| `linux.halt.gdb.stop`・`close` | 同上が**終わるまで待つ**（時間で打ち切らない。打ち切ると BKPT を残したまま応答しうる） |
| デーモンの普通の終了（Ctrl-C・SIGTERM） | uvicorn が lifespan を抜け、`Probe.close` → `_release_device` → `LinuxHaltService._close_linux_halt`（後始末の段 linux_halt）（同上。錠の外で） |
| USB の切断（デーモンが開き直す `recover_link`） | 同じ `_release_device` を通る（切れた機器への後始末は失敗してよい） |
| WebSocket の利用者が切れる | 何もしない（口はデーモンのもの） |
| デーモン・CLI が強制終了（kill -9・電源断） | 後始末は走らない。BKPT がメモリに残り、HDBGen が立ったままなら BKPT でコアが止まり続ける（ターゲットが固まる）。**次のデーモン（または CLI）で `open` → `rescue`（bp/wp を消し、停止デバッグを切り、止まったコアを再開。メモリは触らない）→ `recover`（ジャーナルの BKPT を、今も BKPT がある所だけ元に戻す）**。`open` と `status` の応答の `journal.entries` が、残っているかもしれない BKPT の物理番地（事実。今もあるかは読まない）を示す |

- ジャーナルは BKPT を書くたびに書き出す（`bp.BreakpointTable`。前からの仕組み）。`gdb.serve` は始める前にジャーナルの BKPT を戻す（CLI の attach と同じ）。
- **主担当の判断**: `open` は自動では recover しない（CLI の ps 等と同じ。ジャーナルの事実を返し、戻すかは使う側が決める）。
- **主担当の判断**: `close` はコアの走り・止まりを変えない（`halt_all` で止めたコアは止まったまま。応答の `halted` は閉じる前に読んだ事実）。
  デーモンの終了でも同じ（止めたのは使う側の明示の操作）。BKPT を置くのは gdb のサーバだけで、それは上の表で必ず外す。
- 停止デバッグ: `open` で立つ（`ArmACores.open`）。`rescue`・gdb の終わりで切る。切った後に止めて読む操作・`gdb.serve` の前に立て直す
  （`ArmACores.enable_halting_debug`。`status` の `halting_debug` が今の事実）。

## 7. 画面（`LinuxHaltPanel`、View → Linux（JTAG で停止））

- 開く欄（必須に赤い *）とバックエンドの欄（CTI・見張り・OpenOCD。書いた欄だけを送る）。欄は空から（ボードの値・番地・速さを
  入れておかない。前に書いた値だけを戻す）。`fpga_request_times`・`backend`・`mmu_format`・gdb の選択は、選ぶまで送らない。
- ボタン: 開く・閉じる・状態を読む・全コアを止める・全コアを走らせる・プロセス・スレッド（pid）・BKPT を戻す・後始末・gdb のサーバを開く・止める。
- 描くもの（どれも API の応答のまま）: 開いた事実と設定、占有の一覧、コアの表（core・running・breakpoints・watchpoints）か `cores_unread`、
  ジャーナルの entries、最後の操作が返した `stop`（method・cores_halted・skew_upper_bound_ns（null は「分からない（0 の測定ではない）」）・basis）、
  プロセスの表（API の順）、スレッドの表、gdb の事実（state・connect・ended_by・error・recovered・cleanup_errors、
  stop_for_attach・attach_stop・last_stop（skew_verdict・skew_limit_ns は API の値）・stats）。
- 計算・判定・並べ替えの基準を持たない（16 進の書き方・「分からない」の言葉だけ）。だれが操作しても（AI も）操作の記録の通知で読み直す。
  gdb のサーバが動いている間は `linux.halt.gdb.status` を 0.5 秒ごとに読み直す（gdb の操作は API の記録に出ないので。描き直しの間隔）。
- Linux のパネルの「道 B」に `linux.memap.connect_jtag` の欄（chain_idcode・ap・frequency_hz。すべて明示）を足した。

## 8. 確かめたこと・確かめていないこと

- 模擬（SimCores + 合成のカーネルのメモリ）: `tests/linux_halt/test_service.py`（中身。gdb のサーバに本物のソケットで RSP をつなぐ）、
  `tests/test_api_linux_halt.py`（本物のデーモンと Probe。バックエンドを開く所だけ差し替え。人と AI が同じ事実を見る・占有・gdb.stop・
  デーモンの終了の後始末）、`tests/linux_halt/test_cli.py`（新しいコマンド）、画面 `host/ui/tests/linuxhalt.test.ts`。
- **未確認（実機）**: Primer → PYNQ-Z2（Zynq-7000、PJTAG）でデーモン経由の open・CTI・gdb。gdb のスレッドとロジアナ等を同時に使ったときの
  USB の口の共有（錠で順にしているが、実機の負荷で確かめていない）。OpenOCD の経路のデーモン経由。強制終了からの rescue → recover の実機。
