# PYNQ の上で make（測定の道具の送り手）
CFLAGS ?= -O2 -Wall -Wextra -Werror

zc_send: zc_send.c
	$(CC) $(CFLAGS) -o $@ $<

clean:
	rm -f zc_send
