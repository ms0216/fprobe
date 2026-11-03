import json

import pytest

from fprobe.linux_halt.bp import BKPT, BreakpointTable, NoFreeSlot, recover
from fprobe.linux_halt.mmu import Unmapped
from fprobe.linux_halt.sim_cores import SimCores
from fprobe.linux_halt.usermem import UserMem
from .helpers import PGD, map_page

CODE = bytes.fromhex("0000a0e1") * 8                        # mov r0, r0 (nop) x 8
PA = 0x20000


def world(tmp_path, hw_slots=2, unmapped="error"):
    cores = SimCores(2, simultaneous=False, skew_upper_bound_ns=None)
    map_page(cores, 0x10000, PA)
    cores.write_phys(PA, CODE)
    journal = str(tmp_path / "journal.json")
    t = BreakpointTable(cores, journal=journal, hw_slots=hw_slots, unmapped=unmapped)
    t.attach(100, UserMem(cores, PGD, fmt="short"))
    return cores, t, journal


def test_sw_insert_writes_bkpt_and_remove_restores(tmp_path):
    cores, t, journal = world(tmp_path)
    t.insert(100, 0x10004, kind="sw", width=4)
    assert cores.read_phys(PA + 4, 4) == BKPT[4]
    assert cores.read_phys(PA, 4) == CODE[:4]
    t.remove(100, 0x10004, kind="sw")
    assert cores.read_phys(PA, 32) == CODE
    assert t.pending_journal() == []


def test_shared_page_bkpt_removed_everywhere(tmp_path):
    """Review Focus 5: two processes map the same physical page; the BKPT must not be recorded as 'original' by the second,
    and nothing may be left in the target's text after both are gone."""
    cores, t, journal = world(tmp_path)
    map_page(cores, 0x7000, PA, pgd=0x6000)                  # process 200 maps the same page elsewhere
    t.attach(200, UserMem(cores, 0x6000, fmt="short"))
    t.insert(100, 0x10008, kind="sw", width=4)
    t.insert(200, 0x7008, kind="sw", width=4)               # same physical word
    assert cores.read_phys(PA + 8, 4) == BKPT[4]
    assert len(t.pending_journal()) == 1                      # one entry, and its original is the real instruction
    assert t.pending_journal()[0].orig == CODE[8:12]
    t.remove(100, 0x10008, kind="sw")
    assert cores.read_phys(PA + 8, 4) == BKPT[4]              # the other process still owns it
    t.remove(200, 0x7008, kind="sw")
    assert cores.read_phys(PA, 32) == CODE
    assert t.pending_journal() == []


def test_hw_request_without_free_slot_raises_not_falls_back_to_sw(tmp_path):
    cores, t, journal = world(tmp_path, hw_slots=1)
    t.insert(100, 0x10000, kind="hw", width=4)
    with pytest.raises(NoFreeSlot):
        t.insert(100, 0x10004, kind="hw", width=4)
    assert cores.read_phys(PA, 32) == CODE                    # nothing was patched in its place
    assert t.get_hw(0x10004) is None
    assert set(cores.hw_bps.values()) == {0x10000}


def test_hw_breakpoint_is_programmed_on_every_core_and_cleared(tmp_path):
    cores, t, journal = world(tmp_path)
    t.insert(100, 0x10000, kind="hw", width=4)
    assert {c for c, _ in cores.hw_bps} == {0, 1}
    t.remove(100, 0x10000, kind="hw")
    assert cores.hw_bps == {}


def test_unmapped_sw_raises_unless_the_caller_asked_for_hw(tmp_path):
    cores, t, journal = world(tmp_path)
    with pytest.raises(Unmapped):
        t.insert(100, 0x50000, kind="sw", width=4)
    cores2, t2, _ = world(tmp_path, unmapped="hw")
    t2.insert(100, 0x50000, kind="sw", width=4)
    assert 0x50000 in cores2.hw_bps.values()


def test_unmapped_policy_must_be_one_of_the_two(tmp_path):
    with pytest.raises(ValueError):
        BreakpointTable(SimCores(1, False, None), journal=None, hw_slots=1, unmapped="ignore")


def test_journal_lists_the_original_before_the_write_and_is_removed_after(tmp_path):
    cores, t, journal = world(tmp_path)
    t.insert(100, 0x10004, kind="sw", width=4)
    j = json.load(open(journal))
    assert j["entries"] == [{"pa": PA + 4, "width": 4, "orig": CODE[4:8].hex(), "owners": [[100, 0x10004]]}]
    t.remove(100, 0x10004, kind="sw")
    import os
    assert not os.path.exists(journal)


def test_recover_restores_a_crashed_debuggers_breakpoints(tmp_path):
    cores, t, journal = world(tmp_path)
    t.insert(100, 0x10004, kind="sw", width=4)
    assert cores.read_phys(PA + 4, 4) == BKPT[4]
    assert recover(cores, journal).restored == 1                       # the debugger died; a new one recovers
    assert cores.read_phys(PA, 32) == CODE


def test_user_and_tmp_owners_do_not_remove_each_other(tmp_path):
    cores, t, journal = world(tmp_path)
    t.insert(100, 0x10004, kind="sw", width=4)
    t.insert(100, 0x10004, kind="sw", width=4, tmp=True)
    t.remove(100, 0x10004, kind="sw", tmp=True)
    assert cores.read_phys(PA + 4, 4) == BKPT[4]
    t.remove(100, 0x10004, kind="sw")
    assert cores.read_phys(PA + 4, 4) == CODE[4:8]


def test_shadow_hides_bkpt_and_write_keeps_it(tmp_path):
    cores, t, journal = world(tmp_path)
    t.insert(100, 0x10004, kind="sw", width=4)
    raw = t.mem(100).read(0x10000, 12)
    assert raw[4:8] == BKPT[4]
    assert t.shadow(100, 0x10000, raw) == CODE[:12]
    t.write(100, 0x10004, bytes.fromhex("01000000"))          # the program's new instruction becomes the original
    assert cores.read_phys(PA + 4, 4) == BKPT[4]
    t.remove(100, 0x10004, kind="sw")
    assert cores.read_phys(PA + 4, 4) == bytes.fromhex("01000000")


def test_lifted_shows_the_original_inside_and_restores_the_bkpt(tmp_path):
    cores, t, journal = world(tmp_path)
    t.insert(100, 0x10004, kind="sw", width=4)
    with t.lifted(100, 0x10004):
        assert cores.read_phys(PA + 4, 4) == CODE[4:8]
    assert cores.read_phys(PA + 4, 4) == BKPT[4]


def test_lookup_reports_kind_and_ownership_per_process(tmp_path):
    cores, t, journal = world(tmp_path)
    t.insert(100, 0x10004, kind="sw", width=4)
    t.insert(100, 0x10008, kind="sw", width=4, tmp=True)
    t.insert(100, 0x10010, kind="hw", width=4)
    a, b, c = t.lookup(100, 0x10004), t.lookup(100, 0x10008), t.lookup(100, 0x10010)
    assert (a.hw, a.user, a.tmp, a.width) == (False, True, False, 4)
    assert (b.hw, b.user, b.tmp) == (False, False, True)
    assert (c.hw, c.user) == (True, True)
    assert t.lookup(100, 0x10000) is None
    assert t.lookup(300, 0x10004) is None                      # another process has no such breakpoint


def test_sw_breakpoint_is_found_and_lifted_by_physical_address(tmp_path):
    """A core running ANOTHER process stops on a shared BKPT: its pc is a virtual address in its own space, so the
    engine looks the breakpoint up by the physical address it translated that pc to."""
    cores, t, journal = world(tmp_path)
    t.insert(100, 0x10004, kind="sw", width=4)
    assert t.sw_at_pa(PA + 4).width == 4 and t.sw_at_pa(PA) is None
    with t.lifted_pa(PA + 4):
        assert cores.read_phys(PA + 4, 4) == CODE[4:8]
    assert cores.read_phys(PA + 4, 4) == BKPT[4]


# --- findings of the whole-branch review -------------------------------------------------------------------------------

def test_a_temporary_sw_breakpoint_on_a_user_hw_breakpoints_address_is_removed_without_leaving_a_bkpt(tmp_path):
    """review finding 6: _run_to puts a tmp sw breakpoint where the user has an hbreak; removing it must reach the sw table."""
    cores, t, journal = world(tmp_path)
    t.insert(100, 0x10004, kind="hw", width=4)
    t.insert(100, 0x10004, kind="sw", width=4, tmp=True)
    assert cores.read_phys(PA + 4, 4) == BKPT[4]
    merged = t.lookup(100, 0x10004)
    assert (merged.hw, merged.user, merged.tmp) == (True, True, True)
    t.remove(100, 0x10004, kind="sw", tmp=True)
    assert cores.read_phys(PA, 32) == CODE and t.pending_journal() == []
    after = t.lookup(100, 0x10004)
    assert (after.hw, after.user, after.tmp) == (True, True, False)
    t.remove(100, 0x10004, kind="hw")
    assert t.lookup(100, 0x10004) is None


def test_remove_must_say_which_kind_it_removes(tmp_path):
    cores, t, journal = world(tmp_path)
    t.insert(100, 0x10004, kind="sw", width=4)
    with pytest.raises(TypeError):
        t.remove(100, 0x10004)


def reuse_the_frame(cores):
    cores.write_phys(PA + 4, bytes.fromhex("deadbeef"))                # the kernel gave the frame to someone else


def test_a_frame_that_no_longer_holds_our_bkpt_is_not_written_back_on_clear(tmp_path):
    """review finding 7: the page was reclaimed and reused; writing the old instruction would corrupt the new owner."""
    cores, t, journal = world(tmp_path)
    t.insert(100, 0x10004, kind="sw", width=4)
    reuse_the_frame(cores)
    t.clear()
    assert cores.read_phys(PA + 4, 4) == bytes.fromhex("deadbeef")
    assert [e.pa for e in t.stale] == [PA + 4]                         # reported, not hidden
    assert t.pending_journal() == []


def test_recover_does_not_overwrite_a_reused_frame_and_counts_it(tmp_path):
    cores, t, journal = world(tmp_path)
    t.insert(100, 0x10004, kind="sw", width=4)
    t.insert(100, 0x10008, kind="sw", width=4)
    reuse_the_frame(cores)
    result = recover(cores, journal)
    assert (result.restored, result.stale) == (1, 1)
    assert cores.read_phys(PA + 4, 4) == bytes.fromhex("deadbeef") and cores.read_phys(PA + 8, 4) == CODE[8:12]


def test_removing_a_breakpoint_whose_page_is_no_longer_mapped_still_drops_the_owner(tmp_path):
    cores, t, journal = world(tmp_path)
    t.insert(100, 0x10004, kind="sw", width=4)
    cores.write_phys(0x8000 + 4 * 0x10, b"\x00\x00\x00\x00")           # the page table entry of 0x10000 is gone
    t.remove(100, 0x10004, kind="sw")
    assert t.pending_journal() == [] and t.lookup(100, 0x10004) is None
    assert cores.read_phys(PA + 4, 4) == CODE[4:8]                     # the frame still held our BKPT: put back


def test_a_failed_write_leaves_the_recorded_original_unchanged(tmp_path):
    cores, t, journal = world(tmp_path)
    t.insert(100, 0x10ffc, kind="sw", width=4)                         # last word of the page
    before = t.pending_journal()[0].orig
    with pytest.raises(Unmapped):
        t.write(100, 0x10ffc, b"\x01\x02\x03\x04\x05\x06\x07\x08")      # runs into the unmapped next page
    assert t.pending_journal()[0].orig == before
