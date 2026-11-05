"""デーモンの Starlette アプリ: /api（WebSocket の JSON-RPC）・/（画面）・/login・/assets。起動の手順（GDB・流れ）も持つ。"""

import asyncio
import contextlib
import hmac
import json
import secrets
import urllib.parse
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.responses import FileResponse, HTMLResponse, RedirectResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from . import E_INTERNAL, E_INVALID_PARAMS, E_INVALID_REQUEST, E_PARSE, device_of
from .auth import (LOCAL_NAMES, SESSION_COOKIE, SESSION_MAX_AGE_S, _host_name, _login_page, session_cookie)
from .binary import BinaryResult, encode_frame
from .core import Daemon, _Connection
from .jsonable import _NonFinite, _refuse_constant
from .permissions import Permissions
from .spec import Spec, load_spec
from ..gdb.server import GdbServer
from ..gdb.session import Options as GdbOptions

UI_DIR = Path(__file__).resolve().parents[2] / "ui" / "dist"     # 画面のビルドの出力（host/ui）
BUILD_HELP = ("<!doctype html><meta charset=utf-8><title>fprobe</title>"
              "<p>画面がまだビルドされていません。<pre>cd host/ui\nnpm install\nnpm run build</pre>"
              "<p>API（ws://…/api）はこのままでも使えます。")



def create_app(backend: Any, *, token: str | None, activity_dir: Path, spec: Spec | None = None,
               device: dict | None = None, ui_dir: Path = UI_DIR, watch_s: float = 1.0, gdb_port: int | None = None,
               gdb_options: GdbOptions | None = None, gdb_role: str = "human", streams: list[dict] = (),
               stream_logs: list[dict] = (), permissions: Permissions | None = None) -> Starlette:
    """gdb_port を渡すと GDB サーバも開く（0 なら空いているポート。実際のポートは daemon.gdb_port）。GDB の接続の
    利用者の role は gdb_role（既定 human: IDE を操作するのは人。人の一時停止中も GDB の操作は通る）。
    streams（stream_serve の引数の並び）・stream_logs（stream_log_start の引数の並び）は起動時に始める。
    permissions: 許可の表（None ならパスを確かめない。CLI は必ず読んで渡す）。token: 合言葉（None なら無し。CLI は必ず持つ）。"""
    daemon = Daemon(backend, token, activity_dir, spec or load_spec(), device or device_of(None, None, None), watch_s,
                    permissions)
    session_value = None if token is None else session_cookie(token)

    def has_session(cookies) -> bool:
        given = cookies.get(SESSION_COOKIE)
        return session_value is not None and given is not None and hmac.compare_digest(
            given.encode("ascii", "replace"), session_value.encode("ascii"))

    async def api(ws: WebSocket) -> None:
        # ブラウザは WebSocket に同一生成元の制限をかけないので、訪れた Web ページがプローブを操作できてしまう。
        # Origin を送る接続（＝ブラウザ）は、デーモン自身が配る画面（Origin がこのデーモンの Host と同じ）だけ通す。
        # Python・MCP の利用者は Origin を送らない。DNS rebinding（攻撃者の名前を 127.0.0.1 に向け直すと Origin と Host が
        # そろう）を防ぐため、トークンの無いデーモンは Host の名前もこの PC の名前に限る（--lan はトークンが守る）
        origin, host = ws.headers.get("origin"), ws.headers.get("host", "")
        if origin is not None and (origin not in (f"http://{host}", f"https://{host}")
                                   or (daemon.token is None and _host_name(host) not in LOCAL_NAMES)):
            await ws.close(code=1008)
            return
        await ws.accept()
        if daemon.loop is None:                          # 起動の手順（lifespan）を通らない起動でも通知を配る
            daemon.loop = asyncio.get_running_loop()
        conn = _Connection(ws)
        conn.authed = has_session(ws.cookies)
        daemon.connections.add(conn)
        tasks: set[asyncio.Task] = set()

        async def serve(request: dict) -> None:
            try:
                reply = await daemon.handle(conn, request)
            except Exception as e:                       # 想定外でも必ず応答する（利用者を待たせない）
                reply = {"jsonrpc": "2.0", "id": request.get("id"),
                         "error": {"code": E_INTERNAL, "message": f"{type(e).__name__}: {e}",
                                   "data": {"exception": type(e).__name__, "detail": str(e)}}}
            if reply is None or "id" not in request:    # 通知（id なし）には、誤りでも応答しない（JSON-RPC 2.0）
                return
            result = reply.get("result")
            if isinstance(result, BinaryResult):        # バイナリの応答: JSON-RPC の応答 + 区画を 1 つのフレームで
                await ws.send_bytes(encode_frame({**reply, "result": result.result}, result.sections))
            else:
                await ws.send_json(reply)

        try:
            while True:
                text = await ws.receive_text()
                try:
                    request = json.loads(text, parse_constant=_refuse_constant)
                except _NonFinite as e:                    # 要求の id が要るので、字句だけ無害な値にして読み直し、id 付きで断る
                    try:
                        rid = json.loads(text, parse_constant=lambda _t: None).get("id")
                    except (ValueError, AttributeError):
                        rid = None
                    await ws.send_json({"jsonrpc": "2.0", "id": rid,
                                        "error": {"code": E_INVALID_PARAMS,
                                                  "message": f"JSON に無い字句 {e.args[0]} は使えない（数は有限の値）"}})
                    continue
                except (ValueError, RecursionError):        # 深すぎる入れ子は RecursionError（受信の輪から漏らさない）
                    await ws.send_json({"jsonrpc": "2.0", "id": None,
                                        "error": {"code": E_PARSE, "message": "JSON として読めない"}})
                    continue
                if not isinstance(request, dict):           # 一括（配列）の要求は扱わない
                    await ws.send_json({"jsonrpc": "2.0", "id": None,
                                        "error": {"code": E_INVALID_REQUEST, "message": "要求は 1 つのオブジェクト"}})
                    continue
                task = asyncio.create_task(serve(request))      # 長い操作の間も他の要求（read）を受ける
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        except WebSocketDisconnect:
            pass
        finally:
            daemon.connections.discard(conn)
            if conn.client is not None:
                daemon.arbiter.leave(conn.client)
                daemon.forget(conn.client)
                daemon.announce_clients()
            for task in tasks:
                task.cancel()

    async def index(request) -> FileResponse | HTMLResponse | RedirectResponse:
        if token is not None and not has_session(request.cookies):
            return RedirectResponse("/login", status_code=303)
        page = ui_dir / "index.html"
        # 作り直すと assets の名前が変わるので、ページは毎回確かめさせる（古いページが消えた資産を指して白くならない）
        return FileResponse(page, headers={"Cache-Control": "no-cache"}) if page.exists() else HTMLResponse(BUILD_HELP)

    async def login(request) -> HTMLResponse | RedirectResponse:
        """画面の合言葉を 1 回だけ入れてもらい、HttpOnly の cookie にする（URL に合言葉を載せない）。"""
        if token is None:
            return RedirectResponse("/", status_code=303)
        if request.method == "GET":
            return HTMLResponse(_login_page(""))
        origin, host = request.headers.get("origin"), request.headers.get("host", "")
        if origin is not None and origin not in (f"http://{host}", f"https://{host}"):
            return HTMLResponse(_login_page("ほかのページからの送信は受けない"), status_code=403)
        body = (await request.body()).decode("utf-8", "replace")
        given = urllib.parse.parse_qs(body).get("token", [""])[0].strip()
        if not secrets.compare_digest(given.encode("utf-8", "surrogatepass"), token.encode("utf-8", "surrogatepass")):
            return HTMLResponse(_login_page("合言葉が違う"), status_code=403)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(SESSION_COOKIE, session_value, max_age=SESSION_MAX_AGE_S, path="/", httponly=True,
                            samesite="strict", secure=request.url.scheme == "https")
        return response

    @contextlib.asynccontextmanager
    async def lifespan(app):
        daemon.loop = asyncio.get_running_loop()
        watcher = asyncio.create_task(daemon.watch())
        gdb = None
        if gdb_port is not None:
            gdb = GdbServer(daemon, backend, gdb_options, gdb_role)
            daemon.gdb_port = await gdb.start(gdb_port)
        for options in streams:
            await asyncio.to_thread(backend.stream_serve, **options)
        for options in stream_logs:
            await asyncio.to_thread(backend.stream_log_start, **options)
        yield
        if gdb is not None:
            await gdb.close()
        watcher.cancel()

    app = Starlette(routes=[WebSocketRoute("/api", api), Route("/", index), Route("/login", login, methods=["GET", "POST"]),
                            Mount("/assets", StaticFiles(directory=ui_dir / "assets", check_dir=False))],
                    lifespan=lifespan)
    app.state.daemon = daemon
    return app
