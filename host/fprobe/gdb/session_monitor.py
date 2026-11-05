"""GDB の接続の、Flash の書き込み（vFlash…）と monitor のコマンド。

GdbSession（fprobe.gdb.session）に mixin される。monitor の API 呼び出しは GdbHost.call を通る。
"""

import json
import shlex
from typing import Any

HELP = """fprobe の GDB サーバ（monitor のコマンド）
  monitor help                      この案内
  monitor reset [halt|init|run]     ターゲットのリセット（省略は run。halt / init は止める）
  monitor elf <path>                ELF を fprobe に読ませる（RTOS のタスクが GDB のスレッドになる・ソースの言葉の API）
  monitor threads [on|off]          RTOS のタスクを GDB のスレッドとして見せるか
  monitor pause | resume            人の一時停止（AI 等の操作を保留する）/ 再開
  monitor trace [allow-halt|deny-halt]
                                    トレースポイントの実験の状態。当たるたびにコアを止めるのを許す / 許さない
                                    （既定は許す。deny-halt ならモニタ方式でなければ QTStart を断る。この接続だけ）
  monitor status                    デーモンの状態（利用者・一時停止・機器）
  monitor <API のメソッド> [JSON | key=value ...]
                                    fprobe の API を呼ぶ（例: monitor la.capture {"mask": 1, "value": 1}、
                                    monitor recording.start name=crash、monitor rtos.tasks）
"""



class _MonitorMixin:
    """Flash と monitor。"""

    async def _flash_packet(self, text: str, payload: bytes) -> str:
        await self._attach()
        if text.startswith("vFlashErase:"):
            return "OK"                    # 消去は書くときに区画（セクタ）ごと（gdb_flash）。GDB は書く区画だけを消す
        if text.startswith("vFlashWrite:"):
            header, _, data = payload[len("vFlashWrite:"):].partition(b":")
            address = int(header, 16)
            if self._flash and self._flash[-1][0] + len(self._flash[-1][1]) == address:
                self._flash[-1][1].extend(data)
            else:
                self._flash.append((address, bytearray(data)))
            return "OK"
        if text == "vFlashDone":
            blocks, self._flash = [(a, bytes(d)) for a, d in self._flash], []
            if blocks:
                result = await self.host.run(lambda: self.probe.gdb_flash(blocks), control=True, moves=True,
                                             record=("gdb.flash", {"blocks": [[a, len(d)] for a, d in blocks]}))
                await self.console(f"fprobe: wrote {result['bytes']} bytes in {result['seconds']} s\n")
            self._threads = None
            return "OK"
        return ""

    # --- monitor ---------------------------------------------------------------------------------

    async def _monitor(self, text: str) -> str:
        command, _, rest = text.strip().partition(" ")
        rest = rest.strip()
        if command in ("", "help"):
            await self.console(HELP)
            return "OK"
        if command == "reset":
            if rest not in ("", "run", "halt", "init"):
                raise ValueError("reset [halt|init|run]")
            await self._attach()
            halt = rest in ("halt", "init")
            result = await self.host.run(lambda: self.probe.gdb_reset(halt), control=True,
                                         record=("gdb.reset", {"halt": halt}), moves=True)
            self._threads = None
            await self.console(_json(result) + "GDB の写しは古い: maintenance flush register-cache で読み直す\n")
            return "OK"
        if command == "threads":
            if rest in ("on", "off"):
                self._threads_on = rest == "on"
                self._threads = None
            threads = await self._thread_list()
            state = "on" if self._threads_on else "off"
            note = f"（{self._thread_note}）" if self._thread_note else ""
            await self.console(f"threads {state}: {len(threads)} {note}\n")
            return "OK"
        if command == "trace":
            if rest in ("allow-halt", "deny-halt"):
                self._trace_deny_halt = rest == "deny-halt"
            elif rest:
                raise ValueError("trace [allow-halt|deny-halt]")
            summary = await self.host.run(self.probe.gdb_trace_summary, control=False)
            await self.console(_json({"deny_halt": self._trace_deny_halt, **summary}))
            return "OK"
        if command == "elf":
            result = await self.host.call("debug.elf.load", {"path": rest})
        elif command in ("pause", "resume"):
            result = await self.host.call(f"session.control.{command}", {})
        elif command == "status":
            result = await self.host.call("session.status", {})
        else:
            result = await self.host.call(command, _params(rest))
        self._threads = None
        await self.console(_json(result))
        return "OK"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=1) + "\n"


def _params(text: str) -> dict:
    """monitor の引数: JSON のオブジェクト、または key=value の並び（値は JSON として読めなければ文字列）。"""
    if not text:
        return {}
    if text.startswith("{"):
        params = json.loads(text)
        if not isinstance(params, dict):
            raise ValueError("引数は JSON のオブジェクト")
        return params
    params = {}
    for word in shlex.split(text):
        key, eq, value = word.partition("=")
        if not eq:
            raise ValueError(f"引数 {word!r} は key=value の形で")
        try:
            params[key] = json.loads(value)
        except ValueError:
            params[key] = value
    return params
