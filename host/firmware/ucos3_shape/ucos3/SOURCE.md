# µC/OS-III・µC/CPU・µC/LIB のヘッダ（取り込んだ部分）の出典

- 名前: µC/OS-III（uC-OS3）と、そのヘッダが含める µC/CPU（uC-CPU）・µC/LIB（uC-LIB）。元は Micrium（Silicon Labs）、
  いまは Weston Embedded Solutions が公開している
- 版と出典（raw.githubusercontent.com から個々のファイルを取得した。スクリプト等は取得も実行もしていない）:
  - uC-OS3 v3.08.02（`OS_VERSION` 30802）: https://github.com/weston-embedded/uC-OS3 のタグ `v3.08.02`
    （コミット `93d6e79023f1031480c20f479ca7662f3f1c3c3a`）
  - uC-CPU v1.32.01: https://github.com/weston-embedded/uC-CPU のタグ `v1.32.01`（コミット `fe140a6f4d65636b43b3a6a03c6f63b50dc2c5bc`）
  - uC-LIB v1.39.01: https://github.com/weston-embedded/uC-LIB のタグ `v1.39.01`（コミット `68e44026f0173399e2dbfa2ed873f3b261b3b815`）
- 取得日: 2026-10-18
- 著作権: Copyright 2009-2022 Silicon Laboratories Inc.（uC-OS3）/ Copyright 2004-2021 Silicon Laboratories Inc.
  （uC-CPU・uC-LIB）。各ファイルの先頭
- ライセンス: Apache License 2.0（SPDX-License-Identifier: APACHE-2.0。各ファイルの先頭）。全文は同じディレクトリの
  `LICENSE`、リポジトリの注意書きは `NOTICE`（3 つのリポジトリの LICENSE・NOTICE は同じ中身）
- 変更: なし（中身はそのまま。ファイル名は元のまま、ディレクトリだけを平らにした）。設定のテンプレート
  `Cfg/Template/os_cfg.h` は変えて使うので、ここには置かず `../os_cfg.h` に写して変えた（変えた所はその先頭に書いた）

| ファイル | 元の場所 | SHA-256（取得したもの） | 用途 |
|---|---|---|---|
| `os.h` | uC-OS3 `Source/os.h` | `88842476a6ad967b26ac70140ff4cc7f48026f7ae8df8a8295a5ca99e9b9cd47` | 制御ブロック（OS_TCB・OS_SEM・OS_MUTEX・OS_Q・OS_FLAG_GRP・OS_MEM・OS_TMR・OS_COND）・カーネルの変数・状態と種類の定数。試験用の ELF の DWARF の形の元 |
| `os_type.h` | uC-OS3 `Source/os_type.h` | `e031196d418c81eb73b2a7cfc52174464f456c1ddf0cf01cf875b7e2eb33b15d` | OS_TICK・OS_PRIO 等の型の大きさ |
| `os_trace.h` | uC-OS3 `Source/os_trace.h` | `b82c0f4cbb7f23003c31a9996fd53c0fc8b38753dc677c84437308a69768c10b` | `os.h` が含める（トレースの口。OS_CFG_TRACE_EN = 0） |
| `os_cfg_app.h` | uC-OS3 `Cfg/Template/os_cfg_app.h` | `e5b54413255159735e2d1afc8d3230ed93bc0eb2b4ab63ecc242d622c1d16ff6` | 設定のテンプレート（ティックの速さ・カーネルのタスクの優先度） |
| `os_cpu.h` | uC-OS3 `Ports/ARM-Cortex-M/ARMv7-M/GNU/os_cpu.h` | `364d6aa134200037cd4b0297bf7774c6e14d72b4fbf4be261f317d4acf9a4fe9` | ARMv7-M の GNU の port |
| `cpu.h` | uC-CPU `ARM-Cortex-M/ARMv7-M/GNU/cpu.h` | `f7296030d7fe4f2daacdbb76378edcc9cba0f9836e44cad0e1ad1d2bc0fc5eac` | CPU_STK・CPU_INT32U 等の型 |
| `cpu_def.h` | uC-CPU `cpu_def.h` | `13f71044c17eb7dec7203ce0d48c9ac63a6c1fd48b0daa9da142df483d3b5614` | 同 |
| `cpu_core.h` | uC-CPU `cpu_core.h` | `3c370267021760957bcce9b445522fdc6dc2a5a4d95b10b11e7ad9be94bcd48a` | CPU_TYPE_CREATE（オブジェクトの Type の 4 文字） |
| `cpu_cfg.h` | uC-CPU `Cfg/Template/cpu_cfg.h` | `bd8f3908f7442f6417cf84d6b420827f1808fb0ac258f68021469d6f5664513d` | 設定のテンプレート（CPU_CFG_NVIC_PRIO_BITS は `../build.py` が -D で渡す） |
| `lib_def.h` | uC-LIB `lib_def.h` | `0e60fd5607e99af741c2064ef8a4f4811a6d5d8f44f91f14620e5e1ba0598641` | DEF_ENABLED 等 |
| `LICENSE` | 各リポジトリの `LICENSE` | `cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30` | Apache License 2.0 の全文 |
| `NOTICE` | 各リポジトリの `NOTICE` | `cd43e2528645baa45da4bc36270629131dd7f3766748267a666ed0078dae6c0e` | Silicon Labs の注意書き |

リポジトリでは改行が LF に正規化されることがある（ハッシュは取得したままのファイルのもの）。

µC/OS-III のカーネルの実装（Source/*.c・Ports/*/os_cpu_a.S・os_cpu_c.c）は含まない。読み手
（`host/fprobe/rtos/ucos3.py`・`ucos3_objects.py`）が従うカーネルの振る舞いは、同じコミットの次のファイルを読んで確かめた
（取り込んではいない）:

- `Ports/ARM-Cortex-M/ARMv7-M/GNU/os_cpu_a.S`: PendSV が積む文脈（StkPtr → r4〜r11、EXC_RETURN、[s16〜s31]）
- `Ports/ARM-Cortex-M/ARMv6-M/GNU/os_cpu_a.s`: 同（r4〜r7、r8〜r11、EXC_RETURN。並びは ARMv7-M と同じ）
- `Ports/ARM-Cortex-M/ARMv7-M/os_cpu_c.c`: 新しいタスクの最初の文脈（LR は OS_TaskReturn、EXC_RETURN 0xFFFFFFFD）
- `Source/os_task.c`: OS_OPT_TASK_STK_CLR のスタックの 0 の塗り、OSTaskDbgListPtr への足し方（先頭に足す）
- `Source/os_tick.c`: OSTickList の差分の列（TickRemain は前からの差）
- `Source/os_tmr.c`: OSTmrListPtr の差分の列と OSTmrRemainGet、OSTmrTaskTickBase、Dly・Period は OS のティック
- `Source/os_core.c`: 待ちの列（優先度順）、`Source/os_dbg.c`: OSDbg_VersionNbr

試験用の ELF（`../ucos3_shape.elf`・`../ucos3_shape_nodbg.elf`）は、このヘッダの構造体と変数で「止めたときの様子」を
置いたもの（`../ucos3_shape.c`）。
