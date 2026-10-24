"""GDB のトレースポイントの実験（U48）: 定義（QTDP・QTDPsrc・QTDV・QTro・QTBuffer・QTNotes・QTDisconnected）、
当たったときの収集（条件・レジスタ・メモリ・エージェント式・while-stepping・パスカウント）、トレースのバッファ
（大きさ・循環）、フレームの検索（QTFrame）、状態（qTStatus・qTP・qTV）、アップロード（qTfP / qTfV）。

機器には触らない: 当たったときのコアとメモリは `HitTarget` 越し（Probe の GdbTraceTools がブレークポイントで止まった
コアを渡す）。デーモンの作業スレッド（当たったときの収集）と GDB の接続（フレームの読み）の両方から使うので、
公開の操作はすべて内部のロックの中で行う。

形式の根拠: GDB の文書「Tracepoint Packets」と gdbserver（tracepoint.cc）の応答の形。
"""

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Protocol

from .agent import AgentError, evaluate

__all__ = ["DEFAULT_BUFFER", "Experiment", "Frame", "HitTarget", "Tracepoint"]

DEFAULT_BUFFER = 5 * 1024 * 1024        # gdbserver と同じ既定のバッファの大きさ
ADDRESS_MASK = 0xFFFF_FFFF              # 32 ビットのターゲット（Cortex-M）
FRAME_HEADER = 6                        # フレームの見出し（トレースポイントの番号 2 + 大きさ 4。gdbserver の数え方）
MEMORY_BLOCK = 1 + 8 + 2                # メモリのかたまりの見出し
VARIABLE_BLOCK = 1 + 4 + 8              # トレース状態変数のかたまり
_HEX = set("0123456789abcdefABCDEF")


class HitTarget(Protocol):
    """当たった場所で止まっているコア。"""

    def read_register(self, number: int) -> int | None:
        """GDB のレジスタ番号の値（無い・読めなければ None）。"""

    def read_memory(self, address: int, length: int) -> bytes:
        """読めなければ例外。"""

    def step(self) -> None:
        """1 命令進める（while-stepping）。"""


@dataclass
class Action:
    kind: str                           # R（レジスタ）/ M（メモリ）/ X（エージェント式）
    raw: str                            # 受け取ったままの形（アップロードで返す）
    mask: int = 0                       # R: レジスタ番号のビット
    register: int = -1                  # M: 基準のレジスタ（-1 = 絶対番地）
    offset: int = 0                     # M: 基準からの差（-1 なら番地）
    length: int = 0                     # M: バイト数
    code: bytes = b""                   # X: バイトコード


@dataclass
class Tracepoint:
    number: int
    address: int
    enabled: bool
    step: int                           # while-stepping の命令数
    pass_count: int                     # この回数当たったら実験を止める（0 = 止めない）
    condition: bytes | None
    actions: list[Action] = field(default_factory=list)
    step_actions: list[Action] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)       # QTDPsrc の "type:start:slen:hex"
    hits: int = 0
    usage: int = 0                      # このトレースポイントのフレームの大きさの合計


@dataclass
class Variable:
    number: int
    initial: int
    raw: str                            # QTDV の "n:value:builtin:name"（アップロードで返す）
    value: int | None = None


@dataclass
class Frame:
    number: int
    tracepoint: int
    address: int                        # トレースポイントの番地（pc を集めていなければ、これが pc）
    host_s: float
    registers: dict[int, int] = field(default_factory=dict)
    memory: list[tuple[int, bytes]] = field(default_factory=list)
    variables: dict[int, int] = field(default_factory=dict)
    size: int = FRAME_HEADER

    def pc(self, pc_register: int) -> int:
        return self.registers.get(pc_register, self.address)

    def add_memory(self, address: int, data: bytes) -> None:
        if data:
            self.memory.append((address & ADDRESS_MASK, bytes(data)))
            self.size += MEMORY_BLOCK + len(data)

    def read(self, address: int, length: int) -> bytes | None:
        """集めたメモリの address から（続いている分だけ。length まで）。address を集めていなければ None。"""
        out = bytearray()
        at = address
        while len(out) < length:
            for start, data in self.memory:
                if start <= at < start + len(data):
                    take = data[at - start:at - start + length - len(out)]
                    out += take
                    at += len(take)
                    break
            else:
                break
        return bytes(out) if out else None


def _hex_end(text: str, start: int) -> int:
    end = start
    while end < len(text) and text[end] in _HEX:
        end += 1
    return end


def _signed_hex(text: str) -> int:
    """GDB の 16 進（"-1"、64 ビットの 2 の補数の負も。phex_nz の書き方）→ 符号付き。"""
    if text.startswith("-"):
        return -int(text[1:], 16)
    value = int(text, 16)
    if len(text) > 8 and value >> 63:
        return value - (1 << 64)
    return value


def _base_register(text: str) -> int:
    """M の基のレジスタ。GDB は int の %X で書く（-1 = 基なし・番地そのもの → "FFFFFFFF"。remote.c の encode_actions）。"""
    value = _signed_hex(text)
    return value - (1 << 32) if len(text) == 8 and value >> 31 else value


def parse_actions(text: str) -> list[tuple[bool, Action]]:
    """QTDP のアクションの並び（"R…M…X…"、S 以降は while-stepping）→ [(stepping, Action)]。"""
    out: list[tuple[bool, Action]] = []
    stepping = False
    i = 0
    while i < len(text):
        c = text[i]
        if c == "S":
            stepping = True
            i += 1
        elif c in "- ":
            i += 1
        elif c == "R":
            j = _hex_end(text, i + 1)
            out.append((stepping, Action("R", text[i:j], mask=int(text[i + 1:j], 16))))
            i = j
        elif c == "M":
            base, offset, rest = text[i + 1:].split(",", 2)
            j = _hex_end(rest, 0)
            length = int(rest[:j], 16)
            raw = f"M{base},{offset},{rest[:j]}"
            register = _base_register(base)
            out.append((stepping, Action("M", raw, register=-1 if register < 0 else register,
                                         offset=_signed_hex(offset), length=length)))
            i += len(raw)
        elif c == "X":
            n_text, _, rest = text[i + 1:].partition(",")
            n = int(n_text, 16)
            code = bytes.fromhex(rest[:2 * n])
            raw = f"X{n_text},{rest[:2 * n]}"
            out.append((stepping, Action("X", raw, code=code)))
            i += len(raw)
        else:
            raise ValueError(f"扱わないアクション {c!r}（{text!r}）")
    return out


class _Evaluation:
    """エージェント式の窓口（fprobe.gdb.agent.Context）。frame が None なら条件の評価（集めない）。"""

    def __init__(self, target: HitTarget, variables: dict[int, Variable], frame: Frame | None):
        self.target, self.variables, self.frame = target, variables, frame

    def register(self, number: int) -> int:
        value = self.target.read_register(number)
        if value is None:
            raise KeyError("読めない")
        return value

    def memory(self, address: int, length: int) -> bytes:
        return self.target.read_memory(address & ADDRESS_MASK, length)

    def collect(self, address: int, data: bytes) -> None:
        if self.frame is not None:
            self.frame.add_memory(address, data)

    def _variable(self, number: int) -> Variable:
        variable = self.variables.get(number)
        if variable is None:
            raise KeyError("定義されていない（QTDV）")
        return variable

    def variable(self, number: int) -> int:
        variable = self._variable(number)
        if variable.value is None:
            raise KeyError("値が無い")
        return variable.value & ((1 << 64) - 1)

    def set_variable(self, number: int, value: int) -> None:
        self._variable(number).value = value

    def collect_variable(self, number: int) -> None:
        variable = self._variable(number)
        if self.frame is not None and variable.value is not None:
            if number not in self.frame.variables:
                self.frame.size += VARIABLE_BLOCK
            self.frame.variables[number] = variable.value


class Experiment:
    """1 つのトレースの実験（定義・バッファ・状態）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.tracepoints: dict[tuple[int, int], Tracepoint] = {}
        self.variables: dict[int, Variable] = {}
        self.frames: deque[Frame] = deque()
        self.readonly: list[tuple[int, int]] = []
        self.buffer_size = DEFAULT_BUFFER
        self.circular = False
        self.disconnected_tracing = False
        self.user = ""
        self.note = ""
        self.stop_note = ""
        self.running = False
        self.started = False
        self.created = 0
        self.used = 0
        self.stop_reason = "tnotrun:0"
        self.start_time = 0.0
        self.stop_time = 0.0
        self.pc_register = 15
        self.register_sizes: dict[int, int] = {}
        self._last: tuple[int, int] | None = None          # 最後に定義したトレースポイント（続きのアクションの先）
        self.halts = 0                                       # 当たって止めた回数と、収集にかかった時間（monitor trace）
        self.halt_s = 0.0
        self.halt_max_s = 0.0

    # --- 定義 ------------------------------------------------------------------------------------

    def define(self, text: str) -> bool:
        """QTDP。定義が終わった（続きの '-' が無い）なら True。"""
        with self._lock:
            body = text[len("QTDP:"):]
            more = body.endswith("-")
            if body.startswith("-"):
                n_text, addr_text, actions = body[1:].split(":", 2)
                key = (int(n_text, 16), int(addr_text, 16))
                tp = self.tracepoints.get(key)
                if tp is None:
                    raise ValueError(f"トレースポイント {key[0]} at 0x{key[1]:x} は定義されていない")
                for stepping, action in parse_actions(actions):
                    (tp.step_actions if stepping else tp.actions).append(action)
                return not more
            fields = (body[:-1] if more else body).split(":")
            n, address = int(fields[0], 16), int(fields[1], 16)
            condition = None
            for extra in fields[5:]:
                if extra.startswith("X"):
                    length, _, code = extra[1:].partition(",")
                    condition = bytes.fromhex(code[:2 * int(length, 16)])
                elif extra.startswith("F"):
                    raise ValueError("ファスト・トレースポイントは扱わない（ftrace でなく trace を使う）")
            self.tracepoints[(n, address)] = Tracepoint(n, address, fields[2] == "E", int(fields[3], 16),
                                                        int(fields[4], 16), condition)
            return not more

    def source(self, text: str) -> None:
        """QTDPsrc:n:addr:type:start:slen:hex（アップロードで返すだけ）。"""
        with self._lock:
            n_text, addr_text, rest = text[len("QTDPsrc:"):].split(":", 2)
            tp = self.tracepoints.get((int(n_text, 16), int(addr_text, 16)))
            if tp is not None:
                tp.sources.append(rest)

    def variable(self, text: str) -> None:
        """QTDV:n:value:builtin:name。"""
        with self._lock:
            raw = text[len("QTDV:"):]
            n_text, value_text = raw.split(":")[:2]
            self.variables[int(n_text, 16)] = Variable(int(n_text, 16), _signed_hex(value_text), raw)

    def set_readonly(self, text: str) -> None:
        """QTro:start,end[:start,end]…（読むだけの区画。フレームに無いメモリもターゲットから読んでよい）。"""
        with self._lock:
            self.readonly = []
            for item in text[len("QTro"):].split(":"):
                if item:
                    start, end = item.split(",")
                    self.readonly.append((int(start, 16), int(end, 16)))

    def readonly_covers(self, address: int, length: int) -> bool:
        with self._lock:
            return any(start <= address and address + length <= end for start, end in self.readonly)

    def buffer(self, text: str) -> None:
        """QTBuffer:size:<hex | -1> / QTBuffer:circular:<0|1>。"""
        with self._lock:
            _, what, value = text.split(":", 2)
            if what == "size":
                self.buffer_size = DEFAULT_BUFFER if value.startswith("-") else int(value, 16)
            elif what == "circular":
                self.circular = int(value, 16) != 0
            else:
                raise ValueError(f"QTBuffer:{what} は扱わない")

    def notes(self, text: str) -> None:
        """QTNotes:user:hex;notes:hex;tstop:hex。"""
        with self._lock:
            for item in text[len("QTNotes:"):].split(";"):
                key, _, value = item.partition(":")
                decoded = bytes.fromhex(value).decode("utf-8", errors="replace")
                if key == "user":
                    self.user = decoded
                elif key == "notes":
                    self.note = decoded
                elif key == "tstop":
                    self.stop_note = decoded

    def set_disconnected(self, text: str) -> None:
        """QTDisconnected:<0|1>: GDB が切れてもトレースを続けるか。"""
        with self._lock:
            self.disconnected_tracing = int(text.partition(":")[2] or "0", 16) != 0

    def set_enabled(self, text: str) -> Tracepoint:
        """QTEnable:n:addr / QTDisable:n:addr。変えたトレースポイントを返す。"""
        with self._lock:
            head, n_text, addr_text = text.split(":")
            tp = self.tracepoints.get((int(n_text, 16), int(addr_text, 16)))
            if tp is None:
                raise KeyError(f"トレースポイント {n_text} at 0x{addr_text} は無い")
            tp.enabled = head == "QTEnable"
            return tp

    def addresses(self) -> set[int]:
        """有効なトレースポイントの番地（ブレークポイントを置く所）。"""
        with self._lock:
            return {tp.address for tp in self.tracepoints.values() if tp.enabled}

    # --- 始める・止める ---------------------------------------------------------------------------------

    def start(self, pc_register: int = 15, register_sizes: dict[int, int] | None = None) -> None:
        with self._lock:
            self.pc_register = pc_register
            self.register_sizes = dict(register_sizes or {})
            self.frames.clear()
            self.created = self.used = 0
            for tp in self.tracepoints.values():
                tp.hits = tp.usage = 0
            for variable in self.variables.values():
                variable.value = variable.initial
            self.halts, self.halt_s, self.halt_max_s = 0, 0.0, 0.0
            self.running = self.started = True
            self.stop_reason = "tunknown:0"
            self.start_time, self.stop_time = time.time(), 0.0

    def stop(self, reason: str = "tstop", tracepoint: int = 0, text: str | None = None) -> None:
        """止める。reason: tstop（使う人）/ tfull / tdisconnected / tpasscount / terror。"""
        with self._lock:
            if not self.running:
                return
            self.running = False
            self.stop_time = time.time()
            if reason == "tstop":
                self.stop_reason = f"tstop:{self.stop_note.encode('utf-8').hex()}:0"
            elif reason == "terror":
                self.stop_reason = f"terror:{(text or '').encode('utf-8').hex()}:{tracepoint:x}"
            elif reason == "tpasscount":
                self.stop_reason = f"tpasscount:{tracepoint:x}"
            else:
                self.stop_reason = f"{reason}:0"

    def note_halt(self, seconds: float) -> None:
        """当たって止めていた時間（収集の分）を足す。"""
        with self._lock:
            self.halts += 1
            self.halt_s += seconds
            self.halt_max_s = max(self.halt_max_s, seconds)

    # --- 当たったとき -----------------------------------------------------------------------------------

    def hit(self, pc: int, target: HitTarget, host_s: float) -> list[Frame]:
        """pc のトレースポイントが当たった: 条件を見て、集めたフレームを返す（止まった・当たらなければ空）。"""
        with self._lock:
            frames: list[Frame] = []
            for tp in [t for t in self.tracepoints.values() if t.address == pc and t.enabled]:
                if not self.running:
                    break
                if tp.condition is not None:
                    try:
                        if not evaluate(tp.condition, _Evaluation(target, self.variables, None)):
                            continue
                    except AgentError as e:
                        self.stop("terror", tp.number, f"condition: {e}")
                        break
                tp.hits += 1
                frame = self._collect(tp, tp.actions, target, host_s)
                if frame is not None:
                    frames.append(frame)
                for _ in range(tp.step if frame is not None else 0):
                    if not self.running:
                        break
                    try:
                        target.step()
                    except Exception as e:
                        self.stop("terror", tp.number, f"while-stepping: {e}")
                        break
                    frame = self._collect(tp, tp.step_actions, target, host_s)
                    if frame is None:
                        break
                    frames.append(frame)
                if self.running and tp.pass_count and tp.hits >= tp.pass_count:
                    self.stop("tpasscount", tp.number)
            return frames

    def _collect(self, tp: Tracepoint, actions: list[Action], target: HitTarget, host_s: float) -> Frame | None:
        frame = Frame(self.created, tp.number, tp.address, host_s)
        for action in actions:
            if action.kind == "R":
                n, mask = 0, action.mask
                while mask:
                    if mask & 1:
                        value = target.read_register(n)
                        if value is not None and n not in frame.registers:
                            frame.registers[n] = value
                            frame.size += self.register_sizes.get(n, 4)
                    mask >>= 1
                    n += 1
                frame.size += 1 if action.mask else 0
            elif action.kind == "M":
                base = 0 if action.register < 0 else target.read_register(action.register)
                if base is None:
                    continue                                       # 基準のレジスタが読めない: 集めない
                address = (base + action.offset) & ADDRESS_MASK
                try:
                    frame.add_memory(address, target.read_memory(address, action.length))
                except Exception:
                    pass                                           # 読めない区画: 集めない（GDB には <unavailable>）
            else:
                try:
                    evaluate(action.code, _Evaluation(target, self.variables, frame))
                except AgentError as e:
                    self.stop("terror", tp.number, f"collect: {e}")
                    break
        return self._store(tp, frame)

    def _store(self, tp: Tracepoint, frame: Frame) -> Frame | None:
        if frame.size > self.buffer_size:
            self.stop("tfull")
            return None
        if self.used + frame.size > self.buffer_size:
            if not self.circular:
                self.stop("tfull")
                return None
            while self.frames and self.used + frame.size > self.buffer_size:
                self.used -= self.frames.popleft().size
        self.frames.append(frame)
        self.used += frame.size
        self.created += 1
        tp.usage += frame.size
        return frame

    # --- 状態 --------------------------------------------------------------------------------------

    def status_reply(self) -> str:
        """qTStatus の応答。"""
        with self._lock:
            parts = [f"T{int(self.running)}", self.stop_reason, f"tframes:{len(self.frames):x}",
                     f"tcreated:{self.created:x}", f"tfree:{max(self.buffer_size - self.used, 0):x}",
                     f"tsize:{self.buffer_size:x}", f"circular:{int(self.circular)}",
                     f"disconn:{int(self.disconnected_tracing)}",
                     f"starttime:{int(self.start_time * 1e6):x}", f"stoptime:{int(self.stop_time * 1e6):x}"]
            if self.user:
                parts.append(f"username:{self.user.encode('utf-8').hex()}")
            if self.note:
                parts.append(f"notes:{self.note.encode('utf-8').hex()}")
            return ";".join(parts)

    def tracepoint_status(self, number: int, address: int) -> str:
        """qTP の応答: V<当たった数>:<使ったバッファ>。"""
        with self._lock:
            tp = self.tracepoints.get((number, address))
            if tp is None:
                return ""
            return f"V{tp.hits:x}:{tp.usage:x}"

    def variable_reply(self, number: int) -> str:
        """qTV の応答: V<値> / U（値が無い）。"""
        with self._lock:
            variable = self.variables.get(number)
            if variable is None or variable.value is None:
                return "U"
            return f"V{variable.value & ((1 << 64) - 1):x}"

    def summary(self) -> dict:
        """monitor trace の表示。"""
        with self._lock:
            return {"running": self.running, "status": self.stop_reason.split(":")[0], "frames": len(self.frames),
                    "created": self.created, "buffer": {"size": self.buffer_size, "used": self.used,
                                                        "circular": self.circular},
                    "disconnected_tracing": self.disconnected_tracing,
                    "halts": {"count": self.halts, "collect_ms_mean": round(self.halt_s / self.halts * 1e3, 3)
                              if self.halts else None, "collect_ms_max": round(self.halt_max_s * 1e3, 3)},
                    "tracepoints": [{"number": tp.number, "address": tp.address, "enabled": tp.enabled,
                                     "hits": tp.hits, "condition": tp.condition is not None,
                                     "actions": len(tp.actions), "while_stepping": tp.step,
                                     "pass_count": tp.pass_count} for tp in self.tracepoints.values()]}

    # --- フレーム ---------------------------------------------------------------------------------------

    def frame(self, number: int) -> Frame | None:
        with self._lock:
            for f in self.frames:
                if f.number == number:
                    return f
            return None

    def find(self, text: str, current: int | None) -> tuple[int | None, str]:
        """QTFrame:… → (選んだフレームの番号 / None = フレームを見ていない, 応答)。見つからなければ今のまま F-1。"""
        with self._lock:
            arg = text[len("QTFrame:"):]
            if ":" not in arg:
                number = int(arg, 16)
                if number >= 0x8000_0000:                           # -1（ffffffff）: フレームを見るのをやめる
                    return None, "OK"
                f = self.frame(number)
                return (number, f"F{number:x}T{f.tracepoint:x}") if f is not None else (current, "F-1")
            kind, _, rest = arg.partition(":")
            values = [int(v, 16) for v in rest.split(":")]
            pcr = self.pc_register
            tests = {"pc": lambda f: f.pc(pcr) == values[0],
                     "tdp": lambda f: f.tracepoint == values[0],
                     "range": lambda f: values[0] <= f.pc(pcr) <= values[1],
                     "outside": lambda f: not values[0] <= f.pc(pcr) <= values[1]}
            if kind not in tests:
                raise ValueError(f"QTFrame:{kind} は扱わない")
            after = -1 if current is None else current
            for f in self.frames:
                if f.number > after and tests[kind](f):
                    return f.number, f"F{f.number:x}T{f.tracepoint:x}"
            return current, "F-1"

    def frame_info_xml(self, frame: Frame) -> str:
        """qXfer:traceframe-info:read（集めたメモリとトレース状態変数）。"""
        with self._lock:
            parts = ["<traceframe-info>"]
            parts += [f'<memory start="0x{a:x}" length="0x{len(d):x}"/>' for a, d in frame.memory]
            parts += [f'<tvar id="{n}"/>' for n in sorted(frame.variables)]
            parts.append("</traceframe-info>")
            return "\n".join(parts)

    # --- アップロード（GDB がつなぎ直したとき） -------------------------------------------------------------

    def upload_tracepoints(self) -> list[str]:
        """qTfP / qTsP で返す行。"""
        with self._lock:
            lines = []
            for tp in self.tracepoints.values():
                n, a = tp.number, tp.address
                line = f"T{n:x}:{a:x}:{'E' if tp.enabled else 'D'}:{tp.step:x}:{tp.pass_count:x}"
                if tp.condition is not None:
                    line += f":X{len(tp.condition):x},{tp.condition.hex()}"
                lines.append(line)
                lines += [f"A{n:x}:{a:x}:{x.raw}" for x in tp.actions]
                lines += [f"S{n:x}:{a:x}:{x.raw}" for x in tp.step_actions]
                lines += [f"Z{n:x}:{a:x}:{s}" for s in tp.sources]
            return lines

    def upload_variables(self) -> list[str]:
        """qTfV / qTsV で返す行。"""
        with self._lock:
            return [v.raw for v in self.variables.values()]
