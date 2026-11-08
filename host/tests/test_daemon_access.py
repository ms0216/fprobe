"""デーモンの受け口の権限（docs/superpowers/specs/2026-11-03-daemon-access-design.md）: 実行・書き込みの許可の表、
既定のトークン、画面の合言葉の cookie。"""

import json
import os

import pytest
from starlette.testclient import TestClient

from fprobe.api import API_VERSION, E_NOT_ALLOWED
from fprobe.api.daemon import create_app, daemon_token
from fprobe.api.permissions import Permissions, PermissionsFileError, load_permissions, prepare_permissions
from fprobe.api.spec import load_spec

HELLO = {"name": "x", "role": "ai", "api_version": API_VERSION}


class Backend:
    """呼ばれた実装と引数を覚えるだけの機器（パスの確かめはデーモンの仕事なので、実装は何もしない）。"""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def target_status(self):
        return {"state": "DISCONNECTED"}

    def hooks_save(self, path):
        self.calls.append(("hooks_save", {"path": path}))
        return {"path": path, "hooks": 0}

    def la_decoder_load(self, path):
        self.calls.append(("la_decoder_load", {"path": path}))
        return {"loaded": [], "origin": path, "decoders": []}

    def semihosting_configure(self, enabled=True, file_root=None, writable=False, cmdline=""):
        self.calls.append(("semihosting_configure", {"file_root": file_root, "writable": writable}))
        return {"enabled": enabled, "file_root": file_root, "writable": writable, "requests": 0, "exit": None,
                "watching": False}


def _call(ws, method, params, rid=2):
    ws.send_json({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
    return ws.receive_json()


# --- 許可の表 ---------------------------------------------------------------------------------------------

def test_the_table_lists_absolute_paths_and_a_missing_file_allows_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    missing = load_permissions(tmp_path / "permissions.json")
    assert (missing.loaded, missing.exec, missing.write) == (False, (), ())
    assert missing.check("write", str(tmp_path / "x")) is None
    f = tmp_path / "permissions.json"
    f.write_text(json.dumps({"exec": [str(tmp_path / "dec" / "uart.py")], "write": ["~/out"]}), encoding="utf-8")
    p = load_permissions(f)
    assert p.loaded and p.write == ((tmp_path / "home" / "out").resolve(),)
    assert p.exec == ((tmp_path / "dec" / "uart.py").resolve(),)


@pytest.mark.parametrize("body", [{"write": ["relative/dir"]}, {"write": [""]}, {"write": "C:/x"}, {"exec": [3]},
                                  {"read": []}, [], "x"])
def test_a_malformed_table_is_an_error_not_silently_ignored(tmp_path, body):
    f = tmp_path / "permissions.json"
    f.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(PermissionsFileError):
        load_permissions(f)


def test_a_folder_allows_what_is_below_it_and_a_file_only_itself(tmp_path):
    out, dec = tmp_path / "out", tmp_path / "dec" / "uart.py"
    p = Permissions(tmp_path / "p.json", True, exec=(dec.resolve(),), write=(out.resolve(),))
    assert p.check("write", str(out / "a" / "b.vcd")) == (out / "a" / "b.vcd").resolve()
    assert p.check("write", str(out)) == out.resolve()
    assert p.check("write", str(out / ".." / "evil.vcd")) is None                # .. で外へ
    assert p.check("write", str(tmp_path / "outside")) is None
    assert p.check("exec", str(dec)) == dec.resolve()
    assert p.check("exec", str(dec.parent / "other.py")) is None
    assert p.check("write", str(dec)) is None                                    # 種類が違う
    assert p.check("exec", str(out / "x.py")) is None


@pytest.mark.skipif(not hasattr(os, "symlink") or os.name == "nt", reason="シンボリックリンク（POSIX）")
def test_a_link_inside_an_allowed_folder_cannot_lead_outside(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "link").symlink_to(tmp_path / "secret", target_is_directory=True)
    p = Permissions(tmp_path / "p.json", True, exec=(), write=(out.resolve(),))
    assert p.check("write", str(out / "link" / "x")) is None


def test_every_parameter_that_runs_or_writes_a_pc_file_is_marked_in_the_spec():
    marked = {(m.name, name): rule for m in load_spec().methods.values() for name, rule in m.paths.items()}
    assert {k: v[0] for k, v in marked.items()} == {
        ("la.decoder_load", "path"): "exec", ("linux.memap.connect", "xsdb"): "exec",
        **{(m, "vcd_path"): "write" for m in ("la.capture", "la.collect", "la.deep.capture", "la.deep.collect",
                                                "la.stream")},
        ("la.stream", "raw_path"): "write", ("la.raw_to_vcd", "vcd_path"): "write",
        ("la.export", "path"): "write", ("recording.export", "path"): "write", ("snapshot.save", "path"): "write",
        ("snapshot.export_core", "out"): "write", ("target.flash.read", "path"): "write",
        ("rules.states", "dot_path"): "write", ("rules.report", "path"): "write", ("hooks.save", "path"): "write", ("linux.halt.profile.save", "path"): "write",
        ("stream.log_start", "path"): "write", ("profile.export", "path"): "write",
        ("testpoint.export", "path"): "write", ("testpoint.instrument", "out_dir"): "write",
        ("testpoint.instrument", "map_path"): "write", ("linux.core.fetch", "local_path"): "write",
        ("debug.semihosting.configure", "file_root"): "write"}
    assert marked[("debug.semihosting.configure", "file_root")][1] == "writable"


def _app(tmp_path, permissions, token=None):
    return create_app(Backend(), token=token, activity_dir=tmp_path / "activity", permissions=permissions)


def test_a_path_outside_the_table_is_refused_with_the_reason_and_nothing_runs(tmp_path):
    p = Permissions(tmp_path / "permissions.json", True, exec=(), write=((tmp_path / "out").resolve(),))
    app = _app(tmp_path, p)
    with TestClient(app) as tc, tc.websocket_connect("/api") as ws:
        _call(ws, "session.hello", HELLO, 1)
        r = _call(ws, "hooks.save", {"path": str(tmp_path / "elsewhere.json")})
        assert r["error"]["code"] == E_NOT_ALLOWED
        assert r["error"]["data"] == {"reason": "path_not_permitted", "param": "path", "access": "write",
                                      "path": str((tmp_path / "elsewhere.json").resolve()),
                                      "permissions_file": str(tmp_path / "permissions.json"),
                                      "hint_ids": ["paths.write_permitted_dirs"]}      # 断られた失敗には、その書き出し先のヒントが付く
        assert "permissions.json" in r["error"]["message"]
        r = _call(ws, "la.decoder_load", {"path": str(tmp_path / "out" / "dec.py")}, 3)   # 書いてよい所でも実行は別
        assert r["error"]["data"]["access"] == "exec"
    assert app.state.daemon.backend.calls == []


def test_a_permitted_path_reaches_the_implementation_resolved(tmp_path):
    out = tmp_path / "out"
    p = Permissions(tmp_path / "permissions.json", True, exec=(), write=(out.resolve(),))
    app = _app(tmp_path, p)
    with TestClient(app) as tc, tc.websocket_connect("/api") as ws:
        _call(ws, "session.hello", HELLO, 1)
        r = _call(ws, "hooks.save", {"path": str(out / "sub" / ".." / "hooks.json")})
        assert r["result"]["path"] == str((out / "hooks.json").resolve())
    assert app.state.daemon.backend.calls == [("hooks_save", {"path": str((out / "hooks.json").resolve())})]


def test_a_conditional_mark_is_checked_only_when_its_flag_is_true(tmp_path):
    p = Permissions(tmp_path / "permissions.json", True, exec=(), write=())
    app = _app(tmp_path, p)
    with TestClient(app) as tc, tc.websocket_connect("/api") as ws:
        _call(ws, "session.hello", HELLO, 1)
        assert "result" in _call(ws, "debug.semihosting.configure", {"file_root": str(tmp_path)})   # 読むだけ
        r = _call(ws, "debug.semihosting.configure", {"file_root": str(tmp_path), "writable": True}, 3)
        assert r["error"]["data"]["reason"] == "path_not_permitted"
        assert "result" in _call(ws, "debug.semihosting.configure", {}, 4)                 # パスなし


def test_the_table_is_visible_through_the_api_but_cannot_be_changed_there(tmp_path):
    p = Permissions(tmp_path / "permissions.json", True, exec=((tmp_path / "x.py").resolve(),), write=())
    with TestClient(_app(tmp_path, p)) as tc, tc.websocket_connect("/api") as ws:
        _call(ws, "session.hello", HELLO, 1)
        r = _call(ws, "session.permissions", {})["result"]
        assert r == {"checked": True, "file": str(tmp_path / "permissions.json"), "loaded": True, "created": False,
                     "exec": [str((tmp_path / "x.py").resolve())], "write": []}
    assert not [m for m in load_spec().methods if m.startswith("session.permissions.")]   # 変える口は無い
    with TestClient(_app(tmp_path, None)) as tc, tc.websocket_connect("/api") as ws:
        _call(ws, "session.hello", HELLO, 1)
        r = _call(ws, "session.permissions", {})["result"]
        assert r == {"checked": False, "file": None, "loaded": False, "created": False, "exec": [], "write": []}


def test_the_first_start_writes_a_table_that_allows_only_the_out_folder(tmp_path):
    # 表のファイルが無いとき（初めての起動）: <FPROBE_HOME>/out だけに書いてよく、実行は何も許さない表を作り、作ったことを返す
    home = tmp_path / "fprobe"
    p = prepare_permissions(home / "permissions.json", home)
    out = (home / "out").resolve()
    assert (p.loaded, p.created, p.exec, p.write) == (True, True, (), (out,))
    assert out.is_dir()                                                        # すぐ書ける
    assert json.loads((home / "permissions.json").read_text(encoding="utf-8")) == {"exec": [], "write": [str(out)]}
    assert p.check("write", str(out / "a.vcd")) == out / "a.vcd"
    assert p.check("write", str(home / "elsewhere.vcd")) is None
    assert p.as_dict()["created"] is True


def test_an_existing_table_is_never_rewritten(tmp_path):
    home = tmp_path / "fprobe"
    home.mkdir()
    f = home / "permissions.json"
    text = json.dumps({"write": [str(tmp_path / "mine")]})
    f.write_text(text, encoding="utf-8")
    p = prepare_permissions(f, home)
    assert (p.created, p.write) == (False, ((tmp_path / "mine").resolve(),))
    assert f.read_text(encoding="utf-8") == text
    f.write_text("{}", encoding="utf-8")                                        # 利用者が全部消した表もそのまま
    assert prepare_permissions(f, home).write == () and f.read_text(encoding="utf-8") == "{}"
    f.write_text("[", encoding="utf-8")                                         # 壊れた表も書き換えず、誤りにする
    with pytest.raises(PermissionsFileError):
        prepare_permissions(f, home)
    assert f.read_text(encoding="utf-8") == "["


def test_the_created_table_is_reported_through_the_api(tmp_path):
    home = tmp_path / "fprobe"
    p = prepare_permissions(home / "permissions.json", home)
    with TestClient(_app(tmp_path, p)) as tc, tc.websocket_connect("/api") as ws:
        _call(ws, "session.hello", HELLO, 1)
        r = _call(ws, "session.permissions", {})["result"]
        assert r["created"] is True and r["write"] == [str((home / "out").resolve())] and r["exec"] == []


# --- 既定のトークン ------------------------------------------------------------------------------------------

@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX のファイルの権限")
def test_the_daemon_token_is_made_once_kept_and_readable_only_by_its_owner(tmp_path):
    first = daemon_token(None, tmp_path)
    assert first and daemon_token(None, tmp_path) == first
    assert (tmp_path / "daemon_token").stat().st_mode & 0o777 == 0o600
    assert daemon_token("given", tmp_path) == "given" and daemon_token(None, tmp_path) == first
    renewed = daemon_token(None, tmp_path, renew=True)
    assert renewed != first and (tmp_path / "daemon_token").read_text(encoding="ascii") == renewed


def test_a_local_client_reads_the_saved_token_by_itself(tmp_path, monkeypatch):
    import threading
    import time
    import uvicorn
    from fprobe.api.client import ApiError, DaemonClient
    monkeypatch.setenv("FPROBE_HOME", str(tmp_path))
    token = daemon_token(None, tmp_path)
    app = create_app(Backend(), token=token, activity_dir=tmp_path / "activity")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.01)
    url = f"ws://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}/api"
    try:
        with DaemonClient(url, "script", "script") as c:
            assert c.call("session.status")["api_version"] == API_VERSION
        (tmp_path / "daemon_token").write_text("wrong", encoding="ascii")
        with pytest.raises(ApiError) as e:
            DaemonClient(url, "script", "script")
        assert e.value.code == E_NOT_ALLOWED
    finally:
        server.should_exit = True
        thread.join(5)


# --- 画面の合言葉の cookie ------------------------------------------------------------------------------------

def test_the_screen_asks_for_the_token_once_and_keeps_it_in_an_http_only_cookie(tmp_path):
    app = _app(tmp_path, None, token="s3cret")
    with TestClient(app, base_url="http://127.0.0.1:7820") as tc:
        r = tc.get("/", follow_redirects=False)
        assert r.status_code in (302, 303, 307) and r.headers["location"].endswith("/login")
        assert "daemon_token" in tc.get("/login").text                               # どこにあるかを書く
        r = tc.post("/login", data={"token": "wrong"}, follow_redirects=False)
        assert r.status_code == 403 and "set-cookie" not in r.headers
        r = tc.post("/login", data={"token": "s3cret"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/"
        cookie = r.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie and "s3cret" not in cookie
        assert tc.get("/", follow_redirects=False).status_code == 200
        # TestClient の WebSocket は cookie の入れ物を使わないので、ブラウザが送るとおりに明示する
        browser = {"origin": "http://127.0.0.1:7820", "host": "127.0.0.1:7820",
                   "cookie": f"fprobe_session={tc.cookies['fprobe_session']}"}
        with tc.websocket_connect("/api", headers=browser) as ws:
            assert "result" in _call(ws, "session.hello", {**HELLO, "role": "human"}, 1)   # hello の token なし


def test_a_login_posted_from_another_page_is_refused(tmp_path):
    app = _app(tmp_path, None, token="s3cret")
    with TestClient(app, base_url="http://127.0.0.1:7820") as tc:
        r = tc.post("/login", data={"token": "s3cret"}, headers={"origin": "https://evil.example"},
                    follow_redirects=False)
        assert r.status_code == 403 and "set-cookie" not in r.headers


def test_without_the_cookie_the_screen_is_told_to_log_in(tmp_path):
    app = _app(tmp_path, None, token="s3cret")
    with TestClient(app, base_url="http://127.0.0.1:7820") as tc, \
            tc.websocket_connect("/api", headers={"origin": "http://127.0.0.1:7820", "host": "127.0.0.1:7820"}) as ws:
        r = _call(ws, "session.hello", {**HELLO, "role": "human"}, 1)
        assert r["error"]["code"] == E_NOT_ALLOWED and r["error"]["data"] == {"reason": "token"}


def test_the_login_page_names_an_icon_so_the_browser_does_not_ask_for_favicon_ico():
    # ログインページが icon を指定しないと、ブラウザが /favicon.ico を求めて 404 になり、コンソールに誤りが出た（GUI の評価）
    from fprobe.api.auth import _login_page
    assert "rel=icon" in _login_page("")
