"""CAN（クラシック）・CAN FD の解読（ISO 11898-1:2015）。仕様: docs/superpowers/specs/2026-10-18-u20-decoders-design.md

線（CAN トランシーバの RXD。劣勢 = 1、優勢 = 0）を 1 本読む。ビットはサンプル点（ビットの始まりからの割合）で読み、
次のビットの読みより前に変化点があればそこをビットの始まりとして合わせ直す（受信器の再同期と同じ考え）。
- SOF: 劣勢が 10.5 ビット以上続いた後の優勢への変化（ACK 区切り 1 + EOF 7 + フレーム間 3 = 11 ビット）。
- スタッフ: SOF からデータ（クラシックは CRC まで）、同じ値が 5 個続いたら逆の値が 1 個入る（6 個続けば誤り "stuff"）。
- CAN FD: FDF = 1 のフレーム。BRS = 1 ならその BRS のサンプル点から CRC 区切りのサンプル点までデータのビットレート。
  CRC の欄はスタッフ数（グレイ符号 3 ビット + 偶数パリティ）と CRC-17（データ 16 バイト以下）/ CRC-21 で、
  その前と 4 ビットごとに固定スタッフ（直前の逆）が入る。CRC はダイナミックスタッフを含めて計算する（ISO 版）。
  データの最後で同じ値が 5 個続いたときは、ダイナミックスタッフを 1 個入れてから固定スタッフが来るものとする。
"""

from bisect import bisect_left, bisect_right

from ..la import Transitions
from .core import Annotation, ChannelLevels, Decoding

__all__ = ["CRC15", "CRC17", "CRC21", "DLC_LENGTHS", "GRAY", "can_crc", "decode_can"]

DLC_LENGTHS = (0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64)
CRC15 = (15, 0x4599, 0)                       # (幅, 多項式（最上位の項を除く）, 初期値)
CRC17 = (17, 0x1685B, 1 << 16)                # ISO の CAN FD は初期値の最上位が 1
CRC21 = (21, 0x102899, 1 << 20)
GRAY = (0, 1, 3, 2, 6, 7, 5, 4)               # スタッフ数 mod 8 のグレイ符号
IDLE_BITS = 10.5


def can_crc(bits, spec: tuple[int, int, int]) -> int:
    """CAN の CRC（ビットを先頭から 1 つずつ入れるシフトレジスタ）。"""
    width, poly, reg = spec
    top, mask = 1 << (width - 1), (1 << width) - 1
    for b in bits:
        feedback = bool(reg & top) ^ bool(b)
        reg = reg << 1 & mask
        if feedback:
            reg ^= poly
    return reg


class _Error(Exception):
    def __init__(self, kind: str, start: float, end: float, text: str = ""):
        super().__init__(kind)
        self.kind, self.start, self.end, self.text = kind, start, end, text or f"{kind} error"


class _Truncated(Exception):
    pass


class _Bits:
    """1 つのフレームのビットの読み手（サンプル点・再同期・スタッフの除去・ビットレートの切り替え）。"""

    def __init__(self, line: "_Line", nominal: tuple[float, float], data: tuple[float, float] | None):
        self.line, self.nominal, self.data = line, nominal, data

    def begin(self, sof: int) -> None:
        self.boundary = sof - 0.5                # ビットの始まり（サンプルの格子の上の連続値）
        self.last = sof - 1.0                    # 最後に読んだサンプル点
        self.bit, self.sp = self.nominal
        self.run_val, self.run_len = None, 0
        self.stuffs = 0
        self.stuffing = True
        self.destuffed: list[int] = []
        self.with_stuff: list[int] = []
        self.last_raw = 1

    def raw(self) -> tuple[int, float, float]:
        line = self.line
        expected = self.boundary + self.sp * self.bit
        i = bisect_right(line.edges, self.last + 0.5)            # 最後のサンプル点より後の最初の変化
        if i < len(line.edges) and line.edges[i] - 0.5 < expected:
            self.boundary = line.edges[i] - 0.5                  # 再同期: その変化がこのビットの始まり
            expected = self.boundary + self.sp * self.bit
        index = int(expected + 0.5)
        if index >= line.length:
            raise _Truncated
        b = line.level(index)
        self.last = expected
        start = self.boundary
        self.boundary += self.bit
        self.last_raw = b
        return b, start, self.boundary

    def stuffed(self) -> tuple[int, float, float]:
        """ダイナミックスタッフを除いた次の 1 ビット。"""
        if self.run_len == 5:
            self.stuff_bit()
        b, s, e = self.raw()
        if b == self.run_val:
            self.run_len += 1
        else:
            self.run_val, self.run_len = b, 1
        self.destuffed.append(b)
        self.with_stuff.append(b)
        return b, s, e

    def stuff_bit(self) -> None:
        b, s, e = self.raw()
        if b == self.run_val:
            raise _Error("stuff", s, e, "6 equal bits")
        self.run_val, self.run_len = b, 1
        self.stuffs += 1
        self.with_stuff.append(b)

    def read(self, n: int) -> tuple[int, float, float]:
        value, start, end = 0, None, 0.0
        for _ in range(n):
            b, s, end = self.stuffed()
            start = s if start is None else start
            value = value << 1 | b
        return value, start, end

    def fixed(self, n: int, j0: int) -> tuple[int, float, float]:
        """CAN FD の CRC の欄の n ビット（固定スタッフを除く）。j0 は欄の中の位置（4 の倍数の前に固定スタッフ）。"""
        value, start, end = 0, None, 0.0
        for j in range(j0, j0 + n):
            if j % 4 == 0:
                prev = self.last_raw
                b, s, e = self.raw()
                if b == prev:
                    raise _Error("stuff", s, e, "fixed stuff bit")
            b, s, end = self.raw()
            start = s if start is None else start
            value = value << 1 | b
        return value, start, end

    def to_data(self) -> None:
        """BRS のサンプル点でデータのビットレートへ（残りの位相はデータの側の長さ）。"""
        self.bit, self.sp = self.data
        self.boundary = self.last + (1 - self.sp) * self.bit

    def to_nominal(self) -> None:
        self.bit, self.sp = self.nominal
        self.boundary = self.last + (1 - self.sp) * self.bit


class _Line:
    def __init__(self, tr: Transitions, channel: int, invert: bool):
        lv = ChannelLevels(tr, channel)
        self.edges, self.initial, self.length = lv.edges, lv.initial ^ int(invert), tr.length

    def level(self, index: int) -> int:
        return self.initial ^ (bisect_right(self.edges, index) & 1)

    def find_sof(self, after: float, idle: float) -> int | None:
        """after 以降で、劣勢が idle サンプル以上続いた後の優勢への変化。"""
        edges = self.edges
        for i in range(bisect_left(edges, after), len(edges)):
            if self.initial ^ ((i + 1) & 1):                    # この変化の後が劣勢: SOF ではない
                continue
            previous = edges[i - 1] if i else 0
            if edges[i] - previous >= idle:
                return edges[i]
        return None


def decode_can(tr: Transitions, channel: int, *, bitrate: float, data_bitrate: float | None = None,
               sample_point: float = 0.75, data_sample_point: float | None = None, invert: bool = False) -> Decoding:
    """CAN / CAN FD を解読する。bitrate は調停相、data_bitrate は CAN FD の BRS = 1 のデータ相のビットレート。"""
    if bitrate <= 0 or (data_bitrate is not None and data_bitrate <= 0):
        raise ValueError(f"ビットレートが正でない（{bitrate}, {data_bitrate}）")
    data_sample_point = sample_point if data_sample_point is None else data_sample_point
    for sp in (sample_point, data_sample_point):
        if not 0.2 <= sp <= 0.95:
            raise ValueError(f"サンプル点は 0.2〜0.95（{sp}）")
    period = tr.sample_period_s
    nominal = (1 / (bitrate * period), sample_point)
    data = (1 / (data_bitrate * period), data_sample_point) if data_bitrate else None
    params = {"channel": channel, "bitrate": bitrate, "data_bitrate": data_bitrate, "sample_point": sample_point,
              "data_sample_point": data_sample_point, "invert": invert}
    line = _Line(tr, channel, invert)
    bits = _Bits(line, nominal, data)
    out: list[Annotation] = []
    after: float = 0
    while True:
        sof = line.find_sof(after, IDLE_BITS * nominal[0])
        if sof is None:
            break
        end = _frame(bits, sof, out, tr.length)
        if end is None:
            break
        after = end
    return Decoding("can", tuple(out), period, tr.trigger, tr.length, params)


def _span(start: float, end: float) -> tuple[int, int]:
    s = max(0, round(start))
    return s, max(s + 1, round(end))


def _frame(bits: _Bits, sof: int, out: list[Annotation], length: int) -> float | None:
    """1 つのフレーム。終わりの位置（次の SOF を探し始める所）を返す。捕捉の終わりで切れたら None。"""
    bits.begin(sof)
    anns: list[Annotation] = []
    f: dict = {"id": None, "extended": None, "rtr": None, "fd": None, "brs": None, "esi": None, "dlc": None,
               "data": [], "crc": None, "crc_ok": None, "ack": None}

    def add(start, end, kind, text, value=None, error=None, fields=None):
        s, e = _span(start, min(end, length))
        anns.append(Annotation(s, e, kind, text, value=value, error=error, fields=fields))

    error = None
    try:
        _, s, e = bits.stuffed()
        add(s, e, "sof", "SOF")
        base, id_start, id_end = bits.read(11)
        r1, ctrl_start, _ = bits.stuffed()
        ide, _, _ = bits.stuffed()
        if ide:
            ext, _, id_end = bits.read(18)
            can_id = base << 18 | ext
            r1, ctrl_start, _ = bits.stuffed()               # RTR（クラシック）/ RRS（FD）
        else:
            can_id = base
        fdf, _, _ = bits.stuffed()
        f.update(id=can_id, extended=bool(ide), fd=bool(fdf), rtr=bool(r1) and not fdf)
        add(id_start, id_end, "id", f"ID 0x{can_id:0{8 if ide else 3}X}" + (" EXT" if ide else ""), value=can_id,
            fields={"extended": bool(ide)})
        brs = esi = 0
        if fdf:
            bits.stuffed()                                    # res
            brs, _, _ = bits.stuffed()
            if brs:
                if bits.data is None:
                    raise _Error("data_bitrate", ctrl_start, bits.boundary, "BRS without data_bitrate")
                bits.to_data()
            esi, _, _ = bits.stuffed()
        elif ide:
            bits.stuffed()                                    # r0（拡張のクラシック）
        dlc, _, ctrl_end = bits.read(4)
        f.update(brs=bool(brs), esi=bool(esi), dlc=dlc)
        flags = ["FD"] * fdf + ["BRS"] * brs + ["ESI"] * esi + ["RTR"] * f["rtr"]
        add(ctrl_start, ctrl_end, "control", " ".join([f"DLC {dlc}"] + flags), value=dlc,
            fields={"rtr": f["rtr"], "ide": bool(ide), "fdf": bool(fdf), "brs": bool(brs), "esi": bool(esi),
                    "dlc": dlc})
        n = DLC_LENGTHS[dlc] if fdf else (0 if r1 else min(dlc, 8))
        for _ in range(n):
            byte, s, e = bits.read(8)
            f["data"].append(byte)
            add(s, e, "data", f"0x{byte:02X}", value=byte)
        if fdf:
            if bits.run_len == 5:                             # データの最後の 5 個の後のダイナミックスタッフ
                bits.stuff_bit()
            bits.stuffing = False
            crc_in = list(bits.with_stuff)
            sc, s, e = bits.fixed(4, 0)
            gray, parity = sc >> 1, sc & 1
            sc_ok = gray == GRAY[bits.stuffs % 8] and (bin(gray).count("1") + parity) % 2 == 0
            add(s, e, "stuff_count", f"stuff count {GRAY.index(gray)}", value=GRAY.index(gray),
                error=None if sc_ok else "stuff_count", fields={"counted": bits.stuffs % 8})
            spec = CRC17 if n <= 16 else CRC21
            received, s, e = bits.fixed(spec[0], 4)
            computed = can_crc(crc_in + [sc >> k & 1 for k in (3, 2, 1, 0)], spec)
        else:
            crc_in = list(bits.destuffed)
            received, s, e = bits.read(15)
            if bits.run_len == 5:
                bits.stuff_bit()
            computed = can_crc(crc_in, CRC15)
            sc_ok = True
        f.update(crc=received, crc_ok=received == computed)
        add(s, e, "crc", f"CRC 0x{received:X}" + ("" if received == computed else f" (expected 0x{computed:X})"),
            value=received, error=None if received == computed else "crc", fields={"computed": computed})
        if received != computed:
            error = "crc"
        elif not sc_ok:
            error = "stuff_count"
        delimiter, s, e = bits.raw()
        if brs:
            bits.to_nominal()
        if not delimiter:
            raise _Error("form", s, e, "CRC delimiter")
        ack, s, e = bits.raw()
        f["ack"] = not ack
        add(s, e, "ack" if not ack else "nack", "ACK" if not ack else "NACK", error=None if not ack else "ack")
        if ack and error is None:
            error = "ack"
        delimiter, s, e = bits.raw()
        if not delimiter:
            raise _Error("form", s, e, "ACK delimiter")
        eof_start = bits.boundary
        for _ in range(7):
            b, s, e = bits.raw()
            if not b:
                raise _Error("form", s, e, "EOF")
        add(eof_start, e, "eof", "EOF")
        end = e
    except _Error as err:
        add(err.start, err.end, "error", err.text, error=err.kind)
        error, end = err.kind, err.end
    except _Truncated:
        error, end = "truncated", None
    frame_end = length if end is None else end
    text = "?" if f["id"] is None else f"ID 0x{f['id']:X}"
    if f["fd"]:
        text += " FD" + " BRS" * bool(f["brs"])
    if f["rtr"]:
        text += " RTR"
    if f["dlc"] is not None:
        text += f" [{len(f['data'])}]" + "".join(f" {b:02X}" for b in f["data"])
    s, e = _span(sof - 0.5, frame_end)
    out.append(Annotation(s, max(e, s + 1), "frame", text, value=f["id"], error=error, fields=f))
    out.extend(anns)
    return end
