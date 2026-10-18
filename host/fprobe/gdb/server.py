"""デーモンの GDB の待ち受け: 接続ごとに利用者（Client、via = "gdb"）を作り、操作をデーモンの調停と記録に通す。

待ち受けは常に 127.0.0.1（RSP には認証が無い。LAN から使うなら SSH のトンネルで）。
"""

import asyncio
from typing import Any, Callable

from .session import GdbSession, Options

__all__ = ["GdbServer"]

HOST = "127.0.0.1"


class _DaemonHost:
    """GdbHost の実装: デーモン（fprobe.api.daemon.Daemon）の run / invoke に、この接続の利用者として渡す。"""

    def __init__(self, daemon: Any, client: Any):
        self.daemon, self.client = daemon, client

    async def run(self, fn: Callable[[], Any], *, control: bool, record: tuple[str, dict] | None = None,
                  moves: bool = False) -> Any:
        return await self.daemon.run(self.client, "control" if control else "read", fn, record=record, moves=moves)

    async def call(self, method: str, params: dict) -> Any:
        return await self.daemon.invoke(self.client, method, params)


class GdbServer:
    def __init__(self, daemon: Any, probe: Any, options: Options | None = None, role: str = "human"):
        self.daemon, self.probe, self.role = daemon, probe, role
        self.options = options or Options()
        self._server: asyncio.base_events.Server | None = None
        self._sessions: set[asyncio.Task] = set()

    async def start(self, port: int) -> int:
        """待ち受けを始め、実際のポート（0 を渡すと空いているもの）を返す。"""
        self._server = await asyncio.start_server(self._connection, HOST, port)
        return self._server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            for task in list(self._sessions):
                task.cancel()
            await self._server.wait_closed()

    async def _connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername") or ("?", 0)
        client = self.daemon.new_client(f"gdb {peer[0]}:{peer[1]}", self.role, via="gdb")
        self.daemon.join(client)
        task = asyncio.current_task()
        self._sessions.add(task)
        try:
            session = GdbSession(reader, writer, _DaemonHost(self.daemon, client), self.probe, f"gdb{client.id}",
                                 self.options)
            await session.serve()
        finally:
            self._sessions.discard(task)
            self.daemon.leave(client)
