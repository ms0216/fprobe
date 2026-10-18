"""プラグイン: 本体の汎用の API の上に載る、取り外せる層（特定の機器・用途のための処理）。

約束（docs/superpowers/specs/2026-10-18-u41-xtrig-pin-output-design.md §6.1）:
- 1 つのプラグインは `fprobe.plugins.<名前>`（1 つのモジュール）。本体（mcp_server・デーモン・API）からは import しない。
  利用者・AI が明示して import して使う。
- プラグインが呼ぶのは本体の公開 API（host/api/fprobe_api.json の名前と引数）だけ。呼び先は `call(method, **params)` を
  持つもの: デーモンにつないだ `fprobe.api.client.DaemonClient`、またはプロセス内の Probe を包む `ProbeCaller`。
  どちらでも同じプラグインが同じように動く。
- 状態はプラグインのオブジェクトが持つ（デバイスには汎用の設定しか書かない）。
"""

from typing import Any, Protocol

__all__ = ["ApiCaller", "ProbeCaller"]


class ApiCaller(Protocol):
    def call(self, method: str, /, **params: Any) -> Any: ...


class ProbeCaller:
    """プロセス内の Probe（fprobe.mcp_server.Probe）を API の名前で呼ぶ（デーモンと同じ引数の検査をする）。"""

    def __init__(self, probe):
        from ..api.spec import load_spec
        self.probe = probe
        self.spec = load_spec()

    def call(self, method: str, /, **params: Any) -> Any:
        self.spec.validate_params(method, params)
        impl = self.spec.methods[method].impl
        if not impl:
            raise ValueError(f"{method} はデーモンのセッションの操作（Probe には無い）")
        return getattr(self.probe, impl)(**params)
