"""GDB のトレースポイントの定義の型（Action・Tracepoint・Variable・Frame）と、アクション文字列の解析（fprobe.gdb.trace から分けた）。

機器にも実験の状態にも触れない純粋な部分。fprobe.gdb.trace が再公開する。
"""

from dataclasses import dataclass, field

__all__ = ["ADDRESS_MASK", "Action", "FRAME_HEADER", "Frame", "MEMORY_BLOCK", "Tracepoint", "VARIABLE_BLOCK", "Variable", "signed_hex",
           "parse_actions"]

ADDRESS_MASK = 0xFFFF_FFFF              # 32 ビットのターゲット（Cortex-M）
FRAME_HEADER = 6                        # フレームの見出し（トレースポイントの番号 2 + 大きさ 4。gdbserver の数え方）
MEMORY_BLOCK = 1 + 8 + 2                # メモリのかたまりの見出し
VARIABLE_BLOCK = 1 + 4 + 8              # トレース状態変数のかたまり
_HEX = set("0123456789abcdefABCDEF")


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


def signed_hex(text: str) -> int:
    """GDB の 16 進（"-1"、64 ビットの 2 の補数の負も。phex_nz の書き方）→ 符号付き。"""
    if text.startswith("-"):
        return -int(text[1:], 16)
    value = int(text, 16)
    if len(text) > 8 and value >> 63:
        return value - (1 << 64)
    return value


def _base_register(text: str) -> int:
    """M の基のレジスタ。GDB は int の %X で書く（-1 = 基なし・番地そのもの → "FFFFFFFF"。remote.c の encode_actions）。"""
    value = signed_hex(text)
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
                                         offset=signed_hex(offset), length=length)))
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
