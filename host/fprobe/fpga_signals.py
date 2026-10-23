"""FPGA の観測の組（16 ビット）に付ける信号の定義: 名前・ビットの範囲・書式・列挙の名前
（docs/superpowers/specs/2026-10-20-fpga-observation-ui-design.md §3）。

記録には組の生の値だけを入れ、この定義は記録の付帯情報（meta の fpga.signals）と画面の状態（ui.state の
fpga_signals）に置く。時間軸の段は見るときにこの定義から作るので、後から付けた名前も過去の記録に効く。

形: {"groups": {"<組>": [{"name", "lsb", "width", "format": "hex" | "dec" | "bin" | "enum", "enum": {"<値>": "<名前>"}}]}}
"""

from dataclasses import dataclass, field

__all__ = ["FORMATS", "FpgaSignals", "Signal", "WIDTH"]

WIDTH = 16                                        # 観測の回路の組の幅
FORMATS = ("hex", "dec", "bin", "enum")
NAME_MAX = 64


@dataclass(frozen=True)
class Signal:
    name: str
    lsb: int
    width: int
    format: str = "hex"
    enum: dict[int, str] = field(default_factory=dict)

    @property
    def mask(self) -> int:
        return (1 << self.width) - 1

    def extract(self, value: int) -> int:
        return value >> self.lsb & self.mask


@dataclass(frozen=True)
class FpgaSignals:
    groups: dict[int, tuple[Signal, ...]]

    @classmethod
    def from_dict(cls, d: dict | None) -> "FpgaSignals":
        """検査して作る（名前の重複・ビットの重なり・16 ビットの外・列挙の値が幅の外・知らない書式は ValueError）。"""
        groups_in = (d or {}).get("groups", {})
        if not isinstance(groups_in, dict):
            raise ValueError("groups は {組: 信号の並び}")
        groups: dict[int, tuple[Signal, ...]] = {}
        for key, items in groups_in.items():
            try:
                g = int(key)
            except (TypeError, ValueError):
                raise ValueError(f"組の番号 {key!r} が整数でない") from None
            if not 0 <= g <= 255:
                raise ValueError(f"組の番号 {g} は 0〜255")
            if not isinstance(items, list):
                raise ValueError(f"組 {g} の信号は並び")
            signals, used, names = [], 0, set()
            for item in items:
                s = _signal(g, item)
                if s.name in names:
                    raise ValueError(f"組 {g} の信号の名前 {s.name!r} が重複")
                bits = s.mask << s.lsb
                if used & bits:
                    raise ValueError(f"組 {g} の信号 {s.name!r} のビットがほかの信号と重なる")
                used |= bits
                names.add(s.name)
                signals.append(s)
            groups[g] = tuple(signals)
        return cls(groups)

    def to_dict(self) -> dict:
        return {"groups": {str(g): [{"name": s.name, "lsb": s.lsb, "width": s.width, "format": s.format,
                                     "enum": {str(k): v for k, v in sorted(s.enum.items())}} for s in signals]
                           for g, signals in sorted(self.groups.items())}}

    def signals(self, group: int) -> tuple[Signal, ...]:
        return self.groups.get(group, ())

    def text(self, s: Signal, v: int) -> str:
        """信号の値の文字（書式どおり。名前の無い列挙の値は 16 進）。"""
        if s.format == "enum" and v in s.enum:
            return s.enum[v]
        if s.format == "dec":
            return str(v)
        if s.format == "bin":
            return f"0b{v:0{s.width}b}"
        return f"0x{v:X}"

    def fields(self, group: int, value: int) -> dict[str, int | str]:
        """組の値を信号ごとに（列挙は名前、ほかは数）。定義の無い組は {"value": 組の値}。"""
        signals = self.signals(group)
        if not signals:
            return {"value": value}
        out: dict[str, int | str] = {}
        for s in signals:
            v = s.extract(value)
            out[s.name] = self.text(s, v) if s.format == "enum" else v
        return out


def _signal(group: int, item: dict) -> Signal:
    if not isinstance(item, dict):
        raise ValueError(f"組 {group} の信号は辞書")
    name = item.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > NAME_MAX:
        raise ValueError(f"組 {group} の信号の名前は 1〜{NAME_MAX} 文字")
    name = name.strip()
    lsb, width = item.get("lsb"), item.get("width")
    if not isinstance(lsb, int) or not isinstance(width, int) or lsb < 0 or width < 1 or lsb + width > WIDTH:
        raise ValueError(f"組 {group} の信号 {name!r} のビット（lsb {lsb}、幅 {width}）が {WIDTH} ビットに入らない")
    fmt = item.get("format", "hex")
    if fmt not in FORMATS:
        raise ValueError(f"組 {group} の信号 {name!r} の書式 {fmt!r} は {' / '.join(FORMATS)} のどれか")
    enum_in = item.get("enum") or {}
    if not isinstance(enum_in, dict):
        raise ValueError(f"組 {group} の信号 {name!r} の列挙は {{値: 名前}}")
    enum: dict[int, str] = {}
    for k, v in enum_in.items():
        try:
            n = int(k, 0) if isinstance(k, str) else int(k)
        except ValueError:
            raise ValueError(f"組 {group} の信号 {name!r} の列挙の値 {k!r} が整数でない") from None
        if not 0 <= n < 1 << width:
            raise ValueError(f"組 {group} の信号 {name!r} の列挙の値 {n} が幅 {width} ビットの外")
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"組 {group} の信号 {name!r} の列挙の名前が空")
        enum[n] = v.strip()
    return Signal(name, lsb, width, fmt, enum)
