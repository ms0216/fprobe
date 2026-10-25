"""FPROBE_HIL_ETH=<IP> のとき、Primer（USB）向けの実機試験をそのまま PYNQ-Z2 の fprobe（Ethernet、PS の中継越し）に向けて実行する
（自己デバッグ。docs/superpowers/specs/2026-10-25-fprobe-self-debug-design.md）。

- Session.open(serial) を Session.open_eth(IP) に置き換える（serial は無視する）。
- tests.hil.program.program（Primer にビットストリームを書く）は何もしない（PYNQ の回路は使う側が先に書く）。
PYNQ に無い機能（補助の Pico、Primer の J6 の同期、DDR3 の構成の切り替え等）の試験は、失敗として出る（不具合と区別するのは人・AI）。"""
import os

_ETH = os.environ.get("FPROBE_HIL_ETH")

if _ETH:
    from fprobe.session import Session
    from tests.hil import program as _program_module

    _open_eth = Session.open_eth

    def _open(cls, serial=None):
        return _open_eth(_ETH)

    Session.open = classmethod(_open)                   # type: ignore[method-assign]
    _program_module.program = lambda design="fprobe", **kwargs: None
