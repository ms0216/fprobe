#!/usr/bin/env python3
# 測定の道具（PYNQ で sudo で動かす）: ring_sha.py ずらす_B 総_B -> リング（/etc/fprobe/mem の番地）の [ずらす, ずらす + 総) の SHA-256。
# 読みは /dev/mem（O_SYNC = キャッシュを通らない、いまの中継と同じ）。zc_send -m ring -n 総 -o ずらす で送った中身と照合する。
# SPDX-License-Identifier: AGPL-3.0-only
import hashlib
import mmap
import os
import sys

shift, total = int(sys.argv[1], 0), int(sys.argv[2], 0)
base, size = (int(x, 0) for x in open("/etc/fprobe/mem").read().split()[:2])
assert shift + total <= size
fd = os.open("/dev/mem", os.O_RDONLY | os.O_SYNC)
m = mmap.mmap(fd, size, mmap.MAP_SHARED, mmap.PROT_READ, offset=base)
print("ring sha256", hashlib.sha256(m[shift:shift + total]).hexdigest())
