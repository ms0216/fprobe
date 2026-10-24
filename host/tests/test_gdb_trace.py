"""GDB のトレースポイント（U48）の機器に依らない部分: エージェント式（fprobe.gdb.agent）と、トレースの実験の状態
（fprobe.gdb.trace: QTDP 等の読み取り・当たったときの収集・バッファ・フレームの検索・qTStatus・アップロード）。"""

import pytest

from fprobe.gdb import trace
from fprobe.gdb.agent import AgentError, evaluate

RAM = 0x2000_0000


class Target:
    """当たった場所のコアとメモリ（偽物）。レジスタは GDB の番号。"""

    def __init__(self, registers=None, memory=None):
        self.registers = registers or {}
        self.memory = dict(memory or {})
        self.steps = 0

    def read_register(self, number):
        return self.registers.get(number)

    def read_memory(self, address, length):
        out = bytearray()
        for a in range(address, address + length):
            if a not in self.memory:
                raise OSError(f"0x{a:x} は読めない")
            out.append(self.memory[a])
        return bytes(out)

    def put(self, address, data):
        for i, b in enumerate(data):
            self.memory[address + i] = b

    def step(self):
        self.steps += 1
        self.registers[15] = self.registers.get(15, 0) + 2


class Ctx:
    def __init__(self, target, variables=None):
        self.target, self.collected, self.variables, self.traced = target, [], dict(variables or {}), []

    def register(self, n):
        value = self.target.read_register(n)
        if value is None:
            raise KeyError(n)
        return value

    def memory(self, address, length):
        return self.target.read_memory(address, length)

    def collect(self, address, data):
        self.collected.append((address, data))

    def variable(self, n):
        return self.variables[n]

    def set_variable(self, n, value):
        self.variables[n] = value

    def collect_variable(self, n):
        self.traced.append(n)


def bc(*parts) -> bytes:
    out = bytearray()
    for p in parts:
        out += bytes([p]) if isinstance(p, int) else p
    return bytes(out)


def c32(v):
    return bc(0x24, v.to_bytes(4, "big"))


END = 0x27


# --- エージェント式 ----------------------------------------------------------------------------

def test_arithmetic_comparison_and_signedness():
    ctx = Ctx(Target())
    assert evaluate(bc(0x22, b"\x07", 0x22, b"\x03", 0x03, END), ctx) == 4             # 7 - 3
    assert evaluate(bc(0x22, b"\x02", 0x22, b"\x05", 0x03, END), ctx) == (1 << 64) - 3   # 64 ビットで回る
    minus7 = bc(0x22, b"\x07", 0x16, b"\x08")                                            # 7 を 8 ビットで符号拡張 = 7
    assert evaluate(bc(minus7, END), ctx) == 7
    neg = bc(0x22, b"\xf9", 0x16, b"\x08")                                               # 0xF9 → -7
    assert evaluate(bc(neg, 0x22, b"\x02", 0x05, END), ctx) == (1 << 64) - 3             # -7 / 2 = -3（0 へ切り捨て）
    assert evaluate(bc(neg, 0x22, b"\x02", 0x07, END), ctx) == (1 << 64) - 1             # -7 % 2 = -1
    assert evaluate(bc(neg, 0x22, b"\x00", 0x14, END), ctx) == 1                         # -7 < 0（符号付き）
    assert evaluate(bc(neg, 0x22, b"\x00", 0x15, END), ctx) == 0                         # 符号なしでは大きい
    assert evaluate(bc(neg, 0x2A, b"\x08", END), ctx) == 0xF9                            # zero_ext
    assert evaluate(bc(0x22, b"\x01", 0x22, b"\x04", 0x09, END), ctx) == 16             # 1 << 4
    with pytest.raises(AgentError, match="0 で割った"):
        evaluate(bc(0x22, b"\x01", 0x22, b"\x00", 0x06, END), ctx)
    with pytest.raises(AgentError, match="扱わない"):
        evaluate(bc(0x01, END), ctx)                                                     # 浮動小数
    with pytest.raises(AgentError, match="スタックが足りない"):
        evaluate(bc(0x02, END), ctx)


def test_memory_registers_jumps_and_trace():
    t = Target({0: RAM + 4, 13: 0x2000_1000})
    t.put(RAM, (0x1234_5678).to_bytes(4, "little") + b"ab\x00cd")
    ctx = Ctx(t)
    assert evaluate(bc(c32(RAM), 0x19, END), ctx) == 0x1234_5678                         # ref32
    assert evaluate(bc(0x26, b"\x00\x00", 0x17, END), ctx) == ord("a")                  # reg 0 → ref8
    # if (x == 0x12345678) 1 else 2: if_goto で飛ぶ
    code = bc(c32(RAM), 0x19, c32(0x1234_5678), 0x13, 0x20, b"\x00\x12", 0x22, b"\x02", 0x27, 0x22, b"\x01", END)
    assert code[18] == 0x22 and evaluate(code, ctx) == 1
    assert evaluate(bc(c32(RAM), 0x22, b"\x04", 0x0C, END), ctx) == 0                    # trace（集めた後スタックは空）
    assert ctx.collected[-1] == (RAM, (0x1234_5678).to_bytes(4, "little"))
    evaluate(bc(c32(RAM + 4), 0x0D, b"\x02", 0x29, END), ctx)                           # trace_quick 2
    assert ctx.collected[-1] == (RAM + 4, b"ab")
    evaluate(bc(c32(RAM + 4), 0x22, b"\x10", 0x2F, END), ctx)                           # tracenz: 0 まで（含む）
    assert ctx.collected[-1] == (RAM + 4, b"ab\x00")
    with pytest.raises(AgentError, match="読めない"):
        evaluate(bc(c32(0x3000_0000), 0x19, END), ctx)
    with pytest.raises(AgentError, match="レジスタ 5"):
        evaluate(bc(0x26, b"\x00\x05", END), ctx)


def test_stack_ops_and_trace_state_variables():
    ctx = Ctx(Target(), {1: 10})
    assert evaluate(bc(0x22, b"\x01", 0x22, b"\x02", 0x22, b"\x03", 0x33, END), ctx) == 2   # rot: a b c → c a b
    assert evaluate(bc(0x22, b"\x01", 0x22, b"\x02", 0x2B, END), ctx) == 1                   # swap
    assert evaluate(bc(0x22, b"\x05", 0x22, b"\x06", 0x32, b"\x01", END), ctx) == 5         # pick 1
    assert evaluate(bc(0x2C, b"\x00\x01", 0x22, b"\x01", 0x02, 0x2D, b"\x00\x01", END), ctx) == 11
    assert ctx.variables[1] == 11
    evaluate(bc(0x2E, b"\x00\x01", END), ctx)
    assert ctx.traced == [1]
    with pytest.raises(AgentError, match="終わらない"):
        evaluate(bc(0x21, b"\x00\x00"), ctx)                                              # goto 0 の繰り返し


# --- パケットの読み取り ----------------------------------------------------------------------------

def test_tracepoint_definition_actions_condition_and_upload():
    exp = trace.Experiment()
    cond = bc(c32(RAM), 0x19, 0x22, b"\x03", 0x2B, 0x14, END).hex()                       # 3 < x
    assert exp.define(f"QTDP:1:08000200:E:0:2:X{len(cond) // 2:x},{cond}-") is False      # 続きがある
    assert exp.define("QTDP:-1:08000200:R8000-") is False                                 # pc（15）を集める
    assert exp.define("QTDP:-1:08000200:MFFFFFFFF,20000000,8M0d,fffffffffffffff8,4") is True
    exp.source("QTDPsrc:1:08000200:at:0:8:" + "main.c:9".encode().hex())
    exp.variable("QTDV:1:0:0:" + "count".encode().hex())
    tp = exp.tracepoints[(1, 0x0800_0200)]
    assert tp.enabled and tp.pass_count == 2 and tp.condition is not None
    kinds = [(a.kind, a.register, a.offset, a.length) for a in tp.actions]
    assert kinds[0][0] == "R" and tp.actions[0].mask == 1 << 15
    assert kinds[1] == ("M", -1, RAM, 8) and kinds[2] == ("M", 13, -8, 4)
    lines = exp.upload_tracepoints()
    assert lines[0] == f"T1:8000200:E:0:2:X{len(cond) // 2:x},{cond}"
    assert lines[1:4] == ["A1:8000200:R8000", "A1:8000200:MFFFFFFFF,20000000,8", "A1:8000200:M0d,fffffffffffffff8,4"]
    assert lines[4] == "Z1:8000200:at:0:8:" + "main.c:9".encode().hex()
    assert exp.upload_variables() == ["1:0:0:" + "count".encode().hex()]
    with pytest.raises(ValueError):
        exp.define("QTDP:-9:08000200:R1")                                                 # 定義の無い番号へのアクション


def test_only_the_base_register_reads_8_hex_digits_as_a_32_bit_minus_one():
    """GDB は M の基のレジスタを int の %X で書く（-1 = 番地そのもの → FFFFFFFF）。番地・ずれは 64 ビットの 16 進
    （phex_nz）なので、8 桁の ffffffff は番地 0xFFFFFFFF のまま（-1 に読み替えない）。"""
    actions = trace.parse_actions("MFFFFFFFF,ffffffff,1")
    [(stepping, a)] = actions
    assert (a.register, a.offset, a.length) == (-1, 0xFFFF_FFFF, 1)


# --- 当たったときの収集 -------------------------------------------------------------------------------

def make_running(*packets):
    exp = trace.Experiment()
    for p in packets:
        exp.define(p)
    exp.start(pc_register=15)
    return exp


def test_a_hit_collects_registers_memory_and_expressions_into_a_frame():
    collect_x = bc(c32(RAM + 8), 0x22, b"\x02", 0x0C, END).hex()
    exp = make_running("QTDP:1:08000200:E:0:0-", "QTDP:-1:08000200:R8001-",
                       "QTDP:-1:08000200:MFFFFFFFF,20000000,4-",
                       "QTDP:-1:08000200:M0d,4,4-",
                       f"QTDP:-1:08000200:X{len(collect_x) // 2:x},{collect_x}")
    t = Target({0: 7, 15: 0x0800_0200, 13: RAM + 0x10})
    t.put(RAM, bytes(range(32)))
    frames = exp.hit(0x0800_0200, t, host_s=1.0)
    assert [f.number for f in frames] == [0]
    f = frames[0]
    assert f.registers == {0: 7, 15: 0x0800_0200}
    assert f.read(RAM, 4) == bytes(range(4)) and f.read(RAM + 0x14, 4) == bytes(range(0x14, 0x18))
    assert f.read(RAM + 8, 2) == b"\x08\x09" and f.read(RAM + 6, 4) is None              # 集めていない
    assert f.read(RAM + 2, 8) == b"\x02\x03"                                             # 前から続く分だけ
    assert exp.status_reply().startswith("T1;tunknown:0;tframes:1;tcreated:1;")
    assert exp.tracepoint_status(1, 0x0800_0200) == f"V1:{f.size:x}"
    assert exp.hit(0x0800_0300, t, host_s=1.1) == []                                     # トレースポイントの無い番地


def test_condition_pass_count_and_stop_reasons():
    cond = bc(c32(RAM), 0x19, 0x22, b"\x00", 0x13, 0x0E, END).hex()                       # x != 0
    exp = make_running(f"QTDP:2:08000200:E:0:2:X{len(cond) // 2:x},{cond}-", "QTDP:-2:08000200:R1")
    t = Target({0: 1, 15: 0x0800_0200})
    t.put(RAM, b"\x00\x00\x00\x00")
    assert exp.hit(0x0800_0200, t, 0.0) == [] and exp.running                           # 偽: 集めない・数えない
    t.put(RAM, b"\x01\x00\x00\x00")
    exp.hit(0x0800_0200, t, 0.1)
    assert exp.running
    exp.hit(0x0800_0200, t, 0.2)                                                        # 2 回目でパスカウント
    assert not exp.running and exp.status_reply().startswith("T0;tpasscount:2;tframes:2;")
    # 条件が評価できない: 止めて理由を返す
    bad = bc(c32(0x3000_0000), 0x19, END).hex()
    exp = make_running(f"QTDP:3:08000200:E:0:0:X{len(bad) // 2:x},{bad}")
    exp.hit(0x0800_0200, t, 0.0)
    status = exp.status_reply()
    assert status.startswith("T0;terror:") and status.split(";")[1].endswith(":3")
    assert "読めない" in bytes.fromhex(status.split(";")[1].split(":")[1]).decode()
    # 使う人が止めた（QTNotes の tstop の文）
    exp = make_running("QTDP:4:08000200:E:0:0")
    exp.notes("QTNotes:tstop:" + "done".encode().hex())
    exp.stop()
    assert exp.status_reply().startswith("T0;tstop:" + "done".encode().hex() + ":0;")
    assert trace.Experiment().status_reply().startswith("T0;tnotrun:0;")


def test_buffer_full_and_circular():
    exp = trace.Experiment()
    exp.define("QTDP:1:08000200:E:0:0-")
    exp.define("QTDP:-1:08000200:MFFFFFFFF,20000000,40")
    exp.buffer("QTBuffer:size:100")                                                      # 256 バイト
    exp.start(pc_register=15)
    t = Target({15: 0x0800_0200})
    t.put(RAM, bytes(64))
    while exp.running:
        exp.hit(0x0800_0200, t, 0.0)
    assert exp.status_reply().startswith("T0;tfull:0;tframes:3;tcreated:3;")
    exp.buffer("QTBuffer:circular:1")
    exp.start(pc_register=15)
    for _ in range(10):
        exp.hit(0x0800_0200, t, 0.0)
    assert exp.running and [f.number for f in exp.frames] == [7, 8, 9]                   # 古いものを捨てる
    assert "circular:1" in exp.status_reply() and "tcreated:a" in exp.status_reply()


def test_while_stepping_makes_a_frame_per_step():
    exp = make_running("QTDP:1:08000200:E:2:0-", "QTDP:-1:08000200:R1-", "QTDP:-1:08000200:SR8000")
    t = Target({0: 5, 15: 0x0800_0200})
    frames = exp.hit(0x0800_0200, t, 0.0)
    assert t.steps == 2 and [f.registers for f in frames] == [{0: 5}, {15: 0x0800_0202}, {15: 0x0800_0204}]


def test_trace_state_variables_are_reset_at_start_and_readable():
    inc = bc(0x2C, b"\x00\x01", 0x22, b"\x01", 0x02, 0x2D, b"\x00\x01", 0x29, 0x2E, b"\x00\x01", END).hex()
    exp = trace.Experiment()
    exp.define(f"QTDP:1:08000200:E:0:0:X{len(inc) // 2:x},{inc}")                     # 条件で数える（常に真）
    exp.variable("QTDV:1:5:0:" + "n".encode().hex())
    exp.start(pc_register=15)
    t = Target({15: 0x0800_0200})
    exp.hit(0x0800_0200, t, 0.0)
    exp.hit(0x0800_0200, t, 0.0)
    assert exp.variable_reply(1) == "V7" and exp.variable_reply(9) == "U"


# --- フレームの検索 -----------------------------------------------------------------------------------

def test_find_frames_by_number_pc_tracepoint_and_range():
    exp = make_running("QTDP:1:08000200:E:0:0-", "QTDP:-1:08000200:R8000", "QTDP:2:08000300:E:0:0")
    t = Target({15: 0x0800_0200})
    exp.hit(0x0800_0200, t, 0.0)
    exp.hit(0x0800_0300, t, 0.0)                                                         # pc を集めない（番地が pc）
    exp.hit(0x0800_0200, t, 0.0)
    assert exp.find("QTFrame:1", current=None) == (1, "F1T2")
    assert exp.find("QTFrame:5", current=None) == (None, "F-1")
    assert exp.find("QTFrame:ffffffff", current=1) == (None, "OK")
    assert exp.find("QTFrame:pc:8000200", current=0) == (2, "F2T1")                      # 今の次から
    assert exp.find("QTFrame:tdp:2", current=None) == (1, "F1T2")
    assert exp.find("QTFrame:range:8000300:8000300", current=None) == (1, "F1T2")
    assert exp.find("QTFrame:outside:8000300:8000300", current=0) == (2, "F2T1")
    assert exp.frame(1).pc(15) == 0x0800_0300
    xml = exp.frame_info_xml(exp.frame(0))
    assert xml.startswith("<traceframe-info>") and "<memory" not in xml
