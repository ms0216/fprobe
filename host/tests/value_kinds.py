"""試験で値の形（fprobe.dwarf.values。どの値も kind を持つ object）を短く書く。"""


def vint(n: int) -> dict:
    return {"kind": "int", "value": n}


def vfloat(x) -> dict:
    return {"kind": "float", "value": x}


def vbool(b: bool) -> dict:
    return {"kind": "bool", "value": b}


def vbytes(hex_text: str) -> dict:
    return {"kind": "bytes", "hex": hex_text}


def venum(name: str | None, n: int) -> dict:
    return {"kind": "enum", "name": name, "value": n}


def vptr(address: int, to: str, **more) -> dict:
    return {"kind": "pointer", "address": address, "to": to, **more}


def vstruct(**members) -> dict:
    return {"kind": "struct", "members": members}


def varray(items: list, length: int | None = None, **more) -> dict:
    return {"kind": "array", "length": len(items) if length is None else length, "items": list(items), **more}


def ints(*numbers: int) -> list[dict]:
    return [vint(n) for n in numbers]


def vna(reason: str) -> dict:
    return {"kind": "unavailable", "reason": reason}
