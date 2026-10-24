"""fprobe の API（デーモンと利用者の約束）。仕様の正は host/api/fprobe_api.json。"""

API_VERSION = 2

# 誤りのコード（JSON-RPC の error.code。-32000〜-32099 はアプリケーション定義）
E_PARSE = -32700
E_INVALID_REQUEST = -32600
E_METHOD_NOT_FOUND = -32601
E_INVALID_PARAMS = -32602
E_INTERNAL = -32603
E_VERSION = -32010
E_NO_HELLO = -32011
E_NOT_ALLOWED = -32012
E_DEVICE = -32020
E_NOT_CONNECTED = -32021
E_TIMEOUT = -32022
E_NO_DATA = -32023


class NotConnected(RuntimeError):
    """ターゲットに接続していない（E_NOT_CONNECTED）。機能の実装（Probe）が投げ、デーモンは型で判別する。"""


class NoData(RuntimeError):
    """読む物がまだ無い（取っていない・開いていない。E_NO_DATA）。機器の誤りと分けるため: 画面は「無い」と
    「読めなかった」を見分けて出す。機能の実装（Probe）が投げ、デーモンは型で判別する。"""
