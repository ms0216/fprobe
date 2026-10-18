"""プラグイン: キーボードの行列（キーマトリクス）のキーを、FPGA から「押したことにする」。

本体の汎用の API（クロストリガの動作「出力の端子」: xtrig.output_set / output_enable / output_disable /
output_release / output_status）だけを使う。仕組みは fprobe.plugins の説明を参照。

    from fprobe.plugins import ProbeCaller
    from fprobe.plugins.keymatrix import KeyMatrix
    km = KeyMatrix(client)                  # DaemonClient か ProbeCaller(probe)
    km.add_key("A", column=1, row=4)        # 列の線 = ロジアナの ch1（例: 74HC595 の出力）、行の線 = 出力の端子 ch4
    km.press("A"); km.release("A"); km.tap("A", 0.05)

キー = (列の入力の ch、行の出力の ch、active_high)。active_high = True（列を High にして走査し、行は内部プルダウンで
読む型。ZMK の 74HC595 の行列等）: 列が High の間だけ行を High にする（条件の間の動作、遅れ 40 ns 程度）。False は
逆（列を Low にして走査し、行はプルアップ）。
行ごとに 1 つの出力の動作を使い、その行で押しているキーの列の集まりを「どれかが一致」の端子の並びにする（同じ行の
キーを同時に押せる）。押しているキーが無くなった行は無効にする（高インピーダンス）。
仕様: docs/superpowers/specs/2026-10-18-u41-xtrig-pin-output-design.md §6
"""

import time
from dataclasses import dataclass

from . import ApiCaller

__all__ = ["Key", "KeyMatrix"]


@dataclass(frozen=True)
class Key:
    name: str
    column: int          # 列の線を見るロジアナの ch（0〜7）
    row: int             # 行の線につないだ出力の端子のロジアナの ch
    active_high: bool = True


class KeyMatrix:
    def __init__(self, api: ApiCaller):
        self.api = api
        self.keys: dict[str, Key] = {}
        self.pressed: set[str] = set()
        self._rows_set: set[int] = set()              # output_set した行（close で release する）

    def _pins(self) -> dict[int, dict]:
        return {o["channel"]: o for o in self.api.call("xtrig.output_status")["outputs"]}

    def add_key(self, name: str, column: int, row: int, active_high: bool = True) -> Key:
        """キーを登録する。行の端子が出力に使えないか、active_high なのにプルアップがある（動いていない間に Low に
        ならない）なら誤り。"""
        if not 0 <= column <= 7:
            raise ValueError("列はロジアナの ch 0〜7")
        pins = self._pins()
        if row not in pins:
            raise ValueError(f"行 ch{row} は出力に使えない（使える端子: {sorted(pins)}）")
        if active_high and pins[row]["pull_up"]:
            raise ValueError(f"行 ch{row} の端子にはプルアップがあり、離した間も行が High に見える"
                             f"（プル無しの端子を使う: {sorted(c for c, o in pins.items() if not o['pull_up'])}）")
        for other in self.keys.values():
            if other.row == row and other.active_high != active_high:
                raise ValueError(f"行 ch{row} の極性がキー {other.name} と合わない")
        key = Key(name, column, row, active_high)
        self.keys[name] = key
        return key

    def _update_row(self, row: int) -> None:
        down = [self.keys[n] for n in self.pressed if self.keys[n].row == row]
        if not down:
            if row in self._rows_set:
                self.api.call("xtrig.output_disable", channel=row)
            return
        columns = 0
        for key in down:
            columns |= 1 << key.column
        high = down[0].active_high
        self.api.call("xtrig.output_set", channel=row, mode="while", level="high" if high else "low",
                      pattern={"mask": columns, "value": columns if high else 0, "any": True}, enable=True)
        self._rows_set.add(row)

    def press(self, name: str) -> None:
        """キーを押したままにする（列の走査に合わせて行を返す）。"""
        key = self.keys[name]
        self.pressed.add(name)
        self._update_row(key.row)

    def release(self, name: str) -> None:
        key = self.keys[name]
        self.pressed.discard(name)
        self._update_row(key.row)

    def tap(self, name: str, seconds: float = 0.05) -> None:
        """決めた長さだけ押す（ホストの時計。キーボードの走査・デバウンスは ms 単位）。"""
        self.press(name)
        try:
            time.sleep(seconds)
        finally:
            self.release(name)

    def release_all(self) -> None:
        for name in list(self.pressed):
            self.release(name)

    def close(self) -> None:
        """すべて離し、使った行の端子を普段の機能に戻す。"""
        self.pressed.clear()
        for row in sorted(self._rows_set):
            self.api.call("xtrig.output_release", channel=row)
        self._rows_set.clear()

    def __enter__(self) -> "KeyMatrix":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
