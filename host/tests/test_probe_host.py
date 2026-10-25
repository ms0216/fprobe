"""fprobe.probe_host.ProbeHost: mixin が頼る共通の部品を、Probe が実際に持っていること。"""

import typing

from fprobe.probe import Probe
from fprobe.probe_host import ProbeHost


def test_probe_has_everything_probe_host_declares():
    probe = Probe("unused")
    for name in typing.get_type_hints(ProbeHost):
        assert hasattr(probe, name) or hasattr(Probe, name), name
    for name in ("session", "target", "dbg", "_caps"):
        assert hasattr(Probe, name), name


def test_every_mixin_state_is_initialised_by_init():
    # getattr で遅延初期化せず、_init_* で作った状態は Probe(...) の直後から読める
    probe = Probe("unused")
    for name in ("_fpga_obs", "_fpga_obs_opened", "_fpga_latest", "_obs_delays", "_obs_trigger", "_trigger",
                 "_last_connect", "_deep", "_code_at_elf", "_watch_running"):
        assert hasattr(probe, name), name
