"""SWD の解読（ADIv5 の DP/AP）。仕様: docs/superpowers/specs/2026-10-17-f7-protocol-decode-design.md

SWCLK の立ち上がりごとに直前の SWDIO を 1 ビットとして取り出し、そのビット列の上でパケットと並び（ライン リセット・
JTAG/SWD の切り替え・ドーマント）を認識する。SELECT・CTRL/STAT・DLCR の書き込みを追跡して、レジスタ名・
ORUNDETECT・ターンアラウンドの長さ・AP の読みの先送りを解釈する。
"""

from ..la import Transitions
from .core import Annotation, Decoding

__all__ = ["DORMANT_ALERT", "decode_swd"]

DORMANT_ALERT = 0x19BC0EA2_E3DDAFE9_86852D95_6209F392     # 選択の合図（128 ビット、LSB から送る）
SEQUENCES16 = {0xE79E: ("jtag_to_swd", "JTAG-to-SWD"), 0xE73C: ("swd_to_jtag", "SWD-to-JTAG"),
               0xE3BC: ("swd_to_dormant", "SWD-to-DORMANT")}
ACTIVATION = {0x1A: "SWD", 0x0A: "JTAG-Serial"}
ACKS = {0b001: "OK", 0b010: "WAIT", 0b100: "FAULT", 0b111: "NONE"}
LINE_RESET_BITS = 50

DP_BANKED = {0: "CTRL/STAT", 1: "DLCR", 2: "TARGETID", 3: "DLPIDR", 4: "EVENTSTAT"}
MEM_AP = {0x00: "CSW", 0x04: "TAR", 0x08: "TAR_HI", 0x0C: "DRW", 0x10: "BD0", 0x14: "BD1", 0x18: "BD2", 0x1C: "BD3",
          0x20: "MBT", 0xF0: "BASE_HI", 0xF4: "CFG", 0xF8: "BASE", 0xFC: "IDR"}


def _bits(value: int, n: int) -> bytes:
    return bytes(value >> i & 1 for i in range(n))


_SEQ_BITS = {_bits(v, 16): kind for v, kind in SEQUENCES16.items()}
_ALERT_BITS = _bits(DORMANT_ALERT, 128)


def dp_register(addr: int, rnw: bool, dpbank: int) -> str:
    if addr == 0x0:
        return "DPIDR" if rnw else "ABORT"
    if addr == 0x4:
        return DP_BANKED.get(dpbank, f"DP 0x4 bank {dpbank}")
    if addr == 0x8:
        return "RESEND" if rnw else "SELECT"
    return "RDBUFF" if rnw else "TARGETSEL"


def ap_register(addr: int, apbank: int) -> str:
    reg = apbank << 4 | addr
    return MEM_AP.get(reg, f"0x{reg:02X}")


class _State:
    """書き込みから追跡する DP の状態。"""

    def __init__(self, turnaround: int, orundetect: bool):
        self.select = 0
        self.turnaround = turnaround
        self.orundetect = orundetect
        self.pending: str | None = None              # 先送りの AP の読み（次の AP の読みか RDBUFF で値が返る）


def decode_swd(tr: Transitions, *, swclk: int, swdio: int, drive: int | None = None, turnaround: int = 1,
               orundetect: bool = False) -> Decoding:
    """SWD を解読する。drive（プローブが SWDIO を駆動中）を与えると、各相の駆動側を検査する（誤りは "direction"）。
    turnaround・orundetect は捕捉の先頭での値（後は DLCR・CTRL/STAT の書き込みから追う）。"""
    if not 1 <= turnaround <= 4:
        raise ValueError(f"ターンアラウンドは 1〜4（{turnaround}）")
    pos, bits, drv = _extract(tr, swclk, swdio, drive)
    params = {"swclk": swclk, "swdio": swdio, "drive": drive, "turnaround": turnaround, "orundetect": orundetect}
    parser = _Parser(pos, bits, drv, _State(turnaround, orundetect))
    parser.run()
    return Decoding("swd", tuple(parser.out), tr.sample_period_s, tr.trigger, tr.length, params)


def _extract(tr: Transitions, swclk: int, swdio: int, drive: int | None) -> tuple[list[int], bytearray, bytearray | None]:
    """SWCLK の立ち上がりごとに、その直前の SWDIO（と drive）を取り出す。変化点の列を 1 回だけ走査する。"""
    for ch in (swclk, swdio) + ((drive,) if drive is not None else ()):
        if not 0 <= ch < tr.channels:
            raise ValueError(f"チャンネル {ch} は捕捉に無い（0..{tr.channels - 1}）")
    clk = 1 << swclk
    pos, bits = [], bytearray()
    drv = bytearray() if drive is not None else None
    add_pos, add_bit = pos.append, bits.append
    values, starts = tr.values, tr.starts
    prev = values[0] if len(values) else 0
    for i in range(1, len(values)):
        now = values[i]
        if now & clk and not prev & clk:              # 立ち上がり: 直前の値を読む
            add_pos(starts[i])
            add_bit(prev >> swdio & 1)
            if drv is not None:
                drv.append(prev >> drive & 1)
        prev = now
    return pos, bits, drv


class _Parser:
    def __init__(self, pos: list[int], bits: bytearray, drv: bytearray | None, state: _State):
        self.pos, self.bits, self.drv, self.st = pos, bits, drv, state
        self.n = len(bits)
        self.out: list[Annotation] = []

    # ---- 補助 ----
    def span(self, first: int, count: int) -> tuple[int, int]:
        """ビット [first, first + count) のサンプルの区間。"""
        last = min(first + count, self.n) - 1
        return self.pos[first], self.pos[last] + 1

    def value(self, first: int, count: int) -> int:
        v = 0
        for k in range(count):
            v |= self.bits[first + k] << k
        return v

    def driven_by(self, first: int, count: int, host: bool) -> bool:
        """drive があれば、その区間を期待どおりの側が駆動していたか。"""
        if self.drv is None:
            return True
        want = 1 if host else 0
        return all(self.drv[k] == want for k in range(first, min(first + count, self.n)))

    def emit(self, first: int, count: int, kind: str, text: str, **kw) -> None:
        s, e = self.span(first, count)
        self.out.append(Annotation(s, e, kind, text, **kw))

    # ---- 走査 ----
    def run(self) -> None:
        bits, n, i = self.bits, self.n, 0
        while i < n:
            if bits[i] == 0:                          # 休止
                i += 1
                continue
            z = bits.find(0, i)
            run = (n if z < 0 else z) - i
            if run >= 8:
                j = i + run
                if run >= LINE_RESET_BITS:
                    self.emit(i, run, "line_reset", "LINE RESET", fields={"bits": run})
                    self.st.pending = None
                    seq = _SEQ_BITS.get(bytes(bits[j:j + 16]))
                    if seq is not None:
                        kind, text = seq
                        self.emit(j, 16, kind, text, value=self.value(j, 16))
                        i = j + 16
                        continue
                if bytes(bits[j:j + 128]) == _ALERT_BITS:
                    self.emit(j, 128, "dormant_alert", "SELECTION ALERT")
                    k = j + 128
                    if k + 12 <= n and not any(bits[k:k + 4]):
                        code = self.value(k + 4, 8)
                        self.emit(k, 12, "activation", f"ACTIVATE {ACTIVATION.get(code, f'0x{code:02X}')}",
                                  value=code)
                        i = k + 12
                    else:
                        i = k
                    continue
                if run >= LINE_RESET_BITS:
                    i = j
                    continue
            i = self.packet(i)

    def packet(self, i: int) -> int:
        """i（値 1）から要求を読む。要求でなければ i + 1 を返す（1 ビット進めて探し直す）。"""
        bits, n, st = self.bits, self.n, self.st
        if i + 8 > n:
            return n
        ap, rnw, a2, a3, par, stop, park = bits[i + 1:i + 8]
        if stop != 0 or park != 1:
            return i + 1
        trn = st.turnaround
        if par != (ap ^ rnw ^ a2 ^ a3):
            # パリティの誤った要求: ターゲットは応答しない（ACK の相がプルアップの 1）ときだけ要求と認める
            a = i + 8 + trn
            if a + 3 <= n and bits[a] and bits[a + 1] and bits[a + 2]:
                self.emit(i, 8, "request", "REQUEST (parity)", error="request_parity")
                return a + 3 + trn
            return i + 1
        addr = a2 << 2 | a3 << 3
        port = "AP" if ap else "DP"
        apsel = st.select >> 24 & 0xFF
        if ap:
            reg = ap_register(addr, st.select >> 4 & 0xF)
            where = f"AP{apsel} {reg}"
        else:
            reg = dp_register(addr, bool(rnw), st.select & 0xF)
            where = f"DP {reg}"
        fields: dict = {"port": port, "rnw": bool(rnw), "addr": addr, "reg": reg, "apsel": apsel if ap else None,
                        "ack": None, "data": None, "value_of": None}
        request_error = None if self.driven_by(i, 8, host=True) else "direction"
        self.emit(i, 8, "request", f"{port} {'R' if rnw else 'W'} {reg}", value=self.value(i, 8),  # 送った順の 8 ビット
                  error=request_error)
        targetsel = not ap and not rnw and addr == 0xC
        if targetsel:
            d = i + 8 + trn + 3 + trn                 # ACK の相は誰も駆動しない
            ack_name = None
        else:
            a = i + 8 + trn
            if a + 3 > n:
                return self.finish(i, n - i, fields, where, "truncated")
            ack = self.value(a, 3)
            ack_name = ACKS.get(ack, f"0b{ack:03b}")
            fields["ack"] = ack_name
            ack_error = {"OK": None, "WAIT": "wait", "FAULT": "fault", "NONE": "no_ack"}.get(ack_name, "protocol")
            if ack_name != "NONE" and not self.driven_by(a, 3, host=False):
                ack_error = ack_error or "direction"
            self.emit(a, 3, "ack", ack_name, value=ack, error=ack_error)
            if ack_name not in ("OK", "WAIT", "FAULT"):
                return self.finish(i, a + 3 + trn - i, fields, where, ack_error)
            if ack_name != "OK" and not st.orundetect:
                return self.finish(i, a + 3 + trn - i, fields, where, ack_error)
            d = a + 3 + (0 if rnw else trn)
        if d + 33 > n:
            return self.finish(i, n - i, fields, where, "truncated")
        data = self.value(d, 32)
        parity_ok = bits[d + 32] == (bin(data).count("1") & 1)
        data_error = None if parity_ok else "parity"
        if data_error is None and not self.driven_by(d, 33, host=not rnw):
            data_error = "direction"
        end = d + 33 + (trn if rnw else 0)
        if ack_name not in (None, "OK"):              # ORUNDETECT の WAIT / FAULT: データの相はあるが意味は無い
            self.emit(d, 33, "data", f"0x{data:08X}", value=data, error=data_error)
            return self.finish(i, end - i, fields, where, {"WAIT": "wait", "FAULT": "fault"}[ack_name])
        fields["data"] = data
        if parity_ok:
            self.track(ap, rnw, addr, data, fields, where)
        self.emit(d, 33, "data", f"0x{data:08X}", value=data, error=data_error)
        return self.finish(i, min(end, self.n) - i, fields, where, data_error or request_error)

    def track(self, ap: int, rnw: int, addr: int, data: int, fields: dict, where: str) -> None:
        """成功した転送から状態を更新し、読みの値がどのレジスタのものかを付ける。"""
        st = self.st
        if ap and rnw:
            fields["value_of"] = st.pending
            st.pending = where
        elif not ap and rnw and addr == 0xC:
            fields["value_of"] = st.pending
            st.pending = None
        elif not ap and not rnw:
            if addr == 0x8:
                st.select = data
            elif addr == 0x4 and st.select & 0xF == 0:
                st.orundetect = bool(data & 1)
            elif addr == 0x4 and st.select & 0xF == 1:
                st.turnaround = (data >> 8 & 3) + 1

    def finish(self, i: int, count: int, fields: dict, where: str, error: str | None) -> int:
        """パケット全体の注釈を出し、次のビットの位置を返す。"""
        count = max(1, min(count, self.n - i))
        port = where.split(" ", 1)[0]                 # "DP" か "AP<n>"
        text = f"{port} {'R' if fields['rnw'] else 'W'} {fields['reg']}"
        if fields["data"] is not None:
            text += f" 0x{fields['data']:08X}"
        if fields["ack"] is not None:
            text += f" {fields['ack']}"
        if fields["value_of"] is not None:
            text += f" (= {fields['value_of']})"
        s, e = self.span(i, count)
        self.out.append(Annotation(s, e, "transfer", text, value=fields["data"], error=error, fields=fields))
        return i + count
