# fprobe — FPGA マルチファンクション・デバッグプローブ

SWD/JTAG デバッガ・ロジックアナライザ・SWO/UART・クロストリガを 1 台に統合したデバッグプローブ。
PC から（特に AI から）ターゲットとその FPGA の中を、1 本の時間軸で観測・制御できる。fprobe は測定器で、測った事実だけを返し、判断は使う人・AI がする。
全体設計は [docs/superpowers/specs/2026-10-15-fprobe-architecture-design.md](docs/superpowers/specs/2026-10-15-fprobe-architecture-design.md)。

## ここから読む

| 知りたいこと | 文書 |
|---|---|
| 何を用意し、どう書き込み、どう接続を確かめるか | [はじめに](docs/guide/getting-started.md) |
| 対象の FPGA の中を観測して MCU・ピンと並べる通しの手順 | [FPGA 解析](docs/guide/fpga-analysis.md) |
| ピンの信号を読む（配線・取り込み・解読・観測と並べる） | [ロジアナ](docs/guide/logic-analyzer.md) |
| AI（MCP）から使う（登録・デーモン・道具の名前） | [MCP](docs/guide/mcp.md) |
| うまくいかない | [トラブルシューティング](docs/guide/troubleshooting.md) |
| 全メソッド・全道具の一覧 | [API 一覧](docs/api.md) |
| ボードごとの機能の対応表 | [ボードの対応表](docs/architecture/feature-matrix.md) |

最短の例（PYNQ-Z2 を Ethernet で使う。詳細は [はじめに](docs/guide/getting-started.md)）:

```sh
.venv/Scripts/python -m pip install -e "gateware[dev]" -e "host[dev,mcp]"      # インストール
.venv/Scripts/python -m fprobe_gw build fprobe_pynq --program                  # ビルドして書き込み
claude mcp add fprobe -- .venv\Scripts\fprobe-mcp.exe --eth 192.168.77.2       # MCP に登録（AI から使う）
```

あとは AI に「`probe_info` で機能を確かめて」と頼む。FPGA の観測（`fpga_obs`）を持つのは PYNQ-Z2 の構成だけ（[FPGA 解析](docs/guide/fpga-analysis.md)）。

## セットアップ（Windows）

```sh
python -m venv .venv
.venv/Scripts/python -m pip install -e "gateware[dev]" -e "host[dev]"
```

Gowin EDA Education 1.9.11.03 が `C:\Gowin\Gowin_V1.9.11.03_Education_x64` に必要（別の場所なら環境変数 `FPROBE_GOWIN_HOME`）。

RTL は VHDL-2008（`gateware/rtl/`）。シミュレーションには GHDL 6（`winget install ghdl.ghdl.ucrt64.mcode`。
別の場所なら環境変数 `FPROBE_GHDL_HOME`）を使い、テストは cocotb で書いて pytest から実行する。

## テスト

```sh
.venv/Scripts/python -m pytest gateware            # VHDL シミュレーション（cocotb + GHDL）とツールの単体テスト
.venv/Scripts/python -m pytest gateware --hil      # 実機テスト（Primer 20K 接続時。SRAM に書き込む）
.venv/Scripts/python -m pytest host                # ホストライブラリの単体テスト
```

## ビルドと書き込み

```sh
.venv/Scripts/python -m fprobe_gw detect                   # 書き込み対象の判定（JTAG アクセスなし）
.venv/Scripts/python -m fprobe_gw build fprobe --program   # 製品デザインをビルドして SRAM に書き込み
.venv/Scripts/python -m fprobe_gw gen                      # 生成物（ディスクリプタ ROM・CSR・ケイパビリティ等）を再生成
```

書き込みは Primer 20K（BL702 の USB シリアル番号から引いた接続ポートにある `USB Debugger A`、IDCODE `0x0000081B`）
にのみ行われ、該当が一意でない場合や IDCODE が一致しない場合は拒否される。

## ホストから使う

```python
from fprobe import Device, generated as g

with Device.open() as dev:
    print(dev.capabilities())           # ボード・デザイン・クロック・使えるチャンネル
    dev.write32(g.SYSTEM_SCRATCH, 0x1234)
    print(dev.time() / dev.capabilities().timebase_hz, "s")
```

レジスタ表は [docs/registers.md](docs/registers.md)（生成物）。

### UART のホストリンク（SerialLink、小さいボード向け）

USB HS の PHY が無いボード（Tang Nano 9K 等）でも、同じホストソフトが USB-UART（COM ポート）越しに使える
（論理チャンネルを COBS + CRC16 のフレームで多重化）。Primer 20K では `fprobe_serial` 構成で試せる（BL702 の COM ポート）。

```python
from fprobe.session import Session

with Session.open_serial("COM3", 2_000_000) as s:         # COM ポートはボードの USB シリアル番号で選ぶこと
    print(s.device.capabilities())                        # host_link = 2（SerialLink）、features で外した機能が分かる
    with s.debug(target_override="stm32f401xe", frequency=4_000_000) as dbg:
        print(hex(dbg.target.read32(0xE000ED00)))
```

構成: `fprobe_serial`（Primer 20K）、`fprobe_nano9k`（Tang Nano 9K、クロストリガ・多段トリガを外した構成。ビルドのみ確認）。

### Ethernet のホストリンク（EthLink、UDP）

Primer 20K Dock の Ethernet（RTL8201F、100 Mb/s）で、USB と同じ論理チャンネルを UDP で運ぶ（構成 `fprobe_eth`。
DDR3 の深いバッファ・クロストリガを含む製品デザインと同じコア）。PC は標準のソケットだけで使える（ドライバ不要）。
ボードの既定は IP `192.168.77.2`・UDP ポート 63920。直結するときは PC の LAN アダプタを `192.168.77.1/24` にする。

```python
from fprobe.session import Session

with Session.open_eth("192.168.77.2") as s:                  # ping 192.168.77.2 にも答える
    print(s.device.capabilities())                            # host_link = 3（EthLink）
    with s.debug(target_override="stm32f401xe") as dbg:
        print(hex(dbg.target.read32(0xE000ED00)))
```

MCP: `fprobe-mcp --eth 192.168.77.2`。UDP は落ちうるので、FPGA → PC のデータグラムに通し番号を付け、
落ちた数を `s.link.lost_frames` に数える（バイト列のチャンネルの欠けを黙って見逃さない）。

### PYNQ-Z2（Zynq-7020）

PYNQ-Z2 では、同じ fprobe_core を PL に載せ、PS（ARM）の Linux の中継 `fprobe-relay` が UDP 63920 と PL の
`zynq_frame_link`（AXI4-Lite）の間でフレームを受け渡す（線上の形式は Ethernet 版と同じなので、PC 側は `Session.open_eth`
のまま）。Primer では構成に分けている機能（JTAG・サンプラ・故障注入・専用の入力・RTS/CTS）を 1 つの構成 `fprobe_pynq`
に入れる。ビルドは Vivado（`C:\AMDDesignTools\2025.2\Vivado`、環境変数 `FPROBE_VIVADO_HOME`）。

```sh
FPROBE_PYNQ_SUDO_PASSWORD=... python -m fprobe_gw pynq install   # 中継と書き込みの補助を PYNQ に据え付ける（一度）
python -m fprobe_gw build fprobe_pynq --program                  # Vivado でビルドし、JTAG（PYNQ-Z2 の USB シリアル番号で口を選び、IDCODE を照合）で書く
python -m fprobe_gw pynq status
```

詳細（配線・基板の PMOD の抵抗・書き込みの手順・Linux が固まる条件）: [docs/boards/pynq-z2.md](docs/boards/pynq-z2.md)。

### 標準のデバッグツール（CMSIS-DAP v2）

ドライバの追加なしに pyOCD・OpenOCD・probe-rs から CMSIS-DAP v2 として使える（SWCLK 〜25 MHz）。
他のプローブ（ST-LINK 等）を巻き込まないよう、必ず fprobe の VID:PID（1209:0001）とシリアルで指定する。

```sh
pyocd commander -u 0001 -t stm32f401xe -f 25m
openocd -c "adapter driver cmsis-dap; cmsis-dap vid_pid 0x1209 0x0001; adapter serial 0001; transport select swd" \
        -f target/stm32f4x.cfg -c "adapter speed 25000"          # GDB はポート 3333
probe-rs read --chip STM32F401RETx --probe 1209:0001:0001 --speed 25000 b32 0xE000ED00 1
```

実機試験: `tests/hil/test_openocd_hw.py`（識別・Flash 書き込みと検証・GDB サーバ）、`test_probe_rs_hw.py`。

### デバッガとロジアナを同時に使う

Windows の libusb は機器の全インターフェースを 1 つのハンドルで握るため、pyOCD と `fprobe.Device` を別々に
開くことはできない。`fprobe.session.Session` が 1 つのハンドルを両方に渡す（`pip install -e host[debug]`）。

```python
from fprobe.la import LogicAnalyzer, write_vcd
from fprobe.session import Session

with Session.open("0001") as s:
    la = LogicAnalyzer(s.device)
    period = la.start(rate_hz=25e6, pretrigger=1024, fall=1 << 8)   # ch8 = プローブが SWDIO を離した
    with s.debug(target_override="stm32f401xe", frequency=4_000_000) as dbg:
        print(hex(dbg.target.read32(0xE000ED00)))
        la.wait()
        write_vcd(la.read(period), "swd.vcd")
```

### ロジアナの深いバッファ（DDR3 128 MB）

外部メモリのあるボードでは、ロジアナを DDR3 に記録できる（16 ch × 100 MS/s、RAW で 0.67 秒。変化点圧縮なら信号次第で
それ以上）。読み出しは USB の拡張ストリーム（約 40 MB/s）。長い記録は変化点のまま扱う（`read_transitions`）。

```python
from fprobe.la import DeepAnalyzer, write_vcd

la = DeepAnalyzer(s.device)
la.start(rle=True, fall=1 << 8, post_beats=100_000)       # トリガ後 100,000 拍（RLE は 1 拍 = 4 変化点）
...                                                        # ターゲットを動かす
la.wait()
tr = la.read_transitions(before_beats=10_000)
write_vcd(tr, "deep.vcd")
```

DDR3 の状態は `fprobe.memory.Memory`。メモリ試験（`Memory(dev).test(mem_clock_hz=...)`）は基板立ち上げ用の構成
`memtest`（`python -m fprobe_gw build memtest --program`）で行う（製品構成 `fprobe` は試験回路を持たない）。
DDR3 のあるボードでは内蔵 BSRAM のロジアナを持たず（LA_DEPTH = 0）、`LogicAnalyzer` は自動で深いバッファを使う。

### ロジアナの連続ストリーミング

外部メモリ（120 MB）を FIFO にして、止めずに PC へ流し続ける（USB 約 40 MB/s。RAW なら 16 ch × 約 20 MS/s、
変化点圧縮なら信号次第でそれ以上）。PC が受け取りに遅れて未読がリングに迫ると止めて overrun を立てる（黙って欠けない）。

```python
from fprobe.la import DeepAnalyzer

stats = DeepAnalyzer(s.device).stream_to_vcd("long.vcd", duration_s=60, rle=True)   # 少しずつ書く
print(stats.samples, stats.mb_per_s, stats.overrun)
```

CLI: `fprobe la out.vcd --stream 60 --rle`。MCP: `la_stream`。

### DAP のイベント記録

線上の DAP 転送（pyOCD 等の読み書き、クロストリガの割り込み書き込み）を、ロジアナと同じ時間軸の時刻付きで
DDR3 のリングに記録する（ロジアナの深いバッファと同時に使える）。ポストされた AP の読みは `resolve` で対応付ける。

```python
from fprobe.events import EventLog, resolve

log = EventLog(s.device)
log.start()
...                                                        # pyOCD でターゲットを操作する
log.stop()
for a in resolve(log.read()):
    print(a.time, a.kind, hex(a.address), hex(a.value))   # ap_write / ap_read / dp_read / dp_write / inject（割り込み書き込み）/ fail
```

### JTAG

CMSIS-DAP の JTAG に対応（J12: TCK = SWCLK、TMS = SWDIO、TDI、TDO = SWO のピン）。pyOCD はチェーンを指定できない
（IR 4 の 1 台を仮定）ので、`Session.debug(jtag_chain=[...])` で IR 長を渡す（TDO 側の装置から）。

```python
with Session.open("0001") as s, s.debug(target_override="stm32f401xe", jtag_chain=[4, 5]) as dbg:
    print(hex(dbg.target.read32(0xE000ED00)))
```

### クロストリガ

ロジアナのトリガ・デバッガの番地アクセス・ホストの指令を、ロジアナのトリガとターゲット停止（DHCSR への割り込み
書き込み）にハードウェアでつなぐ（`fprobe.xtrig`、レジスタは docs/registers.md の `xtrig`）。

```python
from fprobe.xtrig import CrossTrigger, Source

xt = CrossTrigger(s.device)
xt.set_inject()                                # 既定: DHCSR ← C_HALT（停止）
xt.route(to_inject=Source.LA_TRIGGER)          # 波形の条件でターゲットを止める（実測: トリガから 90 µs 弱）
xt.set_match(0x20004008)                       # デバッガがこの番地に書いたら…
xt.route(to_la=Source.DAP_MATCH)               # …ロジアナをトリガ
```

順序・回数・時間の条件は多段トリガ（`TriggerSequencer`、最大 4 段・パターン 2 つ・番地一致・ホスト指令）:

```python
from fprobe.xtrig import Event, Pattern, Stage, TriggerSequencer

seq = TriggerSequencer(s.device)
seq.configure([Stage(Event.PATTERN_A), Stage(Event.PATTERN_B, within_s=10e-6)],   # A の後 10 µs 以内に B
              a=Pattern(rise=1 << 3), b=Pattern(fall=1 << 5))
xt.route(to_inject=Source.SEQUENCE)            # 成立したらターゲットを止める
seq.arm()
```

### ソースの言葉でのデバッグ（fprobe.debug）

ELF のシンボルと行番号（`fprobe.symbols`）で、場所を関数名・変数名・`ファイル:行` で扱う。

```python
from fprobe.debug import Debugger
from fprobe.symbols import Symbols

with Session.open("0001") as s, s.debug(target_override="stm32f401xe") as dbg_s:
    dbg = Debugger(dbg_s.target, Symbols("app.elf"))
    dbg.breakpoint("my_function")                  # Flash はハードの比較器、RAM はソフトの BKPT（pyOCD が選ぶ）
    stop = dbg.run_until_halt(timeout_s=2)         # Stop(reason="breakpoint", where=my_function+0 (app.c:42))
    print(dbg.read_variable("counter"), [str(f.where) for f in dbg.backtrace()])
```

試験用のファームウェア `host/firmware/sram_demo`（zig cc、SRAM で動く）で実機試験する（`test_debug_hw.py`）。

### MCU と FPGA が連携するシステムの解析（FPGA 観測・システム解析）

対象の FPGA に観測回路（[target_ip/fprobe_obs](target_ip/fprobe_obs/README.md)。ストリーム型 `obs_core`、極小の FPGA 向けの
スナップショット型 `obs_snap_core`）を組み込むと、fprobe がその JTAG で FPGA 内部の信号を読み、MCU（SWD）・ピン（ロジアナ）と
同じ時間軸に並べる。UI は `#layout=system`（表示 → システム解析のレイアウト）:
- MCU のメモリ（アドレスか ELF の変数名。1 秒あたりの増加数）と、FPGA の最新値・条件（`restart changed and state=BUSY`）の
  成立レートを並べて表示する。
- 「記録開始」で MCU のメモリ（名前付き）・ピン・FPGA・DAP を 1 つの記録にまとめる。「時間軸で表示」で確認する。
- 結果の直前に多い出来事（`recording.correlate`）: 結果（`errors changed`）の直前に起きている条件を、先行率の順に並べる。
  あわせて、記録のうち候補の後の時間窓に当たる時間の割合（coverage）も測って示す。どちらも測った事実だけで、原因の判定・
  経験則による順位づけはしない。候補の自動探索もできる。
  「表示」でその例の時刻へ、「頻度を表示」で条件の表へ追加する。値の継続時間（`recording.durations`）。
- 区間ごとの集計（`recording.transactions`）: 記録をリクエスト等の区間に切り、開始時の値ごとに結果の割合・条件の回数
  （例: B の大きい要求だけ再試行され、再試行された要求だけ誤る）。
- まれにしか起きない不具合: 記録中に条件でマーカーを付け、記録を停止する（`recording.trigger_set`）。FPGA の信号だけの条件なら
  ハードウェアのクロストリガで MCU を即座に停止する（`fpga_obs.trigger_set`。遅延は SWD 4 MHz で約 64 µs、1 MHz で約 0.2 ms）。
- ターゲットを書き換えた・リセットした後: FPGA は自動で再接続、MCU は同じ設定で再接続。表と条件はデーモンが保持し、
  別の PC（`--lan`）・AI（`ui_state` の system）と共有する。
通して試せるデモ: [Shrike-Lite（RP2040 + ForgeFPGA）](target_ip/fprobe_obs/examples/shrike_lite/README.md)。
設計: [docs/superpowers/specs/2026-10-21-system-analysis-design.md](docs/superpowers/specs/2026-10-21-system-analysis-design.md)。

### プラグイン（機器・用途に固有の処理）

本体の汎用の API の上に載る、取り外せる層（`fprobe.plugins.<名前>`。本体からは import しない）。デーモンの
`DaemonClient` でも、プロセス内の `ProbeCaller(probe)` でも同じように動く。

| プラグイン | 内容 |
|---|---|
| `keymatrix` | キーボードの行列のキーを FPGA から押したことにする（クロストリガの出力） |
| `spiflash` | SPI NOR Flash を直接読み書きする（J-Flash SPI に当たる） |
| `rp2350_trace` | RP2350 のトレース FIFO と DMA で ETM の命令トレースを SRAM に溜めて解読する |
| `zmk_health` | ZMK のキーボードを止めずに診断する（BLE の接続・チャタリング除け・電池・起動からの時間・スタック） |

```python
from fprobe.plugins import ProbeCaller
from fprobe.plugins.zmk_health import ZmkHealth
print(ZmkHealth(ProbeCaller(probe)).report()["findings"])   # target_connect（attach）と elf_load の後
```

### CLI と MCP サーバ

- `fprobe-daemon [--serial 0001 | --port COM3 | --eth 192.168.77.2] [--lan]`: fprobe を 1 つ握り、API（JSON-RPC 2.0 over
  WebSocket、`ws://127.0.0.1:7820/api`、文書は [docs/api.md](docs/api.md)、仕様の正は `host/api/fprobe_api.json`）で全機能を出す。
  AI（MCP）と人間（画面・スクリプト）が同時につながり、すべての操作は記録（`~/.fprobe/activity/*.jsonl`、通知 `activity`）として
  流れる。人間は `session.control.pause` で AI の操作を保留させ、交代できる。`fprobe-mcp` はデーモンの利用者で、デーモンが
  無ければ起動する（使い方は変わらない）。デーモンは MCP の終了後も常駐して機器を握り続けるので、`fprobe` の CLI や HIL の
  試験を使うときはデーモンを止める。一時停止で 300 秒を超えて保留された要求は、再開後に実行せず E_TIMEOUT で返す。
- 画面（人間用、ブラウザ）: 最初に一度 `cd host/ui && npm install && npm run build`（Node.js 24）。`fprobe-daemon` を起動して
  表示される `画面: http://127.0.0.1:7820/` を開く（MCP が起こしたデーモンでも同じ URL）。AI の操作が 1 件ずつ流れ（行を選ぶと
  引数・結果）、「一時停止」で AI の操作を保留させて画面からターゲットを操作し、「再開」で AI に戻す。ターゲットの状態・
  止まった場所・電圧、プローブのケイパビリティ、つながっている利用者も見える。`--lan` のときは URL の `#token=…` を付けて開く。
  開発: `npm run dev`（/api を 7820 のデーモンへ流す）、`npm test`（単体と、偽の機器のデーモンとの結合）、`npm run check`（型）。
  機器なしで試すときは `python host/tests/fake_daemon.py --port 7820`。
  「デバッガ」タブ（`#tab=debug` でも開く）: ELF を読む / RAM へ読み込む、ソースの行の左端を押してブレークポイント、実行・停止・
  1 命令・リセット、止まった行の強調、変数（名前を足す。「見張る」でウォッチポイント）・呼び出し履歴・レジスタ。AI が置いた
  ブレークポイントや止めた場所も同じように見える。ソースはデーモンが ELF の行番号の情報から引く（ELF が参照するファイルだけ）。
  「波形」タブ（`#tab=wave`）: 最後の捕捉（AI が取ったものも）を表示。ホイールで拡大縮小、ドラッグで移動、ダブルクリックで全体、
  カーソルの時刻（トリガ = 0）と各チャンネルの値。画面から捕捉（速度・トリガ前・トリガのチャンネルと向き・深いバッファ）。
  デーモンが捕捉を画面の幅に縮めて送る（`la.view`）ので、深いバッファの長い捕捉でも同じ操作で見られる。
- `fprobe info` / `fprobe la out.vcd --rate 25e6 --fall 8`（`python -m fprobe` でも可）
- `fprobe-mcp --serial 0001`: AI エージェント向けの MCP サーバ（stdio、`pip install -e host[mcp]`）。
  道具は API のメソッド名の `.` を `_` にしたもの（例: `target_connect`, `debug_run_until_halt`, `la_capture`）。
  一覧と引数は [docs/api.md](docs/api.md)。
  ソースの言葉で調べる（`debug_evaluate` の C の式、`debug_backtrace` の各段の引数・ローカル変数、`debug_disasm_text`）、
  周辺レジスタ（`target_peripheral_read` が SVD でフィールドに解読）、止めずに見る（`rtt_start` / `rtt_read`、`swo_text`、
  セミホスティングは自動で処理）、捕捉の通信の解読（`la_decode`: UART・SPI・I2C・SWD）もできる。
  セミホスティングのファイル操作（`debug.semihosting.configure` の file_root）は API からだけ指定でき、AI の道具には出さない。
  Claude Code なら `claude mcp add fprobe -- <venv>\Scripts\fprobe-mcp.exe --serial 0001`。
