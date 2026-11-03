"""2026-11-03 のレビュー（停止の見張り。docs/reports/2026-11-03-cloud-A.md。直す前は落ちた）: arm_and_restart が途中で例外（リンクの時間切れ等）になったとき、見張りが外れるか。"""
import pytest

from linux_halt.test_backend_arma import SimRestartWatch, make


class FailingAfterGo(SimRestartWatch):
    """値の一致を有効にし、サンプラを動かし、go も FPGA に届いた（再開が書かれた）が、ホストへの応答が失われた。"""

    def arm_and_restart(self):
        super().arm_and_restart()
        raise TimeoutError("link: no response to the INJECT_START write")


def test_an_exception_inside_arm_and_restart_leaves_the_watch_disarmed():
    a, cores, link = make()
    w = FailingAfterGo(cores)
    a.use_fpga_halt_watch(w, event_skew_upper_bound_ns=None, poll_s=0.0)
    a.halt_all()
    with pytest.raises(TimeoutError):
        a.resume_all()
    # FPGA の値の一致は有効のまま（ホストは armed と思っていないので、step・halt_all でも外さない）
    assert not w.enabled, "watch left enabled in the FPGA after arm_and_restart raised (host _watch_armed=%s)" % a._watch_armed
