"""機器なしで共通タイムライン（G01）のすべての段の型を見せるデモの記録（fake_daemon.py --demo / --demo-recording）。

2 秒間（タイムベース 10 MHz）の、FreeRTOS 風のファームウェアの記録を作る:

- 波形 8 ch（UART の TX / RX の線・SWD の時計とデータ・LED・センサの割り込み・I2C の SCL / SDA）。かたまり 2 つ（間に隙間）
- rtos: タスク 5 つの実行（区間）・割り込み 4 つ（SysTick が USART2 の中に入れ子）・待ち・切り替え・カーネルの出来事
- uart: TX（ログの行）・RX（コマンド）のバイト。annotation: UART と I2C の解読の箱、印、データの欠け
- sample（変数 2 つ）・aux（電圧。途中で下がる）・user（値 2 つ・区間の入れ子・ボタンの出来事）: 値のグラフ
- host（AI と人の操作）・target（RUNNING / HALTED。ブレークポイントで止まる）・dap（AP / DP の転送）

タスクの名前と TCB は fake_demo.TASKS と同じ（RTOS のパネルと揃う）。決まった乱数で、毎回同じ記録になる。
"""

from __future__ import annotations

import math
import random
from array import array
from pathlib import Path

from fprobe.la import Transitions
from fprobe.recording import Recorder

HZ = 10_000_000                    # タイムベース（100 ns）
SPAN = 2 * HZ                      # 2 秒
MS = HZ // 1000
US = HZ // 1_000_000
BIT = HZ // 115_200                # UART の 1 ビット（約 8.7 µs）
TASKS = [("main", 0x20002000), ("sensor", 0x20002060), ("comm", 0x200020C0), ("logger", 0x20002120),
         ("IDLE", 0x20002180)]
ISRS = {15: "SysTick", 22: "EXTI0", 54: "USART2", 33: "DMA1_Stream6"}
CHANNELS = ["UART_TX", "UART_RX", "SWCLK", "SWDIO", "LED", "SENSOR_IRQ", "I2C_SCL", "I2C_SDA"]
COUNTER, SENSOR = 0x20000010, 0x20000014
HALTS = [(int(0.80 * HZ), int(0.86 * HZ), "breakpoint"), (int(1.92 * HZ), None, "breakpoint")]


class _Wave:
    """チャンネルごとの変化（サンプル番号の集合）を集めて Transitions にする（1 サンプル = 1 タイムベース）。"""

    def __init__(self):
        self.toggles: list[tuple[int, int]] = []

    def toggle(self, t: int, ch: int) -> None:
        self.toggles.append((t, ch))

    def level(self, t0: int, t1: int, ch: int) -> None:
        """[t0, t1) の間だけ High（前後は Low）。"""
        self.toggle(t0, ch)
        self.toggle(t1, ch)

    def piece(self, start: int, end: int, idle: int) -> Transitions:
        values, starts, v = array("H", [idle]), array("Q", [0]), idle
        for t, ch in sorted(x for x in self.toggles if start <= x[0] < end):
            v ^= 1 << ch
            if starts[-1] == t - start:
                values[-1] = v
            else:
                values.append(v)
                starts.append(t - start)
        return Transitions(values, starts, end - start, 0, 0, 1 / HZ, len(CHANNELS))


def _uart_bits(wave: _Wave, ch: int, t: int, byte: int) -> int:
    """8N1 のバイトを線に（アイドル High: 変化 = 反転）。終わりの時刻を返す。"""
    bits = [0] + [(byte >> i) & 1 for i in range(8)] + [1]
    level = 1
    for i, b in enumerate(bits):
        if b != level:
            wave.toggle(t + i * BIT, ch)
            level = b
    return t + 10 * BIT


def write_demo_recording(directory: Path) -> None:
    rng = random.Random(20261018)
    rec = Recorder(directory, HZ, meta={"name": "demo", "channel_names": CHANNELS})
    wave = _Wave()
    rows: list[tuple] = []

    def add(stream, kind, t, a=0, b=0, body=None, t_end=None, unc=None):
        rows.append((stream, kind, int(t), a, b, body, None if t_end is None else int(t_end), unc))

    halted = lambda t: any(h0 <= t < (h1 if h1 is not None else SPAN) for h0, h1, _ in HALTS)   # noqa: E731

    # --- RTOS: 1 ms ごとの SysTick と、タスクの実行（区間）・切り替え・待ち ---------------------------------
    previous = None
    for tick in range(SPAN // MS):
        t = tick * MS
        if halted(t):
            continue
        st = t + rng.randrange(0, 3 * US)
        add("rtos", "isr", st, 15, 0, {"name": "SysTick"}, st + 3 * US + rng.randrange(0, 2 * US))
        cursor = st + 6 * US
        plan = [("main", rng.randrange(80, 200)), ("sensor", 60 if tick % 10 == 0 else 0),
                ("comm", rng.randrange(20, 120) if tick % 3 == 0 else 0), ("logger", 150 if tick % 100 == 0 else 0)]
        for name, us in plan + [("IDLE", 0)]:
            if name != "IDLE" and not us:
                continue
            handle = dict(TASKS)[name]
            end = t + MS if name == "IDLE" else cursor + us * US
            if previous != handle:
                add("rtos", "switch", cursor, previous or 0, handle,
                    {"from": next((n for n, h in TASKS if h == previous), None), "to": name})
                previous = handle
            add("rtos", "run", cursor, handle, 0, {"task": name, "method": "trace", "resolution": 1}, end)
            if name == "comm" and rng.random() < 0.5:
                add("rtos", "queue_send", cursor + 10 * US, 0x20003000, handle, {"object": "log_queue", "task": name})
            if name == "logger":
                add("rtos", "queue_receive", cursor + 5 * US, 0x20003000, handle, {"object": "log_queue", "task": name})
            cursor = end
        if tick % 10 == 0:                                 # sensor は 10 ms ごとに起き、次まで delay で待つ
            add("rtos", "blocked", t + 300 * US, TASKS[1][1], 0, {"task": "sensor", "on": "delay", "outcome": "timeout"},
                t + 10 * MS)
        if tick % 3 == 1:
            add("rtos", "blocked", t + 200 * US, TASKS[2][1], 0x20003040,
                {"task": "comm", "on": "rx_queue", "outcome": "received"}, t + 2 * MS + 100 * US)
        if tick % 10 == 5:                                  # センサの割り込み（EXTI0）と I2C の読み
            e = t + 400 * US
            wave.level(e, e + 20 * US, 5)
            add("rtos", "isr", e + 2 * US, 22, 0, {"name": "EXTI0"}, e + 9 * US)
            _i2c_read(wave, add, e + 30 * US, rng)
    add("rtos", "inversion", int(0.4 * HZ), 0, 0, {"high": "sensor", "low": "logger", "blocker": "log_mutex"},
        int(0.4 * HZ) + 420 * US)
    add("rtos", "gap", int(1.2 * HZ), 0, 0, {"reason": "trace buffer overflow"}, int(1.4 * HZ))

    # --- UART: logger のログ（TX、100 ms ごと）とコマンド（RX）。USART2・DMA の割り込み、解読の箱 --------------
    counter = 0
    for k in range(1, 20):
        t = k * 100 * MS + 50 * US
        if halted(t):
            continue
        line = f"[{k / 10:5.1f}] tick counter={counter + k * 100} sensor={int(500 + 400 * math.sin(k / 3))}\r\n"
        add("rtos", "isr", t - 4 * US, 33, 0, {"name": "DMA1_Stream6"}, t - 1 * US)
        for ch in line.encode():
            end = _uart_bits(wave, 0, t, ch)
            add("uart", "tx", t, 0, ch, None, end)
            add("annotation", "uart.data", t, ch, 0, {"protocol": "uart", "text": _text(ch), "value": ch,
                                                     "error": None, "fields": None}, end)
            t = end + BIT // 2
    for at, cmd in ((int(0.5 * HZ), "status\r\n"), (int(1.5 * HZ), "dump 0x20000010\r\n"), (int(1.62 * HZ), "rst?\r")):
        t = at
        for i, ch in enumerate(cmd.encode()):
            end = _uart_bits(wave, 1, t, ch)
            bad = cmd.startswith("rst") and i == 2               # 枠の誤り（解読の誤りの赤）
            add("uart", "rx", t, 0, ch, None, end)
            add("annotation", "uart.data", t, ch, 0, {"protocol": "uart", "text": "framing error" if bad else _text(ch),
                                                     "value": ch, "error": "framing" if bad else None,
                                                     "fields": None}, end)
            isr = end + 1 * US
            add("rtos", "isr", isr, 54, 0, {"name": "USART2"}, isr + 4 * US)
            t = end + BIT
    # SysTick が USART2 の中に入る（入れ子）: USART2 を SysTick の直前に始めて長くする
    for k in range(40):
        t = (25 + 47 * k) * MS
        if halted(t):
            continue
        add("rtos", "isr", t - 2 * US, 54, 0, {"name": "USART2"}, t + 12 * US)

    # --- 変数のサンプル（sample）・電圧（aux）・ユーザの値と区間（user） ---------------------------------------
    for k in range(SPAN // MS):
        t = k * MS + 500 * US
        if halted(t):
            continue
        counter_value = k * 4 + (k // 256) * 3
        add("sample", "word", t, COUNTER, counter_value & 0xFFFF_FFFF)
        add("sample", "word", t + 2 * US, SENSOR, int(2048 + 1500 * math.sin(k / 70) + rng.gauss(0, 60)))
        if k % 5 == 0:
            temp = 24.0 + 3 * math.sin(k / 400) + rng.gauss(0, 0.05)
            add("user", "value", t + 10 * US, 1, int(temp), {"name": "temperature", "value": round(temp, 3),
                                                           "source": "rtt", "index": 1})
            depth = max(0, int(4 + 4 * math.sin(k / 37) + rng.randrange(-1, 2)))
            add("user", "value", t + 12 * US, 2, depth, {"name": "queue_depth", "value": depth, "source": "rtt",
                                                       "index": 2})
        if k % 10 == 5:
            s = k * MS + 450 * US
            add("user", "interval", s, 0, 0, {"name": "process_sample", "duration": 180 * US, "depth": 0}, s + 180 * US)
            add("user", "interval", s + 40 * US, 0, 0, {"name": "filter", "duration": 90 * US, "depth": 1},
                s + 130 * US)
    for k in range(200):
        t = k * 10 * MS + 3 * MS
        dip = 350 * math.exp(-((t - 1.3 * HZ) / (0.03 * HZ)) ** 2)
        add("aux", "mv", t, 0, int(3300 - dip + rng.gauss(0, 6)))
    for t in (int(0.33 * HZ), int(1.07 * HZ), int(1.71 * HZ)):
        add("user", "event", t, 0, 0, {"name": "button", "source": "rtt"})

    # --- ターゲットの状態・操作（AI と人）・印・DAP の転送 -------------------------------------------------
    add("target", "state", 0, body={"state": "RUNNING"})
    pc = 0x08000188
    for h0, h1, reason in HALTS:
        add("target", "state", h0, body={"state": "HALTED", "reason": reason, "pc": pc})
        add("host", "call", h0 - 200 * US, body={"client": "claude", "role": "ai", "method": "debug.breakpoint.set",
                                                "params": {"location": "sram_demo.c:19"}})
        for j, (method, role) in enumerate((("target.status", "ai"), ("memory.read", "ai"), ("debug.backtrace", "human"),
                                            ("variable.read", "ai"))):
            add("host", "call", h0 + (3 + 4 * j) * MS, body={"client": "claude" if role == "ai" else "ui",
                                                           "role": role, "method": method})
            _dap_burst(wave, add, h0 + (3 + 4 * j) * MS + 100 * US, rng, 24)
        if h1 is not None:
            add("host", "call", h1 - 50 * US, body={"client": "ui", "role": "human", "method": "target.resume"})
            add("target", "state", h1, body={"state": "RUNNING"})
    add("host", "call", int(1.25 * HZ), body={"client": "claude", "role": "ai", "method": "memory.read",
                                              "error": "target running"})
    for k in range(60):                                       # 走行中のサンプラの読み（DAP）
        t = int((0.05 + k * 0.03) * HZ)
        if not halted(t):
            _dap_burst(wave, add, t, rng, 4)
    add("annotation", "mark", int(0.05 * HZ), body={"text": "boot complete"})
    add("annotation", "mark", int(0.795 * HZ), body={"text": "AI: counter jumps by 3 here?"})
    add("annotation", "mark", int(1.3 * HZ), body={"text": "Vtarget dip"})
    add("annotation", "events_lost", int(1.21 * HZ), 37)
    for k in range(SPAN // (250 * MS)):                       # LED は 250 ms ごとに反転
        wave.toggle(k * 250 * MS + 1, 4)

    rec.add_many(rows)
    rec.add_waveform(wave.piece(0, int(1.2 * HZ), 0b11), 0)
    rec.add_waveform(wave.piece(int(1.4 * HZ), SPAN, 0b11), int(1.4 * HZ))
    rec.close()


def _text(ch: int) -> str:
    return f"'{chr(ch)}'" if 0x20 <= ch < 0x7F else f"0x{ch:02X}"


def _i2c_read(wave: _Wave, add, t: int, rng: random.Random) -> None:
    """センサ（0x48）の 2 バイトの読み: 線（SCL = ch6, SDA = ch7。アイドル Low の表現のまま反転だけ）と解読の箱。"""
    half = 25 * US // 10                                     # 400 kHz の半周期（2.5 µs）
    value = 0x0C00 + rng.randrange(0, 0x100)
    parts = [("start", "S", 1), ("address", "0x48 R", 9), ("data", f"0x{value >> 8:02X}", 9),
             ("data", f"0x{value & 0xFF:02X}", 9), ("stop", "P", 1)]
    for kind, text, bits in parts:
        end = t + bits * 2 * half
        for i in range(bits):
            wave.toggle(t + 2 * i * half, 6)
            wave.toggle(t + (2 * i + 1) * half, 6)
            if rng.random() < 0.5:
                wave.toggle(t + 2 * i * half + half // 2, 7)
        add("annotation", f"i2c.{kind}", t, 0, 0, {"protocol": "i2c", "text": text, "value": None, "error": None,
                                                   "fields": None}, end)
        t = end


def _dap_burst(wave: _Wave, add, t: int, rng: random.Random, n: int) -> None:
    """DAP の転送 n 個（SWCLK = ch2、SWDIO = ch3 の線と、dap の出来事）。"""
    clock = 2 * US // 10                                     # 2.5 MHz の SWD
    for i in range(n):
        kind = rng.choice(["ap_read", "ap_read", "ap_write", "dp_read"])
        address = rng.choice([COUNTER, SENSOR, 0xE000EDF0, 0x40020014]) if kind.startswith("ap") else 0x4
        ack = 1 if rng.random() > 0.02 else 2
        add("dap", kind, t, address, rng.randrange(0, 1 << 32), {"ack": ack})
        for k in range(46):
            wave.toggle(t + k * clock, 2)
            wave.toggle(t + k * clock + clock // 2, 2)
            if rng.random() < 0.5:
                wave.toggle(t + k * clock + clock // 4, 3)
        t += 46 * clock + 2 * US
