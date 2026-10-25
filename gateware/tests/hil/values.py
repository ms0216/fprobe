"""API の値（版 2 から {"kind": ..., "value": ...}）を、試験で数として比べるための取り出し。"""


def num(x):
    """{"kind": "int"|"float", "value": n} なら n。すでに数ならそのまま。入れ子でも底まで取る。"""
    while isinstance(x, dict) and "value" in x:
        x = x["value"]
    return x
