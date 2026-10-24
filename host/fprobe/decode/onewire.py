"""1-Wire の解読（リンク層: リセット・プレゼンス・ビット、ネットワーク層: ROM コマンド・ROM・データ）。
仕様: docs/superpowers/specs/2026-10-18-u20-decoders-design.md

線は休止で 1（プルアップ）。主が線を 0 にする長さで区別する: 標準速度ではサンプル点（15 µs）より短い 0 は 1、
長い 0（〜120 µs）は 0、480 µs 前後はリセット。リセットを離した直後の 0 はスレーブのプレゼンス。
オーバードライブ（OD Skip ROM 0x3C・OD Match ROM 0x69 の後）は 1/8 ほどの時間。標準のリセットで標準に戻る。
"""

from ..la import Transitions
from .core import Annotation, Channel, ChannelLevels, Decoding

__all__ = ["ROM_COMMANDS", "decode_onewire", "onewire_crc8"]

# 速度ごとの時間（µs）の区別。規格の値（経験の余裕を足さない）: ビットのサンプル点 = tRDV、ビットの 0 の最長 = tLOW0 の最大、
# リセットの最短 = tRSTL の最小、プレゼンスを待つ最長 = tPDHIGH の最大 + tPDLOW の最大、スロットの長さ = tSLOT の最小。
# 出典: 1-Wire の時間の定義 = Maxim AN937「Book of iButton Standards」の 1-Wire の信号の時間（標準: tRDV 15・tLOW0 60〜120・
# tRSTL ≥ 480・tPDH 15〜60・tPDL 60〜240・tSLOT 60〜120、オーバードライブ: tRDV 2・tLOW0 6〜16・tRSTL 48〜80・tPDH 2〜6・
# tPDL 8〜24・tSLOT 6〜16）。Maxim AN126「1-Wire Communication Through Software」の主の推奨の時間（標準: 読みのサンプル点
# A + E = 15・リセット H = 480、オーバードライブ: A + E = 2・H = 70）もこの範囲の中。個々の機器のデータシートの値ではない。
# 規格の外のパルスは "timing" の誤り（長さ low_us を付ける）。別の値で区別するときは decode_onewire の timing で明示する
TIMING = {False: {"sample": 15.0, "bit_max": 120.0, "reset_min": 480.0, "presence_window": 300.0, "slot": 60.0},
          True: {"sample": 2.0, "bit_max": 16.0, "reset_min": 48.0, "presence_window": 30.0, "slot": 6.0}}
ROM_COMMANDS = {0x33: "READ ROM", 0x55: "MATCH ROM", 0xCC: "SKIP ROM", 0xF0: "SEARCH ROM", 0xEC: "ALARM SEARCH",
                0x3C: "OVERDRIVE SKIP ROM", 0x69: "OVERDRIVE MATCH ROM", 0xA5: "RESUME"}
_TO_OVERDRIVE = (0x3C, 0x69)


def onewire_crc8(data) -> int:
    """Dallas/Maxim の CRC-8（x^8 + x^5 + x^4 + 1、LSB から）。"""
    crc = 0
    for byte in data:
        for _ in range(8):
            mix = (crc ^ byte) & 1
            crc >>= 1
            if mix:
                crc ^= 0x8C
            byte >>= 1
    return crc


def _rom_text(rom: int) -> str:
    return f"{rom & 0xFF:02X}-{rom >> 8 & 0xFFFF_FFFF_FFFF:012X}-{rom >> 56:02X}"


class _Net:
    """ネットワーク層の状態（リセットごとに ROM コマンドから）。"""

    def __init__(self, out: list[Annotation]):
        self.out = out
        self.phase = "idle"              # idle / rom_command / rom / search / data
        self.bits: list[tuple[int, int, int]] = []     # (値, 始まり, 終わり)
        self.command = None
        self.search_triplet: list[int] = []
        self.search_rom = 0
        self.search_n = 0
        self.search_start = 0
        self.search_error: str | None = None

    def reset(self) -> None:
        self.phase, self.bits, self.command = "rom_command", [], None

    def bit(self, value: int, start: int, end: int) -> bool:
        """1 ビット。オーバードライブへ移るなら True。"""
        if self.phase == "idle":
            self.phase = "data"                      # リセットを見ずに始まった: 機能のデータとして読む
        if self.phase == "search":
            return self._search(value, start, end)
        self.bits.append((value, start, end))
        need = 64 if self.phase == "rom" else 8
        if len(self.bits) < need:
            return False
        number = sum(b << i for i, (b, _, _) in enumerate(self.bits))
        s, e = self.bits[0][1], self.bits[-1][2]
        self.bits = []
        if self.phase == "rom_command":
            name = ROM_COMMANDS.get(number)
            self.out.append(Annotation(s, e, "rom_command", name or f"0x{number:02X}", value=number,
                                       fields={"name": name}))
            self.command = number
            if number in (0x33, 0x55, 0x69):
                self.phase = "rom"
            elif number in (0xF0, 0xEC):
                self.phase, self.search_rom, self.search_n, self.search_triplet = "search", 0, 0, []
                self.search_error = None
            else:
                self.phase = "data"
            return number in _TO_OVERDRIVE
        if self.phase == "rom":
            self._rom(number, s, e, search=False)
            self.phase = "data"
            return False
        self.out.append(Annotation(s, e, "data", f"0x{number:02X}", value=number))
        return False

    def _search(self, value: int, start: int, end: int) -> bool:
        if not self.search_triplet and self.search_n == 0:
            self.search_start = start
        self.search_triplet.append(value)
        if len(self.search_triplet) < 3:
            return False
        bit, complement, chosen = self.search_triplet
        self.search_triplet = []
        if bit and complement and self.search_error is None:
            self.search_error = "no_device"          # どのスレーブも応えない
        elif bit != complement and chosen != bit and self.search_error is None:
            self.search_error = "direction"          # 全員が同じ値なのに主が別の向きを選んだ
        self.search_rom |= chosen << self.search_n
        self.search_n += 1
        if self.search_n == 64:
            self._rom(self.search_rom, self.search_start, end, search=True, error=self.search_error)
            self.phase = "data"
        return False

    def _rom(self, rom: int, start: int, end: int, *, search: bool, error: str | None = None) -> None:
        data = rom.to_bytes(8, "little")
        crc_ok = onewire_crc8(data[:7]) == data[7]
        self.out.append(Annotation(start, end, "rom", "ROM " + _rom_text(rom), value=rom,
                                   error=error or (None if crc_ok else "crc"),
                                   fields={"family": data[0], "serial": int.from_bytes(data[1:7], "little"),
                                           "crc": data[7], "crc_ok": crc_ok, "search": search,
                                           "command": self.command}))


def decode_onewire(tr: Transitions, channel: Channel, *, overdrive: bool, timing: dict | None = None) -> Decoding:
    """1-Wire を解読する。overdrive は捕捉の始まりの速度（既定を持たない。ROM コマンドで切り替わるのは自動で追う）。timing: 区別の時間
    {False: 標準, True: オーバードライブ}（省略は規格の値 TIMING。使った値は params に入る）。"""
    timing = TIMING if timing is None else timing
    lv = ChannelLevels(tr, channel)
    us = 1e-6 / tr.sample_period_s                  # 1 µs のサンプル数
    out: list[Annotation] = []
    net = _Net(out)
    edges = list(lv.edges)
    level = lv.initial
    if level == 0 and edges:                        # 捕捉の始まりが 0 の途中: その 0 は読まない
        edges, level = edges[1:], 1
    pulses = [(edges[i], edges[i + 1] if i + 1 < len(edges) else None) for i in range(0, len(edges), 2)]
    od = overdrive
    presence_until = None                           # リセットを離してからプレゼンスを待つ期限
    for k, (fall, rise) in enumerate(pulses):
        nxt = pulses[k + 1][0] if k + 1 < len(pulses) else tr.length
        if rise is None:
            out.append(Annotation(fall, tr.length, "bit", "?", error="truncated"))
            break
        low = (rise - fall) / us
        t = timing[od]
        if presence_until is not None and fall <= presence_until:
            out.append(Annotation(fall, rise, "presence", "presence", value=1))
            out[reset_index] = _with_presence(out[reset_index], True)
            presence_until = None
            continue
        presence_until = None
        if low >= timing[False]["reset_min"] or (od and low >= t["reset_min"]):
            if low >= timing[False]["reset_min"]:
                od = False                          # 標準のリセットは標準の速度に戻す
            out.append(Annotation(fall, rise, "reset", "RESET" + (" (OD)" if od else ""),
                                  fields={"overdrive": od, "presence": False, "low_us": round(low, 2)}))
            reset_index = len(out) - 1
            presence_until = rise + timing[od]["presence_window"] * us
            net.reset()
            continue
        if low > t["bit_max"]:
            out.append(Annotation(fall, rise, "bit", "?", error="timing", fields={"low_us": round(low, 2)}))
            continue
        value = int(low < t["sample"])
        end = max(rise, min(nxt, fall + round(t["slot"] * us)))
        out.append(Annotation(fall, max(end, fall + 1), "bit", str(value), value=value,
                              fields={"low_us": round(low, 2), "overdrive": od}))
        if net.bit(value, fall, max(end, fall + 1)):
            od = True
    return Decoding("onewire", tuple(out), tr.sample_period_s, tr.trigger, tr.length,
                    {"channel": channel, "overdrive": overdrive, "timing": timing})


def _with_presence(a: Annotation, presence: bool) -> Annotation:
    return Annotation(a.start, a.end, a.kind, a.text, a.value, a.error, {**a.fields, "presence": presence})
