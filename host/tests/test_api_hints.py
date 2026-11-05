"""session.hints: 使い方の落とし穴の記録（host/api/hints.json）。診断でなく、確かめる事実と手順の文書。実在しないメソッドを指さない。"""
import pytest

from fprobe.api.spec import load_hints, load_spec

SPEC = load_spec()
HINTS = load_hints()["hints"]


def test_hint_ids_are_unique_and_every_field_is_filled():
    ids = [h["id"] for h in HINTS]
    assert len(ids) == len(set(ids)) and ids
    for h in HINTS:
        assert h["title"] and h["symptom"] and h["methods"] and h["recipe"], h["id"]
        for c in h["facts_to_check"]:
            assert c["api"] and c["reads"], h["id"]


@pytest.mark.parametrize("hint", HINTS, ids=[h["id"] for h in HINTS])
def test_a_hint_names_only_methods_that_exist(hint):
    """ヒントが指す API（付けるメソッド・確かめる事実を読む API）は、実在する（仕様にある）。古くなったら試験が落ちる。"""
    for name in [*hint["methods"], *(c["api"] for c in hint["facts_to_check"])]:
        assert name in SPEC.methods, f"{hint['id']}: {name} は API に無い"


def test_the_daemon_serves_hints_by_method_or_id(tmp_path):
    from test_api_daemon import FakeBackend, _serve
    from fprobe.api.client import ApiError, DaemonClient
    url, _, stop = _serve(FakeBackend(), tmp_path)
    try:
        ai = DaemonClient(url, "claude", "ai")
        everything = ai.call("session.hints")
        assert everything["total"] == len(HINTS) and everything["version"] == 1
        for_open = ai.call("session.hints", method="linux.halt.open")["hints"]
        assert for_open and all("linux.halt.open" in h["methods"] for h in for_open)
        one = ai.call("session.hints", id=HINTS[0]["id"])
        assert one["total"] == 1 and one["hints"][0]["id"] == HINTS[0]["id"]
        assert ai.call("session.hints", method="la.capture")["total"] == 1 and ai.call("session.hints", method="probe.info")["total"] == 0   # 付いていないメソッドは空（断らない）
        with pytest.raises(ApiError, match="ヒントは無い"):
            ai.call("session.hints", id="no.such.hint")
        ai.close()
    finally:
        stop()


def test_a_failed_call_names_the_hints_attached_to_that_method(tmp_path):
    """失敗の応答の data.hint_ids に、そのメソッドに付いたヒントの id が付く（メソッド名での引き当てだけ。原因の判断はしない）。"""
    from test_api_daemon import FakeBackend, _serve
    from fprobe.api.client import ApiError, DaemonClient
    url, _, stop = _serve(FakeBackend(), tmp_path)
    try:
        ai = DaemonClient(url, "claude", "ai")
        with pytest.raises(ApiError) as bad_args:                    # 引数の誤り: watch.set に付いたヒント
            ai.call("watch.set", expression="s_ticks")
        assert "watch.expressions_list" in bad_args.value.data["hint_ids"]
        with pytest.raises(ApiError) as device:                      # 機器の誤り（偽の機器は target.memory.read を断る）
            ai.call("target.memory.read", address=0x20000000, length=16)
        assert device.value.data.get("exception")                        # 機器の誤りには、型違いの when を持つヒントは付かない
        assert "args.integer_not_string" not in device.value.data.get("hint_ids", [])
        with pytest.raises(ApiError) as plain:                       # ヒントの無いメソッドの誤りには欄を足さない
            ai.call("probe.info", nonsense=1)
        assert "hint_ids" not in plain.value.data
        ai.close()
    finally:
        stop()


def test_a_hint_with_when_is_attached_only_to_the_kind_of_failure_it_is_about(tmp_path):
    """when を持つヒントは、失敗の種類が合うときだけ hint_ids に付く: 必須の引数が無い失敗に『整数に文字列を渡すな』は付かない。
    （新規利用者の役の Haiku の 2 回目: 必須の抜けにヒントが食い違った）"""
    from test_api_daemon import FakeBackend, _serve
    from fprobe.api.client import ApiError, DaemonClient
    url, _, stop = _serve(FakeBackend(), tmp_path)
    try:
        ai = DaemonClient(url, "claude", "ai")
        with pytest.raises(ApiError) as missing:                      # 必須の抜け: validator = required
            ai.call("target.connect", target="x")
        assert "args.integer_not_string" not in missing.value.data.get("hint_ids", [])
        with pytest.raises(ApiError) as wrong_type:                   # 型違い: validator = type
            ai.call("target.connect", target="x", frequency_hz="fast")
        assert "args.integer_not_string" in wrong_type.value.data["hint_ids"]
        with pytest.raises(ApiError) as unknown_arg:                  # watch.set の未知の引数: additionalProperties
            ai.call("watch.set", expression="s_ticks")
        assert "watch.expressions_list" in unknown_arg.value.data["hint_ids"]
        ai.close()
    finally:
        stop()


def test_every_when_names_known_keys():
    for h in HINTS:
        for key, values in h.get("when", {}).items():
            assert key in ("code", "validator", "exception") and values and all(isinstance(v, str) for v in values), h["id"]
