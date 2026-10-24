# 同梱の SVD（周辺レジスタの定義）

`fprobe.svd.find_svd` / `load_device` がデバイス名から引く SVD の置き場所の 1 つ（同梱）。

## 置き場所の決まり（探す順）

1. 明示の場所（`load_device(..., paths=[...])`、ファイルかディレクトリ）
2. 環境変数 `FPROBE_SVD_PATH`（`os.pathsep` 区切りのディレクトリ）
3. `~/.fprobe/svd`（利用者が置く）
4. ここ（`fprobe/data/svd`、同梱）
5. pyOCD の内蔵（`pyocd/debug/svd/svd_data.zip`、pyOCD が入っていれば。Apache-2.0）

各場所で、ファイル名（拡張子 `.svd` / `.xml` を除く）が渡した名前と同じものを選ぶ（大文字小文字は区別しない）。
部品名の先頭に合う・`x` を任意の 1 文字とみなす等の名前の慣習では選ばない（使う側が SVD のファイル名を渡す）。
例: `STM32F401` → `STM32F401.svd`。

## 同梱のファイル

| ファイル | 内容 | 出典 | 版 | ライセンス |
|---|---|---|---|---|
| `STM32F401.svd` | STM32F401（Nucleo F401RE） | STMicroelectronics の配布物（STM32F4 の SVD 一式 v2.0 の中の STM32F401.svd、ファイルの版 1.8）。入手は https://github.com/modm-io/cmsis-svd-stm32 の `stm32f4/STM32F401.svd`（ST の配布物の改行と行末の空白だけ変えた写し）。2026-10-17 に取得。SHA-256 `52cdce64199c959dd4484c455b8f8f1fb1c2eae70eb7984de56a1f5ec06dab73` | 1.8 | Apache-2.0（Copyright (c) 2024 STMicroelectronics。ファイルの先頭に記載） |

`LICENSE-Apache-2.0.txt` は Apache License 2.0 の全文（同梱の SVD のライセンス）。ファイルは変更せずに置く。
