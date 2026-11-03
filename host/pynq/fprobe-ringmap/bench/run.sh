#!/bin/sh
# 測定の道具（PC の Git Bash で動かす）: run.sh "<zc_send の引数>" ["<2 本目の zc_send の引数>"]
# PYNQ の /tmp/zcbench（zc_send・mon.py・top.py を置いて make した所）で送り手を sudo で起動し、送っている間 3〜9 秒の
# /proc の差（mon.py）とスレッドごとの CPU（top.py）を取る。PC は recv.py で受ける（1.7.6 の run2.sh と同じ流れ）。
# SPDX-License-Identifier: AGPL-3.0-only
D="$(cd "$(dirname "$0")" && pwd)"
K="${FPROBE_PYNQ_KEY:-$HOME/.ssh/fprobe_pynq}"
P="xilinx@${FPROBE_PYNQ_HOST:-<家の LAN のアドレス>}"
PY="${PY:-/c/Users/M/Desktop/DebugProbe-with-FPGA/.venv/Scripts/python.exe}"
SSH="ssh -i $K -o IdentitiesOnly=yes -o BatchMode=yes $P"
port() { echo "$1" | sed -n 's/.*-p \([0-9]*\).*/\1/p'; }
C="cd /tmp/zcbench; rm -f s1.txt s2.txt m.txt t.txt; ({ echo xilinx | sudo -S -p '' ./zc_send $1; } > s1.txt 2>&1 < /dev/null &);"
[ -n "$2" ] && C="$C ({ echo xilinx | sudo -S -p '' ./zc_send $2; } > s2.txt 2>&1 < /dev/null &);"
C="$C (python3 mon.py 3 6 > m.txt 2>&1 < /dev/null &); (python3 top.py 3 6 > t.txt 2>&1 < /dev/null &)"
$SSH "$C"
sleep 0.5
"$PY" "$D/recv.py" "${FPROBE_PYNQ_HOST:-<家の LAN のアドレス>}" $(port "$1") $([ -n "$2" ] && port "$2")
sleep 2.5
$SSH "cd /tmp/zcbench; cat s1.txt; [ -f s2.txt ] && cat s2.txt; cat m.txt; cat t.txt"
