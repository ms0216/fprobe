"""変数のライブ観測の、走行中の書き込み（watch_write。止めない。大域・ポインタをたどる式・番地・ビットフィールド）の部品（U43）。

WatchService（fprobe.probe_watch）に mixin される。
"""

import re

from .debug import PyocdAccess
from .dwarf.cexpr import Value, evaluate
from .dwarf.typesys import EnumType, strip, type_name
from .dwarf.values import encode
from .watch_value import _frame, _read_value

_RAW = re.compile(r"(0x[0-9a-fA-F]+)(?::(\d+))?")

class WatchWriteMixin:
    """走行中の書き込み。"""

    def watch_write(self, expression: str, value: int | float | str) -> dict:
        """走行中の変数に書く（止めない。CubeMonitor・µC/Probe の実行中の変更）。expression: 大域・静的の C の式
        （ポインタをたどってもよい）か番地（"0x20000000:4" は整数を書く）。value: 数・列挙の名前・真偽。書いた後に
        読み直した値を返す。ビットフィールドは読み・変え・書きで、ターゲットが同じ語を同時に書くと失われうる（atomic）。"""
        if (view := self._host._linux_view()) is not None:              # Linux のプロセスに attach している間
            return view.watch_write(expression=expression, value=value)
        raw = _RAW.fullmatch(expression.strip())
        with self._host._lock:
            access = PyocdAccess(self._host.target)
            if raw:
                address, size = int(raw[1], 16), int(raw[2] or 4)
                if isinstance(value, (str, float)) or size not in (1, 2, 4, 8):
                    raise ValueError("番地には 1 / 2 / 4 / 8 バイトの整数を書く")
                access.write_memory(address, (int(value) & ((1 << 8 * size) - 1)).to_bytes(size, "little"))
                after = int.from_bytes(access.read_memory(address, size), "little")
                return {"expression": expression, "address": f"0x{address:08X}", "type": None,
                        "value": {"kind": "int", "value": after}, "atomic": True}
            info = self._watch_info()
            if info is None:
                raise RuntimeError("ELF に DWARF が無い（番地なら \"0x20000000:4\" の形で）")
            v = evaluate(expression, info, _frame(access.read_memory))
            if v.address is None:
                raise ValueError(f"{expression!r} はメモリ上に無い（レジスタ・定数には書かない）")
            atomic = v.bit_size is None
            if atomic:
                access.write_memory(v.address, encode(v.type, value))
            else:
                self._write_bits(access, v, value)
            return {"expression": expression, "address": f"0x{v.address:08X}", "type": type_name(v.type),
                    "value": _read_value(v, access.read_memory), "atomic": atomic}

    @staticmethod
    def _write_bits(access, v: Value, value: object) -> None:
        s = strip(v.type)
        number = s.value_of(value) if isinstance(s, EnumType) and isinstance(value, str) else value
        if isinstance(number, bool):
            number = int(number)
        if not isinstance(number, int):
            raise TypeError(f"ビットフィールドには整数を書く（{value!r}）")
        n = v.bit_size
        lo, hi = (-(1 << (n - 1)), 1 << (n - 1)) if getattr(s, "signed", False) else (0, 1 << n)
        if not lo <= int(number) < hi:
            raise ValueError(f"{number} は {n} ビットのビットフィールドに入らない")
        size = (v.bit_offset + n + 7) // 8
        data = int.from_bytes(access.read_memory(v.address, size), "little")
        mask = ((1 << n) - 1) << v.bit_offset
        data = (data & ~mask) | ((int(number) << v.bit_offset) & mask)
        access.write_memory(v.address, data.to_bytes(size, "little"))
