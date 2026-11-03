#!/bin/bash
# モジュールのビルドの gcc（build.sh が PATH の先頭に「gcc」の名前で置く）。PYNQ のカーネル 6.6.10-xilinx は GCC 12.2 で
# ビルドされ、kbuild はその構成から GCC 12 の次の 3 つの指定を足すが、PYNQ の上の gcc は 11.2 で知らないので落ちる
# （2026-11-02 に確かめた）。それだけを外して /usr/bin/gcc に渡す:
#   -ftrivial-auto-var-init=zero        スタックの変数の自動のゼロ埋め（CONFIG_INIT_STACK_ALL_ZERO）。このモジュールは
#                                       使う前に全部代入するので、外しても振る舞いは変わらない
#   -mstack-protector-guard=tls, -mstack-protector-guard-offset=N
#                                       タスクごとのスタックの番人（CONFIG_STACKPROTECTOR_PER_TASK）。外すと番人を大域の
#                                       __stack_chk_guard から読む命令になり、カーネルはそれを出していないので、
#                                       代わりに -fno-stack-protector で番人の検査そのものを入れない
# カーネルの構成は GCC_PLUGINS なし・RANDSTRUCT_NONE。構造体の並びが同じことは build.sh が asm-offsets.h の一致で確かめる。
# SPDX-License-Identifier: GPL-2.0-only
args=()
for a in "$@"; do
	case "$a" in
	-ftrivial-auto-var-init=zero | -mstack-protector-guard=tls | -mstack-protector-guard-offset=*) ;;
	*) args+=("$a") ;;
	esac
done
exec /usr/bin/gcc "${args[@]}" -fno-stack-protector
