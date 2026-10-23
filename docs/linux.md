# 組み込み Linux のアプリのデバッグ（R26 段階 1: 道 A）

仕様: docs/superpowers/specs/2026-10-22-linux-app-debug-design.md。計画: docs/superpowers/plans/2026-10-22-linux-stage1.md。

アプリを作り直さずに（printf を足さずに）、Linux のアプリの関数の実行を時刻付きで受け取り、fprobe の記録の時間軸に並べます。
画面は「Linux」のパネル、API と MCP は `linux.*` です。

## 流れ

1. `linux.connect`（target・host・user・key・sudo）: SSH でつなぎ、能力の事実を読みます。
   - 事実の中身: カーネルの設定、tracefs、trace_clock、perf・ptrace の設定、道具、uid、sudo。
   - 読めないものは null（不明）で返します。
2. `linux.agent.deploy`: エージェント（C、`host/fprobe/linux/agent/fprobe_agent.c`）を、ターゲットの gcc で作ります。
   - 作った gcc と、源の sha256 を返します。
3. `linux.agent.start`: エージェントを SSH の中で起動します。
4. `linux.probe.add`: プローブを付けます。
   - 付けられる方式は、ptrace と uprobe のどちらかです。
   - バイナリはターゲットから取り寄せて、`$FPROBE_HOME/linux/<target>/sysroot/` に置きます。
5. `linux.sync`（count）: ネットワークでの時刻合わせを、手で 1 回します。
6. `linux.trace.start`（sync_interval_s）: 出来事の受け取りと、時刻合わせの繰り返しを始めます。`linux.trace.stop` で止めます。
7. `linux.events.read`: 受けた出来事を読みます。
   - 記録中なら、同時に `<target>.ptrace` / `<target>.uprobe` の stream にも入ります。

## 方式

| 方式 | 仕組み | 使える所 | 振る舞いへの影響 |
|---|---|---|---|
| ptrace | ソフトのブレークポイント（Thumb `0xde01`、ARM `0xe7f001f0`）。当たるたびに全スレッドを止め、レジスタを読み、1 歩進めて置き直す | 32 ビットの ARM（ARM・Thumb とも） | 止めるので変わる。時刻はエージェントが止まりを知った時刻 |
| uprobe | tracefs の uprobe_events。引数は fetchargs で明示する | CONFIG_UPROBE_EVENTS のあるカーネル。32 ビットの ARM では ARM 命令の関数だけ | 小さい（カーネルの中で取る） |

断る場合:
- ptrace: 最初の命令が PC を書く（分岐する）関数、または Thumb の IT で始まる関数には付けません。1 歩進めて置き直す方式が成り立たないためです。
- uprobe: 32 ビットの ARM の Thumb の関数には付けません。カーネルが ARM 命令として読み違え、アプリを壊しうるためです。

## 時刻

- エージェントの時計は CLOCK_MONOTONIC_RAW です。tracefs の時計は、使えれば mono_raw にそろえます。
- 時刻合わせは、エージェントに時刻を問い、（ターゲットの時刻、PC の往復の中点、往復の半分）を組にします。
  - 出来事の時刻は、隣り合う 2 つの組の間を内挿して換算します。
  - 最後の組より後の出来事は、延ばして換算しません。次の組ができるまで、記録に入れるのを待ちます。
- 出来事の `sync` に、次の 2 つを別々に付けます。
  - ネットワークの不確かさ（`half_rtt_ns`）
  - 記録の時計の不確かさ（`clockmap_uncertainty_ticks`）

## 実機の事実（2026-10-22、PYNQ-Z2 の PynqLinux 3.0、カーネル 6.6.10-xilinx）

- 能力: FTRACE・KPROBES・UPROBES は無い。perf・ハードのブレークポイントはある。gcc はある。gdbserver は無い。
- **perf のハードのブレークポイント（PERF_TYPE_BREAKPOINT）で、別のプロセスの関数を取ろうとした結果**:
  - そのプロセスが CPU を使い切って進まなくなった。
  - kill -9 の後、SSH・シリアルとも応答しなくなった。電源の入れ直しが要った。
  - そのため、この方式は fprobe に入れていない。
- ptrace のプローブ（`gateware/tests/hil/test_linux_hw.py`）:
  - -marm 版・-mthumb 版とも、10 ms ごとの 300 回の呼び出しを 300 件すべて取れた。r0 は 0〜299 の連番だった。
  - アプリの終了コードは 0 だった。
  - 往復の半分の最大は 3〜9 ms で、組の範囲の外の出来事は 0 件だった。
- fprobe の機器（PYNQ の中継）で記録しながら同じ試験をした: 300 件すべてが stream `t.ptrace` に時刻の順で入り、組の範囲の外は 0 件、不確かさは 4.5〜7 ms（2026-10-22）。

実機の試験:

```
FPROBE_LINUX_HOST=192.168.77.2 FPROBE_LINUX_KEY=~/.ssh/fprobe_pynq pytest gateware/tests/hil/test_linux_hw.py --hil -s
```

## 実機の事実（2026-10-22、Raspberry Pi 2B、Raspberry Pi OS 13、カーネル 6.18.50+rpt-rpi-v7）

- 能力: UPROBES・UPROBE_EVENTS は無い。FTRACE・KPROBES はある。gcc あり、gdbserver 無し、sudo はパスワード無し。
- gcc の既定は ARMv6（初代 Pi に合わせたもの）。Thumb の題材は `-march=armv7-a+fp -mthumb` で作る（`-mthumb` だけでは Thumb-1 になり断られる）。
- JTAG（config.txt に `enable_jtag_gpio=1` と `gpio=22=pu`。配線は Primer の DVP のヘッダ J14 = TCK・J16 = TDI・M15 = TMS・T11 = TDO・GND と、
  Pi の 22・37・13・18・39 番）。**加えて Pi の 16 番（GPIO23 = RTCK、ずっと High）を Primer の T12 へ**（信号の線の隣の一定の線。これが無いと 5 MHz 以上で
  値が壊れた。R28）。この配線で JTAG は 10 MHz（2026-10-23。速い読み 1024 語 275 kB/s、道 B の試験の止めた最大 61 ms）: JTAG-DP（IDCODE 0x4BA00477）の AP は AP0 = APB-AP だけ。止めずにメモリを読む入口は無い。
- 新しい OpenSSH は、認証に何度も失敗した相手からの接続をしばらく断る（PerSourcePenalties）。ユーザー名を当て推量で試さない。
- ptrace のプローブ（`test_linux_hw.py`・`test_linux_agent_hw.py`）: ARM 版・Thumb 版とも 300/300、外す・止める・4 スレッド・2 つ目のプローブ・fork の 7 つの試験がすべて通った。往復の半分の最大 5〜9 ms、組の範囲の外 0 件。

## 道 B: リングを JTAG で読む（段階 2。計画 docs/superpowers/plans/2026-10-23-linux-stage2-pi.md）

エージェントが、ptrace の当たりを物理メモリのリングにも書きます（`linux.ring.open`）。fprobe はそれを JTAG で読みます
（`linux.ring.read` / `linux.ring.watch`。番地の引数は `address`（止めて読むなら仮想番地、memap なら物理番地））。番号 seq は道 A と同じで、両方で受けた出来事は 1 つにまとめ、`paths` に a・b を付けます。
番号の合わない記録は `torn`、CRC の合わない記録は `corrupt`、追い越されて読めなかった範囲は `lost` として、事実のまま返します。

読み方（`access`）:
- `halt`: `target.connect` でつないだ Cortex-A のコアを止め、そのコアに読ませて、すぐ再開します。止めている間は振る舞いが変わります。
  止めた回数・時間（合計・最大）は `linux.agent.status` の `ring_stats` に出ます。
  1 回に読む記録の上限 `max_records` は、止めている時間の上限になるので、使う側が明示して渡します。
- `memap`（止めずに読む）: まだありません。ターゲットの DAP に、システムのメモリにつながる MEM-AP（AHB-AP / AXI-AP）が要ります。

### Pi 2B の準備（2026-10-23 に行ったもの）

- リングのメモリの予約: `/boot/firmware/overlays/fprobe-ring.dtbo`（reserved-memory `fprobe-ring@20000000`、`reg = <0x20000000 0x100000>`、
  no-map は付けない = カーネルの線形の写しに残す）と、config.txt に `dtoverlay=fprobe-ring`。
  起動時に「OF: reserved mem: 0x20000000..0x200fffff (1024 KiB) map non-reusable fprobe-ring@20000000」と出ます。
- 止めるコアを空ける: cmdline.txt に `isolcpus=3 irqaffinity=0-2`。止めて読むのはコア 3（`target.connect` の core=3）。
  - コア 1（仕事をしているコア）を止めたときは、SD の回路（sdhost）がエラーになり、再起動が要りました。
  - 止めたコアが戻らないと Linux 全体が止まり、ほかのコアがウォッチドッグに餌をやり続けるので、自動の再起動も起きませんでした。
- 読む番地: Pi 2B は PAGE_OFFSET 0x80000000（2G/2G）なので、`address = 0x80000000 + 0x20000000 = 0xA0000000`。
- エージェントは sudo で起動します（/dev/mem）: `linux.connect(..., sudo=true)`。

### 実機の事実（2026-10-23、Pi 2B、Primer の JTAG 2 MHz）

- `gateware/tests/hil/test_linux_ring_pi_hw.py`: 10 ms ごとに 3000 回の当たりを `linux.ring.watch`（0.5 秒ごと、max_records 1000）で読んだ。
  - 途中で Pi の LAN を 15 秒落とした。落ちている間にも、道 B で 462 件を読めた。
  - 3000/3000 件、lost・torn・corrupt は 0 件。止めた回数は 63 回で、1 回は平均 64 ms、最大 80 ms。
  - 試験の後、Pi のカーネルのログに SD のエラーも RCU の停止の警告も無かった。
- 止めて読む速さ（DCC の速いモード + ブロック転送）: 2 MHz で 256 語 66 kB/s。1 語ずつの読みの約 30 倍。
  - 5 MHz では、DAP の誤りの印なしに値が壊れることがありました（空きの時間を足しても同じ。原因は未特定）。
  - 記録の CRC がこれを見つけるので、壊れた記録は `corrupt` として出ます。
  - 調べた事実（2026-10-23、Primer のロジアナで JTAG の線を記録し、jtag_arm で解読）:
    - 壊れた読みでも、FPGA の端子で見た TDO の値と、PC が受け取った値は一致していた（fprobe の受け渡しは正しい。値はターゲットが
      出した時点で壊れている）。
    - 線の上に WAIT は 1 回も無く、スキャン（7.35 µs）と間（1.68 µs）は壊れた語の前後でも規則的だった。
    - 壊れ方は 2 種類: 上位のビットが化ける（0xEAFFF7DC → 0xFFFFF7DC）・同じ語が 2 回出て以後がずれる。
    - コア 1・コア 3 のどちらを止めても起きる（5 MHz・256 語で 10 回中 6 回）。2 MHz では起きていない。
    - 残る候補: Cortex-A7 の DCC の速いモードの振る舞い（DTRTX の読みが LDC の完了を待たない等）か、ジャンパ線の信号の質。
  - 切り分けの続き（2026-10-23 朝）:
    - 速いモードを使わず、値の変わらないコアのデバッグのレジスタ（DIDR）をまとめ読みしても、5 MHz で壊れた（20 回 × 512 語の約 7 割。
      壊れた値はほぼ 0）。ROM の表（DAP の内側）は 15 MHz でも壊れなかった。→ 速いモードの使い方の問題ではない。
    - JTAG の空きのクロック（DAP_TransferConfigure の idle 0〜32）を変えても直らなかった（速さは変わるので空きは効いている）。
    - RTCK（Pi の GPIO23 = ARM_RTCK）を Primer の DVP のヘッダの T12（ロジアナ ch4）につないで測った: RTCK はずっと High で変わらない
      （Cortex-A7 の CoreSight の DAP は RTCK を使わない。GPIO23 を出力にして Low / High にすると T12 が追うので、線は正しい）。
    - **RTCK の線（ずっと High に駆動される線）を足した後は、5〜10 MHz で壊れなくなった**: DIDR のまとめ読みは 10 MHz で 20,480 語中 0
      （530 kB/s）、12.5・15 MHz で 5・8 語。コア 3 を止めた速いモードの読みは 5〜10 MHz で 10 回とも一致（約 97 kB/s で頭打ち）。
    - 見立て: 信号の線の束の隣に、電圧の変わらない線（帰り道）が増えて、クロストーク・グランドの揺れが減った（ジャンパ線の信号の質）。
      同じ効果は、信号の線の隣に GND の線を 1 本足しても得られるはず（docs/boards/tang-primer-20k-dock.md の推奨と同じ）。未確認。
    - 止めて読む速さは、JTAG の速さより、CPU のデバッグ回路の 1 語ずつの読み（DCC）で頭打ちになる（5〜10 MHz で約 90〜97 kB/s）。

## 止めて調べる: gdb（段階 3。計画 docs/superpowers/plans/2026-10-23-linux-stage3-gdb.md）

- `linux.gdb.batch {target, commands, pid | core+binary, timeout_s}`（AI 向け）: ターゲットの gdb を `-batch -nx` で走らせ、命令を順に実行した
  出力をそのまま返します。命令は 1 つずつ `-ex` の引数にし、シェルの単語として引用します。pid につける間、プロセスは止まります。
- `linux.gdb.serve {target, pid, port}`（人・手元の gdb 向け）: ターゲットの `gdbserver --once --attach - PID`（標準入出力）を SSH の中で起動し、
  PC の `127.0.0.1:port` で gdb の接続を 1 つ待って中継します（`target remote 127.0.0.1:port`）。`linux.gdb.status` / `linux.gdb.stop`。
  ポートは 127.0.0.1 だけで開きます（ほかの機器から見えません）。

実機の事実（2026-10-23、Pi 2B、gdb・gdbserver 16.2）: `gateware/tests/hil/test_linux_gdb_pi_hw.py`
- batch の `bt` で `main` → `__nanosleep64` の呼び出しの履歴が取れ、アプリは終了コード 0 で終わりました。
- 中継に RSP の `qSupported` を送ると `PacketSize=...` が返り、`D`（切り離し）の後もアプリは終了コード 0 で終わりました。
- 1 回だけ、batch が終了コード 1・出力なしで終わりました（再現しない。stderr を返すので、起きたら事実で分かります）。

## カーネルの出来事と journal（段階 4 の一部。計画 docs/superpowers/plans/2026-10-23-linux-stage4-ftrace-log.md）

- `linux.ftrace.enable {target, events: ["sched/sched_switch", ...]}` / `disable`: tracefs のイベントを有効にします。出来事は `linux.trace.start`
  で開く trace_pipe から受け、`method: "ftrace"`（comm・pid・cpu・行の `k=v` のままの args）で、アプリの出来事と同じ列・記録
  （stream `<target>.ftrace`）に入ります。時刻は trace_clock（mono_raw にそろえる）で、エージェントの CLOCK_MONOTONIC_RAW と同じ時計です。
- `linux.log.read {target, lines, unit?, since_cursor?}`: journal を `journalctl -o json` で読み、各行を JSON のまま返します。
  時刻 `__MONOTONIC_TIMESTAMP` は CLOCK_MONOTONIC（µs）で、出来事の時計（CLOCK_MONOTONIC_RAW）とは別なので換算しません。

実機の事実（2026-10-23、Pi 2B）: `gateware/tests/hil/test_linux_ftrace_pi_hw.py`
- sched_switch と ptrace の tick を一緒に受けた（出来事 21,329 件、tick 273 件、アプリへの切り替え 632 件）。
- 各 tick の直前のアプリへの切り替えまでの時間は、中央 0.7 ms・最大 2.6 ms（tick の周期 10 ms の中）。2 つの時計がそろっている。
- journal の 5 行を読めた（next_cursor あり）。
- 能力の事実: Pi 2B は tracefs と kprobe_events があり、uprobe_events は無い（以前は tracefs を「無い」と出していた。直した）。

## 止めずに読む道 B（memap。Zynq の案 3、2026-10-23）

- `linux.memap.connect {target, xsdb, cable_serial, chain_idcode}`: Vivado の xsdb（hw_server）で、ボードの USB の JTAG から DAP の AHB-AP を使い、
  CPU を止めずに物理メモリを読む口を開きます。xsdb の場所・JTAG のケーブルのシリアル・チェーンにあるはずの装置の IDCODE（Zynq-7020 の PL は
  0x23727093）は明示します（違うボードを読まない）。PYNQ-Z2 のケーブルのシリアルは xsdb では `1234-tulA`。
- `linux.ring.read` / `watch` の `access="memap"`、`address` は物理番地。リングは `cached=false`（O_SYNC）で開きます
  （memap は CPU のキャッシュを通らないため。`cached=true` のリングは断ります）。
- `linux.ring.open` の `linger=true`: SSH（道 A）が切れても、エージェントは ptrace しているプロセスが終わるまで、ptrace とリングへの書き込みを続けます。
  道 A への書き込みは待たないので（書けない行は捨てて数え、出来事 `a_dropped` で知らせる）、道 A が詰まっても道 B は止まりません。
- パスワードの要る sudo（PYNQ）: `linux.connect` の `sudo_password_env` に、パスワードを入れた PC の環境変数の名前を渡します。

実機の事実（2026-10-23、PYNQ-Z2）: `gateware/tests/hil/test_linux_ring_pynq_hw.py`
- xsdb の memap で、CPU を止めずに DDR を 470 kB/s で読めました（16384 語 139 ms）。
- リングは物理番地 0x1DF00000 の 1 MB（リング専用）。`host/pynq/uEnv.txt` の `mem=479M` で Linux から外しています
  （その上の 0x1E000000〜 の 32 MB は深いバッファ）。2026-10-23 の 479M での再試験: 3000/3000 件を道 B で読み、
  LAN が落ちている間に 418 件、lost・torn・corrupt・頭の誤りは 0 件、CPU を止めた時間は 0。
- 10 ms ごとに 3000 回の当たりを `linux.ring.watch`（0.5 秒ごと）で読みました。途中で PYNQ の LAN を 15 秒落としました。
  - 落ちている間にも 464 件を読めました。3000/3000 件で、lost・torn・corrupt は 0 件、CPU を止めた時間は 0 でした。
  - 頭の CRC が合わない読みが 1 回あり、事実として数えて次の読みで続きを読みました。
- 修正前は、SSH が切れるとエージェントが終わり、道 B も止まっていました（3000 件中 2320 件で止まった）。`linger` で直しました。
- PC の側で SSH が切れても、ターゲットの SSH のサーバーがそれを知るまで、エージェントの入力は閉じません（古い接続）。
  `linux.agent.start` の `watchdog_s` を渡すと、デーモンがその 3 分の 1 ごとに ping を送り、エージェントは入力が `watchdog_s` 途絶えたら
  デーモンがいなくなったとみなして終わります（linger のリングがあれば ptrace しているプロセスが終わってから）。Pi で確かめた
  （`test_linux_agent_b_hw.py` の watchdog の 2 つ）。

## core dump（段階 4。2026-10-23）

- `linux.core.status(pid)`: core_pattern（`|` で始まれば別のプログラムに渡す）・core_uses_pid・suid_dumpable と、pid の
  上限（RLIMIT_CORE。null = unlimited）・cwd・exe。どこに core が書かれるかは、この事実から決まる（推定しない）。
- `linux.core.pattern_set(pattern)`: core_pattern を書く（システム全体。sudo）。前の値 previous を返すので、戻すときはそれを渡す。
- `linux.core.limit_set(pid, soft, hard)`: そのプロセスの上限を prlimit で変える（バイトか "unlimited"）。
- `linux.core.fetch(path, local_path)`: core を PC に写す（bytes・sha256）。中身はターゲットの gdb で
  `linux.gdb.batch(core=..., binary=...)`。

実機の事実（Pi 2B）: 既定は core_pattern = `core`（プロセスの cwd に書く）・core_uses_pid = 1・上限 0（書かれない）。
`gateware/tests/hil/test_linux_core_pi_hw.py`: 上限を unlimited、core_pattern を `/tmp/fprobe-core.%e.%p` にして落ちた題材の
core（278528 バイト）を写し、gdb の bt に落ちた関数と行が出た。core_pattern は元に戻した。
PYNQ の既定は `|/usr/share/apport/apport ...`（apport に渡す）。

## perf（段階 4。2026-10-23）

- `linux.perf.stat(events, duration_s, pid)`: perf stat の行（--no-scale の生の値・単位・走った時間と割合）。数えられなければ value = null と
  counted（`<not supported>` 等）。perf_metric は perf が計算した比（perf の値として分けて返す）。
- `linux.perf.record(duration_s, frequency_hz, max_samples, pid)`: 標本を出来事（method "perf"）にして、ほかの出来事と同じ時間軸に入れる。
  時計は `--clockid monotonic_raw`（エージェントと同じ CLOCK_MONOTONIC_RAW なので時刻合わせの組で換算できる）。時刻は
  perf script の既定の書式の µs（resolution_ns = 1000。`--ns` は `--clockid` と組むと書式が崩れた）。
- 実機（Pi 2B、linux-perf 6.18.50 を入れた）: 回り続ける題材に stat（task-clock・cycles・instructions）と record（499 Hz で
  0.5 秒、247 標本 = perf の言った数、読めない行 0）。`gateware/tests/hil/test_linux_perf_pi_hw.py`。PYNQ には perf が無い。
