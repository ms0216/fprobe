#!/bin/sh
# 使い方: relay_deploy.sh  （本拠地の中継を PYNQ で make して入れ替える。元の版は /usr/local/bin/fprobe-relay.orig に1度だけ残す）
set -e
K="-i <home>/.ssh/fprobe_pynq -o BatchMode=yes"; H=xilinx@<家の LAN のアドレス>
SRC=<home>/Desktop/DebugProbe-with-FPGA/host/pynq/fprobe-relay
ssh $K $H "rm -rf /tmp/relay-new && mkdir /tmp/relay-new"
scp $K $SRC/main.c $SRC/relay.c $SRC/relay.h $SRC/Makefile $H:/tmp/relay-new/ >/dev/null
ssh $K $H "cd /tmp/relay-new && make 2>&1 | tail -5"
echo xilinx | ssh $K $H "sudo -S sh -c '[ -e /usr/local/bin/fprobe-relay.orig ] || cp /usr/local/bin/fprobe-relay /usr/local/bin/fprobe-relay.orig; systemctl stop fprobe-relay; cp /tmp/relay-new/fprobe-relay /usr/local/bin/fprobe-relay; systemctl start fprobe-relay; sleep 2; systemctl is-active fprobe-relay' 2>&1 | grep -v assword"
