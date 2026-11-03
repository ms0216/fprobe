import pytest

from fprobe.linux_halt import cti
from fprobe.plugins import zynq_cti
from .sim_cti import SimCtm


def test_the_board_facts_are_the_measured_ones_from_the_pynq_z2():
    b = zynq_cti.ZYNQ7000
    assert b.cti_bases == (0xF8898000, 0xF8899000)
    assert (b.halt_out, b.restart_out, b.halt_chan, b.restart_chan, b.dbgack_in, b.halt_mode) == (0, 1, 0, 1, 0, "level")


def test_the_acknowledge_poll_limit_has_no_default():
    with pytest.raises(TypeError):
        zynq_cti.cross_halt(zynq_cti.ZYNQ7000, object(), apb_offset=0)


def test_the_apb_offset_has_no_default_because_the_apb_view_is_not_verified():
    with pytest.raises(TypeError):
        zynq_cti.cross_halt(zynq_cti.ZYNQ7000, object(), ack_timeout_polls=1000)


def test_the_cross_halt_it_builds_halts_both_cores_of_the_modelled_a9s_and_releases():
    ctm = SimCtm(n_cores=2, pulses=False, restart_works=False)
    io = type("Io", (), {"read32": lambda s, a: ctm.read32(a - 0x80000000 + 0x1000 * 0), "write32": lambda s, a, v: ctm.write32(a - 0x80000000, v)})()
    # the model's CTIs sit at 0x1000 and 0x2000: ask the plugin for those by choosing a board with such bases
    board = zynq_cti.CtiBoard("model", (0x80001000, 0x80002000), halt_out=0, restart_out=1, halt_chan=0, restart_chan=1,
                             dbgack_in=0, halt_mode="level", apb_view_offset=0)
    cross = zynq_cti.cross_halt(board, io, apb_offset=0, ack_timeout_polls=1000)
    cross.arm(event_sync_trigin=board.dbgack_in)
    cross.halt_all()
    assert [c.halted for c in ctm.cores] == [True, True]
    cross.release()
    assert all(not c.dbgrq for c in ctm.cores)
    assert isinstance(cross, cti.CrossHalt)


def test_the_cti_address_offset_follows_how_the_dap_shows_the_cores():
    assert zynq_cti.ZYNQ7000.apb_offset_for("ahb_system_map") == 0
    assert zynq_cti.ZYNQ7000.apb_offset_for("apb_rom_table") == 0x80098000 - 0xF8898000
    with pytest.raises(ValueError):
        zynq_cti.ZYNQ7000.apb_offset_for("unknown")
