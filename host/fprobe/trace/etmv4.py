"""ETMv4 の命令トレースのパケットの解読（Arm IHI 0064 ETMv4 Architecture Specification）。

対象は M-profile（Cortex-M33 の ETM-M33 = ETMv4.2 等）の命令トレース: データトレース・投機（commit / cancel /
mispredict）・条件命令のトレース・Q パケットは M-profile に無いので、来れば解けないバイトとして扱い次の A-sync まで
読み飛ばす。リターンスタック（TRCCONFIGR.RS）・分岐の全出力（BB）は再構成の側で使わない前提（fprobe が設定する
ときは切る。coresight.Etmv4Setup）。

    items = Etmv4Decoder(Etmv4Config()).decode(data)      # 共通の要素（fprobe.trace.elements）の並び

ヘッダ（1 バイト目）の割り当て:
  0x00 拡張（0x00×11 + 0x80 = A-sync、0x00 0x03 = Discard、0x00 0x05 = Overflow）、0x01 Trace Info、
  0x02 / 0x03 Timestamp（0x03 はサイクル数付き）、0x04 Trace On、0x05 Function Return、0x06 Exception、
  0x07 Exception Return、0x0C〜0x0F・0x10〜0x1F Cycle Count（形式 2・1・3）、0x70 Ignore、0x71〜0x7F Event、
  0x80 / 0x81 Context、0x82 / 0x83 / 0x85 / 0x86 Address with Context（32 / 64 ビット、IS0 / IS1）、
  0x90〜0x92 Exact Match Address、0x94 / 0x95 Short Address、0x9A / 0x9B / 0x9D / 0x9E Long Address、
  0xC0〜0xFF Atom（形式 1〜6）。
"""

from dataclasses import dataclass, replace

from . import elements as el

__all__ = ["Etmv4Config", "Etmv4Decoder"]

_ASYNC_ZEROS = 11


@dataclass(frozen=True)
class Etmv4Config:
    """解読に要る ETM の構成（TRCIDRn から。fprobe.trace.coresight.Etmv4Info.decoder_config）。
    commit_opt1: TRCIDR0.COMMOPT = 1（Cycle Count 形式 1 にコミットの欄が無い）。
    vmid_bytes / cid_bytes: Context の VMID・Context ID の大きさ（TRCIDR2。M-profile では出ない）。"""
    commit_opt1: bool = True
    vmid_bytes: int = 1
    cid_bytes: int = 4


class _Short(Exception):
    """バイト列の終わりでパケットが切れた。"""


class _Bad(Exception):
    """解けないヘッダ。"""


class _Reader:
    def __init__(self, data: bytes, start: int):
        self.data, self.start, self.pos = data, start, start

    def byte(self) -> int:
        if self.pos >= len(self.data):
            raise _Short
        b = self.data[self.pos]
        self.pos += 1
        return b

    def cont(self, limit: int = 5) -> int:
        """続きの印（ビット 7）で伸びる、下位から 7 ビットずつの値。"""
        value = 0
        for n in range(limit):
            b = self.byte()
            value |= (b & 0x7F) << (7 * n)
            if not b & 0x80:
                return value
        raise _Bad("続きの印の付いた値が長すぎる")

    @property
    def raw(self) -> bytes:
        return bytes(self.data[self.start:self.pos])


class Etmv4Decoder:
    def __init__(self, config: Etmv4Config | None = None):
        self.config = config or Etmv4Config()

    def decode(self, data: bytes) -> list[el.Element]:
        data = bytes(data)
        self._out: list[el.Element] = []
        self._stack: list[tuple[int, str]] = [(0, "thumb")] * 3          # アドレスの履歴（0 が最新）
        self._timestamp = 0
        self._threshold = 0
        self._pending: el.ExceptionEntry | None = None                    # 戻り先のアドレスを待つ例外
        synced = False
        i = 0
        while i < len(data):
            if not synced:
                j = self._find_async(data, i)
                if j is None:
                    break
                self._emit(el.Async(offset=j, raw=data[j:j + _ASYNC_ZEROS + 1], skipped=j - i))
                i, synced = j + _ASYNC_ZEROS + 1, True
                self._stack = [(0, "thumb")] * 3
                continue
            if data[i] == 0x00 and i + 1 < len(data) and data[i + 1] == 0x00:
                synced = False                                            # A-sync（前に余分な 0 があってもよい）
                continue
            reader = _Reader(data, i)
            try:
                items = self._packet(reader)
            except _Short:
                items, i = [el.Truncated(offset=i, raw=data[i:])], len(data)
            except _Bad as e:
                items, i, synced = [el.DecodeError(offset=i, raw=data[i:i + 1], message=str(e))], i + 1, False
            else:
                i = reader.pos
            for item in items:
                self._emit(item)
        if self._pending is not None:
            self._out.append(self._pending)
        return self._out

    def _emit(self, item: el.Element) -> None:
        """要素を出す。Exception のパケットは、続く Address を戻り先として 1 つの要素にまとめる。"""
        if self._pending is not None:
            pending, self._pending = self._pending, None
            if isinstance(item, el.Address):
                self._out.append(replace(pending, raw=pending.raw + item.raw, return_address=item.address))
                return
            self._out.append(pending)
        if isinstance(item, el.ExceptionEntry):
            self._pending = item
            return
        self._out.append(item)

    @staticmethod
    def _find_async(data: bytes, start: int) -> int | None:
        """start から、11 個以上の 0 に続く 0x80 を探し、A-sync の先頭（0x80 の 11 バイト前）を返す。"""
        zeros = 0
        for j in range(start, len(data)):
            b = data[j]
            if b == 0:
                zeros += 1
            elif b == 0x80 and zeros >= _ASYNC_ZEROS:
                return j - _ASYNC_ZEROS
            else:
                zeros = 0
        return None

    # --- パケット -------------------------------------------------------------------------

    def _packet(self, r: _Reader) -> list[el.Element]:
        return [self._one(r)] if not self._with_context(r) else self._address_with_context(r)

    def _with_context(self, r: _Reader) -> bool:
        return r.data[r.start] in (0x82, 0x83, 0x85, 0x86)

    def _address_with_context(self, r: _Reader) -> list[el.Element]:
        h = r.byte()
        isa = "thumb" if h in (0x83, 0x86) else "arm"
        address = self._long(r, isa, wide=h in (0x85, 0x86))
        item = el.Address(offset=r.start, raw=r.raw, address=address, isa=isa, form="context")
        return [item, self._context(r, r.pos, payload=True)]

    def _one(self, r: _Reader) -> el.Element:
        h = r.byte()
        o = r.start
        if h == 0x00:
            ext = r.byte()
            if ext == 0x03:
                return el.Discard(offset=o, raw=r.raw)
            if ext == 0x05:
                return el.Overflow(offset=o, raw=r.raw)
            raise _Bad(f"知らない拡張のパケット 0x00 0x{ext:02X}")
        if h == 0x01:
            return self._trace_info(r)
        if h in (0x02, 0x03):
            return self._timestamp_packet(r, with_cycles=h == 0x03)
        if h == 0x04:
            return el.TraceOn(offset=o, raw=r.raw)
        if h == 0x05:
            return el.FunctionReturn(offset=o, raw=r.raw)
        if h == 0x06:
            return self._exception(r)
        if h == 0x07:
            return el.ExceptionReturn(offset=o, raw=r.raw)
        if h in (0x0E, 0x0F):                                             # Cycle Count 形式 1
            if not self.config.commit_opt1:
                r.cont()                                                  # コミットの数（M-profile では使わない）
            count = r.cont(3) if h == 0x0E else None
            return el.CycleCount(offset=o, raw=r.raw, count=count)
        if h in (0x0C, 0x0D):                                             # 形式 2: 下位 4 ビット + 閾値
            b = r.byte()
            return el.CycleCount(offset=o, raw=r.raw, count=self._threshold + (b & 0x0F))
        if 0x10 <= h <= 0x1F:                                             # 形式 3: 下位 2 ビット + 閾値
            return el.CycleCount(offset=o, raw=r.raw, count=self._threshold + (h & 0x03))
        if h == 0x70:
            return el.Ignore(offset=o, raw=r.raw)
        if 0x71 <= h <= 0x7F:
            return el.Event(offset=o, raw=r.raw, events=h & 0x0F)
        if h in (0x80, 0x81):
            return self._context(r, o, payload=h == 0x81)
        if 0x90 <= h <= 0x92:
            address, isa = self._stack[h & 0x03]
            self._push(address, isa)
            return el.Address(offset=o, raw=r.raw, address=address, isa=isa, form="exact")
        if h in (0x94, 0x95):
            isa = "thumb" if h == 0x95 else "arm"
            return el.Address(offset=o, raw=r.raw, address=self._short(r, isa), isa=isa, form="short")
        if h in (0x9A, 0x9B, 0x9D, 0x9E):
            isa = "thumb" if h in (0x9B, 0x9E) else "arm"
            address = self._long(r, isa, wide=h in (0x9D, 0x9E))
            return el.Address(offset=o, raw=r.raw, address=address, isa=isa, form="long")
        if h >= 0xC0:
            pattern = _atoms(h)
            if pattern is not None:
                return el.Atoms(offset=o, raw=r.raw, pattern=pattern)
        raise _Bad(f"M-profile の命令トレースに無いヘッダ 0x{h:02X}")

    def _trace_info(self, r: _Reader) -> el.TraceInfo:
        plctl = r.cont()
        info = r.cont() if plctl & 0x01 else 0
        if plctl & 0x02:
            r.cont()                                                      # KEY（投機の番号。M-profile では使わない）
        if plctl & 0x04:
            r.cont()                                                      # SPEC（投機の深さ）
        threshold = r.cont() if plctl & 0x08 else None
        self._threshold = threshold or 0
        self._stack = [(0, "thumb")] * 3
        return el.TraceInfo(offset=r.start, raw=r.raw, cycle_counting=bool(info & 0x01), cc_threshold=threshold)

    def _timestamp_packet(self, r: _Reader, with_cycles: bool) -> el.Timestamp:
        value, bits = 0, 0
        for n in range(9):
            b = r.byte()
            if n == 8:                                                    # 9 バイト目は 8 ビットすべて
                value |= b << 56
                bits = 64
                break
            value |= (b & 0x7F) << (7 * n)
            bits = 7 * (n + 1)
            if not b & 0x80:
                break
        mask = (1 << bits) - 1
        self._timestamp = (self._timestamp & ~mask) | value
        cycles = r.cont(3) if with_cycles else None
        return el.Timestamp(offset=r.start, raw=r.raw, value=self._timestamp, cycles=cycles)

    def _exception(self, r: _Reader) -> el.ExceptionEntry:
        b1 = r.byte()
        number = (b1 >> 1) & 0x1F
        fault_pending = False
        if b1 & 0x80:
            b2 = r.byte()
            number |= (b2 & 0x1F) << 5
            fault_pending = bool(b2 & 0x20)
        address_kind = (b1 >> 5 & 0x2) | (b1 & 0x1)                       # E1:E0
        return el.ExceptionEntry(offset=r.start, raw=r.raw, number=number, name=el.exception_name(number),
                                 fault_pending=fault_pending, address_kind=address_kind)

    def _context(self, r: _Reader, offset: int, payload: bool) -> el.Context:
        if not payload:
            return el.Context(offset=offset, raw=r.raw, unchanged=True)
        start = r.pos
        info = r.byte()
        vmid = cid = None
        if info & 0x40:
            vmid = int.from_bytes(bytes(r.byte() for _ in range(self.config.vmid_bytes)), "little")
        if info & 0x80:
            cid = int.from_bytes(bytes(r.byte() for _ in range(self.config.cid_bytes)), "little")
        raw = r.raw if offset == r.start else bytes(r.data[start:r.pos])
        return el.Context(offset=offset, raw=raw, non_secure=bool(info & 0x20), exception_level=info & 0x03,
                          vmid=vmid, context_id=cid)

    # --- アドレス -------------------------------------------------------------------------

    def _push(self, address: int, isa: str) -> None:
        self._stack = [(address, isa)] + self._stack[:2]

    def _short(self, r: _Reader, isa: str) -> int:
        shift = 1 if isa == "thumb" else 2
        b0 = r.byte()
        value, bits = (b0 & 0x7F) << shift, 7 + shift
        if b0 & 0x80:
            value |= r.byte() << (7 + shift)
            bits += 8
        mask = (1 << bits) - 1
        address = (self._stack[0][0] & ~mask) | value
        self._push(address, isa)
        return address

    def _long(self, r: _Reader, isa: str, wide: bool) -> int:
        b = [r.byte() for _ in range(8 if wide else 4)]
        if isa == "thumb":
            value = (b[0] & 0x7F) << 1 | b[1] << 8
        else:
            value = (b[0] & 0x7F) << 2 | (b[1] & 0x7F) << 9
        for n in range(2, len(b)):
            value |= b[n] << (8 * n)
        if not wide:
            value |= self._stack[0][0] & ~0xFFFF_FFFF
        self._push(value, isa)
        return value


def _atoms(h: int) -> str | None:
    """Atom のヘッダ → E / N の並び（古い順）。"""
    def bits(value: int, count: int) -> str:
        return "".join("E" if value >> n & 1 else "N" for n in range(count))
    if h in (0xF6, 0xF7):                                                 # 形式 1
        return bits(h, 1)
    if 0xD8 <= h <= 0xDB:                                                 # 形式 2
        return bits(h, 2)
    if 0xF8 <= h:                                                         # 形式 3
        return bits(h, 3)
    if 0xDC <= h <= 0xDF:                                                 # 形式 4
        return bits((0xE, 0x0, 0xA, 0x5)[h & 0x3], 4)
    if h in (0xD5, 0xD6, 0xD7, 0xF5):                                     # 形式 5
        return bits({0xF5: 0x1E, 0xD5: 0x00, 0xD6: 0x0A, 0xD7: 0x15}[h], 5)
    if 0xC0 <= h <= 0xD4 or 0xE0 <= h <= 0xF4:                            # 形式 6: (n+3) 個の E と最後の 1 つ
        n = (h & 0x1F) + 3
        return "E" * n + ("N" if h & 0x20 else "E")
    return None
