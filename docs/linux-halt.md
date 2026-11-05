# JTAG で全コアを止めて Linux のアプリをデバッグする（`fprobe.linux_halt`）

状態（2026-11-03 に更新。下の各節に日付つきの実測）:
- **PYNQ-Z2 → Raspberry Pi 2B（2026-11-03、起動ファイル fw93、JTAG 25 MHz・取り込みの遅れ 3）**: 実機の一式（`tests/linux_halt/hw/test_hw.py`）
  29 件と HaltSkew が通った。FPGA の停止の見張り（再開を FPGA が書く回路）は 30 回すべて FPGA の方式で 19〜109 µs。こちらから
  止めるときの上限は、FPGA の時刻（`--fpga-request-times`）で 11.1 µs、PC の時計で 1.5〜3.3 ms（各 20 回）。
- Raspberry Pi 2B（Cortex-A7 × 4）で、fprobe（Tang Primer 20K、構成 `fprobe_jtag`）の JTAG 経由の実機試験 31 件が通った（実際の TCK 3.71 MHz・11.14 MHz。要求 4・15 MHz）。
- PYNQ-Z2（Cortex-A9 × 2）は、同じコードを OpenOCD 経由で動かした（CTI の同時停止を含む）。fprobe の JTAG（PJTAG）経由も、2026-11-01 に CTI の同時停止と時間差の測定を実機で確認した（AHB-AP のシステムのアドレスで、`ArmACores.open` が 2 コアと CTI に届く。`gateware/tests/hil/test_zynq_cti_hw.py`・`test_zynq_cti_skew_hw.py`）。Linux のプロセス一覧（`test_zynq_linux_awareness_hw.py`、`ps` と一致）と、標準の gdb でのアプリのデバッグ（`test_zynq_gdb_hw.py`。ブレークポイント・bt・next・step・変数・finish・detach。CTI で同時停止。`attach` の `--cti-*` 引数）も、2026-11-01 に Primer の PJTAG 経由で実機確認した。
- FPGA の停止の見張り（下）は、模擬（cocotb）と実機（Pi 2B、2026-11-02〜03）で確かめた。

プロトタイプ（別の作業場所 `linux-awareness-trial` の `lxdbg/`）の移植。仕様は同じ作業場所の `docs/fprobe-spec-draft.md`、計画は
`docs/superpowers/plans/2026-10-28-fprobe-linux-halt-debug.md`、事実の根拠は `docs/fprobe-integration-design.md`。

## 何をするか

全コアを JTAG で止め、カーネルの構造体を読んで止めた時点のプロセスを再現し、標準の gdb（RSP）でつなぐ。ターゲットに何も入れない。
**止めたときの状態が、イベントの瞬間の事実であること**（全コアの同時停止）が要件で、停止のたびに「方式・時間差の上限・根拠・基準との比較」を返す。

## 層

| モジュール | 役目 |
|---|---|
| `core_access` | コアを触る口（`CoreAccess`）、`StopReport`（停止の事実）、`CoreState` |
| `backend_arma` | fprobe 自身の経路。1 本の DAP で Cortex-A × n（`ArmDebugCore`）。CTI か FPGA の停止の見張りがあれば使う |
| `backend_openocd` | OpenOCD 経由（ボードは `fprobe/plugins/openocd_boards.py`） |
| `cti` | CTI のドライバ。ボード固有の値は `fprobe/plugins/zynq_cti.py` |
| `fprobe.halt_watch` | FPGA の停止の見張り（CTI の無い SoC） |
| `mmu` / `kernel` / `process` / `usermem` | ページテーブルのウォーク、カーネル構造体（DWARF）、プロセス、仮想メモリ |
| `bp` / `watch` | ブレークポイント（物理アドレスで管理）、ウォッチポイント |
| `engine` | all-stop の実行制御（保留イベント・ステップ・終了・シグナル） |
| `rsp` | gdb RSP サーバ（`fprobe.gdb.rsp` の整形を使う。デーモンの `GdbSession` には未統合） |
| `service` | CLI とデーモンの API `linux.halt.*` が呼ぶ中身（設定 `HaltOptions`・操作 `HaltSession`。gdb のサーバは別のスレッド） |

## デーモンの API と画面（`linux.halt.*`、2026-11-01）

設計: `docs/superpowers/specs/2026-11-01-linux-halt-api-ui-design.md`。CLI の各コマンドと一対一（中身は同じ `service`）:
`linux.halt.open`（共通の引数）・`close`・`status`・`halt_all`（`halt`）・`resume_all`（`resume`）・`processes`（`ps`）・`threads`（`threads --pid`）・
`recover`・`rescue`・`gdb.serve`（`attach`。RSP サーバをデーモンの中で開き、手元の gdb が `target remote 127.0.0.1:port`）・`gdb.status`・`gdb.stop`。
fprobe の経路で開いている間はプローブの生の DAP を占有する（target.connect・jtag.scan 等・linux.memap.connect_jtag・構成の書き替えは断る）。
画面は View → Linux（JTAG で停止）。デーモンが強制終了されたら、次のデーモンで `open` → `rescue` → `recover`（下の「強制終了された
デバッガの後始末」と同じ）。

## 設定の保存・読み込み（`linux.halt.profile.*`、2026-11-05）

`linux.halt.open` と `linux.halt.debug.attach` の引数は多いので、ファイルに書いて使い回せる。
- `linux.halt.profile.save(path)`: 開いている口の `open` の引数と、attach 中なら `attach` の引数（**pid は除く**。実行のたびに変わる）、
  offsets のファイルの SHA-256 を書く。
- `linux.halt.profile.load(path)`: 読んで返すだけ（何も開かない・動かさない）。返った `open` を `linux.halt.open` へ、`attach` に pid を足して
  `linux.halt.debug.attach` へ渡す。`offsets_matches` は保存時と今の offsets のファイルの SHA-256 の一致（事実だけ。合わなくても止めない。
  カーネルを入れ替えたら offsets を作り直す）。
- 画面（Linux のパネルの「設定」の「設定ファイル」）の読み込みは、欄に入れるだけ。「開く」「attach」は人が押す（AI が読んで渡すのと同じ操作が記録に残る）。
- 形式は `{"format": "fprobe.linux_halt.profile", "version": 1, "saved_at_unix_s", "open", "offsets_sha256", "attach"}`。値に既定を補わない
  （ファイルに無い引数は API が断る）。仕様: docs/superpowers/specs/2026-11-05-linux-halt-profile-design.md

## 止め方と、測った時間差（2026-10-29。どれも 0 ではない）

時間差は、目標の側で測った値（各コアがカウンタを r4 に読み続け、止めた後に読む。root で実行。`linux-awareness-trial/tools/skew_fine.py`）。

| 止め方 | ボード | コア間の時間差 |
|---|---|---|
| CTI でこちらから止める（`use_cross_halt`） | PYNQ-Z2 | 40 回で最大 31 ns。400 回の測定（2026-11-01、Primer の PJTAG。下の `--cti-*` の節）では最大 8 刻み = 49 ns で、当時の上限 55 ns は小さすぎた |
| ブレークポイント → CTI の入力 0 → 他のコア | PYNQ-Z2 | 209〜252 ns |
| 全コアへの停止要求を 1 回の USB の往復で（CTI なし） | Pi 2B、JTAG 11.14 MHz（要求 15 MHz） | 23.3 µs（3.71 MHz で 66.4 µs） |
| **fprobe（Primer の PJTAG）で** CTI でこちらから止める（`use_cross_halt`） | PYNQ-Z2、JTAG 2 MHz | 40 回中 37 回が有効。中央値 12 ns、最大 31 ns（1 刻み 6.15 ns）。2026-11-01。400 回（負荷あり・なし）では最大 8 刻み = 49 ns（下の `--cti-*` の節） |
| **fprobe（Primer の PJTAG）で** ブレークポイント → CTI の入力 0 → 他のコア | PYNQ-Z2、JTAG 2 MHz | 40 回中 40 回が有効。コア 1 が 209〜252 ns 遅れ（中央値 234 ns）。OpenOCD 経由と同じ値。2026-11-01 |
| ブレークポイント → ホストが気づく → 他のコア | Pi 2B、JTAG 11.14 MHz（要求 15 MHz） | 526〜1051 µs |
| ブレークポイント → FPGA の停止の見張り → 他のコア | — | 未測定（模擬では動く）→ 下の 2026-11-02 の節 |

## PYNQ-Z2 → Raspberry Pi 2B の実機（2026-11-02 夜、JTAG の線を短くした後）

条件: PYNQ-Z2 の JA（16 mA・SLEW FAST、fw72 = fprobe_pynq_deep）→ Pi 2B のヘッダ、継ぎ目の無い短いデュポンの線、GND は Pi の 20・14 番の 2 本、
TRST は Pi の側でプルアップ。Pi の ARM は 600 MHz（governor powersave）。PC は Ethernet（<家の LAN のアドレス>）。

- 線の化け: 4 MHz で 1 万回 × 3、2 万回で 1・4・10 MHz とも失敗 0（`docs/boards/pynq-z2.md`）。25 MHz は TDO の取り込みの遅れ 2〜4 が正しく
  （10・16.7 MHz は 0〜4）、遅れ 3 で 4 コアの DBGDSCR の読み 2 万回の失敗 0。
- 実機試験（`host/tests/linux_halt/hw/test_hw.py`、JTAG 4 MHz）: HaltSkew 以外の 29 件すべて通過（Exit の 6 件を含む。後始末の直し d954c71e・fda2aa42 の後）。
- HaltSkew（対象の側の時刻。lxspin の各コアが自分の時刻を書き続け、止まった後に読む。3 回、JTAG 4 MHz）:

| 止め方 | 結果 |
|---|---|
| こちらから全コアを止める（gdb の割り込み） | コア間の時間差 0.065 ms・0.065 ms・0.065 ms |
| ブレークポイント → ホストが気づく → 他のコア | 最後のコアが止まるまで 1.603・1.057・1.149 ms |
| ブレークポイント → **FPGA の停止の見張り** → 他のコア（`--fpga-halt-watch-*`） | 0.153・0.254 ms、1 回だけ 3.625 ms（見張りが効かなかった疑い、未調査） |
| ブレークポイント → ホスト、上限を FPGA の時刻で（`--fpga-request-times`） | 1.017・1.453・3.962 ms |

- **JTAG 25 MHz**（TDO の取り込みの遅れ 3、`jtag.sample_delay_set`）: HaltSkew 以外の 29 件すべて通過。HaltSkew（3 回）:

| 止め方 | 結果（25 MHz） |
|---|---|
| こちらから全コアを止める | コア間の時間差 0.011・0.011・0.011 ms（ホストの回）、0.012 ms × 3（FPGA の見張りの回） |
| ブレークポイント → ホストが気づく → 他のコア | 0.937・1.068 ms、1 回 5.438 ms |
| ブレークポイント → **FPGA の停止の見張り** → 他のコア | **0.025・0.021 ms**、1 回 4.290 ms |

  FPGA の見張りの 21〜25 µs は、目標（PL §7: 25 MHz・4 コアで約 15〜42 µs、導出）の中。
- **数 ms の回の原因（2026-11-02、fw75、25 MHz、API で 1 回ずつ停止の根拠 basis を出して確かめた）**: 全コアの再開（`ArmACores.resume`）が
  コア 0 → 3 を 1 つずつ（PC と PYNQ の往復を 1 つごとに）再開し、全部で数 ms かかる。見張りは全コアが走ってから有効になる。その間に
  コア 0 がブレークポイントに当たると、残りは「まだ再開していない（parked）」か「見張りが無い（host-timed）」になり、ホストが止めて
  ms かかる。6 回の内訳: 見張り 0.021 ms が 1 回、host-timed 0.051・3.9 ms が 2 回、「コア 2・3 を再開する前に別のコアが止まった」
  1.9〜3.7 ms が 3 回。止め方（見張り）の不具合ではなく、**再開の手順の遅さ**。改善: 全コアの再開を 1 回の往復にまとめる（ホストだけ）、
  さらに FPGA が全コアを一斉に再開して見張りを有効にする（回路）。再開のコア間の時間差も同時に縮む。
- **全コアの再開を 1 回の往復に（2026-11-02、ホスト。設計 docs/superpowers/specs/2026-11-01-stop-precision-design.md 3.8）**:
  `ArmACores.resume` は、全コアのレジスタを戻してから（どのコアもまだ止まったまま）、全コアの DBGDRCR の再開の要求と DSCR の読みを
  1 回の往復で送る。RESTARTED が 0 のコアだけ読み直す。途中で取りやめる「残り」が無いので parked は使わない（すぐ止まったコアは次の
  `wait_event` が見つけて他を止める。その停止の上限も分かる）。「ここより前には止まっていない」は要求を送る直前の時刻（後の時刻は
  上限が嘘になる）。偽の DAP で resume_all の往復 34 → 17、最初のコアが走り出してから最後のコアまで 24 → 0。
  **実機（2026-11-02、fw75 = 今日の回路の変更を全部入れた fprobe_pynq_deep、25 MHz、遅れ 3）**: 一式 29/29。HaltSkew: こちらから止める
  0.012〜0.013 ms、bp → 最後のコア: ホスト 0.990〜1.756 ms、**FPGA の見張り 0.040・0.029・0.035 ms（数 ms の回なし）**。API で 10 回、停止の根拠付き:
  8 回が FPGA の見張りで 0.019〜0.041 ms、2 回はホストの方式（attach の直後の 1 回はブレークポイント以外の停止で g_hit が前の呼び出しのまま、
  もう 1 回は 0.040 ms で見張りが有効になる前）。改善の前（fw72）は 6 回中 5 回が数 ms だった。
  **実機（2026-11-03、fw79 = 8f538b91 = S1〜S3・H2・b72168fa を入れた fprobe_pynq_deep、25 MHz、遅れ 3）**: 一式 29/29。HaltSkew: こちらから
  止める 0.011〜0.012 ms、bp → 最後のコア: ホスト 0.609〜0.818 ms、FPGA の見張り 0.124・0.042・0.031 ms。API で 20 回: 18 回が FPGA の見張りで
  0.025〜0.043 ms、2 回がホストの方式（ms）。**ホストの方式に落ちる理由を突き止めた**: `ArmACores.resume` は「再開 → DSCR の確認 → 見張りを
  arm」の順で、arm までの ms の窓（Ethernet の往復）で bp に当たったコアは、値の一致（入った瞬間だけ発火し、enable の後のその番号の最初の
  標本では出さない）で発火せず、`any(scan)` で見張りを外す。**直した（2026-11-03、設計書 §3.6。実機は下の「再開を FPGA が書く」で確認）**: 再開の前に arm し、
  サンプラが走っている標本を一度も見ないうちに止まったコアを、回路の印（`sampler_match.unmatched`）とホストの DSCR の読みで確実に
  見分けてホストの方式に移る（タイムアウトを待たない）。外す前に発火していれば FPGA の停止として報告する。
- **DAP の往復を減らした（2026-11-03、ホスト）**: コアに命令を入れる手順（ITR・DTR）を、命令ごとの DSCR の読みと一緒に 1 回の往復に
  まとめ（`V7DebugPort.run`。返った DSCR で「前の命令が終わっていた・DCC が空だった・各命令が終わった」を順に確かめ、確かめられなければ
  待って空にしてから 1 命令ずつやり直す。列は頭からやり直せるものだけ）、全コアの入る（止まった直後の r0・r1・PC・CPSR の読み）・戻す
  （再開の前）・状態の読み（CONTEXTIDR・SVC の SP）を全コアで 1 回ずつの往復に、ブレークポイントの組の 3 語の書き・組の点検・接続の
  ROM テーブルの走査とロックの解除をまとめた。偽の DAP の往復（前 → 後）: 接続 152 → 29、止める 59 → 5、状態 24 → 2、g 26 → 1、
  G 13 → 1、continue 22 → 3、ブレークポイントで止まった後の停止 53 → 3、ステップ 25 → 7、m 4 B 5 → 2、m 64 B 11 → 4、M 4 B 17 → 6、
  組の点検 44 → 1。`host/tests/test_armv7a_batch.py` が性能の約束（上限）として固定する。メモリの LDR / STR は頭から
  やり直せない（後置きの加算・MMIO の読み書きの副作用）: 1 語の読みは LDR と受け取り（MCR r1）を 1 回の往復にし、確かめられなければ
  LDR はやり直さず受け取りだけをやり直す（`execute_once_and_read`）。STR の列はまとめていない（DTRRX が空だと書く前に分からないと、
  前の値を書いてしまう）。
- FPGA の停止の見張りを使うと、終わった後に PYNQ の DAP（やがて制御の口も）が応答しなくなる不具合があった（`ArmACores.close` が
  見張りを外さずに閉じた）。8e6d23d5 で直し、直した後の上の値は試験の前後で PYNQ が応答し続けた回のもの。

## FPGA の停止の見張り（CTI の無い SoC）

設計: `docs/superpowers/specs/2026-10-29-halt-watch-design.md`。全コアが走っている間、FPGA が各コアの DBGDSCR を読み続け、1 つが止まったら
FPGA が全コアの DBGDRCR に停止要求を書く。ホストが USB 越しに気づくのを待たない。既存の部品（DAP サンプラ・値の一致・クロストリガ・
割り込み書き込み）をつないだもので、それらを持つ構成が要る（PYNQ-Z2 の `fprobe_pynq`。Primer には入らない）。

```
python -m fprobe.linux_halt attach --backend fprobe --part bcm2836 --probe-eth <家の LAN のアドレス> --probe-eth-link-up-wait 15 \
    --fpga-halt-watch-bound-ns unmeasured --fpga-halt-watch-poll-s 0.001 ...
```

- `--probe-eth-link-up-wait`: `--probe-eth` で開くとき、リンクの確立を待つ秒数（識別の READ をこの間だけ送り直す。既定値は無い。
  送り直した回数は開いたときに `probe identify read: resent N times` と出す。デーモン経由では API の `probe.info` の `link_open`）。
- `--fpga-halt-watch-bound-ns`: 1 つのコアが止まってから最後のコアが止まるまでの上限（目標の側で測った値）。測っていなければ `unmeasured`
  と書く（既定値は無い）。停止の報告は、FPGA が止めた場合でも「順の停止」で、上限はこの値だけを使う。
- 待つ間、ホストは FPGA の状態を 1 回の CSR の読みずつ読む。FPGA が全コアの走っている値を読んだ（`sampler_match.unmatched`）後は
  JTAG を使わない（見張りの邪魔をしない）。どちらで止めたかは `last_wait["by"]`（"fpga" / "host"）。
- ホストがコアを個別に止める・ステップする前には、見張りを外す。見張りは再開の要求の前（全コアが止まっている間）に始める。
  FPGA が走っている値を読む前に止まったコア（再開の直後のブレークポイント等）は発火しないので、ホストが止まっているのを読んだ後の
  FPGA の印でそれを確定し、ホストの方式に移る（`last_wait["fpga_missed"]`、報告の basis に理由。設計書 §3.6。2026-11-03）。
- 外すときは値の一致を先に止め、外した後に状態を読む: 外す前に FPGA が止めていれば、FPGA の停止として報告する。
- **再開を FPGA が書く（ケイパビリティ `watch_start` の回路、2026-11-03）**: 再開の要求（全コアの DBGDRCR ← RRQ | CSE）も FPGA が
  割り込み書き込みの「始動の列」で書き、その後の読みだけを比べる。条件は DBGDSCR の HALTED = RESTARTED = 1（再開の後に止まった。
  ARMv7 の定義）で、最初の読みでも発火する。上の「FPGA が走っている値を読む前に止まったコア」の窓が無くなり、待つ間 JTAG を使わない。
  報告の basis に「FPGA が再開を書いた・RESTARTED・最初の読みだったか」を書く（`last_wait["fpga"]` の `restarted_by_fpga`・`fire_first`・
  `t_restart_first/last`）。設計 `docs/superpowers/specs/2026-11-03-halt-watch-fpga-restart-design.md`。実機（2026-11-03、fw93、Pi 2B、25 MHz）: 見張り 30 回すべて
  FPGA の方式で 19〜109 µs、30 回とも basis に「FPGA が全コアの再開を書いた」。再開の直後の最初の読みで止まった回はこの試しでは 0 回。
- サンプラ・値の一致・割り込み書き込みを占有する（同時に他の用途には使えない）。

## CTI の無い「こちらから全コアを止める」の上限を、FPGA の時刻で（`--fpga-request-times`、2026-11-01）

設計: `docs/superpowers/specs/2026-11-01-stop-precision-design.md` 3.3。実装: `fprobe/linux_halt/request_times.py`、`ArmACores.use_fpga_request_times`。

いまの上限（PC の時計）は「走っているのを PC が読んだ時刻」から「全部止まったのを PC が読んだ時刻」までで、USB / Ethernet の往復が入る
（過去の Pi 2B: 実際 約 23 µs に対し報告 107 ms）。`--fpga-request-times`（`ArmACores.use_fpga_request_times(EventRequestTimes.open(device))`）を
付けると、各コアの DBGDRCR への停止要求の書き込みに FPGA が付けた時刻（イベント記録の種類 1。DRW の番地はホストが書いた TAR から FPGA が
求めたもの。ACK が OK = JTAG-DP が要求を受理した転送）を使う:

    上限 = t(最後のコアの書き込み) − t(最初のコアの書き込み) + (D_first − D_last)、 D_first − D_last = 0 周期
    （ns への換算は timebase_hz で切り上げ）

**DAP の時刻が線上の点の回路**（ケイパビリティ `dap_line_time`。`docs/superpowers/specs/2026-11-01-time-accuracy-design.md` 段階 S2、§12）:
種類 1 の時刻は、その書き込みの DR スキャンの Update-DR を出る TCK の立ち上がり（対象に届く点そのもの。PHY が `rsp_time` として出す）。
端子の段（`dap_output_ticks`。PYNQ-Z2 の IOB の FF）はどの書き込みにも同じだけ足されるので差に入らない。D は定義から 0 で、下の段数の
議論は要らない（上限 = t(最後) − t(最初)）。

以下の D の段数の表は、**古い回路**（時刻 = PHY の完了の 2 刻み後）のもの。古い回路でも差を取るだけなので式は同じ:

D = 「書き込みが対象に届く瞬間（JTAG の Update-DR の TCK の立ち下がり。JTAG-DP はここで AP の書き込みを始める）から、その転送に時刻が付く
瞬間まで」。どの書き込みでも同じ手順・同じ設定を通るので、差は 0 周期（推測の余裕は足さない）。D の値そのものは、上限には入らない（引き算で消える）:

| 段 | 周期（sys） | 根拠 |
|---|---|---|
| Update-DR の立ち下がり（G_DR_TAIL の 2 ビット目の emit。TMS は 1 ビット目で 1 → Update-DR） | 起点 | `jtag_phy.vhd:223-225, 230-232` |
| そのビットの Low 相・High 相 | hp + hp（timer は hp−2 から −1 まで） | `jtag_phy.vhd:378-396, 398-412` |
| 区間の終わり → `finish` → G_IDLE | 1（取り込みの残りを待つ周期があっても、sample_delay と hp だけで決まり、書き込みごとに同じ） | `jtag_phy.vhd:347-362, 266-271` |
| idle のクロック N ビット（DAP_TransferConfigure の値） | N·(2hp+1) | `jtag_phy.vhd:226-232, 349` |
| G_IDLE の終わり → `done` | 1 | `jtag_phy.vhd:368-370, 140-141` |
| `debug_port` の応答のレジスタ | 1 | `debug_port.vhd:97-108` |
| サンプラ・割り込み書き込みの応答の口（自分の命令でなければ素通し。調停の待ちは命令の側で、時刻の前に入る） | 0 | `dap_sampler.vhd:197`、`swd_injector.vhd:169` |
| `dap_monitor` の ev_valid のレジスタ | 1 | `dap_monitor.vhd:114-116` |
| `event_collector` がその周期の time_count を付ける（ホストの転送が最優先。待ちなし） | 0 | `event_collector.vhd:114-116` |
| タイムベースは sys の毎周期 1 ずつ（刻み = sys の周期。量子化の誤差なし） | — | `timebase.vhd:35` |

よって D = 2hp + 4 + N·(2hp+1) 周期（hp = TCK の半周期の設定、N = idle のクロック数）。hp・N・TDO の取り込みの遅れは、1 回の DAP_Transfer
（全コアの要求を 1 回の往復で送る `RawDap.mem_write32_many`）の中では変わらない（DAP_SWJ_Clock・DAP_TransferConfigure は別の命令）ので、
D はどの書き込みでも同じ。設計書 3.1 で 1 ビットの長さが変わっても、D の値が変わるだけで差は 0 のまま。
注（2026-11-02）: 上の表の idle の行の N·(2hp+1) は、設計書 3.1 の前の回路（1 ビット = 2hp + 1 周期）の数え方。3.1 の後の jtag_phy は
1 ビット = 2·hp 周期（`jtag_phy.vhd:12-13`）なので、idle は N·2hp、D = 2hp + 4 + N·2hp 周期（導出。シミュレーションで数えていない）。差が 0 なのは変わらない。

**仮定**（報告の根拠の文と `StopReport.assumptions` に書く。使う側が判断する）: 各コアが DBGDRCR への書き込み（対象の中の APB の転送）を
受けてから止まるまでの遅れは、コアごとに同じ。測っていない。同じ報告に、PC の時計の上限（この仮定なしの値）も
`bound_without_assumptions_ns` に並べる（`host clock: … ns`。API の `linux.halt` の stop、画面の停止の報告にも同じ 2 つの欄）。

FPGA の時刻を使うのは、次をすべて満たす停止だけ。満たさなければ PC の時計の上限のまま、理由を根拠の文に添える（`host-timed: …; the FPGA
timestamps were not used: <理由>`。黙って混ぜない）:
- 全コアが走っているのを読んでから止めた（ブレークポイント等で先に止まったコアを補う停止は、そのコアの止まった時刻が書き込みの時刻ではない）。
- 書き込みの後、状態の読み出し（`ArmDebugCore.halt` は止まっていなければもう一度要求を書く）の前に、全コアが止まっているのを読んだ。
- 全コアの止まった理由（DSCR.MOE）が停止要求（走っているのを読んだ後・要求が届く前に自分で止まったコアは、MOE が別の理由になる）。
- 記録が使える: この構成にイベント記録がある・記録中・この停止の間に記録できなかったイベントが無い（lost が増えていない）・リングが
  読む前に上書きしていない・始め直されていない。
- 各コアの書き込みが、番地と値で 1 件ずつ見つかる（番地を追えなかった DRW = 種類 12 があれば、並びの順で当てはめない）。

CTI とは一緒に使わない（CTI はハードウェアが止める）。FPGA の停止の見張りとは一緒に使える（見張りの無い停止 = gdb の中断等に使う）。
**実機（2026-11-03、fw93 = `dap_line_time` の回路、Pi 2B、JTAG 25 MHz、こちらから全コアを止める 20 回）**: 上限は 20 回とも 11.1 µs
（1110 刻み @ 100 MHz。PC の時計では 1.5〜3.3 ms）。対象の側で測った止まりのずれ（HaltSkew）は 11〜13 µs。初めは `EventLog.read` が
窓の道を選ばず 0 バイトで止まった（TCP の流れがつながっているとき。edbdbc77 で直した）。古い回路の D の式は RTL を読んで出したもので、
シミュレーションで数えていない。

## コアが自分で止まった停止（ブレークポイント等）の上限を、FPGA の時刻で（2026-11-03）

実機の評価（PYNQ → Pi 2B、linux.halt.debug.*）で、ブレークポイントで止めたときの上限が、FPGA の見張り（bound unmeasured）では null、
見張りなしでは PC の時計で 3.7〜4.8 ms だった。こちらから止めるときの `--fpga-request-times` と同じく、FPGA の時刻で出せれば出す
（出せなければ理由を basis に書く）。どちらも、要求で止めたコアは「書き込みが届いた瞬間に止まる」の仮定つき（`StopReport.assumptions`。
APB の転送とデバッグ状態に入るまでは測っていない）。

- **FPGA の見張りが止めた停止**（`ArmACores._watch_span`）: 上限 = FPGA の最後の停止要求の書き込みの点（`inject_time`、Update-DR）−
  止まったコアを FPGA が最後に「走っている」と読んだ標本の読みを頼んだ転送の点（`sampler_match.prev`）。条件: DAP_LINE_TIME の回路
  （古い回路の標本の時刻は値が届いた周期で、止まった時刻の下限にならない）・他の全コアの止まった理由が停止要求（DSCR.MOE。自分で
  止まったコアはその標本より前に止まりえる）。呼び出し側の上限（`--fpga-halt-watch-bound-ns`）は根拠の文に並べ、使えないときの上限にする。
  仮定なしの上限は PC の時計。
- **ホストが見つけた停止**（`--fpga-request-times`。`request_times.event_stop_ticks`）: 再開の前にイベント記録の位置を取り（`resume`）、
  停止の後に記録の末尾を、要る転送が見つかるまで広げて読む。下限 = 各コアについて、境（止まったコア: 止まったのを読んだ DSCR の読み、
  他のコア: 停止要求の書き込み）より前の「HALTED = 0 を読んだ DSCR の読みを頼んだ転送」か「再開の要求の書き込み」のいちばん後の
  ものの最小（DAP_LINE_TIME の回路は転送の点、古い回路はその前の転送の時刻）。上限 = 最後の停止要求の書き込み（仮定つき）/ 各コアが
  止まっているのを読んだ読みの値が返った転送（仮定なし。`bound_without_assumptions_ns`）。使えないとき（記録が無い・再開から後に
  記録できなかったイベントがある・番地を追えなかった DRW・再開しなかったコア等）は PC の時計の上限と、その理由。

**実機（2026-11-03、e7837b7、PYNQ-Z2 → Pi 2B、linux.halt.debug.* でデータ競合のアプリ race.c:11 のブレークポイント、各 5 回。
`tests/linux_halt/hw/test_hw_debug.py`）**: FPGA の見張りあり 40.8〜61.3 µs（仮定なし = PC の時計 約 9 ms）、見張りなし・
`--fpga-request-times` 1.06〜1.55 ms（仮定なし = FPGA の時刻 2.1〜2.6 ms）。前（PC の時計・見張りの bound unmeasured）は 3.7〜4.8 ms・null。
10 回とも両スレッドの行・who・before・after・counter を 1 回で読め（両スレッドが同じ値を読んで同じ値を書く瞬間が取れた）、
後始末で Pi は無事。

## Zynq-7000 を Primer の JTAG（PJTAG）で止めるときの `--cti-*`（2026-11-01 の実測）

```
python -m fprobe.linux_halt attach --backend fprobe --part zynq7000 --jtag-khz 2000 ...     --cti-board zynq7000 --cti-request-bound-ns 74 --cti-event-bound-ns 277 --cti-hw-wait-s 0.05 --cti-ack-polls 100
```

- CTI の番地の足し方は、DAP の見え方（`ArmACores.discovery`）で決まる: APB-AP の番地なら 0x80098000 − 0xF8898000、AHB-AP のシステムのアドレスなら 0（`plugins/zynq_cti.py` の `apb_offset_for`）。2026-11-01 の午前は AHB-AP だけ、夜は AP 1 に APB-AP が見えた（PS の再起動の後。理由は未確認）。
- 上限の値は、使う側が決める引数で、この測定から出した値の例（`test_zynq_cti_skew_hw.py`、各コアが Global Timer を r4 に読み続け、止めた後に r4 を読む）。
  上限 = 測った最大の差 + ldr 1 つの長さ（3.02 刻み）+ タイマーの 1 刻み（6.15 ns）。
  - こちらから止める: 最大 8 刻み（49 ns。400 回、負荷あり・なしとも。40 回では 5 刻み = 31 ns で、上限 55 ns は小さすぎた）+ 3.02 + 1 刻み = 12.02 刻み = 74 ns。
  - ブレークポイント → CTI: 最大 41 刻み（252 ns）+ 3.02 + 1 刻み = 45.02 刻み = 277 ns。
- 別の個体・配線・JTAG のクロックでは、測り直した値を渡す。測っていなければ `unmeasured`（既定の値は無い）。
- 強制終了されたデバッガの後始末: `python -m fprobe.linux_halt rescue ...`（bp/wp を消し、停止デバッグを切り、止まったコアを再開）→
  `recover`（メモリに残った BKPT を戻す）。**共有ライブラリ（libc）に BKPT が残ると、全プロセスが落ちる**（sshd を含む）。強制終了しない。

## ステップ・終了・シグナルの扱い（2026-11-06。Haiku の実機の周回と独立のレビューで直した分。実機の確かめは Pi が戻ってから）

- **ELF の外へ戻る step**（main が libc へ戻る等）: 直前の命令が呼び出し（BL / BLX）だったかは、**LR が「その命令の次」になったか**で決める（命令セットの事実。`call_made`）。
  戻りなら LR まで走らず、そこ（ELF の外）で止まる（以前は LR = 0 などで番地 0 にブレークポイントを置こうとして `0x0 is not mapped` で落ちた）。
- **step out の戻り先**: 呼び出し元のフレームの戻り番地の **bit 0**（Thumb か ARM か）を `FrameState.thumb` に残す。実行ファイルにも取り寄せたシンボルにも無い所（stripped の libc の内部）への戻りは、この bit 0 で幅を決める。
- **例外に入った命令を待つ間はハードウェアのブレークポイント**: step した命令が未定義命令の例外（VFP の初回使用など）に入ると、その命令に戻るのを待つ。ここでソフトウェアのブレークポイント（メモリを BKPT に書き換え）を使うと、
  カーネルが例外の処理で命令をメモリから読み直して BKPT を読み、**SIGILL で死ぬ**（`data_types` の step over で再現）。メモリを書き換えないハードウェアのブレークポイントを使い、空きが無ければソフトウェアに戻る。
- **プロセスの終了は固定される**: 終了を一度見たら（`last_stop.kind = exit`）、以後の step・読み・ブレークポイント・メモリは、最初の操作も含めて同じ文（「プロセス N は終了している（exit, code …）。close して attach し直す」）で断る。
  状態の読みと close は通る。終了コードはカーネルの値で `(終了状態 << 8) | 終了させたシグナル`。
- **シグナルで止まった**とき（reason = signal）、`debug.step` の応答に `signal`（番号）と `signal_name`（SIGSEGV 等。Linux ARM の番号の表）が付く。シグナルが配送される直前で止まっていて、次に走らせると配送される（致命的ならプロセスが死ぬ）。
- **共有ライブラリの中のブレークポイント**: 名前・ファイル:行 は attach の ELF（実行ファイル）だけで解く。ライブラリの中は実行時の番地（`"0x76dc02ec"`）で置け、命令セットは取り寄せたシンボルの bit 0 で決める。
- `linux.halt.processes` はカーネルのタスク一覧を JTAG で読むので、**対象の ssh が死んでいても中身が見える**（2026-11-05 の Pi の事故でユーザープロセスの正常さを確かめた）。

## 引数に既定が無いもの（使う側が決める）

JTAG クロック、同時停止の基準（`skew_limit_ns`）、CTI の時間差の上限（`use_cross_halt`）、FPGA の停止の見張りの上限と読む間隔、`apb_offset`、
`thread_id`、`kernel_thumb`、`max_tasks` / `max_threads`、`hw_slots`、`unmapped` の方針、ブレークポイントの方式（`sw` / `hw`）。

## 確かめたこと・確かめていないこと

- 模擬のテスト: `cd host && python -m pytest tests/linux_halt tests/test_halt_watch.py`（pyocd が要る: `pip install -e ".[dev,debug]"`）。
  ゲートウェア: `cd gateware && python -m pytest tests --ignore=tests/hil`。
- 実機の試験: `tests/linux_halt/hw`（環境変数は `tests/linux_halt/hw/harness.py` の先頭）。
- 確かめた: Pi 2B（PYNQ-Z2 から。2026-11-03 の版）、Zynq-7000（Primer の PJTAG から。2026-11-01 の版。CTI の同時停止・プロセス一覧・gdb）。
- **未確認**: Zynq-7000 を 2026-11-01 より後の版で（見張り・DAP・API を変えた。Primer で Zynq を見る配線が要る）、LPAE・AArch64 の
  ページ表のウォーク（手元の対象は 32 ビットの短い形だけ）。
- 配線と作業の手順: `linux-awareness-trial/docs/wiring-2026-10-29-two-probes.md`。

## Raspberry Pi 2B の JTAG の注意（2026-11-02 の実測。PYNQ-Z2 の JA から、ジャンパ線）

- **TRST（GPIO22、ヘッダ 15 番）は JTAG の機能（ALT4）でプルが無く、つながないと浮く。** Low に落ちると TAP がリセットされ続け、
  TDO が全部 1（チェーンに装置が見えない）になる。High に浮いている間は動くので、日によって変わる。つながないなら Pi の側で
  プルアップする（`sudo pinctrl set 22 pu`。再起動で消えるので、続けるなら `config.txt` の `gpio=22=pu`）。
- **Pi の ARM が 900 MHz（`ondemand` で忙しいとき・`performance`）だと、コアからコアへ移る読みが失敗する。** 4 コアのデバッグの
  レジスタを、IR の切り替えを挟んですき間なく読むと、次のコアの TAR の書き込みのスキャンで ACK が 000（TDO が全部 0）になる
  （回路の転送でも、PC が作るビット列をすき間なく流しても同じ。600 MHz では起きない）。Run-Test/Idle を 200 クロック（4 MHz で
  50 µs）入れても直らず、PC との往復（ミリ秒）を挟むとほぼ直る。原因は Pi の側にあると見ているが、仕組みは分かっていない。
  確実に動かすなら、デバッグの間は Pi の ARM を 600 MHz にする
  （`echo powersave | sudo tee /sys/devices/system/cpu/cpufreq/policy0/scaling_governor`）。
- **線の上で転送が化ける**（デュポンの線 2 本を継いだ 60 cm で、16 mA・FAST でも 4 MHz で 1 万回に数回。`docs/boards/pynq-z2.md`）。
  化けた転送は読みの失敗だけでなく、Pi に別の書き込みをしうる（DP の CTRL/STAT が 0 に書かれる = デバッグの電源の要求が消え、
  以後の AP の転送が全部失敗する。コアのメモリが壊れて Pi が自分で再起動したこともある）。attach は失敗を隠さず終わる（再試行しない）。
- **後始末**（2026-11-02）: attach（gdb のサーバ）の終わりの後始末は、走らせている間の失敗でも先に全コアを止めてから BKPT を外す
  （止めずに外そうとして断られ、BKPT が残っていた）。後始末が DAP の転送の誤りで失敗したら、**後始末のためにだけ** DP をつなぎ直して
  もう 1 度後始末する（電源の要求が消えたままだと、do_exit の HW の bp と停止デバッグが残り、終了するプロセスごとにコアが止まって
  Pi が固まった）。最初の失敗は `cleanup_errors`、つなぎ直した事実は `cleanup_reconnected`、2 度目の失敗は
  `cleanup_errors_after_reconnect` に出る（`linux.halt.gdb.status`、CLI の標準エラー、画面）。それでも残ったら `recover` → `rescue`。
- Pi が固まって ssh も届かないときは、JTAG でウォッチドッグを書いて再起動できる（コア 0 を止め、MMU を切ってから、物理番地の
  PM_WDOG 0x3F100024 に 0x5A00000A、PM_RSTC 0x3F10001C に 0x5A000020 を、コアの `write_memory` で書く。Linux の再起動と同じ値。
  SD カードを閉じずに再起動するのは電源を抜くのと同じ）。
