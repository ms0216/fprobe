"""試験用: JTAG のチェーンの模型（IEEE 1149.1 の TAP の状態遷移、IR / DR のシフト）。JtagWire として使える。

TDI → 装置 N-1 → … → 装置 0 → TDO。装置 0 が TDO 側（fprobe.jtag_chain と同じ番号）。"""

from dataclasses import dataclass, field

# 状態の遷移（TMS = 0, TMS = 1）
_NEXT = {
    "TLR": ("RTI", "TLR"), "RTI": ("RTI", "SELDR"),
    "SELDR": ("CAPDR", "SELIR"), "CAPDR": ("SHDR", "EX1DR"), "SHDR": ("SHDR", "EX1DR"),
    "EX1DR": ("PADR", "UPDR"), "PADR": ("PADR", "EX2DR"), "EX2DR": ("SHDR", "UPDR"), "UPDR": ("RTI", "SELDR"),
    "SELIR": ("CAPIR", "TLR"), "CAPIR": ("SHIR", "EX1IR"), "SHIR": ("SHIR", "EX1IR"),
    "EX1IR": ("PAIR", "UPIR"), "PAIR": ("PAIR", "EX2IR"), "EX2IR": ("SHIR", "UPIR"), "UPIR": ("RTI", "SELDR"),
}


@dataclass
class Tap:
    ir_length: int
    idcode: int | None = None                          # None = リセットで BYPASS
    ir_capture: int = 0b01
    idcode_instruction: int | None = None              # 既定はすべて 0 以外の IR 値（下の __post_init__）
    user: dict[int, tuple[int, int]] = field(default_factory=dict)  # IR 値 → (DR 長, Capture-DR の値)
    ir: int = 0
    sr: int = 0
    sr_length: int = 1
    updates: list[tuple[str, int]] = field(default_factory=list)   # (IR / DR, Update で入った値)

    def __post_init__(self):
        if self.idcode_instruction is None:
            self.idcode_instruction = 0b1110 & ((1 << self.ir_length) - 1) or 1
        self.reset()

    @property
    def bypass(self) -> int:
        return (1 << self.ir_length) - 1

    def reset(self):
        self.ir = self.idcode_instruction if self.idcode is not None else self.bypass

    def dr(self) -> tuple[int, int]:
        if self.ir == self.idcode_instruction and self.idcode is not None:
            return 32, self.idcode
        if self.ir in self.user:
            return self.user[self.ir]
        return 1, 0

    def on_state(self, prev: str, state: str) -> None:
        """状態が prev から state に移った（Update の後の動作を模す装置が上書きする）。"""


class FakeChain:
    """JtagWire。TDO の線の不良は tdo_stuck（0 / 1）で。"""

    def __init__(self, taps: list[Tap], tdo_stuck: int | None = None):
        self.taps = taps
        self.state = "TLR"
        self.tdo_stuck = tdo_stuck
        self.clocks = 0

    def tick(self, tms: int, tdi: int) -> int:
        self.clocks += 1
        st = self.state
        shifting = st in ("SHDR", "SHIR")
        outs = [t.sr & 1 for t in self.taps]
        tdo = outs[0] if shifting and self.taps else 1
        if shifting:
            for i, t in enumerate(self.taps):
                inp = outs[i + 1] if i + 1 < len(self.taps) else tdi
                t.sr = (t.sr >> 1) | (inp << (t.sr_length - 1))
        nxt = _NEXT[st][tms]
        for t in self.taps:
            if nxt == "CAPIR":
                t.sr, t.sr_length = t.ir_capture, t.ir_length
            elif nxt == "CAPDR":
                t.sr_length, t.sr = t.dr()
            elif nxt == "UPIR":
                t.ir = t.sr & t.bypass
                t.updates.append(("IR", t.ir))
            elif nxt == "UPDR":
                t.updates.append(("DR", t.sr))
            elif nxt == "TLR":
                t.reset()
            t.on_state(st, nxt)
        self.state = nxt
        return tdo if self.tdo_stuck is None else self.tdo_stuck

    def sequence(self, cycles, tms, tdi, capture):
        assert 1 <= cycles <= 64
        out = 0
        for k in range(cycles):
            out |= self.tick(tms, (tdi >> k) & 1) << k
        return out if capture else None
