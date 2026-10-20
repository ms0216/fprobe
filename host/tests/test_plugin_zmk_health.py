"""プラグイン zmk_health: ZMK のキーボードを止めずに診断する（BLE・チャタリング除け・電池・起動からの時間・スタック）。"""

import pytest

from fprobe.plugins.zmk_health import ZmkHealth

VALUES = {
    "acl_conns[0].state": {"enum": "BT_CONN_CONNECTED", "value": 7},
    "acl_conns[0].role": 1,
    "acl_conns[0].le.interval": 6,
    "acl_conns[0].le.latency": 30,
    "acl_conns[0].le.timeout": 400,
    "acl_conns[0].le.phy.tx_phy": 2,
    "kscan_matrix_config_0.debounce_config.debounce_press_ms": 3,
    "kscan_matrix_config_0.debounce_config.debounce_release_ms": 5,
    "kscan_matrix_config_0.debounce_scan_period_ms": 1,
    "kscan_matrix_config_0.poll_period_ms": 10,
    "last_state_of_charge": 0,
    "curr_tick": 32768 * 3600,
}
TASKS = {"tasks": [
    {"name": "BT LW WQ", "stack": {"size": 1408, "peak_used": 936}},
    {"name": "hot", "stack": {"size": 1000, "peak_used": 900}},
    {"name": "no stack info"},
]}


class FakeApi:
    def __init__(self, values=VALUES, tasks=TASKS):
        self.values, self.tasks, self.calls = values, tasks, []

    def call(self, method, /, **params):
        self.calls.append((method, params))
        if method == "debug.evaluate":
            assert params["live"] is True                      # 止めない
            if params["expression"] not in self.values:
                raise RuntimeError(f"{params['expression']} は無い")
            return {"value": self.values[params["expression"]]}
        if method == "rtos.tasks":
            assert params["live"] is True
            return self.tasks
        raise AssertionError(method)


def test_report_reads_everything_without_halting():
    r = ZmkHealth(FakeApi()).report()
    (conn,) = r["ble"]
    assert conn == {"index": 0, "state": "BT_CONN_CONNECTED", "role": "peripheral", "interval_ms": 7.5,
                    "latency": 30, "timeout_ms": 4000, "tx_phy": "2M"}
    assert r["debounce"] == {"press_ms": 3, "release_ms": 5, "scan_period_ms": 1, "poll_period_ms": 10}
    assert r["battery_percent"] == 0 and r["uptime_s"] == pytest.approx(3600)
    assert r["stacks"][0] == {"name": "BT LW WQ", "size": 1408, "peak": 936, "percent": 66.5}


def test_findings_point_out_what_needs_attention():
    findings = ZmkHealth(FakeApi()).report()["findings"]
    assert any("hot" in f and "90" in f for f in findings)            # スタック 80% 超
    assert any("電池" in f and "0%" in f for f in findings)
    assert not any("BT LW WQ" in f for f in findings)                  # 66% は所見にしない


def test_missing_symbols_are_reported_as_unavailable_and_the_rest_continues():
    values = {k: v for k, v in VALUES.items() if not k.startswith("acl_conns")}
    r = ZmkHealth(FakeApi(values=values)).report()
    assert r["ble"] == [] and "BLE" in " ".join(r["unavailable"])
    assert r["debounce"]["press_ms"] == 3
