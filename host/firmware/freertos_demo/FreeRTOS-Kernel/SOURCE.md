# FreeRTOS カーネル（取り込んだ部分）の出典

- 名前: FreeRTOS-Kernel
- 版: V11.1.0（タグ `V11.1.0`）
- 出典: https://github.com/FreeRTOS/FreeRTOS-Kernel （アーカイブ https://codeload.github.com/FreeRTOS/FreeRTOS-Kernel/tar.gz/refs/tags/V11.1.0）
- 取得日: 2026-10-17
- アーカイブの SHA-256: `0e21928b3bcc4f9bcaf7333fb1c8c0299d97e2ec9e13e3faa2c5a7ac8a3bc573`
- 著作権: Copyright (C) 2021 Amazon.com, Inc. or its affiliates. All Rights Reserved.（各ファイルの先頭）
- ライセンス: MIT（SPDX-License-Identifier: MIT）。全文は同じディレクトリの `LICENSE.md`（下にも写す）。
- 変更: なし（ファイルはそのまま）。設定は `../FreeRTOSConfig.h`、トレースフックは `../fprobe_trace.h`。

取り込んだファイル（fprobe の試験用ファーム `freertos_demo` のビルドに要るものだけ）:

| ファイル | 用途 |
|---|---|
| `tasks.c`・`list.c`・`queue.c` | カーネル（タスク・リスト・キュー / セマフォ / ミューテックス） |
| `timers.c`・`event_groups.c`・`stream_buffer.c` | ソフトウェアタイマ・イベントグループ・ストリーム / メッセージバッファ（2026-10-18 に同じアーカイブから追加） |
| `include/*.h` | カーネルのヘッダ（すべて） |
| `portable/GCC/ARM_CM4F/port.c`・`portmacro.h` | Cortex-M4F の port（GCC の構文。zig cc（clang）で通る） |
| `portable/MemMang/heap_4.c` | ヒープ |
| `LICENSE.md` | ライセンス |

## ライセンスの全文（LICENSE.md）

```
MIT License

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
