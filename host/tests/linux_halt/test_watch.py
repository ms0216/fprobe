import pytest

from fprobe.linux_halt.bp import NoFreeSlot
from fprobe.linux_halt.sim_cores import SimCores
from fprobe.linux_halt.watch import WatchTable


def make(slots=2):
    cores = SimCores(2, simultaneous=False, skew_upper_bound_ns=None)
    return cores, WatchTable(cores, hw_slots=slots)


def test_add_programs_every_core_and_maps_access_kind():
    cores, w = make()
    w.add(0x1000, 4, "a")
    assert cores.hw_watches == {(0, 0): (0x1000, 4, "rw"), (1, 0): (0x1000, 4, "rw")}
    w.add(0x2000, 4, "w")
    assert cores.hw_watches[(0, 1)] == (0x2000, 4, "w")


def test_remove_clears_every_core():
    cores, w = make()
    w.add(0x1000, 4, "r")
    w.remove(0x1000, 4, "r")
    assert cores.hw_watches == {}
    assert len(w) == 0


def test_no_free_slot_raises_and_leaves_no_core_programmed():
    cores, w = make(slots=1)
    w.add(0x1000, 4, "w")
    before = dict(cores.hw_watches)
    with pytest.raises(NoFreeSlot):
        w.add(0x2000, 4, "w")
    assert cores.hw_watches == before


def test_unknown_kind_is_refused():
    cores, w = make()
    with pytest.raises(ValueError):
        w.add(0x1000, 4, "x")


def test_suspend_and_restore_one_core():
    cores, w = make()
    w.add(0x1000, 4, "w")
    w.suspend(1)
    assert (1, 0) not in cores.hw_watches and (0, 0) in cores.hw_watches
    w.restore(1)
    assert cores.hw_watches[(1, 0)] == (0x1000, 4, "w")
