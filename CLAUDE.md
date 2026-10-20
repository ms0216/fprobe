# DebugProbe-with-FPGA

FPGA ベースの多機能デバッグプローブ。目標は「目指せ・追い越せ J-Link」。
SWD/JTAG デバッガ・ロジックアナライザ・SWO/UART・ターゲット電源制御を 1 台に統合し、
PC（特に生成 AI エージェント）からターゲットを多角的に制御・観測できるようにする。

## 目的と優先順位

1. **解析力**（最優先）: ロジアナとデバッガの一体化。クロストリガ、共通タイムスタンプで「なぜ壊れたか」を見つける力。
2. **日常のデバッガとして使える**: 書き込み・デバッグが安定して速い。OpenOCD / pyOCD / probe-rs 等の標準ツールで使える。
3. **AI による開発自動化の効率化・高精度化**が本来の目的。人間が使っても便利であること（副次目的）。

## 設計原則（必ず守る）

- **横着しない。正しく・美しい設計にする。** 場当たり的な実装・仕様の手抜き・「とりあえず動く」コードで済ませない。
  レイヤ・責務・インターフェースを明確にし、各モジュールは単独で理解・テストできること。
- **移植性を前提に設計する。** 主ターゲットは Tang Primer 20K だが、将来 Tang Nano 4K / 9K / Shrike-Lite 等で
  「簡易・安価版」を作るニーズを想定する。具体的には:
  - RTL はボード非依存の **コア** と、ボード固有の **トップ / ピン配置 / PLL / プリミティブ** を分離する。
    ベンダー固有プリミティブ（Gowin 等）はラッパー越しにのみ使う。
  - **ホストリンクを抽象化**する（USB HS via ULPI / 外付け MCU 経由の USB FS / UART 等）。上位プロトコルはリンクに依存しない。
  - **サンプルバッファを抽象化**する（DDR3 / PSRAM・HyperRAM / 内蔵 BSRAM）。
  - 機能はパラメータ・ケイパビリティで有効/無効を切り替え、**PC 側はデバイスが申告するケイパビリティを見て動作**する
    （小さい FPGA ではロジアナ chs 数やバッファ深さが減るだけで、同じ PC ソフトが動く）。
- 標準で済む部分は標準プロトコル（CMSIS-DAP v2, CDC-ACM）。独自の強みは拡張インターフェース + Python / MCP で提供。
- **特定のターゲット・機器のための機能をデバッガに足さない。まずターゲット側を改造して（配線を足す等）、デバッガの汎用の
  機能に合わせる**（利用者、2026-10-18）。そうしないと固有の機能が単調に増え続ける。FPGA には汎用の機能だけを入れる。
  FPGA の回路を増やさずに PC のソフトだけで済む機器固有の話は、本体ではなく**プラグイン**（本体の汎用の API の上に載る、
  取り外せる層）にする。

## 確定したアーキテクチャ（案1）

- **Tang Primer 20K Dock が中心。PC とは USB-OTG（USB3317, ULPI, High-Speed）1 本。**
  - USB-JTAG（BL702, Full-Speed）は FPGA ビットストリーム書き込み専用。
  - Ethernet（RTL8201F, 10/100M）は将来の第 2 ホストリンク（リモート / AI 用）候補。
- FPGA 内: USB HS デバイス（VHDL 自作、`gateware/rtl/usb/`）/ CMSIS-DAP プロセッサ（**ハードウェア FSM**。ソフト CPU では速度目標を満たせないため）/ SWD・JTAG エンジン（複数ポート）/
  ロジアナ（DDR3 128MB バッファ）/ SWO 受信・UART ブリッジ / クロストリガ / 共通タイムスタンプ。
- **Raspberry Pi Pico WH（RP2040）は補助（M11）**: Primer の J14 に UART で接続し、ADC でターゲット電圧を測って FPGA に送る（CSR `aux`・イベント 6・MCP `target_voltage`）。PC からは見えない（Pico の USB は給電と書き込みだけ）。2026-10-17 に Pico 2 から変更（RP2350 の入力エラッタ E9）。電源スイッチ・電流計測は追加ハードが必要なため保留（INA226 を FPGA の I2C に直結する案が上位互換）。
- **RTL は VHDL-2008 に統一**（ユーザー要望）。USB HS デバイススタックも VHDL で自作。ベンダー暗号化 IP は原則使わない（使う場合は抽象 I/F の裏に隠す）。
- 開発ツール（ビルド・書き込み・PLL 計算・CSR/ディスクリプタ生成・テスト）は Python。VHDL の検証は GHDL 6 + cocotb 2 を pytest から起動。
- USB 複合デバイス: CMSIS-DAP v2（SWO 含む）/ 独自バルク拡張ストリーム（ロジアナ・イベント・制御）/ CDC-ACM ×1〜2。
  ディスクリプタは `fprobe_gw/usb/fprobe_device.py` が正で、`python -m fprobe_gw gen` で生成物（VHDL ROM・CSR・ホスト定数・レジスタ表）を再生成する。CSR は `fprobe_gw/csr/fprobe_map.py` が正。
- ピン配置は `fprobe_gw/boards/primer20k.py`（資源グループ）が正。.cst/.sdc/.tcl はビルド時に生成される。
- PC 側: Python ライブラリ（全機能）+ MCP サーバ（AI 用）。波形は VCD 出力（PulseView 等で閲覧）。

性能比較の結果、案2（Pico 2 単体 CMSIS-DAP + Primer ロジアナ）が上回る点は無いため採用しない。

## ハードウェア

手持ち（追加購入なしが現方針）:
- Tang Primer 20K Dock（GW2A-LV18, DDR3 128MB）— 主機
- Raspberry Pi Pico WH — 補助（ADC。docs/superpowers/specs/2026-10-17-m11-aux-pico-design.md）。**Pico の USB を先につなぎ、信号線は後**（電源の無い Pico の GP26 に 3.3V をかけて ADC を壊した。2026-10-17）
- ESP32-WROOM-32 — 補助の ADC の校正（8 ビット DAC、実際の出力 0.06〜2.5 V）
- Raspberry Pi Pico 2, Nucleo F401RE — 主にテスト用ターゲット
- Tang Nano 4K（GW1NSR-4C, Cortex-M3 内蔵, HyperRAM）/ Tang Nano 9K（GW1NR-9, PSRAM）/ Shrike-Lite（RP2040 + 小型 ForgeFPGA）— 簡易版の移植先候補

デバッグ対象:
- Cortex-M: STM32, nRF, RP2040/RP2350（SWD）
- Raspberry Pi 2（Cortex-A7）/ Pi Zero（ARM1176, **ARMv6 で Cortex-A ではない**）— **JTAG のみ**。よって JTAG は必須機能。

PC 接続中デバイスの識別（ツールは必ずシリアル番号で対象を指定すること）:

| デバイス | USB ID / シリアル | COM |
|---|---|---|
| Tang Primer 20K（BL702, FT2232 互換） | 0403:6010 / `FACTORYAIOT_PRO` | COM3 |
| Nucleo F401RE（ST-LINK/V2-1） | 0483:374B / `<ST-LINK のシリアル>` | COM7 |
| 補助マイコン: Raspberry Pi Pico WH（MicroPython、`host/firmware/aux_pico`） | 2E8A:0005 / `<Pico WH のシリアル>` | COM13 |
| ADC 校正用: ESP32-WROOM-32（MicroPython、DAC1 = GPIO25 → Pico GP26） | 10C4:EA60 / `<ESP32 のシリアル>` | COM12 |
| ~~Raspberry Pi Pico~~（ADC 故障。補助に使わない） | 2E8A:0005 / `<Pico のシリアル>` | — |
| PYNQ-Z2（Zynq-7020）— fprobe の移植先（2026-10-19 から fprobe 専用。仕様 docs/superpowers/specs/2026-10-19-pynq-z2-port-design.md） | 0403:6010 / `1234-TUL` | COM6 |

Primer と PYNQ-Z2 はどちらも FT2232 互換 JTAG のため、openFPGALoader 等ではシリアル番号指定なしで実行しないこと。
PYNQ-Z2 の PL は JTAG（Vivado の hw_server）で書く（2026-10-20 に利用者が承認。Linux 経由は Linux が固まった）。
必ずシリアル `1234-TUL` で口を選び、IDCODE が Zynq-7020 であることを照合してから書く（Primer と取り違えない）。
書く前に PYNQ の上の `fprobe-pl before` で中継を止め、PS と PL の間のレベルシフタを切る（`fprobe_gw build/program
--board pynq_z2` がする。詳細は docs/boards/pynq-z2.md）。
本プロジェクトの書き込みツール（`fprobe_gw program` / `build --program`）は、Primer の USB シリアル番号から OS 上の接続ポートを引き、
そのポートの JTAG チャンネルだけを選んで IDCODE を照合してから書く（同じ「USB Debugger A」の Tang Nano 9K 等が接続されていても触れない）。

制約:
- Primer の I/O は 3.3V。レベル変換なしではターゲットは 3.3V 限定（1.8V 対応は将来課題）。

## ツールチェーン

- Gowin EDA: `C:\Gowin\Gowin_V1.9.11.03_Education_x64\IDE\bin`（`gw_sh.exe`）。Programmer: `C:\Gowin\Gowin_V1.9.11.03_Education_x64\Programmer\bin`。
  （`C:\Gowin\Gowin_V1.9.12.03_x64` は空なので使わない）
- Python 3.13（venv: `.venv`。`pip install -e gateware[dev] -e host[dev]`）、GHDL 6.0（winget, mcode 版）、cocotb 2.1。
- ホストライブラリは `host/fprobe`（ゲートウェアのテストもこの符号化・解析を使う）。テストは `pytest gateware` と `pytest host`。
- このPCの cmd はカレントのバッチを探さないため、ツールは絶対パスで直接起動する。
- Gowin の出力物（`*.fs` 等）は読み取り専用属性で作られる。`programmer_cli` には `.fs` を絶対パスで渡す。
- 全体設計: `docs/superpowers/specs/2026-10-15-fprobe-architecture-design.md`

## VHDL コーディング規約

- VHDL-2008（GHDL `--std=08` / Gowin `vhd2008`）。VHDL-2019 の構文（式中の `x when c else y` 等）は使わない。
- 文字列リテラル（`report` 等）は ASCII のみ（GHDL が非 ASCII を拒否する）。日本語はコメントに書く。
- `ieee.numeric_std` を使う。リセットは同期・アクティブハイの `rst`（非同期要求は `reset_sync` で同期化）。
- ストリーム I/F のポート名は `<prefix>_valid` / `<prefix>_ready` / `<prefix>_data`。
- VHDL は大文字小文字を区別しないため、状態名には `S_` 接頭辞を付けて信号名と衝突させない。
- **すべての信号を明示的に駆動する。** 宣言の初期値（`:= '0'`）は合成で無視され、駆動されない配線は実機で値が定まらない（配置で変わり、シミュレーションでは初期値で隠れる）。配列の使わない要素、`generate` で外した機能の状態の信号も駆動する。ビルドは合成の警告 EX1998（駆動されない配線）があれば失敗する。
- 共通関数は `rtl/common/util_pkg.vhd`。すべての entity に cocotb テストを付ける（`tests/tb/*_tb.py` + pytest ラッパ）。

## 進め方

- 作業はブレインストーミング → 仕様書 → 実装計画 → 実装 の順。仕様書は `docs/superpowers/specs/` に置く。
- やりとり・コメント・ドキュメントは日本語。
