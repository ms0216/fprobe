# CMSIS-RTOS2 RTX5 のヘッダ（取り込んだ部分）の出典

- 名前: CMSIS-RTOS2 RTX（Arm CMSIS 5 の RTX5）
- 版: RTX V5.5.0（`osRtxVersionKernel` 50050000）、CMSIS-RTOS2 API 2.1.3
- 出典: Zephyr の TF-M の試験のモジュール https://github.com/zephyrproject-rtos/tf-m-tests
  （コミット `502ea90105ee18f20c78f710e2ba2ded0fc0756e`）の `lib/ext/CMSIS/RTOS2/`。元は https://github.com/ARM-software/CMSIS_5
- 取得日: 2026-10-18（この PC の Zephyr のワークスペース `~/zmk/ws/modules/tee/tf-m/tf-m-tests` から写した）
- SHA-256（写した元のファイル。リポジトリでは改行が LF に正規化される）:
  - `rtx_os.h`: `2753805c5f67f35c95876020224da76333ec2713b3322391e1e746f8cd64f1d4`
  - `cmsis_os2.h`: `9582815aaec33b2987491ab70e4469c0e528ccc75fb51730a4ae09693cf6dac0`
- 著作権: Copyright (c) 2013-2019 Arm Limited. All rights reserved.（各ファイルの先頭）
- ライセンス: Apache License 2.0（SPDX-License-Identifier: Apache-2.0。各ファイルの先頭）。全文は
  https://www.apache.org/licenses/LICENSE-2.0
- 変更: なし（中身はそのまま）。

| ファイル | 用途 |
|---|---|
| `rtx_os.h` | RTX5 の制御ブロック（osRtxThread_t・osRtxInfo_t・各オブジェクト）の定義。試験用の ELF の DWARF の形の元 |
| `cmsis_os2.h` | `rtx_os.h` が含める CMSIS-RTOS2 の API の型（osPriority_t・osThreadState_t 等） |

RTX5 のカーネルの実装（rtx_*.c・irq_*.S）は含まない。試験用の ELF（`../rtx5_shape.elf`）は、このヘッダの構造体で
「止めたときの様子」を置いたもので、リストの繋ぎ方は RTX5 の rtx_thread.c に合わせて書いた（`../rtx5_shape.c`）。
