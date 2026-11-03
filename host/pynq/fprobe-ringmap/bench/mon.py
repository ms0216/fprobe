#!/usr/bin/env python3
# 測定の道具（PYNQ で動かす。1.7.6 の b1_mon.py と同じ）: mon.py 待つ秒 窓の秒 -> 窓の間の /proc の差
# （CPU ごとの割合・NET の softirq・eth0 の割り込み・送ったバイトとパケット・再送）
import sys, time

def stat():
    d = {}
    for line in open('/proc/stat'):
        if line.startswith('cpu') and line[3] != ' ':
            f = line.split(); d[f[0]] = list(map(int, f[1:8]))
    return d

def softirq():
    d = {}
    for line in open('/proc/softirqs'):
        f = line.split()
        if f[0] in ('NET_TX:', 'NET_RX:'):
            d[f[0][:-1]] = list(map(int, f[1:3]))
    return d

def irq():
    for line in open('/proc/interrupts'):
        if 'eth0' in line:
            return list(map(int, line.split()[1:3]))

def snmp():
    lines = [l.split() for l in open('/proc/net/snmp') if l.startswith('Tcp:')]
    return dict(zip(lines[0][1:], map(int, lines[1][1:])))

def net(k):
    return int(open('/sys/class/net/eth0/statistics/' + k).read())

def snap():
    return stat(), softirq(), irq(), snmp(), net('tx_bytes'), net('tx_packets'), net('rx_packets'), time.time()

delay, win = float(sys.argv[1]), float(sys.argv[2])
time.sleep(delay)
a = snap(); time.sleep(win); b = snap()
dt = b[7] - a[7]
for c in ('cpu0', 'cpu1'):
    x = [q - p for p, q in zip(a[0][c], b[0][c])]
    tot = sum(x)
    # user nice system idle iowait irq softirq
    print(c, 'user %.1f sys %.1f irq %.1f softirq %.1f idle %.1f' % tuple(100.0 * x[i] / tot for i in (0, 2, 5, 6, 3)))
for k in ('NET_TX', 'NET_RX'):
    print(k, [ (q - p) / dt for p, q in zip(a[1][k], b[1][k])])
print('eth0 irq/s', [(q - p) / dt for p, q in zip(a[2], b[2])])
txb = (b[4] - a[4]) / dt; txp = (b[5] - a[5]) / dt; rxp = (b[6] - a[6]) / dt
print('tx MB/s(wire bytes) %.2f  tx pkt/s %.0f  rx pkt/s %.0f  retrans/s %.1f' % (txb / 1e6, txp, rxp, (b[3]['RetransSegs'] - a[3]['RetransSegs']) / dt))
