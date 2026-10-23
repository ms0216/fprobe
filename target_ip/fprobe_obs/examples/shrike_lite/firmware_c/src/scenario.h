/* SPDX-License-Identifier: Apache-2.0
 * 題材（g_scenario）: MCU と FPGA の連携の不具合を実行中に切り替える（仕様 §4）。
 *   0 = 不具合なし
 *   1 = S1（MCU → FPGA）: 待ち回数が短く、計算中にリトライが来ると FPGA が途中の和を消さない（MODE bit0）
 *   2 = S2（FPGA → MCU）: FPGA がまれに A・B の書き込みを取りこぼす（MODE bit1。頻度は g_s2_rate = LFSR のビット数）
 *   3 = S3（タイミング）: タイマ割り込みが GPIO の出力を古い写しで丸ごと書き戻す（題材の印 GPIO27 を反転するつもりで、
 *       転送中なら CS も上げてしまう）。周期は g_s3_period_us（既定 99991 µs = 約 10 回/秒）
 *   4 = S4（1 回きり）: S1 に加えて、A = 0xFF でリトライが来ると FPGA が固まる（MODE bit2）。g_scenario = 0 と
 *       g_reset = 1 で戻す。頻度は g_poll_limit（大きいほどリトライが減ってまれになる） */
#ifndef SCENARIO_H
#define SCENARIO_H

#include <stdint.h>

/* 題材 s を演算器と割り込みに反映する（g_poll_limit は題材の既定に設定し直す。その後は PC から変えてよい） */
void scenario_apply(uint32_t s);

/* 題材の割り込みを止める（FPGA に回路を送る前に必ず。S3 の割り込みは CS を含む GPIO の出力を書き戻すので、
 * 動いたまま送ると回路のデータが乱れて FPGA が電源の入れ直しまで戻らなかった。2026-10-21） */
void scenario_quiesce(void);

/* 題材の印（GPIO27）を 1 回パルスにする（S4: タイムアウトを検知した瞬間） */
void scenario_cue(void);

#endif
