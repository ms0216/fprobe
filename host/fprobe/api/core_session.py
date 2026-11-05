"""デーモンの要求の検査と session.* の答え: メソッド・引数の検査（_validate）、PC のパスの許可（_permit）、session.hints・
session.methods・session.hello。機器には触れない。Daemon に混ぜて使う（self.spec・self.permissions・self.token 等は Daemon が持つ）。"""

import secrets
from pathlib import Path

from . import API_VERSION, E_INVALID_PARAMS, E_METHOD_NOT_FOUND, E_NOT_ALLOWED, E_VERSION
from .arbiter import Client
from .core_types import _Connection, _Rpc, _with_hints
from .jsonable import _has_lone_surrogate
from .spec import InvalidParams, cut, load_hints


class SessionMixin:
    def _validate(self, method: str, params: dict) -> None:
        """メソッドが仕様にあり、引数が仕様に合うか（合わなければ _Rpc）。"""
        if method not in self.spec.methods:
            front = method.rsplit(".", 1)[0] if "." in method else ""
            same = [n for n in self.spec.methods if n.startswith(front + ".")] if front else []
            near = same[:12]
            more = f"（ほか {len(same) - len(near)} 件。session.methods の prefix で全部）" if len(same) > len(near) else ""
            raise _Rpc(E_METHOD_NOT_FOUND, f"{cut(method, 80)} は無い（session.methods でメソッドの一覧と説明が取れる。prefix に名前の先頭を渡す。"
                       + (f"同じ前半 {cut(front, 60)!r} のメソッド: {', '.join(near)}{more}" if near else "例: prefix=\"target.\"") + "）",
                       {"reason": "unknown_method", "candidates": near, "candidates_total": len(same)})
        try:
            self.spec.validate_params(method, params)
        except InvalidParams as e:
            raise _Rpc(E_INVALID_PARAMS, str(e), _with_hints(method, None, {"code": "invalid_params", "validator": e.validator})) from None
        if _has_lone_surrogate(params):
            raise _Rpc(E_INVALID_PARAMS, f"{method}: 引数に孤立サロゲート（UTF-8 にできない文字。JSON の ud800 等）がある")

    def _permit(self, method: str, params: dict) -> dict:
        """PC のパスの引数（仕様の x-path）を許可の表で確かめ、解いた絶対パスに置き換えた引数を返す。表に無ければ _Rpc。"""
        if self.permissions is None:
            return params
        rules = self.spec.methods[method].paths
        if not rules:
            return params
        props = self.spec.methods[method].params.get("properties", {})
        out = dict(params)
        for name, (access, when) in rules.items():
            value = params.get(name)
            if value is None:
                continue
            if when is not None and not params.get(when, props.get(when, {}).get("default", False)):
                continue
            resolved = self.permissions.check(access, value)
            if resolved is None:
                shown = str(Path(value).expanduser().resolve())
                what = "実行する" if access == "exec" else "書く"
                raise _Rpc(E_NOT_ALLOWED,
                           f"{method}: {name} の {shown} は許可の表に無い（{what}パスは {self.permissions.file} の "
                           f"\"{access}\" に利用者が書く。API からは変えられない）",
                           {"reason": "path_not_permitted", "param": name, "access": access, "path": shown,
                            "permissions_file": None if self.permissions.file is None else str(self.permissions.file)})
            out[name] = str(resolved)
        return out

    def _hints(self, params: dict) -> dict:
        """session.hints: 使い方の落とし穴の記録（host/api/hints.json の写し）。メソッド名か id で引く。判断は入れない。"""
        doc = load_hints()
        picked = doc["hints"]
        if params.get("id") is not None:
            picked = [h for h in picked if h["id"] == params["id"]]
            if not picked:
                raise _Rpc(E_INVALID_PARAMS, f"session.hints: id {params['id']!r} のヒントは無い（id の一覧は引数なしで取れる）")
        if params.get("method") is not None:
            picked = [h for h in picked if params["method"] in h["methods"]]
        return {"version": doc["version"], "total": len(picked), "hints": picked}

    def _methods(self, params: dict) -> dict:
        """session.methods: この API 自身の説明（仕様 host/api/fprobe_api.json の写し）。判断は入れない: 並べ替えず、仕様の順。"""
        methods, name = self.spec.methods, params.get("name")
        if name is not None:
            if name not in methods:
                starts = [n for n in methods if n.startswith(name)] if name else []
                near = starts[:30]
                more = f"（ほか {len(starts) - len(near)} 件）" if len(starts) > len(near) else ""
                raise _Rpc(E_INVALID_PARAMS, f"session.methods: {cut(repr(name), 80)} というメソッドは無い"
                           + (f"（この名前で始まるメソッド: {', '.join(near)}{more}）" if near else
                              "（prefix に名前の先頭を渡すと一覧が返る。例: prefix=\"target.\"）"),
                           {"reason": "unknown_method", "candidates": near, "candidates_total": len(starts)})
            m = methods[name]
            return {"api_version": API_VERSION, "total": 1, "methods": [
                {"name": m.name, "kind": m.kind, "summary": m.summary, "params": m.params, "result": self.spec.result_schema(name),
                 "binary": m.binary, "irreversible": m.irreversible}]}
        prefix = params.get("prefix", "")
        picked = [m for m in methods.values() if m.name.startswith(prefix)]
        with_summary = params.get("with_summary", False)
        return {"api_version": API_VERSION, "total": len(picked), "methods": [
            {"name": m.name, "kind": m.kind, **({"summary": m.summary} if with_summary else {})} for m in picked]}

    def _hello(self, conn: _Connection, params: dict) -> dict:
        if conn.client is not None:
            raise _Rpc(E_NOT_ALLOWED, "session.hello は 1 回だけ（名乗り直すと一時停止・保留の持ち主が分からなくなる）")
        # バイト列で比べる（compare_digest は ASCII でない str で TypeError を投げ、誤りの答えが内部の誤りになる）
        if self.token is not None and not conn.authed and not secrets.compare_digest(
                str(params.get("token") or "").encode("utf-8", "surrogatepass"), self.token.encode("utf-8", "surrogatepass")):
            raise _Rpc(E_NOT_ALLOWED, "トークンが違う（このデーモンの合言葉は、起動した利用者の ~/.fprobe/daemon_token。"
                                      "画面は /login で入れる）", {"reason": "token"})
        if params["api_version"] != API_VERSION:
            raise _Rpc(E_VERSION, f"API の版 {params['api_version']} はこのデーモン（{API_VERSION}）と合わない")
        conn.client = Client(next(self._ids), params["name"], params["role"], automated=params.get("automated"))
        self.announce_clients()
        return {"api_version": API_VERSION, "boot": self.boot, "client_id": conn.client.id, "device": self.device,
                "control": self.arbiter.state()}
