# はじめに（最初の 30 分）

この文書は、初めて fprobe を使う人が「何を用意し、どの順に何をすれば、つながって動くか」を 1 本で追うためのものです。
FPGA の中を観測する使い方は [fpga-analysis.md](fpga-analysis.md)、AI（MCP）からの使い方は [mcp.md](mcp.md)、
うまくいかないときは [troubleshooting.md](troubleshooting.md) を見てください。

## 1. これは何か

- FPGA ボード 1 枚で、SWD / JTAG デバッガ、ロジックアナライザ、SWO / UART の受信、クロストリガ（ハードウェアで数 µs 以内の連動）を兼ねる。
- 標準の CMSIS-DAP v2 として pyOCD・OpenOCD・probe-rs から使える。独自の拡張（ロジアナ・イベント記録・FPGA の観測）は
  Python ライブラリと MCP サーバから使う。
- すべての記録（MCU のメモリ、ピン、DAP の転送、対象の FPGA の信号）は、デバイスのタイムベースという 1 本の時間軸に載る。
- 全機能は 1 つの API（JSON-RPC、仕様の正は `host/api/fprobe_api.json`、文書は [../api.md](../api.md)）に出ていて、
  人の画面と AI の道具（MCP）は同じ API の同じ機能を使う。
- fprobe は測定器で、返すのは測った事実（回数・時刻・間隔・割合・欠け）です。原因の判定はしません。判断は使う人・AI がします。

## 2. 用意するもの

ボードの構成を選びます。機能の対応表の正は [../architecture/feature-matrix.md](../architecture/feature-matrix.md) です（設計ごとに何があるか）。

| 構成 | 向く使い方 | 無い機能（2026-10-25 の対応表から） |
|---|---|---|
| PYNQ-Z2（設計 `fprobe_pynq`、深いバッファは `fprobe_pynq_deep`） | 全機能の基準。FPGA の観測（`fpga_obs`）を持つのはこの 2 設計だけ。ホストとは Ethernet（PS の Linux の中継 `fprobe-relay`、UDP） | 補助マイコン（aux）は無い。`fprobe_pynq` に DDR のロジアナの深いバッファは無い（内蔵 8192 サンプル。`fprobe_pynq_deep` は PS の DDR 32 MB） |
| Tang Primer 20K Dock（設計 `fprobe`・`fprobe_jtag`・`fprobe_la`・`fprobe_eth` 等） | USB HS で SWD / JTAG・DDR3 128 MB のロジアナ。Primer の 1 つの設計に全機能は入らず、SWD 用・JTAG 用・ロジアナ入力用に分かれる（`target_connect` が必要なら自動で書き替える） | **FPGA の観測（`fpga_obs`）は無い**。設計によって JTAG・DAP サンプラ・故障注入が無い |
| Tang Nano 9K（`fprobe_nano9k`） | 小さい構成。UART のホストリンク。ビルドのみ確認 | クロストリガ・多段トリガ・FPGA の観測が無い |

対象（デバッグしたいもの）と、そのための配線も要ります。電圧は 3.3 V のみです（レベル変換なし）。

- SWD: SWCLK・SWDIO・GND（ボードごとのピンは各ボードの文書: [../boards/pynq-z2.md](../boards/pynq-z2.md)、[../boards/tang-primer-20k-dock.md](../boards/tang-primer-20k-dock.md)）。
- 対象の FPGA を観測するときは [fpga-analysis.md](fpga-analysis.md) の配線の節。

## 3. インストール

```sh
python -m venv .venv
.venv/Scripts/python -m pip install -e "gateware[dev]" -e "host[dev]"      # Linux・macOS は .venv/bin/python
```

入口（使い方）ごとに入れる extras を選べます。`host[...]` の extras は次のとおりで、`fprobe[...]` の自己参照で積み上がります。

| 入口 | 入れるもの | 中身 |
|---|---|---|
| ライブラリ（USB・Ethernet の `Device`、VCD） | `pip install -e host` | pyusb・libusb-package |
| ライブラリ + デバッガ（pyOCD と同時利用） | `host[debug]` | + pyocd・capstone |
| デーモン + 画面（`fprobe-daemon`） | `host[daemon]` | + uvicorn・starlette・websockets・jsonschema・pyyaml・pyserial・numpy |
| AI（`fprobe-mcp`） | `host[mcp]` | `daemon` + mcp（MCP が必要ならデーモンを起こすので daemon を含む） |
| 試験（`pytest host`） | `host[dev]` | `mcp` + `aux` + pytest・httpx |

- Primer のビルド・書き込みには Gowin EDA Education 1.9.11.03（`C:\Gowin\Gowin_V1.9.11.03_Education_x64`、別の場所なら環境変数 `FPROBE_GOWIN_HOME`）。
- PYNQ-Z2 のビルドには Vivado（環境変数 `FPROBE_VIVADO_HOME`）。
- VHDL のシミュレーション（開発者向け）には GHDL 6（環境変数 `FPROBE_GHDL_HOME`）。
- 画面（ブラウザ）を使うなら `cd host/ui && npm ci && npm run build`（Node.js 24 が標準。22 でも動くことを確かめた）。
- 動作確認（機器不要）: `cd host && ../.venv/Scripts/python -m pytest -q`（`host[dev]` が要る）。
  速さの約束の試験（`perf`）は既定から外れているので、続けて `pytest -m perf` を走らせる（[../testing-speed.md](../testing-speed.md)）。
- 入口ごとに、まっさらな venv で動くことを確かめる検査: `python tools/install/check_extras.py`（PyPI が要る）。
  網の要らない静的な照合は `host/tests/test_install_extras.py`（extras と import の食い違いを見る）。

インストールで入るコマンドは `fprobe`（CLI）、`fprobe-mcp`（MCP サーバ）、`fprobe-daemon`（デーモン）の 3 つです。

## 3.5 機器なしで試す（偽の機器のデモ）

機器が無くても、画面と MCP を偽の機器で動かせます（試験用の偽物で、値は本物ではありません）。`host[mcp]` と画面のビルド（上）だけが要ります。

```sh
python host/tests/fake_daemon.py --demo --port 7820
```

- 1 行目に `PORT 7820` が出たら、ブラウザで `http://127.0.0.1:7820/` を開く。デモの ELF を読んで接続・停止した状態で、各パネルに偽の値、
  Timeline に偽の捕捉と記録が出る。画面が白いときは画面をビルドしていない（`cd host/ui && npm ci && npm run build`）。
- 同じデーモンへ MCP をつなぐ（別の端末）。`fprobe-mcp` は同じポートにデーモンがいればそれを使う:

```sh
claude mcp add fprobe-demo -- <venv>/bin/fprobe-mcp --listen 7820
```

  AI に「`probe_info` を呼んで」と頼むと偽の機器のケイパビリティが返る（道具は 367 個。`fprobe-mcp` が API から作る）。
  AI の操作は画面の活動の欄に流れる。人と AI が同じ API を見ていることを確かめられる。
- 止めるのは Ctrl+C。

## 4. ビットストリームを書く

書き込みは SRAM への書き込みです（電源を切ると消える）。外部 Flash に書くのは `--persistent` を付けたときだけです。

### Primer 20K

```sh
.venv/Scripts/python -m fprobe_gw detect                   # 書き込み対象の判定（JTAG アクセスなし）
.venv/Scripts/python -m fprobe_gw build fprobe --program   # ビルドして SRAM に書く
```

書き込みツールは、Primer の USB シリアル番号から接続ポートを引き、そのポートの JTAG チャンネルだけを選んで IDCODE（`0x0000081B`）を
照合してから書きます。該当が一意でない・IDCODE が違うときは拒否します（他の FT2232 系ボードを巻き込まないため）。
作ってあるビットストリームを書くだけなら `python -m fprobe_gw program <top.fs> --board primer20k`。

### PYNQ-Z2

```sh
python -m fprobe_gw pynq install                      # 中継と書き込みの補助を PYNQ に据え付ける（一度。環境変数 FPROBE_PYNQ_SUDO_PASSWORD が要る）
python -m fprobe_gw build fprobe_pynq --program       # Vivado でビルドし、JTAG で書き、PS の中継を起動する
python -m fprobe_gw pynq status
```

書く前に PS 側の `fprobe-pl before` で中継を止め、PS と PL の間のレベルシフタを切る手順は `build --program` / `program` が行います。
配線・手順・Linux が固まる条件は [../boards/pynq-z2.md](../boards/pynq-z2.md)。PYNQ-Z2 の既定の IP は `192.168.77.2`
（USB の LAN アダプタで直結し、PC 側を `192.168.77.1/24` にする）。

使える構成の一覧は `python -m fprobe_gw build --help`。

## 5. 最初の接続の確認

```sh
.venv/Scripts/python -m fprobe info                       # USB 接続の場合。ケイパビリティ・タイムベース・個体

```

`fprobe info` は、ボード・設計・クロックの周波数・使えるチャンネル・機能（features）を表示します。ここに期待する機能
（FPGA の観測なら `fpga_obs`）が出ていることを確かめてください。出ない構成では、その機能は使えません。
`fprobe` の CLI の `--eth` / `--port` は `run` サブコマンドだけの引数です。Ethernet の PYNQ-Z2 は下の Python の例か、デーモン（`fprobe-daemon --eth`）と `probe_info` で確かめます。
USB の機器が複数あるときは `--serial` か、`fprobe name set` で付けた名前の `--name` で指定します。

Python から:

```python
from fprobe.session import Session
with Session.open_eth("192.168.77.2") as s:
    print(s.device.capabilities())
```

AI から使うなら、MCP の登録（[mcp.md](mcp.md)）をして、`probe_info` を呼ばせます。

```sh
.venv/Scripts/fprobe-daemon.exe --eth 192.168.77.2    # 画面: http://127.0.0.1:7820/
```

## 6. 失敗したときの見方

- 機器が見つからない / すでに使われている: `fprobe-daemon` が機器を握っていると `fprobe` の CLI・Python は開けません（デーモンを止める）。
  Windows の libusb は機器のインターフェースを 1 つのハンドルで握るため、pyOCD と `fprobe.Device` は別々に開けません（`Session` を使う）。
- 書き込みが拒否された: IDCODE の照合に失敗した、またはポートが一意でない。`python -m fprobe_gw detect` で見える機器を確かめる。
- Ethernet でつながらない: `ping 192.168.77.2`。PC 側のアドレスが `192.168.77.1/24` か。
- 症状別の一覧は [troubleshooting.md](troubleshooting.md)。
- **API（WebSocket）を直接使うとき**: 名前を当てずっぽうで試さず、`session.methods`（一覧と説明。`prefix` で絞る、`name` で 1 つの全部）と `session.hints`
  （使い方の落とし穴の記録。失敗した API の名前を `method` に）。ELF の関数・変数は `debug.symbol.search`（名前の一部から探す）。
  失敗の応答の `ApiError.data` に `hint_ids`・候補が付く。ヒントの一覧は [hints.md](hints.md)。
