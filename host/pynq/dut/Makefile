# PYNQ の上で: make（ARM 命令でビルドする。uprobe の将来の制約を避ける）
dut_app: dut_app.c dut_regs.h
	$(CC) -O2 -marm -Wall -Wextra -o $@ dut_app.c

clean:
	rm -f dut_app
