# Shrike-Lite: MCU と FPGA が連携するシステムを fprobe で解析するデモ

Vicharak Shrike-Lite（RP2040 + Renesas ForgeFPGA SLG47910）上の小さな組み込みシステムと、それを fprobe で
MCU と FPGA の両側から同時に観測するための一式。MCU のファームウェアは C（Zephyr）で、ELF の変数名・ソース行・
スレッドで調べられる。MCU と FPGA の連携の不具合（題材）を 4 種類、実行中に切り替えられる。
仕様: docs/superpowers/specs/2026-10-21-shrike-c-demo-design.md

```
 RP2040（Zephyr, firmware_c/）──SPI0（1 MHz）──▶ FPGA の演算器（rtl/demo_accel.vhd, 25 MHz）
   │ GPIO26 = 要求中 1、GPIO27 = 題材の印                │ 観測回路（スナップショット型 obs_snap_core）
   ▼                                                    ▼
 fprobe（PYNQ-Z2）: ロジアナ JB1・JB7 ◀──────┐   観測用 JTAG（RPi ヘッダ 15・16・18・22）
                    SWD（マルチドロップ）◀────┴─ RP2040 の SWD      │
                                                                  ▼
                    1 本の時間軸（MCU の変数・RTT のログ・スレッド・FPGA の信号・ピン）
```

## 題材（不具合の型）

MCU は A・B（8 ビット）を書き込んで演算を要求し、STATUS を `g_poll_limit` 回まで読んで完了を待つ。完了しなければ
待ち回数を倍にしてリトライし、結果を A × B と比べる（誤りは `g_errors`）。題材は `g_scenario` に書いて切り替える
（fprobe のメモリの書き込み・変数の書き込みで。フラッシュは書き直さない）。

| `g_scenario` | 型 | 仕込み | MCU だけ / FPGA だけで見えること | 両方を並べると分かること | 捕まえ方 |
|---|---|---|---|---|---|
| 0 | なし | — | 誤り 0 | — | — |
| 1 | MCU → FPGA | 待ち回数が短く、計算中にリトライが来ると FPGA が途中の和を消さない（MODE bit0） | MCU: ときどき結果が違う / FPGA: 計算は正しく進む | 計算中（BUSY）に restart が来た回だけ誤る | 結果の直前に多い出来事・区間ごとの集計（結果あり・なし） |
| 2 | FPGA → MCU | FPGA がまれに A・B の書き込みを取りこぼす（MODE bit1。頻度は `g_s2_rate` = LFSR のビット数、既定 10） | MCU: まれに結果が違う（リトライも無い）/ FPGA: 取りこぼしの印（dropped の反転）だけ | 取りこぼしの直後の要求が誤る | FPGA の条件 `dropped changed` で MCU を止め、その要求の A・B と処理の場所を見る |
| 3 | タイミング | タイマ割り込み（`g_s3_period_us`、既定 99991 µs）が GPIO の出力を古い写しで丸ごと書き戻す。SPI の転送中に当たると CS が上がり、FPGA へのコマンドが壊れる | MCU: ときどき結果が違う / FPGA: 転送が途中で切れる | 割り込みの印（ch 14）が転送中（spi = 1）に変わった回の後に誤る | 結果の直前に多い出来事（`ch14 changed and spi=1`） |
| 4 | 1 回きり | S1 に加えて、A = 0xFF でリトライが来ると FPGA が固まる（MODE bit2） | MCU: ある時から全部タイムアウト / FPGA: BUSY のまま | A = 0xFF の要求のリトライで固まった | FPGA の条件 `stuck rose` で MCU を止め、止まった瞬間の変数とスレッド |

S4 から戻すには `g_scenario = 0`（MODE の書き込みで FPGA の固まりが解ける）。`g_reset = 1` は FPGA に回路を送り直す
（そのたびに FPGA の電源を切り入れする。下の配線の節）。送り直しに失敗したとき（ID が違う等）は要求を止めて、次の
`g_reset` を待つ（黙って動き続けない）。

## 実測（2026-10-22、`analyze.py`、PYNQ-Z2 の fprobe・SWD 1 MHz）

fprobe は原因を判定しない。返すのは測った事実（回数・割合・時刻）で、見分けるのは使う人・AI。
`preceded` = 結果の直前（窓の中）に候補があった割合、`coverage` = 記録のうち候補の後の窓に当たる時間の割合
（いつも成り立つ候補は coverage が 1 に近く、preceded が 1 でも区別にならない）。

| 題材 | 記録 | 結果 | 事実（fprobe の出力） |
|---|---|---|---|
| S1 | 6 秒・窓 10 ms | 誤り 551 回 | 区間ごとの集計: 誤りのあった区間（551）は `restart changed` が平均 1.80 回、無かった区間（251）は 0.195 回。誤りは区間の始め（BUSY）から中央値 4.0 ms（1.4〜7.9 ms）。自動の候補（`addr changed` 等）は preceded 1.0 だが coverage 0.98〜1.0 |
| S2 | 20 秒・窓 20 ms | 誤り 2 回 | `dropped changed`: preceded 1.0（2/2）・coverage 0.20 %。`state changed and state=BUSY`: preceded 1.0・coverage 99.97 %。`restart changed`: 0。FPGA の条件 `dropped changed` で MCU を止めると、要求のスレッドの SPI の転送の中（`spi_pl022_xfer`）で止まる |
| S3 | 30 秒・窓 12 ms・ピンつき | 誤り 2 回 | `ch14 changed and spi=1`（転送中の割り込み、102 回）: preceded 1.0（2/2）・coverage 4.1 %。`ch14 changed`（300 回）: 1.0・12 %。`spi rose`: 1.0・99.999 %。割り込みは 100 ms ごとで転送中に当たるのはまれ（6 秒の記録では誤り 0 回だった）。ピンの欠け 2 件（167 ms）は、かかる結果を判定から外したと出る |
| S4 | — | 固まり | FPGA の条件 `stuck rose` で MCU を止めると、SPI の転送の中（`spi_pl022_xfer`）で止まり、呼び出し履歴と変数が読める |

連動の遅れ（FPGA の条件 `state=BUSY` → MCU の停止）: 記録の時刻で、観測した条件の値の立ち上がりから DHCSR の停止の
書き込みまで（`analyze.py --latency`。MCU の変数の読み 200 Hz を同時に動かしたまま）。

| SWD | 測った値（回ごと） |
|---|---|
| 1 MHz | 205.6 µs（1 回目の実行）、349.8・361.9・349.8 µs（2 回目の実行の 3 回） |
| 4 MHz | 101.1・101.1・101.1 µs |

停止の書き込みは、仕組み上ホストの DAP コマンドの境界まで待ち、ホストが書いた CSW・TAR の控えがあれば 4 転送
（約 190 SWCLK）、無ければそれを読んで戻す転送を足す（`gateware/rtl/debug/swd_injector.vhd`）。1 MHz の 2 回の実行の差
（約 150 SWCLK）はこの差と大きさが合うが、どちらの経路だったかは記録に残らないので確かめていない。MCU のブレークポイント → ロジアナの捕捉は、「ターゲットが止まった」を源にする
クロストリガが無いので測っていない。

## 配線（PYNQ-Z2 ↔ Shrike-Lite）

| 信号 | PYNQ-Z2 | Shrike-Lite |
|---|---|---|
| 観測の TCK | Raspberry Pi ヘッダ 15 | 左列 下から 4 本目（シルク F0、F_GPIO0） |
| 観測の TMS | 16 | 左列 下から 5 本目（シルク F18、F_GPIO18） |
| 観測の TDI | 18 | 左列 下から 3 本目（F_GPIO1） |
| 観測の TDO | 22 | 左列 下から 1 本目（F_GPIO7） |
| GND | 20 | 左列の GND |
| SWD（SWCLK・SWDIO・GND） | PMOD JA | J5（表面から見て左から SWCLK・GND・SWDIO） |
| MCU のマーカー（要求中） | PMOD JB 1（ロジアナ ch 10） | 右列 GPIO26 |
| 題材の印 | PMOD JB 7（JB 1 の真下。ロジアナ ch 14） | 右列 GPIO27 |

どれも 3.3 V 同士（レベル変換は要らない）。PMOD の 6・12 番（3.3 V）には何もつながない。Shrike の USB は電源だけ
（C のデモは USB を使わない）。

- つなぐ順: Shrike の USB（電源）と PYNQ の電源を入れてから信号の線を刺す。外す順はその逆（信号の線を先に抜き、
  それから電源を切る）。
- 逆流: Shrike の USB を抜いたまま PYNQ が観測の JTAG（TCK・TMS・TDI）や SWD を駆動すると、電源の無い RP2040・FPGA の
  入力に 3.3 V がかかる。Shrike の電源を切るときは、先に fprobe の観測を閉じ（`fpga_obs.close`）・ターゲットを切り離す
  （`target.disconnect`）か、線を抜く。手元の Shrike は F_GPIO2 の入力がすでに働かない（docs/boards/shrike-lite.md）。
- FPGA の送り込み（起動時と `g_reset` のたび）で、ファームウェアは RP2040 の GPIO12（FPGA の電源 PWR）を 100 ms 下げて
  から上げる。その間も PYNQ は観測の JTAG を駆動している。PWR が FPGA の I/O の電源まで切るかは回路図で確かめていない
  ので、送り直す前に観測を閉じておくと確実（`analyze.py` は送り直しを使わない）。

## ビルドと書き込み

前提: リポジトリの `.venv`（`pip install -e gateware[dev] -e host[dev]`）、ForgeFPGA Workshop（Go Configure Software Hub。
場所は環境変数 `FPROBE_FORGEFPGA_HOME` で差し替え）、Zephyr（下）。

```sh
python target_ip/fprobe_obs/examples/shrike_lite/build.py --firmware   # FPGA → それを埋め込んだファームウェア
```

- FPGA は ForgeFPGA の yosys・eda-placer を GUI なしで呼ぶ（`fprobe_gw.toolchain.forgefpga`）。資源（2026-10-21）:
  ロジックのブロック 86/140（61 %）・LUT 400・FF 312・clk の最高 34.2 MHz（25 MHz で使う）。記録型の観測は入らない
  （配置で RBB が足りない）ので標本型。1 クロックの出来事（S2 の取りこぼし）は反転の印（dropped）で数え落とさない。
- ファームウェアは Zephyr 4.1（`fprobe_gw.toolchain.zephyr`。既定は `~/zmk` の Zephyr・SDK・west を書き換えずに使う。
  環境変数 FPROBE_ZEPHYR_WS・FPROBE_ZEPHYR_SDK・FPROBE_WEST で差し替え）。出力は `build/bug/fw/zephyr/zephyr.elf`
  と `zephyr.uf2`。使った Zephyr の版は出力の JSON に入る。
- 書き込みは fprobe の SWD（画面の「書き込み」・API `target.flash`）。2026-10-21 の実機: 68 KB を 6.0 秒（消去 0.78 s・
  書き込み 3.0 s・検証 0.87 s）。Shrike に MicroPython が残っていれば、書く前に Windows でドライブを取り出す。
- 書く前に、今の Flash を退避する: `target.flash.read`（path に保存先、address 0x10000000、length 0x200000 = 2 MB）。
  MicroPython のデモに戻すには、その BIN を `target.flash`（address 0x10000000）で書く（作者の手元の退避は
  `~/.fprobe/backups/shrike-lite_<Shrike のシリアル>_micropython_20261021.bin`）。
- 退避が無いとき・fprobe が使えないときは UF2 で書く: RP2040 の BOOTSEL のボタン（場所は基板のシルクで確かめる）を押したまま USB をつなぐと RP2040 の
  ドライブが出るので、`zephyr.uf2`（このデモ）か Vicharak の MicroPython の UF2 をそこにコピーする。

## fprobe での見方

1. UI を `http://<PC>:7820/#token=<トークン>&layout=system` で開く。
2. ターゲットに接続（`rp2040`、attach）し、ELF（`zephyr.elf`）を読み込む（書き込んだ ELF は自動で読まれる）。
3. **MCU だけ**: ソース行（`main.c` の結果の比較の行）にブレークポイント、変数（`g_errors`・`g_last_a` 等）を名前で、
   スレッドの一覧（`req`・`idle`）、RTT のログ（`req <番号> a <A> b <B> result <結果> ok|NG ...`）。10 分止めても
   再開して動き続ける（MicroPython は 150 秒で固まった）。
4. **FPGA だけ**: FPGA 観測パネルでプリセット「スナップショット型」→ 接続。信号の定義（グループ 0）:
   `1:0 state`（0=IDLE, 1=BUSY, 2=DONE）・`2 spi`・`3 restart`・`4 write`・`7:5 addr`（0=ID, 1=CTRL, 2=A, 3=B,
   4=RESULT, 6=STATUS, 7=LEFT）・`11:8 left_hi`・`12 dropped`（S2 で捨てるたびに反転）・`13 stuck`・`15:14 mode`。
   GPIO27 の線（題材の印 = ch 14）が無いときは、印の代わりに RTT のログと、スレッド・変数（`g_retries` 等）で見る。
5. **両方**: システム解析パネルで MCU のメモリに `g_requests`・`g_errors`・`g_retries`（名前で足せる）、FPGA の条件に
   題材ごとの条件。「記録開始」→「結果の直前に多い出来事」・「区間ごとの集計」、FPGA の条件の行の「MCU を停止」。

AI からは `system.status`・`fpga_obs.read`・`recording.correlate`・`recording.transactions`・`fpga_obs.trigger_set`・
`debug.*`・`rtos.*`・`rtt.*` で同じ情報を取得できる。`analyze.py` が題材ごとの手順の例。
