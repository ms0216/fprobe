"""試験用: SEGGER SystemView のパケットを作る（SEGGER_SYSVIEW.c の _SendPacket・ENCODE_U32・_EncodeStr と同じ規則）。"""


def varint(v: int) -> bytes:
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def string(s: str) -> bytes:
    data = s.encode()
    if len(data) < 255:
        return bytes([len(data)]) + data
    return bytes([0xFF, len(data) >> 8, len(data) & 0xFF]) + data


class Writer:
    """時刻（サイクル）を進めながらパケットを並べる。shift・base は INIT の ID の縮め方。"""

    def __init__(self, base: int = 0x2000_0000, shift: int = 2):
        self.out = bytearray()
        self.last = 0
        self.base, self.shift = base, shift

    def shrink(self, address: int) -> int:
        return (address - self.base) >> self.shift

    def packet(self, event_id: int, payload: bytes = b"", t: int | None = None) -> "Writer":
        if event_id < 24:
            self.out += varint(event_id) + payload
        else:
            self.out += varint(event_id) + varint(len(payload)) + payload
        delta = 0 if t is None else t - self.last
        if t is not None:
            self.last = t
        self.out += varint(delta)
        return self

    def start(self, t: int, sys_freq: int = 64_000_000, desc: str = "N=demo,O=FreeRTOS,D=STM32F4") -> "Writer":
        self.out += b"\x00" * 10
        self.packet(10)                                                    # TRACE_START（差 0）
        self.packet(24, varint(sys_freq) + varint(sys_freq) + varint(self.base) + varint(self.shift))
        self.out += varint(12) + varint(t) + varint(0)                     # SYSTIME_CYCLES
        self.last = t
        self.packet(14, string(desc), t)
        return self

    def task_info(self, task: int, prio: int, name: str, t: int) -> "Writer":
        return self.packet(9, varint(self.shrink(task)) + varint(prio) + string(name), t)

    def bytes(self) -> bytes:
        return bytes(self.out)
