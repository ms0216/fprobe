"""UART の解読の上の Modbus RTU。仕様: docs/superpowers/specs/2026-10-18-u20-decoders-design.md

バイトの間の無音が gap_s 秒以上の所でフレームを区切る。既定は規格（Modbus over Serial Line 2.5.1.1）の t3.5: 19200 bps
以下は 3.5 文字（1 文字 = UART の設定のスタート + データ + パリティ + ストップのビット ÷ ボー）、それより速いときは固定の
1.75 ms。gap_chars（文字数）か gap_s（秒）を渡せば、それで区切る。
フレーム = [スレーブ] [機能] [データ…] [CRC-16（下位バイトが先）]。要求と応答は形（規格で長さが決まる機能）と、
前のフレーム（スレーブは要求なしに送らない・応答は同じスレーブと機能）で見分ける。どちらでも決まらないフレーム
（捕捉の始まりの FC5/6 のエコー等）は kind="frame"・direction=None にする（要求とみなさない）。
応答には要求からの遅れ（latency_s）を付ける。
"""

from .core import Annotation, Decoding

__all__ = ["EXCEPTIONS", "FUNCTIONS", "decode_modbus", "modbus_crc"]

FUNCTIONS = {1: "READ_COILS", 2: "READ_DISCRETE_INPUTS", 3: "READ_HOLDING_REGISTERS", 4: "READ_INPUT_REGISTERS",
             5: "WRITE_SINGLE_COIL", 6: "WRITE_SINGLE_REGISTER", 7: "READ_EXCEPTION_STATUS", 8: "DIAGNOSTICS",
             15: "WRITE_MULTIPLE_COILS", 16: "WRITE_MULTIPLE_REGISTERS", 17: "REPORT_SERVER_ID",
             22: "MASK_WRITE_REGISTER", 23: "READ_WRITE_MULTIPLE_REGISTERS", 43: "ENCAPSULATED_INTERFACE"}
EXCEPTIONS = {1: "ILLEGAL_FUNCTION", 2: "ILLEGAL_DATA_ADDRESS", 3: "ILLEGAL_DATA_VALUE", 4: "SERVER_DEVICE_FAILURE",
              5: "ACKNOWLEDGE", 6: "SERVER_DEVICE_BUSY", 8: "MEMORY_PARITY_ERROR", 10: "GATEWAY_PATH_UNAVAILABLE",
              11: "GATEWAY_TARGET_FAILED_TO_RESPOND"}


def modbus_crc(data) -> int:
    """CRC-16/MODBUS（初期値 0xFFFF、多項式 0xA001 の反転形）。"""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = crc >> 1 ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def _u16(b: list[int], i: int) -> int:
    return b[i] << 8 | b[i + 1]


def decode_modbus(lower: Decoding, *, gap_chars: float | None = None, gap_s: float | None = None) -> Decoding:
    """UART の解読の上で Modbus RTU のフレームを読む。"""
    if lower.protocol != "uart":
        raise ValueError("uart の解読の上に重ねる")
    p = lower.params
    char_s = (1 + p["data_bits"] + (p["parity"] != "none") + p["stop_bits"]) / p["baud"]
    if gap_chars is not None and gap_s is not None:
        raise ValueError("gap_chars と gap_s はどちらか一方")
    if gap_chars is not None:
        gap_s = gap_chars * char_s
    elif gap_s is None:
        gap_s = 3.5 * char_s if p["baud"] <= 19200 else 1.75e-3          # 規格の t3.5
    if gap_s <= 0:
        raise ValueError(f"区切りの無音が正でない（{gap_s} 秒）")
    params = {"gap_s": gap_s}
    data = [a for a in lower.of("data", "break")]
    out: list[Annotation] = []
    if not data:
        return lower.derive("modbus", out, params)
    gap = gap_s / lower.sample_period_s                                   # サンプル
    frames: list[list[Annotation]] = [[data[0]]]
    for prev, a in zip(data, data[1:]):
        if a.start - prev.end >= gap or a.kind == "break" or prev.kind == "break":
            frames.append([a])
        else:
            frames[-1].append(a)
    state: _State = None
    for frame in frames:
        if frame[0].kind == "break":
            continue
        ann, state = _frame(frame, state, lower.sample_period_s)
        out.append(ann)
    return lower.derive("modbus", out, params)


# 次のフレームについて分かっていること: None = 分からない、MASTER = 主の送るもの（要求）、
# (スレーブ, 機能, データ, 終わり) = その要求の後（応答か、応答が無く主が次の要求を送ったか）
MASTER = "master"
_State = tuple[int, int, list[int], int] | str | None


def _frame(frame: list[Annotation], state: _State, period: float) -> tuple[Annotation, _State]:
    raw = [a.value for a in frame]
    start, end = frame[0].start, frame[-1].end
    uart_error = next((a.error for a in frame if a.error), None)
    if len(raw) < 4 or None in raw:
        return Annotation(start, end, "frame", "? " + " ".join(f"{b:02X}" for b in raw if b is not None),
                          error=uart_error or "short", fields={"bytes": [b for b in raw if b is not None]}), state
    body, crc = raw[:-2], raw[-2] | raw[-1] << 8
    crc_ok = modbus_crc(body) == crc
    slave, function, payload = body[0], body[1], body[2:]
    base = function & 0x7F
    request = state if isinstance(state, tuple) else None
    fields: dict = {"slave": slave, "function": base, "function_name": FUNCTIONS.get(base), "crc_ok": crc_ok,
                    "payload": payload}
    error = uart_error or (None if crc_ok else "crc")
    after = MASTER if crc_ok else None                   # 応答の後は主が送る（スレーブは要求なしに送らない）
    if function & 0x80:
        code = payload[0] if payload else None
        fields.update(direction="response", exception_code=code, exception_name=EXCEPTIONS.get(code))
        kind, text = "exception", f"← {slave:02X} {fields['function_name'] or base} ! {EXCEPTIONS.get(code, code)}"
        fields["latency_s"] = _latency(request, slave, base, start, period)
        return Annotation(start, end, kind, text, value=slave, error=error or "exception", fields=fields), after
    direction = _direction(slave, base, payload, state)
    fields["direction"] = direction
    name = fields["function_name"] or f"FC{base}"
    if direction == "response":
        fields["latency_s"] = _latency(request, slave, base, start, period)
        _response_fields(base, payload, fields)
        text = f"← {slave:02X} {name}" + _summary(fields)
        return Annotation(start, end, "response", text, value=slave, error=error, fields=fields), after
    if direction is None:                                # 形からも前のフレームからも決まらない（要求とみなさない）
        return Annotation(start, end, "frame", f"? {slave:02X} {name}", value=slave, error=error, fields=fields), None
    _request_fields(base, payload, fields)
    text = f"→ {slave:02X} {name}" + _summary(fields)
    if not crc_ok:
        after = None
    elif slave != 0:
        after = (slave, base, payload, end)
    return Annotation(start, end, "request", text, value=slave, error=error, fields=fields), after  # 0: 応答の無い一斉


def _shapes(fc: int, p: list[int]) -> set[str]:
    """規格で長さが決まる機能について、データの形から取りうる向き（どちらも取りうるなら両方、どちらでもなければ空）。"""
    if fc in (1, 2, 3, 4):
        return {d for d, ok in (("request", len(p) == 4), ("response", bool(p) and p[0] == len(p) - 1)) if ok}
    if fc in (5, 6):
        return {"request", "response"} if len(p) == 4 else set()            # 応答は要求のエコー
    if fc in (15, 16):
        return {d for d, ok in (("request", len(p) >= 5 and p[4] == len(p) - 5), ("response", len(p) == 4)) if ok}
    if fc == 23:
        return {d for d, ok in (("request", len(p) >= 9 and p[8] == len(p) - 9),
                                ("response", bool(p) and p[0] == len(p) - 1)) if ok}
    return {"request", "response"}                                          # 形を調べない機能


def _direction(slave: int, fc: int, p: list[int], state: _State) -> str | None:
    """要求か応答か。形で 1 つに決まればそれ。両方を取りうるときは前のフレームで決める（規格: スレーブは要求なしに
    送らず、応答は要求と同じスレーブ・機能、FC5/6 は要求のエコー）。決まらなければ None（推定しない）。"""
    shapes = _shapes(fc, p)
    if slave == 0:                                                           # 一斉（応答は無い）
        shapes &= {"request"}
    if len(shapes) != 2:
        return next(iter(shapes), None)
    if state == MASTER:
        return "request"
    if isinstance(state, tuple):
        answered = state[0] == slave and state[1] == fc and (fc not in (5, 6) or p == state[2])
        return "response" if answered else "request"
    return None


def _request_fields(fc: int, p: list[int], f: dict) -> None:
    if fc in (1, 2, 3, 4) and len(p) >= 4:
        f.update(start=_u16(p, 0), count=_u16(p, 2))
    elif fc in (5, 6) and len(p) >= 4:
        f.update(start=_u16(p, 0), values=[_u16(p, 2)])
    elif fc == 16 and len(p) >= 5:
        f.update(start=_u16(p, 0), count=_u16(p, 2), values=[_u16(p, 5 + 2 * i) for i in range((len(p) - 5) // 2)])
    elif fc == 15 and len(p) >= 5:
        count = _u16(p, 2)
        f.update(start=_u16(p, 0), count=count, values=_bits(p[5:], count))


def _response_fields(fc: int, p: list[int], f: dict) -> None:
    if fc in (3, 4, 23) and p:
        f.update(values=[_u16(p, 1 + 2 * i) for i in range(p[0] // 2)])
    elif fc in (1, 2) and p:
        f.update(values=_bits(p[1:], 8 * p[0]))
    elif fc in (5, 6) and len(p) >= 4:
        f.update(start=_u16(p, 0), values=[_u16(p, 2)])
    elif fc in (15, 16) and len(p) >= 4:
        f.update(start=_u16(p, 0), count=_u16(p, 2))


def _bits(data: list[int], count: int) -> list[int]:
    return [data[i // 8] >> (i % 8) & 1 for i in range(min(count, 8 * len(data)))]


def _summary(f: dict) -> str:
    text = ""
    if "start" in f:
        text += f" @0x{f['start']:04X}"
    if "count" in f:
        text += f" ×{f['count']}"
    if "values" in f:
        values = f["values"]
        text += " [" + ", ".join(f"0x{v:04X}" if v > 1 else str(v) for v in values[:8]) + \
            (", …" if len(values) > 8 else "") + "]"
    return text


def _latency(request, slave: int, fc: int, start: int, period: float) -> float | None:
    if request is None or request[0] != slave or request[1] != fc:
        return None
    return (start - request[3]) * period
