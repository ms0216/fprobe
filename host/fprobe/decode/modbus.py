"""UART の解読の上の Modbus RTU。仕様: docs/superpowers/specs/2026-10-18-u20-decoders-design.md

バイトの間の無音が gap_chars 文字以上の所でフレームを区切る（1 文字の長さは UART のバイトの注釈の長さの中央値）。
既定の 2.5 は、規格のフレームの中の上限（1.5 文字）とフレームの間の下限（3.5 文字）の中間（送り手の時間のずれに強い）。
フレーム = [スレーブ] [機能] [データ…] [CRC-16（下位バイトが先）]。要求と応答は形と直前の要求で見分け、
応答には要求からの遅れ（latency_s）を付ける。
"""

from statistics import median

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


def decode_modbus(lower: Decoding, *, gap_chars: float = 2.5) -> Decoding:
    """UART の解読の上で Modbus RTU のフレームを読む。"""
    if lower.protocol != "uart":
        raise ValueError("uart の解読の上に重ねる")
    if gap_chars <= 0:
        raise ValueError(f"gap_chars が正でない（{gap_chars}）")
    data = [a for a in lower.of("data", "break")]
    out: list[Annotation] = []
    if not data:
        return lower.derive("modbus", out, {"gap_chars": gap_chars})
    char = median(a.end - a.start for a in data if a.kind == "data") if any(a.kind == "data" for a in data) else 1
    frames: list[list[Annotation]] = [[data[0]]]
    for prev, a in zip(data, data[1:]):
        if a.start - prev.end >= gap_chars * char or a.kind == "break" or prev.kind == "break":
            frames.append([a])
        else:
            frames[-1].append(a)
    request: tuple[int, int, list[int], int] | None = None      # (スレーブ, 機能, データ, 終わり)
    for frame in frames:
        if frame[0].kind == "break":
            continue
        ann, request = _frame(frame, request, lower.sample_period_s)
        out.append(ann)
    return lower.derive("modbus", out, {"gap_chars": gap_chars})


def _frame(frame: list[Annotation], request, period: float):
    raw = [a.value for a in frame]
    start, end = frame[0].start, frame[-1].end
    uart_error = next((a.error for a in frame if a.error), None)
    if len(raw) < 4 or None in raw:
        return Annotation(start, end, "frame", "? " + " ".join(f"{b:02X}" for b in raw if b is not None),
                          error=uart_error or "short", fields={"bytes": [b for b in raw if b is not None]}), request
    body, crc = raw[:-2], raw[-2] | raw[-1] << 8
    crc_ok = modbus_crc(body) == crc
    slave, function, payload = body[0], body[1], body[2:]
    base = function & 0x7F
    fields: dict = {"slave": slave, "function": base, "function_name": FUNCTIONS.get(base), "crc_ok": crc_ok,
                    "payload": payload}
    error = uart_error or (None if crc_ok else "crc")
    if function & 0x80:
        code = payload[0] if payload else None
        fields.update(exception_code=code, exception_name=EXCEPTIONS.get(code))
        kind, text = "exception", f"← {slave:02X} {fields['function_name'] or base} ! {EXCEPTIONS.get(code, code)}"
        fields["latency_s"] = _latency(request, slave, base, start, period)
        return Annotation(start, end, kind, text, value=slave, error=error or "exception", fields=fields), None
    answered = request is not None and request[0] == slave and request[1] == base
    is_response = _is_response(base, payload, answered, request)
    name = fields["function_name"] or f"FC{base}"
    if is_response:
        fields["latency_s"] = _latency(request, slave, base, start, period)
        _response_fields(base, payload, fields)
        text = f"← {slave:02X} {name}" + _summary(fields)
        return Annotation(start, end, "response", text, value=slave, error=error, fields=fields), None
    _request_fields(base, payload, fields)
    text = f"→ {slave:02X} {name}" + _summary(fields)
    return Annotation(start, end, "request", text, value=slave, error=error, fields=fields), \
        ((slave, base, payload, end) if slave != 0 and crc_ok else None)


def _is_response(fc: int, p: list[int], answered: bool, request) -> bool:
    if fc in (1, 2, 3, 4):
        fits = bool(p) and p[0] == len(p) - 1
        return fits and (answered or len(p) != 4)
    if fc in (5, 6):
        return answered and p == request[2]
    if fc in (15, 16):
        return len(p) == 4 and answered
    if fc == 23:
        return bool(p) and p[0] == len(p) - 1 and answered
    return answered


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
