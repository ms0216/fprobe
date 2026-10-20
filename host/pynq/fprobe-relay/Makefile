# PYNQ の上で make（gcc）。据え付けは python -m fprobe_gw pynq install
CFLAGS ?= -O2 -Wall -Wextra -Werror

fprobe-relay: main.c relay.c relay.h
	$(CC) $(CFLAGS) -o $@ main.c relay.c

clean:
	rm -f fprobe-relay
