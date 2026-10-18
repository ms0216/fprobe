"""GDB のエージェント式（バイトコード）の評価器。トレースポイントの条件（QTDP の X）と集める式（アクションの X）に使う。

GDB はソースの式（`collect buf[i]`・`if count > 3` 等）を、レジスタ・メモリ・定数のスタック機械のバイトコードに直して
スタブに送る。スタブはそれを当たった場所で評価し、条件なら最後のスタックの頂上（0 以外 = 真）を、集める式なら trace 系の
命令で記録するメモリを使う。機器には触らない: レジスタ・メモリ・トレース状態変数は `Context` 越し。

根拠: GDB の文書「Agent Expressions」（Bytecode Descriptions）と gdb/gdbsupport/ax.def の命令の番号。
値はすべて 64 ビット（GDB の LONGEST）で、符号の要る命令（div_signed・less_signed 等）だけ符号付きに読む。
浮動小数（float 前置き・ref_float 等）と printf（dprintf のエージェント方式）は扱わない（誤り）。
"""

from dataclasses import dataclass
from typing import Protocol

__all__ = ["AgentError", "Context", "MAX_STACK", "MAX_STEPS", "evaluate"]

MASK = (1 << 64) - 1
MAX_STACK = 1024                 # スタックの深さの上限（壊れたバイトコード・誤り）
MAX_STEPS = 100_000              # 命令の数の上限（無限ループの止め）


class AgentError(Exception):
    """バイトコードを評価できない（0 で割った・知らない命令・スタックの不足・メモリが読めない等）。"""


class Context(Protocol):
    """評価が使う窓口（当たった場所のコア・メモリと、トレースのフレームへの記録）。"""

    def register(self, number: int) -> int:
        """GDB のレジスタ番号の値（読めなければ例外）。"""

    def memory(self, address: int, length: int) -> bytes:
        """ターゲットのメモリ（読めなければ例外）。"""

    def collect(self, address: int, data: bytes) -> None:
        """trace 系の命令で読んだメモリをフレームに入れる。"""

    def variable(self, number: int) -> int:
        """トレース状態変数の値（無ければ例外）。"""

    def set_variable(self, number: int, value: int) -> None: ...

    def collect_variable(self, number: int) -> None:
        """tracev: トレース状態変数の値をフレームに入れる。"""


def _signed(value: int) -> int:
    value &= MASK
    return value - (1 << 64) if value >> 63 else value


def _truncating_div(a: int, b: int) -> int:
    q = abs(a) // abs(b)
    return q if (a < 0) == (b < 0) else -q


@dataclass
class _Machine:
    code: bytes
    ctx: Context
    pc: int = 0

    def __post_init__(self) -> None:
        self.stack: list[int] = []

    # --- スタック ----------------------------------------------------------------------------------

    def push(self, value: int) -> None:
        if len(self.stack) >= MAX_STACK:
            raise AgentError("スタックがあふれた")
        self.stack.append(value & MASK)

    def pop(self) -> int:
        if not self.stack:
            raise AgentError(f"スタックが足りない（0x{self.pc - 1:x} の命令）")
        return self.stack.pop()

    def immediate(self, size: int) -> int:
        if self.pc + size > len(self.code):
            raise AgentError("バイトコードが途中で切れている")
        value = int.from_bytes(self.code[self.pc:self.pc + size], "big")
        self.pc += size
        return value

    def jump(self, target: int) -> None:
        if not 0 <= target < len(self.code):
            raise AgentError(f"飛び先 0x{target:x} がバイトコードの外")
        self.pc = target

    def read(self, address: int, length: int) -> bytes:
        try:
            data = bytes(self.ctx.memory(address & MASK, length))
        except Exception as e:
            raise AgentError(f"0x{address:x} の {length} バイトが読めない: {e}") from e
        if len(data) != length:
            raise AgentError(f"0x{address:x} の {length} バイトが読めない")
        return data

    def trace(self, address: int, length: int) -> None:
        if length:
            self.ctx.collect(address & MASK, self.read(address, length))

    def trace_string(self, address: int, limit: int) -> None:
        """tracenz: limit バイトまで、最初の 0 のバイト（含む）まで集める。"""
        data = bytearray()
        while len(data) < limit:
            at, size = address + len(data), min(64, limit - len(data))
            try:
                chunk = self.read(at, size)
            except AgentError:
                try:
                    chunk = self.read(at, 1)                     # 読める区画の端の近く: 1 バイトずつ
                except AgentError:
                    if data:
                        break                                    # 区画の端まで集めた
                    raise
            zero = chunk.find(0)
            if zero >= 0:
                data += chunk[:zero + 1]
                break
            data += chunk
        if data:
            self.ctx.collect(address & MASK, bytes(data))

    # --- 実行 ------------------------------------------------------------------------------------

    def run(self) -> int:
        steps = 0
        while True:
            steps += 1
            if steps > MAX_STEPS:
                raise AgentError(f"命令が {MAX_STEPS} を超えた（終わらない）")
            if self.pc >= len(self.code):
                raise AgentError("end の無いバイトコード")
            op = self.code[self.pc]
            self.pc += 1
            handler = _OPS.get(op)
            if handler is None:
                raise AgentError(f"扱わない命令 0x{op:02x}（浮動小数・printf 等）")
            if handler(self) is _END:
                return self.stack[-1] if self.stack else 0


_END = object()


def _binary(fn):
    def op(m: _Machine):
        b, a = m.pop(), m.pop()
        m.push(fn(a, b))
    return op


def _divide(signed: bool, remainder: bool):
    def op(m: _Machine):
        b, a = m.pop(), m.pop()
        if b == 0:
            raise AgentError("0 で割った")
        if signed:
            sa, sb = _signed(a), _signed(b)
            q = _truncating_div(sa, sb)
            m.push(sa - sb * q if remainder else q)
        else:
            m.push(a % b if remainder else a // b)
    return op


def _shift_left(a: int, b: int) -> int:
    return 0 if b >= 64 else a << b


def _shift_right(signed: bool):
    def fn(a: int, b: int) -> int:
        if signed:
            return _signed(a) >> min(b, 63)
        return 0 if b >= 64 else a >> b
    return fn


def _ref(size: int):
    def op(m: _Machine):
        m.push(int.from_bytes(m.read(m.pop(), size), "little"))  # Cortex-M はリトルエンディアン
    return op


def _const(size: int):
    def op(m: _Machine):
        m.push(m.immediate(size))
    return op


def _ext(m: _Machine):
    bits = m.immediate(1)
    value = m.pop()
    if 0 < bits < 64:
        value &= (1 << bits) - 1
        if value >> (bits - 1):
            value -= 1 << bits
    m.push(value)


def _zero_ext(m: _Machine):
    bits = m.immediate(1)
    value = m.pop()
    m.push(value & ((1 << bits) - 1) if bits < 64 else value)


def _trace(m: _Machine):
    size, address = m.pop(), m.pop()
    m.trace(address, size)


def _trace_quick(size_bytes: int):
    def op(m: _Machine):
        n = m.immediate(size_bytes)
        if not m.stack:
            raise AgentError("スタックが足りない（trace_quick）")
        m.trace(m.stack[-1], n)
    return op


def _tracenz(m: _Machine):
    size, address = m.pop(), m.pop()
    m.trace_string(address, size)


def _if_goto(m: _Machine):
    target = m.immediate(2)
    if m.pop():
        m.jump(target)


def _goto(m: _Machine):
    m.jump(m.immediate(2))


def _reg(m: _Machine):
    number = m.immediate(2)
    try:
        m.push(m.ctx.register(number))
    except Exception as e:
        raise AgentError(f"レジスタ {number} が読めない: {e}") from e


def _dup(m: _Machine):
    value = m.pop()
    m.push(value)
    m.push(value)


def _swap(m: _Machine):
    b, a = m.pop(), m.pop()
    m.push(b)
    m.push(a)


def _pick(m: _Machine):
    n = m.immediate(1)
    if n >= len(m.stack):
        raise AgentError(f"pick {n}: スタックが足りない")
    m.push(m.stack[-1 - n])


def _rot(m: _Machine):
    c, b, a = m.pop(), m.pop(), m.pop()                         # a b c → c a b
    for v in (c, a, b):
        m.push(v)


def _variable(kind: str):
    def op(m: _Machine):
        number = m.immediate(2)
        try:
            if kind == "get":
                m.push(m.ctx.variable(number))
            elif kind == "set":
                if not m.stack:
                    raise AgentError("スタックが足りない（setv）")
                m.ctx.set_variable(number, _signed(m.stack[-1]))
            else:
                m.ctx.collect_variable(number)
        except AgentError:
            raise
        except Exception as e:
            raise AgentError(f"トレース状態変数 {number}: {e}") from e
    return op


_OPS = {
    0x02: _binary(lambda a, b: a + b),
    0x03: _binary(lambda a, b: a - b),
    0x04: _binary(lambda a, b: a * b),
    0x05: _divide(True, False),
    0x06: _divide(False, False),
    0x07: _divide(True, True),
    0x08: _divide(False, True),
    0x09: _binary(_shift_left),
    0x0A: _binary(_shift_right(True)),
    0x0B: _binary(_shift_right(False)),
    0x0C: _trace,
    0x0D: _trace_quick(1),
    0x0E: lambda m: m.push(int(m.pop() == 0)),
    0x0F: _binary(lambda a, b: a & b),
    0x10: _binary(lambda a, b: a | b),
    0x11: _binary(lambda a, b: a ^ b),
    0x12: lambda m: m.push(~m.pop()),
    0x13: _binary(lambda a, b: int(a == b)),
    0x14: _binary(lambda a, b: int(_signed(a) < _signed(b))),
    0x15: _binary(lambda a, b: int(a < b)),
    0x16: _ext,
    0x17: _ref(1),
    0x18: _ref(2),
    0x19: _ref(4),
    0x1A: _ref(8),
    0x20: _if_goto,
    0x21: _goto,
    0x22: _const(1),
    0x23: _const(2),
    0x24: _const(4),
    0x25: _const(8),
    0x26: _reg,
    0x27: lambda m: _END,
    0x28: _dup,
    0x29: lambda m: m.pop() and None,
    0x2A: _zero_ext,
    0x2B: _swap,
    0x2C: _variable("get"),
    0x2D: _variable("set"),
    0x2E: _variable("trace"),
    0x2F: _tracenz,
    0x30: _trace_quick(2),
    0x32: _pick,
    0x33: _rot,
}


def evaluate(code: bytes, ctx: Context) -> int:
    """バイトコードを評価し、end の時点のスタックの頂上（空なら 0。64 ビットの符号なし）を返す。"""
    return _Machine(bytes(code), ctx).run()
