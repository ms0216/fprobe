"""書き込みアルゴリズムの間だけチップのレジスタを決めた状態にする手順（RegisterGuard。使う側が渡す。本体は機器を知らない）。

FlashMemory（fprobe.flash）が書き込みアルゴリズムを動かす操作の外側で使う。fprobe.flash が再公開する。
"""

_GUARD_OPS = {"write": ("address", "mask", "value"), "wait": ("address", "mask", "value"),
              "restore": ("address", "mask"), "wait_restored": ("address", "mask")}


class RegisterGuard:
    """書き込みアルゴリズムの間だけチップのレジスタを決めた状態にし、抜けるときに戻す手順（使う側が渡す。本体は機器
    固有の守りを持たない。例: STM32F2 / F4 / F7 のクロックはプラグイン fprobe.plugins.stm32_flash_clock）。
    {"enter": [手順], "exit": [手順]}。手順（op）:
    - write {address, mask, value}: 読んで mask のビットを value にして書く（mask = 0xFFFFFFFF なら value をそのまま）
    - wait {address, mask, value}: (読んだ値 & mask) == value になるまで読む（tries 回で終わらなければ誤り）
    - restore {address, mask}: enter の前に読んでおいた値の mask のビットに戻す
    - wait_restored {address, mask}: (読んだ値 & mask) == (enter の前の値 & mask) になるまで読む
    restore・wait_restored の番地は enter の前に読んで覚える。"""

    def __init__(self, enter: list[dict], exit: list[dict], tries: int = 1000):
        self.enter_steps, self.exit_steps, self.tries = enter, exit, tries
        self.saved_addresses = sorted({s["address"] for s in enter + exit if s["op"] in ("restore", "wait_restored")})

    @classmethod
    def from_dict(cls, spec: dict) -> "RegisterGuard":
        if not isinstance(spec, dict) or set(spec) - {"enter", "exit"}:
            raise ValueError(f"守りは {{enter, exit}}: {spec!r}")
        steps = {}
        for part in ("enter", "exit"):
            steps[part] = []
            for step in spec.get(part) or []:
                need = _GUARD_OPS.get(step.get("op")) if isinstance(step, dict) else None
                if need is None or set(step) != {"op", *need} or not all(isinstance(step[k], int) for k in need):
                    raise ValueError(f"守りの手順は {', '.join(f'{op} {{{', '.join(k)}}}' for op, k in _GUARD_OPS.items())}"
                                     f" のどれか: {step!r}")
                steps[part].append(dict(step))
        return cls(steps["enter"], steps["exit"])

    def enter(self, target) -> dict[int, int]:
        saved = {address: target.read32(address) for address in self.saved_addresses}
        self._run(target, self.enter_steps, saved)
        return saved

    def exit(self, target, saved: dict[int, int]) -> None:
        self._run(target, self.exit_steps, saved)

    def _run(self, target, steps: list[dict], saved: dict[int, int]) -> None:
        for s in steps:
            address, mask, op = s["address"], s["mask"], s["op"]
            if op in ("write", "restore"):
                value = s["value"] if op == "write" else saved[address]
                current = 0 if mask == 0xFFFFFFFF else target.read32(address)
                target.write32(address, (current & ~mask | value & mask) & 0xFFFFFFFF)
            else:
                want = s["value"] if op == "wait" else saved[address] & mask
                for _ in range(self.tries):
                    if target.read32(address) & mask == want:
                        break
                else:
                    raise RuntimeError(f"守りの待ち（0x{address:08X} & 0x{mask:08X} == 0x{want:X}）が終わらない")
