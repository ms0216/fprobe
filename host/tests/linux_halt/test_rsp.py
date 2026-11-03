"""The gdb server of fprobe.linux_halt against the engine's synthetic world (test_engine.World)."""
import socket
import struct

import pytest

from fprobe.gdb import rsp as framing
from fprobe.linux_halt.core_access import MOE_BKPT_INSN, MOE_HALT, CoreState
from fprobe.linux_halt.engine import Stop
from fprobe.linux_halt.rsp import Link, Server
from .test_engine import CODE, PA, STACK, World


class Rig:
    def __init__(self, tmp_path, **kw):
        self.w = World(tmp_path, **kw)
        a, self.peer = socket.socketpair()
        self.link = Link(a)
        self.s = Server(self.w.eng, self.link, backend_errors=(RuntimeError,))

    def h(self, text):
        r = self.s.handle(text.encode() if isinstance(text, str) else text)
        return r.decode("latin-1")

    def console(self):
        self.peer.settimeout(0.2)
        try:
            data = self.peer.recv(65536)
        except socket.timeout:
            return ""
        return b"".join(bytes.fromhex(p.split(b"#")[0].decode()) for p in data.split(b"$O")[1:]).decode()

    def mon(self, cmd):
        self.h("qRcmd," + cmd.encode().hex())
        return self.console()


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    r.w.run(0, 100, 0x10000, moe=MOE_HALT)
    r.w.eng.stop()
    r.s.last = Stop("trap", 100)
    yield r
    r.peer.close()


# --- framing and the link ------------------------------------------------------------------------------------------

def link_pair():
    a, b = socket.socketpair()
    return Link(a), b


def test_link_acks_a_good_packet_and_returns_its_unescaped_payload():
    link, peer = link_pair()
    peer.sendall(framing.frame(b"qC}x"))                         # payload contains an escapable byte after framing
    assert link.read_packet() == b"qC}x"
    assert peer.recv(10) == b"+"


def test_link_naks_a_corrupt_packet_and_waits_for_the_resend():
    link, peer = link_pair()
    peer.sendall(b"$qC#00" + framing.frame(b"qC"))
    assert link.read_packet() == b"qC"
    assert peer.recv(10) == b"-+"


def test_link_retransmits_the_last_packet_on_a_nak():
    link, peer = link_pair()
    link.send("OK")
    first = peer.recv(100)
    peer.sendall(b"-" + framing.frame(b"g"))
    assert link.read_packet() == b"g"
    assert first in peer.recv(100)


def test_ctrl_c_is_seen_while_the_target_runs_and_consumed_once():
    link, peer = link_pair()
    assert link.interrupted() is False
    peer.sendall(b"\x03")
    assert link.interrupted() is True
    assert link.interrupted() is False


def test_no_ack_mode_sends_no_acks():
    link, peer = link_pair()
    link.noack = True
    peer.sendall(framing.frame(b"g"))
    assert link.read_packet() == b"g"
    peer.settimeout(0.1)
    with pytest.raises(socket.timeout):
        peer.recv(10)


# --- queries ---------------------------------------------------------------------------------------------------------

def test_qsupported_advertises_what_is_implemented(rig):
    r = rig.h("qSupported:multiprocess+")
    for feature in ("QStartNoAckMode+", "swbreak+", "hwbreak+", "qXfer:threads:read+", "qXfer:auxv:read+", "vContSupported+"):
        assert feature in r


def test_only_target_xml_is_served_as_features_and_a_short_read_continues(rig):
    assert rig.h("qXfer:features:read:target.xml:0,40").startswith("m<?xml")
    assert rig.h("qXfer:features:read:other.xml:0,40") == ""
    full = rig.h("qXfer:features:read:target.xml:0,ffff")
    assert full.startswith("l")
    n = len(full) - 1
    assert rig.h("qXfer:features:read:target.xml:0,%x" % n).startswith("l")        # an exact fit ends the transfer
    assert rig.h("qXfer:features:read:target.xml:0,%x" % (n - 1)).startswith("m")


def test_the_thread_list_names_the_core_a_running_thread_is_on(rig):
    xml = rig.h("qXfer:threads:read::0,ffff")
    assert 'id="64" core="0"' in xml and 'id="65"' in xml and 'name="app"' in xml
    assert rig.h("qfThreadInfo") == "m64,65" and rig.h("qsThreadInfo") == "l"
    assert rig.h("qThreadExtraInfo,65") == b"app".hex()


def test_h_selects_a_thread_and_zero_or_minus_one_go_back_to_the_default(rig):
    default = rig.h("qC")
    assert rig.h("Hg65") == "OK" and rig.h("qC") == "QC65"
    assert rig.h("Hg0") == "OK" and rig.h("qC") == default
    rig.h("Hg65")
    rig.h("Hg-1")
    assert rig.h("qC") == default


def test_t_says_whether_a_thread_is_alive(rig):
    assert rig.h("T65") == "OK" and rig.h("T3e7") == "E01"


def test_auxv_is_served_through_its_terminator(rig):
    rig.w.m.b[0x9000 + 0x20:0x9000 + 0x20 + 16] = struct.pack("<4I", 6, 4096, 0, 0)
    assert rig.h("qXfer:auxv:read::0,ffff") == "l" + struct.pack("<4I", 6, 4096, 0, 0).decode("latin-1")


# --- registers ---------------------------------------------------------------------------------------------------------

def test_g_and_p_read_the_live_thread_little_endian(rig):
    rig.w.cores.set_regs(0, list(range(17)))
    assert rig.h("g") == b"".join(struct.pack("<I", v) for v in range(17)).hex()
    assert rig.h("p5") == struct.pack("<I", 5).hex()


def test_g_upper_and_p_upper_write_the_live_thread(rig):
    vals = [0x100 + i for i in range(17)]
    assert rig.h("G" + "".join(struct.pack("<I", v).hex() for v in vals)) == "OK"
    assert rig.w.cores.regs(0) == vals
    assert rig.h("P5=11223344") == "OK"
    assert rig.w.cores.regs(0)[5] == 0x44332211 and rig.w.cores.regs(0)[4] == 0x104


# --- memory and breakpoints -----------------------------------------------------------------------------------------

def test_a_read_hides_an_inserted_bkpt_and_a_partial_read_is_legal(rig):
    assert rig.h("Z0,10004,4") == "OK"
    assert rig.w.cores.read_phys(PA + 4, 4) != CODE[4:8]
    assert rig.h("m10000,c") == CODE[:12].hex()                            # the original instruction, not the BKPT
    assert rig.h("m10ff8,10") == "00" * 8                                 # up to the unmapped page: what exists
    assert rig.h("m50000,4") == "E01"                                    # none of it exists: an error


def test_writes_keep_the_breakpoint_and_update_the_original(rig):
    rig.h("Z0,10004,4")
    assert rig.h("M10004,4:01000000") == "OK"
    assert rig.h("m10004,4") == "01000000"
    assert rig.h(b"X10008,4:" + bytes([2, 0, 0, 0])) == "OK"
    assert rig.h("m10008,4") == "02000000"


def test_z_inserts_and_removes_software_hardware_breakpoints_and_watchpoints(rig):
    assert rig.h("Z0,10004,4") == "OK" and rig.h("z0,10004,4") == "OK"
    assert rig.w.cores.read_phys(PA, 32) == CODE
    assert rig.h("Z1,10008,4") == "OK" and 0x10008 in rig.w.cores.hw_bps.values()
    assert rig.h("z1,10008,4") == "OK" and not rig.w.cores.hw_bps
    assert rig.h("Z2,10100,4") == "OK" and len(rig.w.watches) == 1
    assert rig.h("z2,10100,4") == "OK" and len(rig.w.watches) == 0
    assert rig.h("Z5,10100,4") == ""                                     # an unsupported type is an empty reply


def test_a_breakpoint_on_an_unmapped_page_is_refused_with_the_reason_in_the_console(rig):
    assert rig.h("Z0,50000,4") == "E01"
    message = rig.console()
    assert "not mapped" in message and "hbreak" in message


def test_no_free_hardware_slot_is_an_error_not_a_silent_software_breakpoint(rig):
    assert rig.h("Z1,10000,4") == "OK" and rig.h("Z1,10004,4") == "OK"
    assert rig.h("Z1,10008,4") == "E01"
    assert rig.w.cores.read_phys(PA, 32) == CODE


# --- run control and stop replies -----------------------------------------------------------------------------------

def test_the_stop_reply_formats(rig):
    s = rig.s
    assert s.reply_for(Stop("trap", 100)) == "T05thread:64;"
    assert s.reply_for(Stop("swbreak", 100, 0x10004)) == "T05swbreak:;thread:64;"
    assert s.reply_for(Stop("hwbreak", 100, 0x10004)) == "T05hwbreak:;thread:64;"
    assert s.reply_for(Stop("int", 100)) == "T02thread:64;"
    assert s.reply_for(Stop("watch", 100, 0x10100, "w", 1)) == "T05watch:10100;thread:64;"
    assert s.reply_for(Stop("watch", 100, 0x10100, "r", 1)) == "T05rwatch:10100;thread:64;"
    assert s.reply_for(Stop("signal", 100, signal=11, code=11)) == "T0bthread:64;"
    assert s.reply_for(Stop("exit", 100, code=0x300)) == "W03" and s.done
    s.done = False
    assert s.reply_for(Stop("exit", 100, code=11)) == "X0b"


def test_vcont_s_steps_the_named_thread_and_vcont_c_continues_to_the_next_event(rig):
    w = rig.w
    w.cores.fetches[(0, 0x10000)] = 0xE1A00000
    w.cores.on_step = lambda core: w.run(core, 100, 0x10004, moe=MOE_HALT)
    assert rig.h("vCont;s:64") == "T05thread:64;"
    assert w.cores.steps == [0]
    rig.h("Z0,10008,4")
    w.run(2, 101, 0x10008)
    w.cores.queue_event(2)
    assert rig.h("vCont;c") == "T05swbreak:;thread:65;"
    assert rig.h("qC") == "QC65"                                         # the stop reply names a thread: it is current


def test_vcont_question_mark_and_kill_and_noack(rig):
    assert rig.h("vCont?") == "vCont;c;C;s;S"
    assert rig.h("QStartNoAckMode") == "OK"
    assert rig.h("D") == "OK" and rig.s.done


def test_unsupported_packets_get_the_empty_reply(rig):
    assert rig.h("vMustReplyEmpty") == "" and rig.h("qTStatus") == ""


# --- monitor -------------------------------------------------------------------------------------------------------

def test_monitor_stops_reports_method_bound_basis_verdict_and_the_limit(rig):
    out = rig.mon("stops")
    assert "simultaneous" in out and "50 ns" in out and "simulated" in out and "within" in out and "68 ns" in out


def test_monitor_stops_says_unknown_when_the_bound_is_unknown(tmp_path):
    r = Rig(tmp_path, bound=None)
    r.w.eng.stop()
    out = r.mon("stops")
    assert "unknown" in out and "not a measurement of zero" in out
    r.peer.close()


def test_monitor_cores_threads_bps_watches_stats_list_what_they_hold(rig):
    rig.h("Z0,10004,4")
    rig.h("Z2,10100,4")
    assert "cpu0 halted pc=00010000" in rig.mon("cores")
    assert "tid=100" in rig.mon("threads") and "tid=101" in rig.mon("threads")
    assert "00010004 sw user" in rig.mon("bps")
    assert "00010100 len=4 w" in rig.mon("watches")
    assert "foreign_bp_hits=0" in rig.mon("stats")


def test_an_unknown_monitor_command_lists_the_known_ones(rig):
    assert "cores | threads | bps | watches | stats | stops | bp-unmapped" in rig.mon("nonsense")


def test_bp_unmapped_sets_and_reports_the_policy(rig):
    assert "bp-unmapped = error" in rig.mon("bp-unmapped")
    assert "bp-unmapped = hw" in rig.mon("bp-unmapped hw")
    assert "usage" in rig.mon("bp-unmapped nonsense")
