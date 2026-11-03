# PYNQ の上で make（gcc）。据え付けは python -m fprobe_gw pynq install
CFLAGS ?= -O2 -Wall -Wextra -Werror -mfpu=neon     # 実機は ARMv7（Cortex-A9）。DDR の読みに NEON を使う（relay.c の copy_from_device）

fprobe-relay: main.c relay.c relay.h window_regs.h
	$(CC) $(CFLAGS) -pthread -o $@ main.c relay.c

clean:
	rm -f fprobe-relay
