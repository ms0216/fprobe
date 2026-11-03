"""API の応答の照合（tests/api_result_check.py）を、ほかの試験が呼ばないメソッドに効かせる: フックの登録簿（hooks.*）。

登録簿は機器に触らないので、機器なしの Probe で本物のメソッドを呼ぶ（応答は差し込みが仕様の result で照合する）。
実行の記録（hooks.log）はデーモンがフックを走らせて書くので、偽の機器のデーモンで走らせた記録を照合する。
"""

import api_result_check
from fprobe.mcp_server import Probe
from test_api_hooks import HookBackend, _wait
from test_api_daemon import _serve
from fprobe.api.client import DaemonClient


def test_hook_table_methods_answer_in_the_shape_of_the_spec(tmp_path):
    p = Probe("unused")
    added = p.hooks_add("reset", "before", [{"evaluate": "g_state"}, {"mark": "{event}"},
                                            {"call": "target.status", "params": {}}], name="前", required=True)
    assert added["operations"]
    assert p.hooks_list()["hooks"][0]["id"] == added["id"]
    path = tmp_path / "hooks.json"
    assert p.hooks_save(str(path))["hooks"] == 1
    assert p.hooks_load(str(path), replace=False)["loaded"] == 1
    assert p.hooks_log()["entries"] == []
    assert p.hooks_remove(all=True)["removed"] == 2


def test_the_log_of_a_hook_run_by_the_daemon_answers_in_the_shape_of_the_spec(tmp_path):
    backend = HookBackend()
    url, _, stop = _serve(backend, tmp_path, watch_s=0.05)
    try:
        ai = DaemonClient(url, "ai", "ai")
        ai.call("hooks.add", event="reset", phase="before", actions=[{"evaluate": "g_state"}], name="前")
        ai.call("hooks.add", event="reset", phase="after", actions=[{"mark": "{event}/{phase} by {client}"},
                                                                    {"call": "target.status"}])
        ai.call("target.reset")
        _wait(lambda: len(backend.hook_table.read_log()) >= 2)
        log = ai.call("hooks.log")
        ai.close()
    finally:
        stop()
    api_result_check.check("hooks.log", log)        # デーモンの偽の機器は Probe でないので、応答を直接照合する
    assert {r["ok"] for e in log["entries"] for r in e["results"]} == {True}
