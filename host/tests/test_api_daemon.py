"""fprobe.api.daemon / client: 偽の機器（backend）でデーモンを動かし、2 つの利用者から使う。"""

import json
import threading
import time

import pytest
import uvicorn

from fake_daemon import FakeProbe
from fprobe.api import (API_VERSION, E_DEVICE, E_INVALID_PARAMS, E_NO_DATA, E_NO_HELLO, E_NOT_CONNECTED, E_VERSION,
                        NoData, NotConnected)
from fprobe.api.client import ApiError, DaemonClient
from fprobe.api.daemon import create_app


class FakeBackend(FakeProbe):
    """デーモンの試験用: 機器なしの偽物（fake_daemon.FakeProbe）に、試験で起こしたい振る舞いを足したもの。"""

    def __init__(self):
        super().__init__()
        self.connected = True                                      # 接続済みから始める
        self.halt_s = 0.01
        self.status_error: Exception | None = None

    def target_halt(self):
        time.sleep(self.halt_s)
        return super().target_halt()

    def la_view(self, start=0, end=None, width=1000):
        return {"levels": ["0" * width]}

    def target_resume(self):
        pass                                                         # すぐ同じ場所で止まり直す（ループの中のブレークポイント）

    def target_status(self):
        if self.status_error is not None:
            raise self.status_error
        return {"state": super().target_status()["state"]}

    def run_until_halt(self, timeout_s=5.0):
        raise NotConnected("ターゲット未接続（target_connect を先に呼ぶ）")

    def target_disconnect(self):
        raise RuntimeError("ターゲット未接続ではないが、文言に「未接続」を含む機器の誤り")

    def probe_info(self):
        # 実物の probe_info はケイパビリティのチャンネルを frozenset で返す（2026-10-17 にデーモンが応答できず固まった）
        return {"channels": frozenset({3, 0, 1}), "raw": bytes([1, 255]), "nested": (1, 2)}

    def target_voltage(self):
        return {"bad": object()}                                     # JSON にできない値

    def target_reset(self, halt=False):
        raise SystemExit("機器の処理が BaseException を投げた")


def _serve(backend, tmp_path, **kw):
    """偽の機器でデーモンを起動する。(WebSocket の URL, app, 止める関数) を返す。"""
    app = create_app(backend, token=None, activity_dir=tmp_path / "activity", **kw)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]

    def stop():
        server.should_exit = True
        thread.join(5)
    return f"ws://127.0.0.1:{port}/api", app, stop


@pytest.fixture
def daemon(tmp_path):
    url, app, stop = _serve(FakeBackend(), tmp_path)
    yield url, app
    stop()


def test_two_clients_operate_and_activity_flows(daemon):
    url, app = daemon
    gui = DaemonClient(url, "gui", "human")
    ai = DaemonClient(url, "claude", "ai")
    seen = []
    gui.subscribe(["activity"], lambda topic, data: seen.append(data))
    assert ai.call("target.halt") == {"state": "HALTED"}
    assert gui.call("target.status") == {"state": "HALTED"}
    deadline = time.monotonic() + 2
    while len(seen) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert [(e["client"], e["method"]) for e in seen[:2]] == [("claude", "target.halt"), ("gui", "target.status")]
    gui.close(); ai.close()


def test_pause_holds_the_ai_until_resume(daemon):
    url, _ = daemon
    gui = DaemonClient(url, "gui", "human")
    ai = DaemonClient(url, "claude", "ai")
    gui.call("session.control.pause")
    result = []
    t = threading.Thread(target=lambda: result.append(ai.call("target.halt")))
    t.start()
    time.sleep(0.2)
    assert result == [] and gui.call("session.status")["control"]["held"] == 1
    gui.call("session.control.resume")
    t.join(2)
    assert result == [{"state": "HALTED"}]
    gui.close(); ai.close()


def test_params_and_errors(daemon):
    url, _ = daemon
    ai = DaemonClient(url, "claude", "ai")
    with pytest.raises(ApiError) as e:
        ai.call("debug.run_until_halt", timeout_s="soon")
    assert e.value.code == E_INVALID_PARAMS


def test_device_error_is_reported_and_daemon_survives(daemon):
    url, _ = daemon
    ai = DaemonClient(url, "claude", "ai")
    with pytest.raises(ApiError) as e:
        ai.call("debug.run_until_halt", timeout_s=1.0)
    assert e.value.code == E_NOT_CONNECTED and "未接続" in str(e.value)       # 未接続は専用のコード
    assert ai.call("target.status")["state"] in ("HALTED", "RUNNING")


def test_no_data_has_its_own_code(tmp_path):
    """まだ無い（取っていない・開いていない）は機器の誤りと分ける: 画面は「無い」と「読めなかった」を見分けて出す。"""
    backend = FakeBackend()
    backend.status_error = NoData("プロファイルが無い（profile_start）")
    url, _, stop = _serve(backend, tmp_path)
    try:
        ai = DaemonClient(url, "claude", "ai")
        with pytest.raises(ApiError) as e:
            ai.call("target.status")
        assert e.value.code == E_NO_DATA and "プロファイルが無い" in str(e.value)
        ai.close()
    finally:
        stop()


def test_hello_and_version_are_required(daemon):
    url, _ = daemon
    with pytest.raises(ApiError) as e:
        DaemonClient(url, "old", "ai", api_version=999)
    assert e.value.code == E_VERSION
    raw = DaemonClient.raw(url)
    assert raw.request("target.status", {})["error"]["code"] == E_NO_HELLO


def test_disconnect_drops_pending(daemon):
    url, _ = daemon
    gui = DaemonClient(url, "gui", "human")
    ai = DaemonClient(url, "claude", "ai")
    gui.call("session.control.pause")
    threading.Thread(target=lambda: _swallow(lambda: ai.call("target.halt")), daemon=True).start()
    time.sleep(0.2)
    ai.close()
    time.sleep(0.2)
    assert gui.call("session.status")["control"]["held"] == 0
    gui.close()


def _swallow(fn):
    try:
        fn()
    except Exception:
        pass


def test_lan_requires_the_token(tmp_path):
    from starlette.testclient import TestClient
    app = create_app(FakeBackend(), token="s3cret", activity_dir=tmp_path)
    with TestClient(app) as tc, tc.websocket_connect("/api") as ws:
        ws.send_json({"jsonrpc": "2.0", "id": 1, "method": "session.hello",
                      "params": {"name": "x", "role": "ai", "api_version": API_VERSION}})
        assert ws.receive_json()["error"]["code"] == -32012


def test_requests_fail_fast_after_the_connection_is_lost(daemon):
    # デーモンとの接続が切れた後の要求は、待ち続けずにすぐ ConnectionError になる
    url, app = daemon
    ai = DaemonClient(url, "claude", "ai")
    ai._ws.close()
    ai._reader.join(2)
    with pytest.raises(ConnectionError):
        ai.call("target.status")


def test_results_are_made_json_safe(daemon):
    url, _ = daemon
    ai = DaemonClient(url, "claude", "ai")
    assert ai.call("probe.info") == {"channels": [0, 1, 3], "raw": "01ff", "nested": [1, 2]}
    ai.close()


def test_an_unencodable_result_is_an_error_not_a_hang(daemon):
    url, _ = daemon
    ai = DaemonClient(url, "claude", "ai")
    with pytest.raises(ApiError) as e:
        ai.call("probe.voltage")
    assert e.value.code == -32603
    assert ai.call("target.status")["state"] in ("HALTED", "RUNNING")       # その後も使える
    ai.close()


def test_browser_pages_from_other_origins_are_refused(tmp_path):
    # 訪れた Web ページが ws://127.0.0.1:7820 につないでプローブを操作できないこと（ブラウザは WebSocket に同一生成元の
    # 制限をかけない）。Origin を送らない利用者（Python・MCP）と、デーモン自身が配る画面は通す
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
    app = create_app(FakeBackend(), token=None, activity_dir=tmp_path)
    hello = {"jsonrpc": "2.0", "id": 1, "method": "session.hello",
             "params": {"name": "x", "role": "human", "api_version": API_VERSION}}
    with TestClient(app, base_url="http://127.0.0.1:7820") as tc:
        with pytest.raises(WebSocketDisconnect):
            with tc.websocket_connect("/api", headers={"origin": "https://evil.example"}) as ws:
                ws.send_json(hello)
                ws.receive_json()
        # DNS rebinding: 攻撃者の名前を 127.0.0.1 に向け直すと、Origin と Host がそろって攻撃者の名前になる
        with pytest.raises(WebSocketDisconnect):
            with tc.websocket_connect("/api", headers={"origin": "http://evil.example:7820",
                                                       "host": "evil.example:7820"}) as ws:
                ws.send_json(hello)
                ws.receive_json()
        with tc.websocket_connect("/api", headers={"origin": "http://127.0.0.1:7820",
                                                   "host": "127.0.0.1:7820"}) as ws:   # デーモン自身の画面
            ws.send_json(hello)
            assert "result" in ws.receive_json()
        with tc.websocket_connect("/api") as ws:
            ws.send_json(hello)
            assert "result" in ws.receive_json()


def test_a_second_hello_is_refused_so_the_pause_cannot_be_orphaned(daemon):
    # 名乗り直しで利用者の番号が変わると、一時停止した人が切断しても自動で解けなくなる（AI が永久に待つ）
    from fprobe.api import E_NOT_ALLOWED
    url, _ = daemon
    gui = DaemonClient(url, "gui", "human")
    with pytest.raises(ApiError) as e:
        gui.call("session.hello", name="gui2", role="human", api_version=API_VERSION)
    assert e.value.code == E_NOT_ALLOWED
    gui.call("session.control.pause")
    gui.close()
    ai = DaemonClient(url, "claude", "ai")
    time.sleep(0.2)
    assert ai.call("session.status")["control"]["paused"] is False
    ai.close()


def test_hello_reports_which_device_the_daemon_holds(tmp_path):
    from starlette.testclient import TestClient
    app = create_app(FakeBackend(), token=None, activity_dir=tmp_path, device={"link": "eth", "address": "192.168.77.2"})
    with TestClient(app) as tc, tc.websocket_connect("/api") as ws:
        ws.send_json({"jsonrpc": "2.0", "id": 1, "method": "session.hello",
                      "params": {"name": "x", "role": "ai", "api_version": API_VERSION}})
        assert ws.receive_json()["result"]["device"] == {"link": "eth", "address": "192.168.77.2"}


def test_an_operation_finished_after_the_client_left_is_still_recorded(daemon):
    url, app = daemon
    app.state.daemon.backend.halt_s = 0.5                            # 実行中に利用者が切れる
    ai = DaemonClient(url, "claude", "ai")
    threading.Thread(target=lambda: _swallow(lambda: ai.call("target.halt")), daemon=True).start()
    time.sleep(0.1)
    ai.close()
    time.sleep(0.8)
    log = app.state.daemon.activity.path
    assert log.exists() and any('"target.halt"' in line and "HALTED" in line
                                for line in log.read_text(encoding="utf-8").splitlines())


def test_activity_can_be_read_back_in_pages(daemon):
    url, _ = daemon
    c = DaemonClient(url, "claude", "ai")
    for _ in range(5):
        c.call("target.halt")
    assert [e["seq"] for e in c.call("session.activity.list", limit=3)["entries"]] == [3, 4, 5]
    assert [e["seq"] for e in c.call("session.activity.list", before=3, limit=10)["entries"]] == [1, 2]
    c.close()


def test_clients_are_announced(daemon):
    url, _ = daemon
    seen = []
    gui = DaemonClient(url, "gui", "human")
    gui.subscribe(["clients"], lambda topic, data: seen.append(sorted(c["name"] for c in data["clients"])))
    ai = DaemonClient(url, "claude", "ai")
    ai.close()
    time.sleep(0.3)
    assert seen[-2:] == [["claude", "gui"], ["gui"]]
    gui.close()


def test_target_state_is_watched_and_sent_only_on_change(tmp_path):
    backend = FakeBackend()
    url, _, stop = _serve(backend, tmp_path, watch_s=0.05)
    seen = []
    gui = DaemonClient(url, "gui", "human")
    gui.subscribe(["target.state"], lambda topic, data: seen.append(data["state"]))
    time.sleep(0.3)
    gui.call("target.halt")
    time.sleep(0.3)
    backend.connected = False
    time.sleep(0.3)
    assert seen == ["RUNNING", "HALTED", "DISCONNECTED"]
    assert gui.call("session.status")["target"]["state"] == "DISCONNECTED"
    assert all(e["method"] != "target.status" for e in gui.call("session.activity.list")["entries"])
    gui.close()
    stop()


def test_a_watch_error_other_than_disconnected_is_not_sent_and_the_daemon_survives(tmp_path):
    backend = FakeBackend()
    backend.status_error = OSError("USB が抜けた")
    url, _, stop = _serve(backend, tmp_path, watch_s=0.05)
    seen = []
    gui = DaemonClient(url, "gui", "human")
    gui.subscribe(["target.state"], lambda topic, data: seen.append(data))
    time.sleep(0.3)
    assert seen == [] and gui.call("session.status")["api_version"] == API_VERSION
    gui.close()
    stop()


def test_the_screen_is_served_or_explains_how_to_build_it(tmp_path):
    import urllib.request
    dist = tmp_path / "dist"

    def get(url, path):
        return urllib.request.urlopen(url.replace("ws://", "http://").removesuffix("/api") + path).read()

    url, _, stop = _serve(FakeBackend(), tmp_path, ui_dir=dist)
    assert "npm run build" in get(url, "/").decode()
    stop()
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<p>fprobe</p>", encoding="utf-8")
    (dist / "assets" / "a.js").write_text("1", encoding="utf-8")
    url, _, stop = _serve(FakeBackend(), tmp_path, ui_dir=dist)
    assert get(url, "/") == b"<p>fprobe</p>" and get(url, "/assets/a.js") == b"1"
    page = urllib.request.urlopen(url.replace("ws://", "http://").removesuffix("/api") + "/")
    assert page.headers["Cache-Control"] == "no-cache"     # 作り直した後に古いページが消えた資産を指さない
    stop()


def test_a_new_subscriber_gets_the_current_target_state(tmp_path):
    url, _, stop = _serve(FakeBackend(), tmp_path, watch_s=0.05)
    first = DaemonClient(url, "gui", "human")
    first.subscribe(["target.state"], lambda topic, data: None)
    time.sleep(0.3)                                   # 見張りが状態を知った後に
    seen = []
    second = DaemonClient(url, "gui2", "human")
    second.subscribe(["target.state"], lambda topic, data: seen.append(data["state"]))
    time.sleep(0.3)
    assert seen == ["RUNNING"]
    first.close()
    second.close()
    stop()


def test_the_daemon_instance_is_identified_so_a_restart_can_be_told_apart(tmp_path):
    boots = []
    for _ in range(2):
        url, _, stop = _serve(FakeBackend(), tmp_path)
        c = DaemonClient(url, "gui", "human")
        assert c.call("session.status")["boot"] == c.hello["boot"]
        boots.append(c.hello["boot"])
        c.close()
        stop()
    assert boots[0] != boots[1]


def test_a_stop_at_the_same_place_after_running_is_announced_again(tmp_path):
    # ループの中のブレークポイント: 再開してすぐ同じ pc で止まり直しても、画面が読み直せるように通知する
    url, _, stop = _serve(FakeBackend(), tmp_path, watch_s=0.05)
    seen = []
    gui = DaemonClient(url, "gui", "human")
    gui.subscribe(["target.state"], lambda topic, data: seen.append(data))
    gui.call("target.halt")
    time.sleep(0.3)
    gui.call("target.resume")
    time.sleep(0.3)
    gui.call("target.resume")
    time.sleep(0.3)
    halted = [s for s in seen if s["state"] == "HALTED"]
    assert len(halted) == 3 and len({s["run"] for s in halted}) == 3
    gui.close()
    stop()


def test_screen_reads_marked_not_to_record_stay_out_of_the_activity_log(daemon):
    # 拡大・移動のたびの la.view で、AI の作業の記録が埋もれない
    url, _ = daemon
    c = DaemonClient(url, "gui", "human")
    for _ in range(3):
        c.call("la.view", width=10)
    c.call("target.halt")
    assert [e["method"] for e in c.call("session.activity.list")["entries"]] == ["target.halt"]
    c.close()


def test_malformed_frames_get_json_rpc_errors_and_the_connection_survives(daemon):
    import json
    from websockets.sync.client import connect
    url, _ = daemon
    with connect(url) as ws:
        ws.send("これは JSON ではない")
        assert json.loads(ws.recv(timeout=2)) == {"jsonrpc": "2.0", "id": None,
                                                  "error": {"code": -32700, "message": "JSON として読めない"}}
        ws.send(json.dumps([1, 2]))
        reply = json.loads(ws.recv(timeout=2))
        assert reply["id"] is None and reply["error"]["code"] == -32600
        ws.send(json.dumps({"jsonrpc": "2.0", "method": "no.such"}))           # 通知（id なし）には応答しない
        ws.send(json.dumps({"jsonrpc": "2.0", "id": 7, "method": "session.hello",
                            "params": {"name": "x", "role": "script", "api_version": API_VERSION}}))
        assert json.loads(ws.recv(timeout=2))["id"] == 7


def test_the_python_client_survives_an_error_reply_without_an_id(daemon):
    url, _ = daemon
    c = DaemonClient(url, "claude", "ai")
    c._ws.send("壊れた要求")                                        # デーモンは id: null の誤りで答える
    time.sleep(0.2)
    assert c.call("session.status")["api_version"] == API_VERSION
    c.close()


def test_values_json_cannot_carry_become_null():
    from fprobe.api.daemon import jsonable
    assert jsonable({"a": float("nan"), "b": [float("inf"), 1.5]}) == {"a": None, "b": [None, 1.5]}


def test_the_error_code_comes_from_the_exception_type_not_its_words(daemon):
    url, _ = daemon
    c = DaemonClient(url, "claude", "ai")
    with pytest.raises(ApiError) as e:
        c.call("target.disconnect")                                  # 文言に「未接続」があっても型は機器の誤り
    assert e.value.code == E_DEVICE
    c.close()


def test_device_errors_carry_the_exception_type_and_text_as_separate_fields(daemon):
    # 画面は誤りの文の形（"RuntimeError: " の頭）を推して外さず、構造の欄 data.detail（例外の文そのまま）を出す
    url, _ = daemon
    c = DaemonClient(url, "claude", "ai")
    reply = c.request("target.disconnect", {})
    error = reply["error"]
    assert error["code"] == E_DEVICE
    assert error["data"]["exception"] == type_name(error["message"])
    assert error["message"] == f"{error['data']['exception']}: {error['data']['detail']}"
    c.close()


def type_name(message: str) -> str:
    return message.split(":", 1)[0]


def test_quiet_reads_by_the_screen_stay_out_of_the_log_but_operations_never_do(daemon):
    # 画面の自動の読み（止まるたびのレジスタ等）で AI の作業の記録を埋めない。状態を変える操作は quiet でも記録する
    import json
    from websockets.sync.client import connect
    url, _ = daemon
    with connect(url) as ws:
        def call(rid, method, params=None, **extra):
            ws.send(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}, **extra}))
            return json.loads(ws.recv(timeout=3))
        call(1, "session.hello", {"name": "gui", "role": "human", "api_version": API_VERSION})
        call(2, "target.registers.read", quiet=True)               # 観測だけ（control だが状態を変えない）
        call(3, "probe.info", quiet=True)                          # read
        call(4, "target.halt", quiet=True)                         # 操作: quiet は効かない
        call(5, "target.registers.read")                           # 印が無ければ記録する
        methods = [e["method"] for e in call(6, "session.activity.list")["result"]["entries"]]
    assert methods == ["target.halt", "target.registers.read"]


def test_notifications_to_a_client_that_stopped_reading_are_capped(tmp_path):
    # 読まない利用者（固まった画面）への送り待ちが上限なく増えて、デーモンのメモリを食わない
    import asyncio
    from fprobe.api.daemon import NOTIFY_LIMIT, Daemon, _Connection
    from fprobe.api.spec import load_spec

    class StuckSocket:
        async def send_json(self, message):
            await asyncio.Event().wait()                            # 永久に送り終わらない

    async def run():
        d = Daemon(FakeBackend(), None, tmp_path, load_spec(), {"link": "usb", "address": None})
        d.loop = asyncio.get_running_loop()
        conn = _Connection(StuckSocket())
        conn.topics = {"activity"}
        d.connections.add(conn)
        for i in range(NOTIFY_LIMIT + 50):
            d.publish("activity", {"seq": i})
        for _ in range(5):
            await asyncio.sleep(0)
        conn.client = d.new_client("ui", "human", "ws")
        listed = next(c for c in d.clients() if c["name"] == "ui")
        return conn.sending, conn.dropped, listed                 # ループを閉じると送り途中は取り消される
    sending, dropped, listed = asyncio.run(run())
    assert (sending, dropped) == (NOTIFY_LIMIT, 50)
    # 捨てたことを黙らない: 利用者の一覧（session.status の clients）に捨てた数と使った上限
    assert listed["notifications_dropped"] == 50 and listed["notify_limit"] == NOTIFY_LIMIT


def test_a_base_exception_from_the_device_is_still_answered(daemon):
    url, _ = daemon
    c = DaemonClient(url, "claude", "ai")
    with pytest.raises(ApiError) as e:
        c.call("target.reset")
    assert e.value.code == E_DEVICE and c.call("session.status")["api_version"] == API_VERSION
    c.close()


def test_the_time_a_request_was_held_by_a_pause_is_recorded(daemon):
    url, _ = daemon
    gui = DaemonClient(url, "gui", "human")
    ai = DaemonClient(url, "claude", "ai")
    gui.call("session.control.pause")
    t = threading.Thread(target=lambda: ai.call("target.halt"))
    t.start()
    time.sleep(0.4)
    gui.call("session.control.resume")
    t.join(5)
    time.sleep(0.1)
    entry = [e for e in gui.call("session.activity.list")["entries"] if e["method"] == "target.halt"][-1]
    assert entry["held_s"] >= 0.3 and entry["seconds"] >= entry["held_s"]
    gui.close()
    ai.close()


def test_operations_and_target_states_are_noted_for_the_recording(tmp_path):
    # 記録中なら、デーモンは操作の記録（AI と人）とターゲットの状態の変化を、機能の実装（Probe）の記録の口へ渡す
    backend = FakeBackend()
    noted = []
    backend.record_host_event = lambda stream, kind, body: noted.append((stream, kind, body))
    url, _, stop = _serve(backend, tmp_path, watch_s=0.05)
    gui = DaemonClient(url, "gui", "human")
    gui.subscribe(["target.state"], lambda topic, data: None)
    gui.call("target.halt")
    time.sleep(0.3)
    calls = [b for s, k, b in noted if (s, k) == ("host", "call")]
    states = [b["state"] for s, k, b in noted if (s, k) == ("target", "state")]
    assert any(c["method"] == "target.halt" and c["client"] == "gui" for c in calls)
    assert "HALTED" in states
    gui.close()
    stop()


def test_the_backend_can_publish_notifications_during_an_operation(tmp_path):
    # Flash の書き込み等の長い操作は、作業スレッドから進捗の通知を出す（デーモンが送り先をつなぐ）
    import asyncio
    from fprobe.api.daemon import Daemon, _Connection
    from fprobe.api.spec import load_spec

    class Socket:
        def __init__(self):
            self.sent = []

        async def send_json(self, message):
            self.sent.append(message)

    class Backend(FakeBackend):
        def attach_publisher(self, publish):
            self.publish = publish

    async def run():
        backend = Backend()
        d = Daemon(backend, None, tmp_path, load_spec(), {"link": "usb", "address": None})
        d.loop = asyncio.get_running_loop()
        conn = _Connection(Socket())
        conn.topics = {"flash.progress"}
        d.connections.add(conn)
        await asyncio.to_thread(backend.publish, "flash.progress", {"operation": "program", "fraction": 0.5})
        for _ in range(5):
            await asyncio.sleep(0)
        return conn.ws.sent
    sent = asyncio.run(run())
    assert sent == [{"jsonrpc": "2.0", "method": "flash.progress", "params": {"operation": "program", "fraction": 0.5}}]
    assert "flash.progress" in load_spec().notifications


def test_opening_starting_and_stopping_a_recording_is_announced_to_every_window(tmp_path):
    # 画面の時間軸・記録のビューは、だれ（AI・別の窓）が記録を変えても通知 recording で読み直す（操作の記録の文字列に
    # 頼らない）。失敗した操作は知らせない
    class Backend(FakeBackend):
        def recording_open(self, name):
            if name == "missing":
                raise KeyError("記録 'missing' が無い")
            return {"name": name, "events": 3, "span": [0, 10]}

        def recording_start(self, name="", events=True, segment_s=None, segment_bytes=None, max_bytes=None,
                            decimate_after_s=None, decimate_streams=None):
            return {"recording": "20261018-120000", "path": "x"}

        def recording_stop(self):
            return {"recording": "20261018-120000", "events": 5}

    url, _, stop = _serve(Backend(), tmp_path)
    try:
        gui = DaemonClient(url, "gui", "human")
        ai = DaemonClient(url, "claude", "ai")
        seen = []
        gui.subscribe(["recording"], lambda topic, data: seen.append(data))
        ai.call("recording.open", name="demo")
        with pytest.raises(ApiError):
            ai.call("recording.open", name="missing")
        ai.call("recording.start")
        ai.call("recording.stop")
        deadline = time.monotonic() + 2
        while len(seen) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.05)
        assert [(d["action"], d["name"]) for d in seen] == [("open", "demo"), ("start", "20261018-120000"),
                                                              ("stop", "20261018-120000")]
        assert seen[0]["client"]["name"] == "claude" and seen[0]["client"]["role"] == "ai"
        gui.close()
        ai.close()
    finally:
        stop()


# --- 流れの口（U05） ----------------------------------------------------------------------------

class StreamBackend(FakeBackend):
    """流れの口を持つ偽物: 起動時の公開を覚え、口からの書き込み（host 経由）を rtt_write で受ける。"""

    def __init__(self):
        super().__init__()
        self.served, self.logged, self.written = [], [], []
        self.host = None

    def attach_stream_host(self, host):
        self.host = host

    def stream_serve(self, stream, port=0, protocol="raw", writable=True):
        self.served.append((stream, port, protocol))
        return {}

    def stream_log_start(self, stream, path, timestamps=False, append=True):
        self.logged.append((stream, path, timestamps))
        return {}

    def rtt_write(self, channel=0, text=None, data_hex=None, timeout_s=1.0):
        self.written.append((channel, data_hex))
        return {"written": len(bytes.fromhex(data_hex))}


def test_streams_are_served_at_start_and_their_writes_are_arbitrated_and_recorded(tmp_path):
    backend = StreamBackend()
    url, _, stop = _serve(backend, tmp_path, streams=[{"stream": "rtt0", "port": 0, "protocol": "telnet"}],
                          stream_logs=[{"stream": "rtt0", "path": "x.log", "timestamps": True}])
    try:
        assert backend.served == [("rtt0", 0, "telnet")] and backend.logged == [("rtt0", "x.log", True)]
        gui = DaemonClient(url, "gui", "human")
        client = backend.host.join("tcp:rtt0")
        wait = time.monotonic() + 3
        while not any(c["name"] == "tcp:rtt0" and c["via"] == "tcp" for c in gui.call("session.status")["clients"]):
            assert time.monotonic() < wait
            time.sleep(0.02)
        gui.call("session.control.pause")
        done = threading.Event()
        threading.Thread(target=lambda: (backend.host.call(client, "rtt.write", {"channel": 0, "data_hex": "6869"}),
                                         done.set()), daemon=True).start()
        assert not done.wait(0.3) and backend.written == []                   # 人の一時停止中は待つ
        gui.call("session.control.resume")
        assert done.wait(3) and backend.written == [(0, "6869")]
        entries = gui.call("session.activity.list")["entries"]
        assert any(e["client"] == "tcp:rtt0" and e["method"] == "rtt.write" for e in entries)
        backend.host.leave(client)
        gui.close()
    finally:
        stop()


def test_stream_options_are_parsed():
    from fprobe.api.daemon import parse_stream, parse_stream_log
    assert parse_stream("rtt0=19021:telnet") == {"stream": "rtt0", "port": 19021, "protocol": "telnet"}
    assert parse_stream("swo=3443") == {"stream": "swo", "port": 3443, "protocol": "raw"}
    # 行の頭の時刻は流れの名前から決めず（RTT にもバイナリの流れがある）、使う側が :time で明示したときだけ
    assert parse_stream_log("rtt0=a.log") == {"stream": "rtt0", "path": "a.log", "timestamps": False}
    assert parse_stream_log("rtt0=C:\\logs\\a.log:time") == {"stream": "rtt0", "path": "C:\\logs\\a.log",
                                                             "timestamps": True}
    assert parse_stream_log("swo=a.bin")["timestamps"] is False
    for bad in ("rtt0", "rtt0=x", "=1"):
        with pytest.raises(ValueError):
            parse_stream(bad)


def test_binary_methods_answer_with_one_binary_frame_that_matches_the_json_view(daemon):
    # timeline.tiles（バイナリの応答）: 同じ捕捉の la.view と同じ結果に間引ける。誤りはテキストの JSON-RPC のまま
    from timeline_ref import wave_states
    url, app = daemon
    gui = DaemonClient(url, "gui", "human")
    with pytest.raises(ApiError):
        gui.call("timeline.tiles")                                   # 捕捉がまだ無い
    gui.call("la.capture")
    tile = gui.call("timeline.tiles", t0=1000, t1=7000)
    assert tile["wave"]["mode"] == "raw" and tile["source"] == "capture" and tile["capture"]["length"] == 8192
    assert set(tile["sections"]) == set(tile["data"]) == {f"wave.0.{ch}" for ch in range(10)}
    view = app.state.daemon.backend._elf.la_view(1500, 6500, 333)    # 本物の la.view（FakeBackend は差し替えている）
    assert wave_states(tile, tile["data"], 1500, 6500, 333) == view["levels"]
    with pytest.raises(ApiError) as e:
        gui.call("timeline.tiles", source="capture", streams=["uart"])
    assert e.value.code == E_DEVICE
    gui.close()


def test_binary_methods_are_refused_to_callers_that_cannot_take_frames(daemon):
    # GDB の monitor・フックの動作（WebSocket でない利用者）はバイナリの応答を受け取れない
    import asyncio
    from fprobe.api import E_NOT_ALLOWED
    from fprobe.api.daemon import _Rpc
    _, app = daemon
    d = app.state.daemon
    with pytest.raises(_Rpc) as e:
        asyncio.run(d.invoke(d.new_client("gdb", "human", "gdb"), "timeline.tiles", {}))
    assert e.value.code == E_NOT_ALLOWED


def test_the_lan_token_is_kept_across_restarts(tmp_path):
    """--lan の合言葉は、指定が無ければ保存したものを使い回す（デーモンを起動し直しても画面の URL が変わらない。
    2026-10-18 に FPGA の書き替えでデーモンを止めるたびに URL が変わった）。--new-token で作り直す。"""
    from fprobe.api.daemon import lan_token
    first = lan_token(None, tmp_path)
    assert first and lan_token(None, tmp_path) == first
    assert lan_token("given", tmp_path) == "given" and lan_token(None, tmp_path) == first   # 指定は保存を変えない
    renewed = lan_token(None, tmp_path, renew=True)
    assert renewed != first and lan_token(None, tmp_path) == renewed


class LinkBackend(FakeBackend):
    """USB が切れた誤りを 1 回出し、recover_link で直る偽物（B28）。"""

    def __init__(self):
        super().__init__()
        self.lost = False
        self.recovered = []

    def target_status(self):
        if self.lost:
            raise OSError("USBError: [Errno 5] Input/Output Error")
        return super().target_status()

    def recover_link(self, error):
        if not self.lost:
            return None
        self.lost, self.connected = False, False
        self.recovered.append(str(error))
        return {"message": "Primer の USB が切れたので、つなぎ直した。", "changed": {"sampler": [True, False]}}


def test_a_lost_usb_link_is_recovered_and_the_caller_is_told(tmp_path):
    backend = LinkBackend()
    url, _, stop = _serve(backend, tmp_path)
    try:
        ai = DaemonClient(url, "claude", "ai")
        backend.lost = True
        with pytest.raises(ApiError) as e:
            ai.call("target.status")
        assert e.value.code == E_DEVICE and "つなぎ直した" in str(e.value) and "Errno 5" in str(e.value)
        assert len(backend.recovered) == 1
        with pytest.raises(ApiError) as e:                       # 開き直した: 次はふつうに答える（ターゲットは切れた）
            ai.call("target.status")
        assert e.value.code == E_NOT_CONNECTED
    finally:
        stop()


def test_the_watcher_recovers_a_lost_link_and_reports_the_target_disconnected(tmp_path):
    backend = LinkBackend()
    url, _, stop = _serve(backend, tmp_path, watch_s=0.05)
    seen = []
    gui = DaemonClient(url, "gui", "human")
    gui.subscribe(["target.state"], lambda topic, data: seen.append(data))
    try:
        time.sleep(0.2)
        backend.lost = True
        deadline = time.monotonic() + 3
        while not any(x["state"] == "DISCONNECTED" for x in seen) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert backend.recovered and seen[-1]["state"] == "DISCONNECTED"
        lost = seen[-1]["link_lost"]                             # 画面が理由を出せる（訳せるよう、文でなく値で）
        assert lost["changed"] == {"sampler": [True, False]} and lost["at"] > 0
        time.sleep(0.3)
        assert seen[-1].get("link_lost") == lost                 # 見張りの次の読みで消えない
        backend.connected = True                                 # ターゲットにつなぎ直すと消える
        deadline = time.monotonic() + 3
        while seen[-1]["state"] == "DISCONNECTED" and time.monotonic() < deadline:
            time.sleep(0.05)
        assert "link_lost" not in seen[-1]
    finally:
        gui.close()
        stop()


def test_reading_a_recording_does_not_wait_for_a_slow_device_operation(tmp_path):
    # 記録を読むだけの要求（offline: 時間軸のタイル・記録の検索等）は機器の作業スレッドで実行しない: 重い機器の操作の
    # 間も返り、重い記録の読み（大きな記録のタイル）の間も機器の操作・状態の読みが止まらない
    backend = FakeBackend()
    backend.halt_s = 2.0
    gate = threading.Event()

    def slow_overview(width=1000, streams=None):
        gate.wait(5)
        return {"span": None}
    backend.recording_overview = slow_overview
    backend.recording_status = lambda: {"recording": False, "name": None}
    url, app, stop = _serve(backend, tmp_path)
    try:
        client = DaemonClient(url, "gui", "human")
        other = DaemonClient(url, "gui2", "human")
        halting = threading.Thread(target=lambda: client.call("target.halt"))
        halting.start()
        time.sleep(0.2)
        t = time.monotonic()
        assert other.call("recording.status") == {"recording": False, "name": None}       # offline: 機器を待たない
        assert time.monotonic() - t < 1.0
        halting.join()
        reading = threading.Thread(target=lambda: client.call("recording.overview", width=10))
        reading.start()
        time.sleep(0.2)
        t = time.monotonic()
        assert other.call("target.status")["state"] == "HALTED"                           # 重い読みの間も機器は動く
        assert time.monotonic() - t < 1.0
        gate.set()
        reading.join()
    finally:
        stop()


def _entries(app, method):
    log = app.state.daemon.activity.path
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
        hits = [e for e in rows if e.get("method") == method]
        if hits:
            return hits
        time.sleep(0.01)
    return []


def test_activity_records_where_the_call_came_from_and_whether_a_person_made_it(daemon):
    # 使われ方の記録（docs/superpowers/specs/2026-10-23-ui-rebuild-design.md §3.4）: どの窓から（要求の source）・
    # つながり方（via）・自動操作か（hello の automated。画面は navigator.webdriver を申告する）を、事実として残す
    url, app = daemon
    gui = DaemonClient(url, "gui", "human", automated=True, source="panel:registers")
    gui.call("target.halt")
    entry = _entries(app, "target.halt")[0]
    assert (entry["via"], entry["automated"], entry["source"]) == ("api", True, "panel:registers")
    script = DaemonClient(url, "x", "script")
    script.call("target.status")
    entry = _entries(app, "target.status")[0]
    assert entry["via"] == "api" and "automated" not in entry and "source" not in entry   # 申告の無いものは書かない
    gui.close(); script.close()


def test_a_malformed_source_is_refused(daemon):
    url, _ = daemon
    c = DaemonClient(url, "gui", "human")
    for bad in (5, "", "x" * 65):
        reply = c.request_with("target.status", {}, source=bad)
        assert reply["error"]["code"] == -32600
    c.close()
