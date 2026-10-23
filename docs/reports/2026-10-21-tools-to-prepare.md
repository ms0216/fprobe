# 検討を進めるために用意したいツールと導入方法（2026-10-21）

この PC（Windows 11）に入れると、fprobe の検討・試験が進むツールをまとめる。効果の大きい順。
状態は 2026-10-21 に確かめた。ハードウェア（MAX10 の評価ボード、電流計測の INA226、1.8V 用のレベル変換）はここに入れない。

| # | ツール | 何が進むか | 今の状態 | 入れ方の要点 | 費用・アカウント |
|---|---|---|---|---|---|
| 1 | RP2040 の C の開発環境 | Shrike のデモを C で書き直せる（ELF の変数名・ソース行での解析、止めても固まらない） | GCC・cmake・ninja は**ある**。pico-sdk の単体は無い | 下の 1 を参照。A なら追加の導入なし | 無料 |
| 2 | pytest-xdist | テストの並列実行（今は全体で約 2 分半） | 無い | 作業用の環境に pip で入れる | 無料 |
| 3 | Pillow | 画面の撮影の切り出し・比較（時間軸の段の確かめ） | 無い | 作業用の環境に pip で入れる | 無料 |
| 4 | Quartus Prime Lite | FPGA 観測の本番の対象（MAX10）での確かめ（仕様の O4） | 無い | Intel のサイトから。数 GB | 無料。ダウンロードに Intel のアカウントが要る場合あり |
| 5 | 7-Zip | 配布物の展開（今は Python の部品で代用） | 無い | winget で入れる | 無料 |

## 1. RP2040 の C の開発環境

すでにあるもの:

- ARM 向けの GCC: `C:\Users\M\zmk\sdk\zephyr-sdk-0.17.0\arm-zephyr-eabi\bin\arm-zephyr-eabi-gcc.exe`（12.2.0）。XIAO の ZMK の作業で入ったもの。PATH には無い。
- cmake 4.4.2・ninja 1.13.2・git 2.55
- ZMK の作業場所に、Zephyr が使う RP2040 の部品（`hal_rpi_pico`）

方法は 2 つある。

**A. Zephyr のアプリとして作る（追加の導入なし。おすすめ）**

- ZMK の作業場所の Zephyr と SDK をそのまま使う（Zephyr は RP2040 の基板 `rpi_pico` に対応している）。
- 書き込みは fprobe の SWD でする（Shrike-Lite の USB の書き込みは使わない）。
- 費用: なし。ZMK の作業場所を壊さないよう、fprobe 用のアプリは別のフォルダに置く。

**B. pico-sdk を取ってくる（Raspberry Pi の標準の作り方）**

```
git clone -b master https://github.com/raspberrypi/pico-sdk.git C:\Users\M\pico-sdk
cd C:\Users\M\pico-sdk
git submodule update --init
setx PICO_SDK_PATH C:\Users\M\pico-sdk
```

- ダウンロードは数百 MB（submodule に TinyUSB 等）。
- コンパイラは、pico-sdk が前提とする `arm-none-eabi-gcc` が PATH に無い。次のどちらかにする。
  - Zephyr SDK の GCC（名前の頭が `arm-zephyr-eabi`）を pico-sdk に渡す。pico-sdk の設定で頭の名前を変えられるはずだが、
    **未確認**（試してから決める）。
  - Arm の公式の GCC を入れる。winget の名前は版で変わるので `winget search "Arm GNU"` で確かめてから入れる
    （例: `winget install Arm.GnuArmEmbeddedToolchain`）。公式サイト（developer.arm.com の Arm GNU Toolchain）の
    Windows 用のインストーラでもよい。

どちらの方法でも、Shrike の FPGA に回路を書き込む処理と、FPGA との SPI のやりとりを C で書き直す作業が要る
（今は MicroPython のライブラリがしている）。

## 2. pytest-xdist（テストの並列実行）

```
C:\Users\M\Desktop\DebugProbe-with-FPGA\.venv\Scripts\pip install pytest-xdist
```

- 作業用の環境（`.venv`）の中だけに入る。PC 全体には影響しない。
- 使い方: `pytest host -n auto`。並列にすると壊れるテスト（同じファイル・ポートを使う等）が無いかは、入れてから確かめる。
- 常用するなら `host/pyproject.toml` の `dev` に足す（リポジトリの変更になる）。

## 3. Pillow（画像の処理）

```
C:\Users\M\Desktop\DebugProbe-with-FPGA\.venv\Scripts\pip install pillow
```

- 作業用の環境の中だけ。撮った画面の一部を切り出して確かめる用（今日、時間軸の値の段を写せなかった）。

## 4. Quartus Prime Lite（Intel の FPGA の開発の道具）

- Intel の FPGA のダウンロードのページから「Quartus Prime Lite Edition」と、MAX10 の部品（device support）だけを選んで入れる。
  全部入りは非常に大きいので、MAX10 だけにする。
- 無料（Lite はライセンス不要）。ダウンロードに Intel のアカウントのログインを求められることがあるので、私からは入れられない。
- 入れる場所は既定（`C:\intelFPGA_lite\<版>`）でよい。fprobe の道具は場所を設定で受け取る。
- MAX10 の評価ボードが決まってからでよい。

## 5. 7-Zip

```
winget install 7zip.7zip
```

- 無くても今は困っていない（Python の部品で代用している）。優先度は低い。

## 入れたあとに知らせてほしいこと

- 1 の B・4: 入れた場所（既定と違えば）。
- 2・3 は私が入れてよい（作業用の環境の中だけで、PC 全体には影響しない）。指示があれば次の作業で入れる。
