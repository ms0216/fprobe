// SPDX-License-Identifier: Apache-2.0
// fprobe のモニタ方式（止めずにデバッグする。J-Link の Monitor Mode Debugging の相当）のターゲット側。
// 仕様: docs/superpowers/specs/2026-10-18-u37-monitor-mode-design.md
//
// 使い方（Cortex-M3 / M4 / M7 / M33 等。ARMv6-M（M0 / M0+）には DebugMonitor が無いので使えない）:
//   1. fprobe_monitor.c をビルドに加える。
//   2. ベクタ表の 12 番（DebugMonitor）に DebugMon_Handler を置く（CMSIS のスタートアップは同じ名前の弱い定義を持つ
//      ので、加えるだけで置き換わる）。
//   3. 起動時に fprobe_monitor_init(優先度) を呼ぶ。優先度より高い（数の小さい）割り込みは、止まっている間も動く。
//   4. ホストで target.debug_mode.set(mode="monitor")。
//
// 止まっている間は DebugMon の中で待つ。ウォッチドッグは fprobe_monitor_on_poll() で蹴る（弱い定義を上書きする）。
//
// スタック: モニタは自分のスタック（FPROBE_MONITOR_STACK_SIZE バイト）で動く。割り込まれた処理のスタックの下を空けて
// おくため（ホストが SP より下に関数の呼び出しの引数を置ける。停止方式と同じ）。止まっている間に動く優先度の高い割り込み
// もこのスタックを使うので、その深さの分も足す。溢れは底の印で見つけ、箱の flags（bit1）でホストに知らせる。
#ifndef FPROBE_MONITOR_H
#define FPROBE_MONITOR_H

#include <stdint.h>

#define FPROBE_MONITOR_MAGIC_WORD 0x4E4D5046  // "FPMN"（箱の先頭とハンドラの頭の印。アセンブラ用に接尾辞なし）
#define FPROBE_MONITOR_MAGIC 0x4E4D5046u
#define FPROBE_MONITOR_VERSION 1u

#ifndef FPROBE_MONITOR_PRIORITY
#define FPROBE_MONITOR_PRIORITY 0xFFu         // 既定の DebugMon の優先度（最も低い。実装のビットに丸められる）
#endif

#ifndef FPROBE_MONITOR_STACK_SIZE
#define FPROBE_MONITOR_STACK_SIZE 1024        // モニタのスタック（8 の倍数）
#endif

// 命令（ホスト → モニタ）
#define FPROBE_MONITOR_CMD_GO 1u
#define FPROBE_MONITOR_CMD_STEP 2u
#define FPROBE_MONITOR_STEP_MASK_INTERRUPTS 1u

// flags
#define FPROBE_MONITOR_FLAG_FPU 1u            // s16〜s31 を保存する版（__ARM_FP で作った）
#define FPROBE_MONITOR_FLAG_STACK_OVERFLOW 2u // モニタのスタックの底の印が壊れた

// ホストとやりとりする箱。ホストは SWD でこの箱を直接読み書きする（割り込まれた処理のレジスタはすべてここにある）。
// 配置は版 1 で固定（下の _Static_assert と、ホストの fprobe.monitor の定数を、試験が DWARF で照合する）。
struct fprobe_monitor {
    uint32_t magic;               // 0x00 init が最後に書く
    uint32_t version;             // 0x04
    uint32_t size;                // 0x08
    volatile uint32_t flags;      // 0x0C FPROBE_MONITOR_FLAG_*
    volatile uint32_t state;      // 0x10 0 = 走っている、1 = 止まっている
    volatile uint32_t reason;     // 0x14 入口の DFSR（0 = ホストの停止の要求）
    volatile uint32_t stops;      // 0x18 入った回数
    volatile uint32_t seq_cmd;    // 0x1C ホスト: 命令を出すたびに増やす（cmd・arg を書いた後）
    volatile uint32_t cmd;        // 0x20 ホスト
    volatile uint32_t arg;        // 0x24 ホスト
    volatile uint32_t seq_ack;    // 0x28 処理した seq_cmd
    volatile uint32_t result;     // 0x2C 0 = 受理、1 = 知らない命令
    uint32_t exc_return;          // 0x30 入口の LR（bit2 = PSP の枠、bit4 = 0 なら FP 付きの枠）
    uint32_t msp;                 // 0x34 入口の MSP（MSP の枠ならハードが積んだ枠の番地）
    uint32_t psp;                 // 0x38 入口の PSP
    volatile uint32_t primask;    // 0x3C 出口でこの値に戻す（ホストが書ける）
    volatile uint32_t basepri;    // 0x40 同
    uint32_t faultmask;           // 0x44
    uint32_t control;             // 0x48 nPRIV だけ意味がある
    volatile uint32_t sp;         // 0x4C 割り込まれた処理の SP（ホストが書ける。出口で枠をその下に積み直す）
    volatile uint32_t frame[26];  // 0x50 例外の枠の写し: r0 r1 r2 r3 r12 lr pc xpsr [s0〜s15 fpscr 予約]
    volatile uint32_t r4_r11[8];  // 0xB8 出口で書き戻す
    volatile uint32_t s16_s31[16];// 0xD8 FP 付きの枠のときだけ
    uint32_t exit_frame;          // 0x118 出口で積み直した枠の番地
    uint32_t step_basepri;        // 0x11C 割り込みを止めるステップの前の BASEPRI
    uint32_t step_forced;         // 0x120 そのステップで書いた BASEPRI（0 = していない）
    uint32_t prio_bits;           // 0x124 実装された優先度のビット（例 0xF0。init が測る）
    uint32_t stack_size;          // 0x128 モニタのスタックの大きさ（バイト）
};

// アセンブラ（DebugMon_Handler）が使う位置
#define FPROBE_MONITOR_OFF_EXC_RETURN 0x30
#define FPROBE_MONITOR_OFF_MSP 0x34
#define FPROBE_MONITOR_OFF_PSP 0x38
#define FPROBE_MONITOR_OFF_R4 0xB8
#define FPROBE_MONITOR_OFF_S16 0xD8
#define FPROBE_MONITOR_OFF_EXIT_FRAME 0x118
#define FPROBE_MONITOR_SIZE 0x12C

extern struct fprobe_monitor fprobe_monitor;

// DebugMon の優先度を設定し、箱を有効にする（magic を書く）。ホストはこの後でモニタ方式にできる。
void fprobe_monitor_init(uint8_t priority);

// 呼び返し（弱い定義。既定は何もしない）。DebugMon の優先度・モニタのスタックで呼ばれる。
void fprobe_monitor_on_enter(void);   // 止まった（例: モーターを安全な出力に）
void fprobe_monitor_on_exit(void);    // 再開・1 命令の前
void fprobe_monitor_on_poll(void);    // 止まっている間、繰り返し（例: ウォッチドッグを蹴る）

// ベクタ 12（DebugMonitor）に置くハンドラの名前。CMSIS のスタートアップは DebugMon_Handler、Zephyr は
// z_arm_debug_monitor（zephyr/ のモジュールが -DFPROBE_MONITOR_HANDLER=z_arm_debug_monitor で作る）。
#ifndef FPROBE_MONITOR_HANDLER
#define FPROBE_MONITOR_HANDLER DebugMon_Handler
#endif
void FPROBE_MONITOR_HANDLER(void);

#endif
