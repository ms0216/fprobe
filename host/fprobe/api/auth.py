"""デーモンの画面の入口の認証: 合言葉（daemon_token）・画面の cookie・ログインの頁・Host ヘッダの名前。"""

import hashlib
import hmac
import html
import os
import secrets
from pathlib import Path

LOCAL_NAMES = {"127.0.0.1", "localhost", "[::1]"}


def _host_name(host: str) -> str:
    """Host ヘッダからポートを除いた名前（"[::1]:7820" → "[::1]"）。"""
    return host[:host.index("]") + 1] if host.startswith("[") else host.split(":")[0]


SESSION_COOKIE = "fprobe_session"
SESSION_MAX_AGE_S = 365 * 24 * 3600
TOKEN_FILE = "daemon_token"


def session_cookie(token: str) -> str:
    """画面の cookie の値（合言葉そのものでなく、合言葉から導いた値）。"""
    return hmac.new(token.encode("utf-8", "surrogatepass"), b"fprobe-session", hashlib.sha256).hexdigest()


def _login_page(message: str) -> str:
    note = f"<p style=color:#b00>{html.escape(message)}</p>" if message else ""
    return ("<!doctype html><meta charset=utf-8><link rel=icon href=data:,><title>fprobe</title>"
            "<p>このデーモンの合言葉を入れてください（初めて開くときだけ。ブラウザが覚えます）。"
            f"<p>合言葉は、デーモンを起動した利用者の <code>~/.fprobe/{TOKEN_FILE}</code> にあります。{note}"
            "<form method=post action=/login><input name=token type=password autofocus size=40> "
            "<button>開く</button></form>")


def daemon_token(given: str | None, home: Path, renew: bool = False) -> str:
    """デーモンの合言葉（127.0.0.1 でも --lan でも）。指定があればそれ（保存は変えない）。無ければ home/daemon_token に
    保存したものを使い回し、無いか renew なら作って保存する（同じ利用者の Python・MCP はこのファイルを読む）。"""
    if given:
        return given
    path = home / TOKEN_FILE
    if not renew and path.is_file():
        saved = path.read_text(encoding="ascii").strip()
        if saved:
            return saved
    token = secrets.token_urlsafe(16)
    home.mkdir(parents=True, exist_ok=True)
    # 持ち主だけが読める（同じ PC のほかの利用者が読めばデーモンを操作できる）。前からあるファイルも狭める
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        if hasattr(os, "fchmod"):
            os.fchmod(f.fileno(), 0o600)
        f.write(token)
    return token
