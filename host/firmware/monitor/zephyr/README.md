# fprobe のモニタ方式（U37）を hhkb-split の右手（XIAO nRF52840・ZMK）に組み込む

CPU を止めずにデバッグするためのモジュール。ブレークポイント・中断で止まるのは **スレッド（ZMK のアプリ・BLE の
ホスト）と優先度の低い割り込み** だけで、BLE のコントローラの割り込みは動き続ける（CPU を 5 秒止めると BLE の
コントローラの assert で落ちる問題（2026-10-18）を避ける）。

仕様: `C:\Users\M\Desktop\DebugProbe-with-FPGA\docs\superpowers\specs\2026-10-18-u37-monitor-mode-design.md`

## 中身

| ファイル | 役目 |
|---|---|
| `zephyr/module.yml` | Zephyr のモジュール（名前 `fprobe-monitor`） |
| `Kconfig` | `CONFIG_FPROBE_MONITOR`（`CORTEX_M_DEBUG_MONITOR_HOOK` を選ぶ）・`_PRIORITY`（既定 0x60）・`_STACK_SIZE`（既定 = `ISR_STACK_SIZE` = 2048） |
| `CMakeLists.txt` | 1 つ上の `fprobe_monitor.c` を、ハンドラの名前 `FPROBE_MONITOR_HANDLER=z_arm_debug_monitor` で作る |
| `src/fprobe_monitor_zephyr.c` | `SYS_INIT(PRE_KERNEL_1)` で `fprobe_monitor_init(CONFIG_FPROBE_MONITOR_PRIORITY)` を呼ぶ（ハンドラは Zephyr のベクタ表の 12 番が指す `z_arm_debug_monitor`（fault_s.S の弱い定義）を置き換える） |
| `fprobe_monitor.conf` | `CONFIG_FPROBE_MONITOR=y`（`-DEXTRA_CONF_FILE` で足す） |

利用者のリポジトリ（`C:\Users\M\zmk\ws` = hhkb-split）は変えていない。モジュールと設定はビルドの引数で足す。

## ビルド

```sh
<ZMK の作業場>/build_right_mon.sh
```

`build_right_dbg.sh` と同じ指定（CONFIG_THREAD_MONITOR / THREAD_NAME / INIT_STACKS 付き）に、
`-DZMK_EXTRA_MODULES=<ws>;<このディレクトリ>` と `-DEXTRA_CONF_FILE=<このディレクトリ>/fprobe_monitor.conf` を足したもの。

出力（`C:\Users\M\zmk\ws\build\right-mon\zephyr\`）:

| ファイル | 中身 |
|---|---|
| `zmk.elf` | 書き込みに使う（シンボル付き。PT_LOAD は 0x27000〜0x58E90 だけ = アプリの区画だけ） |
| `zmk.hex` | 同じ範囲（0x27000〜） |
| `zmk.bin` | 0x27000 から 204432 バイト |
| `zmk.uf2` | UF2 ブートローダ用（開始 0x27000） |

確かめたこと（2026-10-18 のビルド）:

- ベクタ表 0x27030（12 番）= 0x00048D65 → `z_arm_debug_monitor`（0x48D64、Thumb）。頭は `b.n` の次に
  印 `0x4E4D5046`（"FPMN"）と箱の番地 `0x20005A00`。
- 箱 `fprobe_monitor` = 0x20005A00（0x12C バイト）、モニタのスタック `fprobe_monitor_stack` = 0x20001D78（0x800 バイト）。
- 増分（right-dbg と比べて）: Flash +704 バイト（text +712）、RAM +2348 バイト（箱 300 + スタック 2048）。

## 優先度の決め方

nRF52840 は優先度のビットが 3 本（0x20 刻み、8 段）。Zephyr は例外のために 1 段空けるので、`IRQ_CONNECT` の
優先度 p はハードでは `(p + 1) << 5`。`irq_lock()` は BASEPRI = 0x20。

| 処理 | Zephyr の優先度 | ハード |
|---|---|---|
| 故障・SVC | — | 0x00 |
| BLE コントローラ LLL（RADIO・TIMER0・SWI の radio。`CONFIG_BT_CTLR_LLL_PRIO=0`） | 0 | 0x20 |
| BLE コントローラ ULL（RTC0・SWI の job。`CONFIG_BT_CTLR_ULL_HIGH/LOW_PRIO=1`）、DT の周辺の既定（RTC1 = カーネルの時計、GPIOTE、USBD、CLOCK 等） | 1 | 0x40 |
| **DebugMon（`CONFIG_FPROBE_MONITOR_PRIORITY=0x60`）** | （2 の段） | **0x60** |
| PendSV（スレッドの切り替え）、スレッド | — | 0xE0 / スレッド |

- BLE を止めないには DebugMon を **0x40 より低く（数を大きく）** する（同じ段では割り込めない）。0x60 はその中で
  いちばん高い段。0x60〜0xE0 のどれでも BLE は止まらない（この版では 0x40 より低い割り込みは事実上無く、違いは小さい）。
- 0x60 は `irq_lock` の BASEPRI（0x20）以下なので、クリティカルセクション（`irq_lock`・`k_spin_lock`）の中では
  止まらない（仕様の推奨どおり。カーネルのデータが途中の状態で見えない）。
- 止まっている間: 0x20・0x40 の割り込み（BLE のコントローラ・カーネルの時計・GPIO 等）は動く。スレッド
  （ZMK のアプリ、BLE のホスト、システムのワークキュー）は止まる。
- `debug_mode.set(priority=…)` で書き替えられる（再起動で 0x60 に戻る）。

## 書き込み（fprobe・SWD）

**書くのはアプリの区画（0x27000〜）だけ。0x0 の MBR、0xEC000 の設定（ペアリング）、0xF4000 の UF2 ブートローダは
書かない。** ELF / HEX はアプリの区画しか含まないので、`erase: "sector"` で書けば他は残る（`auto` は全体の消去を
選ぶことがあるので使わない）。

1. 配線を確かめる（XIAO の USB を先に、信号線は後。Primer の 3.3V はつながない）。
2. 停止方式（既定）のまま書く。書き込みはリセットして止めるので、BLE は一度切れ、再起動で左手とつなぎ直す。

   ```
   target.flash  path="<ZMK の作業場>/ws/build/right-mon/zephyr/zmk.elf"  erase="sector"  verify=true  reset=true
   ```

   （MCP なら `target_flash`。ELF なのでシンボルも読まれる。）
3. 左手とつながり、キーが打てることを確かめる。

UF2 でもよい（SWD を使わない）: リセットを 2 回押して出るドライブに `zmk.uf2` を写す。ブートローダは
0x27000 からのアプリの区画だけを書く。

## 元に戻す

- 今までのデバッグ用の版（right-dbg）に戻す:

  ```
  target.flash  path="<ZMK の作業場>/ws/build/right-dbg/zephyr/zmk.elf"  erase="sector"  verify=true  reset=true
  ```

  （right-mon の方が 704 バイト大きいので、末尾の 1 ページの残りは消えずに残るが、使われない。）
  先にモニタ方式にしていたら `target.debug_mode.set mode="halt"` にしてから書く（Flash の書き込みは停止方式でしかできない）。
- 元の中身（2026-10-18 のバックアップ `C:\Users\M\zmk\xiao_right_backup_20261018.bin`、1 MiB の全体）から、
  **アプリの区画だけ** を切り出して書く:

  ```sh
  python -c "d=open('<ZMK の作業場>/xiao_right_backup_20261018.bin','rb').read(); open('<ZMK の作業場>/xiao_right_backup_app_27000.bin','wb').write(d[0x27000:0xEC000])"
  ```

  ```
  target.flash  path="<ZMK の作業場>/xiao_right_backup_app_27000.bin"  address=0x27000  erase="sector"  verify=true  reset=true
  ```

  全体（1 MiB）をそのまま書かないこと（ブートローダと、今のペアリングの設定を古い中身で上書きする）。

## モニタ方式で試す手順

前提: right-mon の版が走っていて、左手とつながっている。**停止方式のまま `target.halt`・ステップ・ブレークポイントを
しない**（5 秒で BLE のコントローラが落ちる）。

1. 見つかるか:

   ```
   target.debug_mode.get
   ```

   `available: true`、`box: 0x20005A00`、`priority: 0x60`（96）、`prio_bits: 0xE0`、`keeps_running` に
   RADIO・TIMER0・RTC0・SWI・RTC1・GPIOTE 等（0x20・0x40 の段）が並ぶこと。`available: false` なら `reason` を見る
   （印が無い = 書いた版が違う、magic が 0 = init が呼ばれていない）。
2. モニタ方式にする（コアは走ったまま）:

   ```
   target.debug_mode.set  mode="monitor"
   ```

3. 止める・見る・再開（短く。まず 1〜2 秒）:

   ```
   target.halt          → target.status（debug_mode: "monitor"、止まっている）
   debug.backtrace      → たいてい idle スレッド（arch_cpu_idle）
   target.resume
   ```

   止めている間、左手の側で右手が切れないこと・再開の後に右手のキーが打てることを確かめる。だんだん長く
   （5 秒 → 10 秒 → 30 秒）して、どこまで切れないかを確かめる（下の「心配」）。
4. スレッドの中のブレークポイント（例: キーの処理。ワークキューのスレッドで走る）:

   ```
   debug.breakpoint.set  location="zmk_physical_layouts_kscan_process_msgq"
   （右手のキーを 1 つ押す）
   debug.run_until_halt → 止まる。debug.backtrace・レジスタ・変数を見る
   debug.step（行）・target.step
   debug.breakpoint.clear
   target.resume
   ```

5. 停止方式に戻す（Flash を書くとき等）:

   ```
   target.debug_mode.set  mode="halt"
   ```

   戻した後は、また `target.halt` をしないこと。

## してはいけないこと（モニタ方式でも）

- **割り込みの中にブレークポイントを置かない。** ここの割り込みはすべて DebugMon（0x60）より優先度が高いので、
  BKPT・FPB は HardFault に格上げされ、キーボードが落ちる（アーキテクチャの決まり）。例: GPIO の割り込みの
  呼び返し（kscan の `*_irq_callback`・`zmk_physical_layout_kscan_callback` は割り込みから呼ばれることがある）、
  BLE のコントローラ（`ll_*`・`ull_*`・`lll_*`・`radio_*`・`ticker_*`）、`isr`・`_handler` の付く関数、k_timer の
  満了の関数。置くのはワークキュー・スレッドで走る関数だけ。
- Flash のブレークポイント（U13）・`target.flash`・`target.reset(halt=true)` はモニタ方式ではできない（誤りになる）。

## 心配（実機で確かめること）

- 止まっている間は BLE の **ホスト**（スレッド）も止まる。コントローラはつながりを保つ（空のパケットを送り続ける）が、
  受けたデータはコントローラの受け口（`CONFIG_BT_CTLR_RX_BUFFERS=1`）に溜まったまま。左手（セントラル）が ATT の
  要求を出して 30 秒答えが無いと、左手が切る（ATT の時間切れ）。長く止めるほど切れやすい。
- 止まっている間もカーネルの時計（RTC1）は進むので、再開の後に溜まったタイマ・遅延の仕事が一度に走る。ZMK の
  眠り（`CONFIG_ZMK_IDLE_SLEEP_TIMEOUT` = 30 分）を超えて止めると、再開の直後に深い眠りに入りうる。
- モニタのスタックは 2048 バイト。止まっている間に動く割り込み（BLE のコントローラ等）がこの上に積む。溢れたら
  `debug_mode.get` の `stack_overflow` が真になる（`-DCONFIG_FPROBE_MONITOR_STACK_SIZE=…` で増やす）。
- モニタのソースは fprobe のリポジトリのものを直接ビルドする。fprobe 側を変えたら作り直す（箱の版が違えば
  ホストが断る）。
