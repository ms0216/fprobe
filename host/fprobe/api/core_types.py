"""デーモンの核が共有する型と定数: 誤り（_Rpc・LinkRecovered）、接続（_Connection）、要求ごとの文脈（source・フック中）、
コアを動かし得る操作などの名前の集合。core.py の Daemon と、そこから分けた部品（core_session・core_watch・core_hooks）が使う。"""

import asyncio
import contextvars

from starlette.websockets import WebSocket

from .arbiter import Client
from .spec import load_hints

NOTIFY_LIMIT = 1000                                          # 利用者ごとの送り途中の通知の上限
WATCHER = Client(0, "daemon", "script")                    # 見張りの読み（記録しない）。見張りが見つけた止まりのフックも
_SOURCE = contextvars.ContextVar("fprobe_source", default=None)      # 要求の source（どの窓から。操作の記録に残す）
_IN_HOOK = contextvars.ContextVar("fprobe_in_hook", default=False)   # フックの動作の中（その操作はフックを起こさない）
# コアを動かし得る操作。終わるたびに見張りの状態の run を 1 増やす（同じ場所で止まり直しても画面が読み直せる）
# 記録を開く・始める・止める操作: 終わったら通知 "recording" を送る（画面の時間軸・記録のビューが読み直す。だれが
# 変えても届く。操作の記録の文字列に頼ると、読み直しが記録に残る場合に止まらなくなる）。値は通知の action
RECORDING_METHODS = {"recording.open": "open", "recording.start": "start", "recording.stop": "stop"}
RUN_METHODS = {"target.connect", "target.halt", "target.resume", "target.step", "target.reset", "target.flash",
               "target.program_ram", "target.debug_mode.set", "debug.run_until_halt", "debug.step", "debug.call",
               # Linux のプロセスの全コア停止デバッグ（attach している間、target.state はそのプロセスの止まった場所）
               "linux.halt.debug.attach", "linux.halt.debug.run", "linux.halt.debug.halt", "linux.halt.debug.step",
               "linux.halt.debug.close", "linux.halt.debug.select", "linux.halt.close"}
# 止める操作: 受けた時点で（作業スレッドの順番を待たずに）機器の実装へ止める要求だけを渡す（backend.request_halt_now。
# 長い待ち（linux.halt.debug.run 等）の中で全コアを止める）。保留される要求（一時停止中の human 以外）は渡さない
INTERRUPT_METHODS = {"target.halt", "linux.halt.debug.halt"}
# 状態の読み: 機器の実装が「作業スレッドを通さずに答えられる」と言えば（backend.read_unqueued が None 以外）、長い操作の
# 後ろに並ばずに答える（Linux のプロセスの全コア停止デバッグ: 機器の錠だけを短く取る。attach の取り寄せ・長い run の間も）
UNQUEUED_READS = {"target.status", "system.status"}


def _who(client: Client) -> dict:
    """利用者の名乗り {id, name, role}（実装への caller と通知 recording に渡す）。"""
    return {"id": client.id, "name": client.name, "role": client.role}


class LinkRecovered(Exception):
    """機器の USB が切れたので開き直した（B28）。ターゲットの接続は切れている。文言は説明と元の誤り。"""


def _applies(hint: dict, context: dict | None) -> bool:
    """ヒントの when（失敗の種類。キー → 値の一覧）が、失敗の事実（context）と合うか。when_not は逆で、当たる失敗には付けない
    （未接続の失敗に、症状が応答の中身のヒントを付けない、等）。どちらも無ければ常に合う。context が無いとき（種類を知らない失敗）は
    when・when_not を持つヒントを付けない。"""
    when, when_not = hint.get("when"), hint.get("when_not")
    if not when and not when_not:
        return True
    if context is None:
        return False
    return (all(context.get(key) in values for key, values in (when or {}).items())
            and not any(context.get(key) in values for key, values in (when_not or {}).items()))


def _with_hints(method: str, data: dict | None, context: dict | None = None) -> dict | None:
    """失敗の応答の data に、そのメソッドに付いたヒントの id（hint_ids。session.hints で引く）を添える。無ければ data のまま。
    メソッド名と、ヒントの when（失敗の種類。context = {"code": "invalid_params" | "device", "validator": 検証の種類 | "exception": 例外の型}）
    での引き当てだけ（失敗の原因が何かは判断しない）。"""
    try:
        ids = [h["id"] for h in load_hints()["hints"] if method in h["methods"] and _applies(h, context)]
    except (OSError, ValueError, KeyError, TypeError):          # hints.json が読めない・壊れている: 元の誤りを隠さない
        return data
    if not ids:
        return data
    return {**(data or {}), "hint_ids": ids}


class _Rpc(Exception):
    """JSON-RPC の誤り。data は構造の欄（機器の誤りなら exception = 例外の型の名前・detail = 例外の文そのまま。
    画面は文の形を推して頭を外さず、detail を出す）。"""
    def __init__(self, code: int, message: str, data: dict | None = None):
        super().__init__(message)
        self.code = code
        self.data = data


class _Connection:
    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.client: Client | None = None
        self.topics: set[str] = set()
        self.sending = 0                                   # 送り途中の通知の数（NOTIFY_LIMIT まで）
        self.dropped = 0                                   # 読まないので捨てた通知の数
        self.tasks: set[asyncio.Task] = set()
        self.authed = False                                # 画面の cookie が合った（hello の token は要らない）
