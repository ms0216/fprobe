#!/bin/sh
# 16 時以降に実行する。PYNQ (<家の LAN のアドレス>) の PL を、Linux の fpga_manager（zynq-fpga）で書く。
# 使い方: pynq_program.sh <fprobe_cap.bin（bit2bin.py で変換済み）> <design_id 16 桁 16 進>
set -e
BIN="$1"; ID="$2"
KEY=<home>/.ssh/fprobe_pynq; HOST=xilinx@<家の LAN のアドレス>
S="ssh -i $KEY -o BatchMode=yes -o ConnectTimeout=10 $HOST"
scp -i $KEY -o BatchMode=yes "$BIN" $HOST:/tmp/fprobe_cap.bin
$S "sudo -n /usr/local/bin/fprobe-pl before"                   # 中継を止め、「回路が載っている」印を消し、レベルシフタを無効にする
# パスワードは標準入力で渡す（リポジトリに書かない。評価ボードの値）
echo xilinx | $S "sudo -S sh -c 'cp /tmp/fprobe_cap.bin /lib/firmware/fprobe_cap.bin && echo 0 > /sys/class/fpga_manager/fpga0/flags && echo fprobe_cap.bin > /sys/class/fpga_manager/fpga0/firmware; echo write-exit=\$?'"
$S "cat /sys/class/fpga_manager/fpga0/state; dmesg | tail -5"
$S "sudo -n /usr/local/bin/fprobe-pl after $ID"                 # 印を置き、中継を起動
sleep 5
$S "systemctl is-active fprobe-relay; cat /run/fprobe/pl-design"
