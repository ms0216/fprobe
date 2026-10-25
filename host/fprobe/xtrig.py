"""クロストリガ: ロジアナのトリガ・DAP の番地一致・ホストの指令を、ロジアナのトリガと割り込み書き込み
（既定はターゲット停止 = DHCSR ← C_HALT）につなぐ。

仕様: docs/superpowers/specs/2026-10-16-m9-cross-trigger-design.md
"""

from dataclasses import dataclass
from enum import IntEnum, IntFlag

from . import generated as g
from .device import Device

__all__ = ["CrossTrigger", "Event", "InjectStatus", "ActionState", "OutputConfig", "OutputMode", "Pattern",
           "PinOutputs", "Source", "Stage", "TriggerSequencer", "pulse_range_s", "pulse_ticks"]

DHCSR, HALT = 0xE000EDF0, 0xA05F0003


class Source(IntFlag):
    LA_TRIGGER = 1      # ロジアナのトリガ
    DAP_MATCH = 2       # ホストの DRW アクセスの番地一致
    HOST = 4            # CrossTrigger.fire()
    SEQUENCE = 8        # 多段トリガ（TriggerSequencer）の発火
    OBS_MATCH = 16      # FPGA の観測の値の一致（CSR obs.match。fprobe.fpga_obs.compile_match）
    SAMPLER_MATCH = 32  # サンプラの値の一致（CSR sampler_match。R21）


@dataclass(frozen=True)
class InjectStatus:
    busy: bool
    result: str         # "ok" / "swd_error" / "not_connected"
    ack: int
    count: int
    time: int           # 最後の書き込みが受理された時のタイムベース


def _field(word: int, field: tuple[int, int]) -> int:
    lsb, width = field
    return word >> lsb & (1 << width) - 1


class CrossTrigger:
    def __init__(self, dev: Device):
        self.dev = dev

    def route(self, *, to_la: Source = Source(0), to_inject: Source = Source(0)) -> None:
        self.dev.write32(g.XTRIG_ROUTE, int(to_la) << g.XTRIG_ROUTE_TO_LA[0] | int(to_inject) << g.XTRIG_ROUTE_TO_INJECT[0])

    def set_inject(self, address: int = DHCSR, value: int = HALT, ap: int = 0) -> None:
        """割り込み書き込みの中身（既定はターゲット停止）。"""
        self.dev.write(g.XTRIG_INJECT_AP, [ap, address, value])

    def set_match(self, address: int, *, address_mask: int = 0xFFFFFFFF, data: int = 0, data_mask: int = 0,
                  on_write: bool = True, on_read: bool = False) -> None:
        """番地一致（ホストの DRW アクセス）の条件。data_mask = 0 なら値を見ない。"""
        self.dev.write(g.XTRIG_MATCH_ADDR, [address, address_mask, data, data_mask,
                                            int(on_write) << g.XTRIG_MATCH_MODE_ON_WRITE[0]
                                            | int(on_read) << g.XTRIG_MATCH_MODE_ON_READ[0]])

    def fire(self) -> None:
        """源 HOST を 1 回発火する。"""
        self.dev.write32(g.XTRIG_COMMAND, 1)

    def inject_status(self) -> InjectStatus:
        status, lo, hi = self.dev.read(g.XTRIG_INJECT_STATUS, 3)
        return InjectStatus(busy=bool(_field(status, g.XTRIG_INJECT_STATUS_BUSY)),
                            result=("ok", "swd_error", "not_connected", "absent")[_field(status, g.XTRIG_INJECT_STATUS_RESULT)],
                            ack=_field(status, g.XTRIG_INJECT_STATUS_ACK),
                            count=_field(status, g.XTRIG_INJECT_STATUS_COUNT), time=hi << 32 | lo)

    def inject_config(self) -> tuple[int, int, int]:
        """割り込み書き込みの中身のレジスタの読み戻し: (AP, 番地, 値)。"""
        ap, address, value = self.dev.read(g.XTRIG_INJECT_AP, 3)
        return _field(ap, (0, 8)), address, value

    def match_config(self) -> dict:
        """番地一致の条件のレジスタの読み戻し（set_match の引数と同じ名前）。"""
        address, address_mask, data, data_mask, mode = self.dev.read(g.XTRIG_MATCH_ADDR, 5)
        return {"address": address, "address_mask": address_mask, "data": data, "data_mask": data_mask,
                "on_write": bool(_field(mode, g.XTRIG_MATCH_MODE_ON_WRITE)),
                "on_read": bool(_field(mode, g.XTRIG_MATCH_MODE_ON_READ))}

    def match_status(self) -> tuple[int, int]:
        """(一致した回数, 最後に一致した時のタイムベース)。"""
        count, lo, hi = self.dev.read(g.XTRIG_MATCH_COUNT, 3)
        return count, hi << 32 | lo


# --- 多段トリガ ---------------------------------------------------------------------------

class Event(IntFlag):
    """多段トリガの段の条件（いずれかで成立）。"""
    PATTERN_A = 1
    PATTERN_B = 2
    DAP_MATCH = 4       # CrossTrigger.set_match の条件
    HOST = 8            # CrossTrigger.fire()


@dataclass(frozen=True)
class Pattern:
    """ピン（ロジアナのチャンネル）の条件。値一致かつ（エッジ指定があれば）どれかがその向きに変化。"""
    mask: int = 0
    value: int = 0
    rise: int = 0
    fall: int = 0

    def __post_init__(self):
        for name in ("mask", "value", "rise", "fall"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 0xFFFF:
                raise ValueError(f"Pattern.{name} は 0〜0xFFFF の整数（ロジアナの ch 0〜15。ch 16 以上は指定できない）: {v!r}")


@dataclass(frozen=True)
class Stage:
    when: Event
    count: int = 1                      # この回数で次の段へ
    within_s: float | None = None       # 段に入ってからこの時間以内に進めなければ最初から（段 0 では無視）


class TriggerSequencer:
    """多段トリガ: 例 「A の後 10 µs 以内に B」= [Stage(Event.PATTERN_A), Stage(Event.PATTERN_B, within_s=10e-6)]。
    発火は CrossTrigger の源 Source.SEQUENCE（route でロジアナのトリガ・割り込み書き込みへつなぐ）。単発。"""

    def __init__(self, dev: Device):
        self.dev = dev
        self.clock_hz = dev.capabilities().timebase_hz

    def configure(self, stages: list[Stage], a: Pattern = Pattern(), b: Pattern = Pattern()) -> None:
        if not 1 <= len(stages) <= 4:
            raise ValueError("段は 1〜4")
        dev = self.dev
        dev.write32(g.TRIGSEQ_CONTROL, 0)
        dev.write(g.TRIGSEQ_PATTERN_A, [a.mask | a.value << 16, a.rise | a.fall << 16,
                                        b.mask | b.value << 16, b.rise | b.fall << 16])
        words, withins = [], []
        for k in range(4):
            st = stages[k] if k < len(stages) else Stage(Event(0))
            if k < len(stages) and not int(st.when):
                raise ValueError(f"段 {k} の条件が空（成立する条件が無く、永久に進まない）")
            if not 1 <= st.count <= 0xFFFF:
                raise ValueError("count は 1〜65535")
            words.append(int(st.when) | st.count << 16)
            if k:
                if st.within_s is not None and not st.within_s > 0:
                    raise ValueError(f"within_s は正の数（制限なしは None）: {st.within_s!r}")
                cycles = 0 if st.within_s is None else max(1, round(st.within_s * self.clock_hz))
                if cycles >= 1 << 32:
                    raise ValueError("within_s が長すぎる")
                withins.append(cycles)
        dev.write(g.TRIGSEQ_STAGE0, words + withins)
        dev.write32(g.TRIGSEQ_CONTROL, len(stages) << g.TRIGSEQ_CONTROL_STAGES[0])

    def arm(self) -> None:
        """段 0 から待つ（発火後に再び使うときも）。"""
        control = self.dev.read32(g.TRIGSEQ_CONTROL) & ~1
        self.dev.write32(g.TRIGSEQ_CONTROL, control)
        self.dev.write32(g.TRIGSEQ_CONTROL, control | 1)

    def disarm(self) -> None:
        self.dev.write32(g.TRIGSEQ_CONTROL, self.dev.read32(g.TRIGSEQ_CONTROL) & ~1)

    def config(self, stages: int) -> tuple[list[Stage], Pattern, Pattern]:
        """段（使う stages 段）とピンの条件 A・B のレジスタの読み戻し（configure の逆）。count の 0 は回路で 1。"""
        pa, ea, pb, eb = self.dev.read(g.TRIGSEQ_PATTERN_A, 4)
        words = self.dev.read(g.TRIGSEQ_STAGE0, 7)
        out = []
        for k in range(min(stages, 4)):
            word = words[k]
            cycles = words[4 + k - 1] if k else 0
            out.append(Stage(Event(_field(word, g.TRIGSEQ_STAGE0_COND)), max(1, _field(word, g.TRIGSEQ_STAGE0_COUNT)),
                             cycles / self.clock_hz if cycles else None))

        def pattern(values: int, edges: int) -> Pattern:
            return Pattern(_field(values, g.TRIGSEQ_PATTERN_A_MASK), _field(values, g.TRIGSEQ_PATTERN_A_VALUE),
                           _field(edges, g.TRIGSEQ_EDGE_A_RISE), _field(edges, g.TRIGSEQ_EDGE_A_FALL))
        return out, pattern(pa, ea), pattern(pb, eb)

    def control(self) -> tuple[bool, int]:
        """(仕掛けているか, 設定した段の数)。"""
        control = self.dev.read32(g.TRIGSEQ_CONTROL)
        return bool(_field(control, g.TRIGSEQ_CONTROL_ENABLE)), _field(control, g.TRIGSEQ_CONTROL_STAGES)

    def status(self) -> tuple[int, bool, int]:
        """(今の段, 発火した, 発火した時のタイムベース)。"""
        status = self.dev.read32(g.TRIGSEQ_STATUS)
        lo, hi = self.dev.read(g.TRIGSEQ_FIRE_TIME_LO, 2)
        return (_field(status, g.TRIGSEQ_STATUS_STAGE), bool(_field(status, g.TRIGSEQ_STATUS_FIRED)), hi << 32 | lo)


# --- クロストリガの動作「出力の端子」 -----------------------------------------------------------
# 仕様: docs/superpowers/specs/2026-10-18-u41-xtrig-pin-output-design.md

class OutputMode(IntEnum):
    PULSE = 0       # 条件の立ち上がりで決めた長さの脈
    HOLD = 1        # 条件の立ち上がりから、無効にするまで
    WHILE = 2       # 条件が成り立つ間（遅れ 4 周期）


MAX_PULSE_LENGTH = 255
MAX_PULSE_UNIT = 7


@dataclass(frozen=True)
class OutputConfig:
    """出力 1 つの設定。条件 = 端子の並び（mask・value・any。ロジアナの ch 0〜7）or 源（sources）。"""
    mode: OutputMode = OutputMode.WHILE
    mask: int = 0               # 見る ch（0 なら並びは使わない）
    value: int = 0
    any: bool = False           # True: 見る ch のどれかが一致で成立、False: すべて一致で成立
    sources: Source = Source(0)
    high: bool = True           # 動いている間に出す値
    length: int = 1             # 脈: 刻みの数（1〜255）
    unit: int = 0               # 脈: 刻み = 16^unit 周期
    claim: bool = True          # 端子を出力に使う（その端子の普段の機能を止める）

    def __post_init__(self):
        if not (0 <= self.mask <= 0xFF and 0 <= self.value <= 0xFF):
            raise ValueError("mask・value はロジアナの ch 0〜7 のビット（0〜0xFF）")
        if not 1 <= self.length <= MAX_PULSE_LENGTH or not 0 <= self.unit <= MAX_PULSE_UNIT:
            raise ValueError("脈の長さは 1〜255 刻み、刻みは 16^0〜16^7 周期")


@dataclass(frozen=True)
class ActionState:
    """出力の動作 1 本の状態。pin は端子の候補の番号（ケイパビリティ outputs の index）。"""
    index: int
    pin: int
    enabled: bool
    active: bool                # 端子を駆動している
    config: OutputConfig        # 読み戻した設定（定義の外の値は丸めてある。読んだ値そのものは mode_raw・length_raw）
    mode_raw: int | None = None
    length_raw: int | None = None

    @property
    def in_use(self) -> bool:
        """端子を握っているか有効（ホストが割り当て済み）。"""
        return self.enabled or self.config.claim


def pulse_ticks(seconds: float, clock_hz: int) -> tuple[int, int]:
    """脈の長さ（秒）→ (length, unit)。length ≤ 255 になる最小の刻みで、長さ以上にする（unit ≥ 1 では
    最初の刻みの位置の分だけ短くなりうるので、実際は ((length − 1)·16^unit, length·16^unit] 周期）。"""
    if not seconds > 0:
        raise ValueError(f"脈の長さは正の数（秒）: {seconds!r}")
    cycles = max(1, round(seconds * clock_hz))
    for unit in range(MAX_PULSE_UNIT + 1):
        tick = 16 ** unit
        length = -(-cycles // tick)
        if length <= MAX_PULSE_LENGTH:
            return length, unit
    raise ValueError(f"脈が長すぎる（最長 {MAX_PULSE_LENGTH * 16 ** MAX_PULSE_UNIT / clock_hz:.0f} 秒）")


def pulse_range_s(length: int, unit: int, clock_hz: int) -> tuple[float, float]:
    """設定した脈の実際の長さの範囲（秒）。"""
    tick = 16 ** unit
    low = length * tick if unit == 0 else (length - 1) * tick + 1
    return low / clock_hz, length * tick / clock_hz


def _when_word(cfg: OutputConfig) -> int:
    return (cfg.mask << g.XTRIG_OUT_WHEN0_MASK[0] | cfg.value << g.XTRIG_OUT_WHEN0_VALUE[0]
            | int(cfg.sources) << g.XTRIG_OUT_WHEN0_SOURCES[0] | int(cfg.any) << g.XTRIG_OUT_WHEN0_ANY[0])


def _ctrl_word(cfg: OutputConfig, enable: bool, pin: int) -> int:
    return (int(enable) << g.XTRIG_OUT_CTRL0_ENABLE[0] | int(cfg.mode) << g.XTRIG_OUT_CTRL0_MODE[0]
            | int(cfg.high) << g.XTRIG_OUT_CTRL0_LEVEL[0] | int(cfg.claim) << g.XTRIG_OUT_CTRL0_CLAIM[0]
            | cfg.length << g.XTRIG_OUT_CTRL0_LENGTH[0] | cfg.unit << g.XTRIG_OUT_CTRL0_UNIT[0]
            | pin << g.XTRIG_OUT_CTRL0_PIN[0])


class PinOutputs:
    """クロストリガの動作「出力の端子」（CSR xtrig_out）。動作 k（同時に actions 本）が、端子の候補 pins[j]
    （ケイパビリティ outputs。ロジアナの ch とプルアップの有無）の 1 本を動かす。"""

    STRIDE = g.XTRIG_OUT_WHEN1 - g.XTRIG_OUT_WHEN0

    def __init__(self, dev: Device):
        self.dev = dev
        caps = dev.capabilities()
        self.pins = tuple(getattr(caps, "outputs", ()))
        self.actions = getattr(caps, "output_actions", 0) if self.pins else 0

    def _check(self, action: int) -> None:
        if not 0 <= action < self.actions:
            raise ValueError(f"出力の動作 {action} は無い（この構成の動作は {self.actions} 本）")

    def _addresses(self, action: int) -> tuple[int, int]:
        when = g.XTRIG_OUT_WHEN0 + self.STRIDE * action
        return when, when + (g.XTRIG_OUT_CTRL0 - g.XTRIG_OUT_WHEN0)

    def configure(self, action: int, pin: int, cfg: OutputConfig, *, enable: bool = False) -> None:
        """無効にしてから条件・動作を書く（書き替えの途中の設定で動かない）。enable で続けて有効にする。"""
        self._check(action)
        if not 0 <= pin < len(self.pins):
            raise ValueError(f"端子の候補 {pin} は無い（{len(self.pins)} 本）")
        when, ctrl = self._addresses(action)
        self.dev.write32(ctrl, _ctrl_word(cfg, False, pin))
        self.dev.write32(when, _when_word(cfg))
        if enable:
            self.dev.write32(ctrl, _ctrl_word(cfg, True, pin))

    def _set_enable(self, action: int, enable: bool) -> None:
        self._check(action)
        _, ctrl = self._addresses(action)
        bit = 1 << g.XTRIG_OUT_CTRL0_ENABLE[0]
        word = self.dev.read32(ctrl)
        self.dev.write32(ctrl, word | bit if enable else word & ~bit)

    def enable(self, action: int) -> None:
        self._set_enable(action, True)

    def disable(self, action: int) -> None:
        """無効にする（保持も戻る）。claim は残る（端子は高インピーダンス）。"""
        self._set_enable(action, False)

    def release(self, action: int) -> None:
        """無効にし、条件を消し、claim を外す（端子を普段の機能に戻し、動作を空ける）。"""
        self._check(action)
        when, ctrl = self._addresses(action)
        self.dev.write32(ctrl, _ctrl_word(OutputConfig(claim=False), False, 0))
        self.dev.write32(when, 0)

    def status(self) -> list[ActionState]:
        if not self.actions:
            return []
        words = self.dev.read(g.XTRIG_OUT_STATUS, 1 + 2 * self.actions)
        active = _field(words[0], g.XTRIG_OUT_STATUS_ACTIVE)
        states = []
        for k in range(self.actions):
            when, ctrl = words[1 + 2 * k], words[2 + 2 * k]
            mode_raw, length_raw = _field(ctrl, g.XTRIG_OUT_CTRL0_MODE), _field(ctrl, g.XTRIG_OUT_CTRL0_LENGTH)
            cfg = OutputConfig(
                mode=OutputMode(min(mode_raw, 2)),
                mask=_field(when, g.XTRIG_OUT_WHEN0_MASK), value=_field(when, g.XTRIG_OUT_WHEN0_VALUE),
                any=bool(_field(when, g.XTRIG_OUT_WHEN0_ANY)), sources=Source(_field(when, g.XTRIG_OUT_WHEN0_SOURCES)),
                high=bool(_field(ctrl, g.XTRIG_OUT_CTRL0_LEVEL)),
                length=max(1, length_raw), unit=_field(ctrl, g.XTRIG_OUT_CTRL0_UNIT),
                claim=bool(_field(ctrl, g.XTRIG_OUT_CTRL0_CLAIM)))
            states.append(ActionState(k, _field(ctrl, g.XTRIG_OUT_CTRL0_PIN),
                                      bool(_field(ctrl, g.XTRIG_OUT_CTRL0_ENABLE)), bool(active >> k & 1), cfg,
                                      mode_raw, length_raw))
        return states

    def action_for(self, pin: int, states: list[ActionState] | None = None, *, allocate: bool) -> int | None:
        """端子の候補 pin を受け持つ動作。無ければ allocate のとき空いている動作（無ければ誤り）、そうでなければ None。"""
        states = self.status() if states is None else states
        for st in states:
            if st.in_use and st.pin == pin:
                return st.index
        if not allocate:
            return None
        for st in states:
            if not st.in_use:
                return st.index
        used = ", ".join(f"ch{self.pins[st.pin].channel}" for st in states if st.pin < len(self.pins))
        raise RuntimeError(f"出力の動作は同時に {self.actions} 本まで（使用中: {used}）。どれかを release する")
