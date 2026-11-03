#!/bin/sh
# race.c → race_arm（ARM, PIE, -O0 -g。libc につながない: 試験は DWARF・行・番地だけを使う）
set -e
cd "$(dirname "$0")"
clang --target=armv7a-linux-gnueabihf -marm -O0 -g -fPIE -ffreestanding -nostdlib -fuse-ld=lld \
  -Wl,-pie -Wl,--no-dynamic-linker -Wl,-e,main -Wl,--build-id=none -fdebug-prefix-map="$(pwd)"=. -o race_arm race.c
