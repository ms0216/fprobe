"""I2C の解読。仕様: docs/superpowers/specs/2026-10-17-f7-protocol-decode-design.md"""

from ..la import Transitions
from .core import Annotation, Channel, Decoding, project

__all__ = ["decode_i2c"]

_SCL, _SDA = 1, 2                                     # project(tr, [scl, sda]) のビット


class _Transfer:
    """START（または Sr）から次の Sr か STOP までの 1 区間。"""

    def __init__(self, start: int, last_ten_bit: int | None):
        self.start = start
        self.address: int | None = None
        self.rw: str | None = None
        self.ten_bit = False
        self.ten_hi: int | None = None                # 10 ビットの書きの見出しの上位 2 ビット（次のバイト待ち）
        self.address_start = 0
        self.ack: bool | None = None                  # アドレスの ACK
        self.data: list[int] = []
        self.error: str | None = None
        self.bytes = 0
        self.last_ten_bit = last_ten_bit              # 同じ取引で前に書いた 10 ビットのアドレス


def decode_i2c(tr: Transitions, *, scl: Channel, sda: Channel) -> Decoding:
    """I2C を解読する。SCL が 1 の間の SDA の変化が START / STOP、SCL の立ち上がりで SDA を読む。

    サンプルの間隔より短い順序は測れない。同じサンプルで SCL と SDA が変わった所は、印を付けて返す:
    - 休止中に SCL と SDA が同じサンプルで下がった: START として解読を続け、注釈の fields に same_sample = True。
      SDA が先なら START、SCL が先なら（規格に無い）休止中の SCL の立ち下がりで、どちらかは測れていない。
    - SCL の立ち上がりと同じサンプルで SDA が変わったビット: 変わった後の SDA を読み、そのバイトの注釈（address・
      data）の fields の same_sample_bits にビットの位置（0 = 最上位、8 = ACK）を並べる。"""
    starts, values = project(tr, [scl, sda])
    out: list[Annotation] = []
    params = {"scl": scl, "sda": sda}
    cur: _Transfer | None = None
    bits: list[int] = []
    same_bits: list[int] = []                         # SCL の立ち上がりと同じサンプルで SDA が変わったビットの位置
    bit_start = 0                                     # 組み立て中のバイトの最初の SCL の立ち上がり
    ack_pending: tuple[int, bool] | None = None       # (9 ビット目の立ち上がり, ACK) — 次の SCL の立ち下がりで閉じる
    ten_bit_memory: int | None = None                 # 取引（START〜STOP）の中で最後に書いた 10 ビットのアドレス

    def close_ack(end: int) -> None:
        nonlocal ack_pending
        if ack_pending is not None:
            at, ack = ack_pending
            out.append(Annotation(at, max(end, at + 1), "ack" if ack else "nack", "ACK" if ack else "NACK"))
            ack_pending = None

    def partial(end: int, condition: bool = True) -> None:
        """START / STOP / 捕捉の終わりでバイトが途中なら、読めたビットまでを誤りとして出す。
        Sr と STOP の前には必ず SCL の立ち上がりが 1 回ある（条件の一部）ので、condition なら最後の 1 ビットは数えない。"""
        if condition and bits:
            bits.pop()
        if bits:
            n = min(len(bits), 8)
            value = 0
            for b in bits[:n]:
                value = value << 1 | b
            out.append(Annotation(bit_start, max(end, bit_start + 1), "data", f"0x{value:02X}?", value=value,
                                  error="incomplete",
                                  fields=marks({"bits": len(bits)}, [b for b in same_bits if b < len(bits)])))
            bits.clear()
        same_bits.clear()

    def close_transfer(end: int, error: str | None = None) -> None:
        nonlocal cur
        if cur is None:
            return
        if error and cur.error is None:
            cur.error = error
        text = "?" if cur.address is None else f"{cur.rw} 0x{cur.address:0{3 if cur.ten_bit else 2}X}"
        if cur.data:
            text += ": " + " ".join(f"{v:02X}" for v in cur.data)
        if cur.ack is False:
            text += " [NACK]"
        out.append(Annotation(cur.start, max(end, cur.start + 1), "transfer", text, value=cur.address,
                              error=cur.error, fields={"address": cur.address, "rw": cur.rw, "ten_bit": cur.ten_bit,
                                                       "data": list(cur.data), "ack": cur.ack}))
        cur = None

    def marks(fields: dict, same: list[int]) -> dict:
        if same:
            fields["same_sample_bits"] = same
        return fields

    def byte_done(value: int, ack: bool, at: int, same: list[int]) -> None:
        nonlocal ten_bit_memory
        c = cur
        first = c.bytes == 0
        c.bytes += 1
        if first and value >> 3 == 0b11110:           # 10 ビットのアドレスの見出し
            c.ten_bit, c.address_start = True, bit_start
            hi, rw = value >> 1 & 3, value & 1
            if rw == 0:
                if ack:
                    c.ten_hi = hi                     # 下位 8 ビットは次のバイト
                else:
                    _address(c, None, "W", ack, at, same)  # 見出しで拒まれた: 下位のビットは来ない
                return
            c.rw = "R"
            known = c.last_ten_bit if c.last_ten_bit is not None and c.last_ten_bit >> 8 == hi else None
            _address(c, known, "R", ack, at, same)
            return
        if c.ten_hi is not None and c.bytes == 2:        # 10 ビットの書きの下位 8 ビット
            address = c.ten_hi << 8 | value
            c.ten_hi = None
            ten_bit_memory = address
            _address(c, address, "W", ack, at, same)  # 範囲は見出しのバイトから
            return
        if first:
            c.address_start = bit_start
            _address(c, value >> 1, "R" if value & 1 else "W", ack, at, same)
            return
        out.append(Annotation(bit_start, at, "data", f"0x{value:02X}", value=value,
                              fields=marks({"rw": c.rw, "ack": ack}, same)))
        if not ack and c.rw == "W" and c.error is None:
            c.error = "nack"                          # 書きのデータを拒まれた（読みの NACK は主が終わりを示す合図）
        if c.rw is not None:
            c.data.append(value)

    def _address(c: _Transfer, address: int | None, rw: str, ack: bool, at: int, same: list[int]) -> None:
        c.address, c.rw, c.ack = address, rw, ack
        text = f"{rw} " + ("0x???" if address is None else f"0x{address:0{3 if c.ten_bit else 2}X}")
        out.append(Annotation(c.address_start, max(at, c.address_start + 1), "address", text, value=address,
                              fields=marks({"rw": rw, "ten_bit": c.ten_bit, "ack": ack}, same)))
        if not ack and c.error is None:
            c.error = "nack"

    prev = values[0] if len(values) else 0
    for i in range(1, len(values)):
        now, at = values[i], starts[i]
        changed = prev ^ now
        scl_high_before, scl_high_now = bool(prev & _SCL), bool(now & _SCL)
        if changed & _SDA and scl_high_before and scl_high_now:
            close_ack(at)
            if now & _SDA:                            # STOP
                if cur is not None:
                    partial(at)
                    out.append(Annotation(at, at + 1, "stop", "P"))
                    close_transfer(at + 1)
                else:                                 # START を見ていない STOP（捕捉が取引の途中から始まる・迷い STOP）
                    out.append(Annotation(at, at + 1, "stop", "P", error="no_start", fields={"start_seen": False}))
                ten_bit_memory = None
            else:                                     # START / Sr
                restart = cur is not None
                partial(at)
                close_transfer(at)
                if not restart:
                    ten_bit_memory = None
                out.append(Annotation(at, at + 1, "restart" if restart else "start", "Sr" if restart else "S"))
                cur = _Transfer(at, ten_bit_memory)
            bits.clear()
            same_bits.clear()
        elif changed & _SDA and changed & _SCL and scl_high_before and cur is None and not now & _SDA:
            # SCL と SDA の立ち下がりが同じサンプル: 順序は測れていない。START として続け、印を付ける
            out.append(Annotation(at, at + 1, "start", "S", fields={"same_sample": True}))
            cur = _Transfer(at, None)
            bits.clear()
            same_bits.clear()
        elif changed & _SCL:
            if scl_high_now:                          # 立ち上がり: SDA を読む
                if cur is not None:
                    if not bits:
                        bit_start = at
                    if changed & _SDA:                # 同じサンプルで SDA も変わった: 変わった後を読む（印を付ける）
                        same_bits.append(len(bits))
                    bits.append(1 if now & _SDA else 0)
                    if len(bits) == 9:
                        value = 0
                        for b in bits[:8]:
                            value = value << 1 | b
                        ack = bits[8] == 0
                        same = list(same_bits)
                        bits.clear()
                        same_bits.clear()
                        byte_done(value, ack, at, same)
                        ack_pending = (at, ack)
            else:
                close_ack(at)
        prev = now
    close_ack(tr.length)
    if cur is not None:
        partial(tr.length, condition=False)
        close_transfer(tr.length, "truncated")
    return Decoding("i2c", tuple(out), tr.sample_period_s, tr.trigger, tr.length, params)
