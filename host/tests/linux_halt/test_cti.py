"""fprobe/linux_halt/cti.py against the register-level model (tests/linux_halt/sim_cti.py). Not a hardware test: see fprobe/linux_halt/cti.py."""
import unittest

from fprobe.linux_halt import cti
from .sim_cti import SimCtm

WIRING = dict(halt_out=0, restart_out=1, halt_chan=0, restart_chan=1)     # ARM's recommended numbers, stated here
ACK = 1000


def build(halt_mode="pulse", **kw):
    ctm = SimCtm(**kw)
    out = kw.get("halt_out", 0), kw.get("restart_out", 1)
    ctis = [cti.Cti(ctm, base, "cti%d" % i, **{**WIRING, "halt_out": out[0], "restart_out": out[1]})
            for i, base in enumerate(sorted(ctm.base))]
    return ctm, cti.CrossHalt(ctis, halt_mode=halt_mode, ack_timeout_polls=ACK)


def halted(ctm):
    return [c.halted for c in ctm.cores]


class CrossHaltProtocol(unittest.TestCase):
    def test_one_write_halts_every_core(self):
        ctm, sync = build()
        sync.arm()
        pulses = []
        orig = ctm.write32
        ctm.write32 = lambda a, v: (pulses.append(a & 0xFFF) if a & 0xFFF == cti.CTIAPPPULSE else None, orig(a, v))[1]
        sync.halt_all()
        self.assertEqual(halted(ctm), [True] * 4)
        self.assertEqual(pulses, [cti.CTIAPPPULSE], "exactly one pulse write for all four cores")

    def test_halt_restart_halt_again_works_for_many_cycles(self):
        ctm, sync = build()
        sync.arm()
        for i in range(3):
            sync.halt_all()
            self.assertEqual(halted(ctm), [True] * 4, "cycle %d" % i)
            sync.release()
            sync.restart_all()
            self.assertEqual(halted(ctm), [False] * 4, "cycle %d" % i)
            sync.rearm_halt()
        self.assertEqual([c.restarts for c in ctm.cores], [3] * 4)

    def test_a_restart_without_the_acknowledge_leaves_the_cores_halted(self):
        """The model's point: a stale halt request stops a core again. The driver's release() must acknowledge."""
        ctm, sync = build()
        sync.arm()
        sync.halt_all()
        for c in sync.ctis:                                       # what a driver that forgot the acknowledge does
            c.ungate(c.restart_chan)
            c.gate(c.halt_chan)
        sync.restart_all()
        self.assertEqual(halted(ctm), [True] * 4)
        self.assertEqual([c.failed_restarts for c in ctm.cores], [1] * 4)

    def test_a_core_that_stops_by_itself_halts_the_others_in_hardware_when_routed(self):
        ctm, sync = build()
        sync.arm(event_sync_trigin=0)
        ctm.core_halts_by_itself(2)
        self.assertEqual(halted(ctm), [True] * 4)

    def test_without_the_routing_only_that_core_stops(self):
        ctm, sync = build()
        sync.arm()
        ctm.core_halts_by_itself(2)
        self.assertEqual(halted(ctm), [False, False, True, False])

    def test_a_core_with_its_halt_gate_closed_is_not_halted(self):
        ctm, sync = build()
        sync.arm()
        sync.ctis[3].gate(0)
        sync.halt_all()
        self.assertEqual(halted(ctm), [True, True, True, False])

    def test_every_register_write_is_preceded_by_the_unlock(self):
        ctm, sync = build()
        sync.arm(event_sync_trigin=0)
        sync.halt_all()
        sync.release()
        sync.restart_all()
        self.assertEqual([s.writes_while_locked for s in ctm.ctis], [0] * 4)

    def test_wrong_trigger_numbers_fail_visibly_not_silently(self):
        """Hardware wired with halt on output 2 / restart on 3, driver left at ARM's recommended 0 / 1: the cores do
        not halt. The caller sees cores that never report halted, which is why the numbers are parameters."""
        ctm = SimCtm(halt_out=2, restart_out=3)
        ctis = [cti.Cti(ctm, b, **WIRING) for b in sorted(ctm.base)]
        sync = cti.CrossHalt(ctis, halt_mode="pulse", ack_timeout_polls=ACK)
        sync.arm()
        sync.halt_all()
        self.assertEqual(halted(ctm), [False] * 4)

    def test_other_trigger_numbers_work_when_given(self):
        ctm, sync = build(halt_out=2, restart_out=3)
        sync.arm()
        sync.halt_all()
        self.assertEqual(halted(ctm), [True] * 4)

    def test_an_acknowledge_that_does_not_take_effect_is_an_error(self):
        ctm, sync = build()
        sync.arm()
        sync.halt_all()
        for s in ctm.ctis:                                        # broken hardware: the request never clears
            s.write = (lambda orig: lambda off, v: None if off == cti.CTIINTACK else orig(off, v))(s.write)
        with self.assertRaises(cti.CtiError):
            sync.ctis[0].ack_halt(None, 3)

    def test_the_status_registers_are_readable_for_the_hardware_check(self):
        ctm, sync = build()
        sync.arm()
        sync.halt_all()
        st = sync.ctis[1].status()
        self.assertEqual(st["trigout"], 1)                        # the halt request is asserted on output 0
        self.assertEqual(st["gate"] & 1, 1)


class RegisterLayout(unittest.TestCase):
    def test_the_offsets_are_the_ones_in_openocds_arm_cti_h(self):
        """Written out here on purpose: the model in sim_cti.py has its own copy, so a wrong constant in the driver
        also shows up as a core that does not react."""
        self.assertEqual((cti.CTICONTROL, cti.CTIINTACK, cti.CTIAPPSET, cti.CTIAPPCLEAR, cti.CTIAPPPULSE),
                         (0x000, 0x010, 0x014, 0x018, 0x01C))
        self.assertEqual((cti.CTIINEN, cti.CTIOUTEN), (0x020, 0x0A0))
        self.assertEqual((cti.CTITRIGINSTATUS, cti.CTITROUTSTATUS, cti.CTICHINSTATUS, cti.CTICHOUTSTATUS, cti.CTIGATE),
                         (0x130, 0x134, 0x138, 0x13C, 0x140))
        self.assertEqual((cti.CTILAR, cti.UNLOCK_KEY), (0xFB0, 0xC5ACCE55))

    def test_the_wiring_numbers_have_no_defaults(self):
        with self.assertRaises(TypeError):
            cti.Cti(SimCtm(), 0x1000)
        with self.assertRaises(TypeError):
            cti.Cti(SimCtm(), 0x1000, halt_out=0, restart_out=1)
        c = cti.Cti(SimCtm(), 0x1000, **WIRING)
        self.assertEqual((c.halt_out, c.restart_out, c.halt_chan, c.restart_chan), (0, 1, 0, 1))

    def test_the_acknowledge_poll_limit_has_no_default(self):
        with self.assertRaises(TypeError):
            cti.CrossHalt([cti.Cti(SimCtm(), 0x1000, **WIRING)], halt_mode="level")


class Programming(unittest.TestCase):
    def test_arm_closes_every_gate_that_was_open(self):
        ctm = SimCtm()
        for s in ctm.ctis:
            s.gate = 0xFFFFFFFF                                   # whatever the previous user left behind
        c = cti.Cti(ctm, sorted(ctm.base)[0], **WIRING)
        c.arm()
        self.assertEqual(ctm.ctis[0].gate, 0)
        self.assertEqual(ctm.ctis[0].control, 1)
        self.assertEqual(ctm.ctis[0].outen, {0: 1, 1: 2})          # output 0 <- channel 0, output 1 <- channel 1

    def test_gates_follow_the_phases(self):
        ctm, sync = build()
        sync.arm()
        self.assertEqual([s.gate & 3 for s in ctm.ctis], [1] * 4)             # halt channel open, restart closed
        sync.halt_all()
        sync.release()
        self.assertEqual([s.gate & 3 for s in ctm.ctis], [2] * 4)             # halt closed, restart open
        sync.restart_all()
        sync.rearm_halt()
        self.assertEqual([s.gate & 3 for s in ctm.ctis], [1] * 4)

    def test_trigger_numbers_choose_the_register_that_is_written(self):
        ctm, sync = build(halt_out=2, restart_out=3)
        sync.arm(event_sync_trigin=2)
        self.assertEqual(ctm.ctis[0].outen, {2: 1, 3: 2})
        self.assertEqual(ctm.ctis[0].inen, {2: 1})                  # the core's own halt entry (input 2) -> channel 0

    def test_halt_and_restart_are_pulsed_from_the_first_cti(self):
        ctm, sync = build()
        sync.arm()
        sync.ctis[0].gate(0)                                      # the first one cannot put events into the matrix
        sync.halt_all()
        self.assertEqual(halted(ctm), [False] * 4)
        sync.ctis[0].ungate(0)
        sync.halt_all()
        sync.release()
        sync.ctis[0].gate(1)
        sync.restart_all()
        self.assertEqual(halted(ctm), [True] * 4)

    def test_the_acknowledge_waits_and_calls_the_wait_function_while_the_request_is_still_up(self):
        reads = []

        class SlowClear:
            """The halt request reads as asserted for three polls after the acknowledge."""
            def __init__(self):
                self.acked, self.polls = False, 0

            def write32(self, addr, v):
                if addr & 0xFFF == 0x010:
                    self.acked = True

            def read32(self, addr):
                if addr & 0xFFF == 0x134:
                    reads.append(1)
                    self.polls += 1
                    return 1 if self.polls <= 3 else 0
                return 0
        waits = []
        cti.Cti(SlowClear(), 0x1000, **WIRING).ack_halt(lambda: waits.append(1), 1000)
        self.assertEqual((len(reads), len(waits)), (4, 3))


class ApbIo(unittest.TestCase):
    """read32/write32 on the APB-AP of the DAP fprobe already opened (the CTIs sit next to the cores' debug units)."""

    class FakeDap:
        def __init__(self):
            self.log = []

        def mem_read32(self, ap, addr):
            self.log.append(("r", ap, addr))
            return 0x12

        def mem_write32(self, ap, addr, value):
            self.log.append(("w", ap, addr, value))

    def test_read32_goes_to_the_named_ap_and_address(self):
        d = self.FakeDap()
        self.assertEqual(cti.ApbIo(d, 1).read32(0x80420140), 0x12)
        self.assertEqual(d.log, [("r", 1, 0x80420140)])

    def test_write32_sends_ap_address_and_value(self):
        d = self.FakeDap()
        cti.ApbIo(d, 2).write32(0x80420140, 0xC5ACCE55)
        self.assertEqual(d.log, [("w", 2, 0x80420140, 0xC5ACCE55)])

    def test_a_cti_write_unlocks_first_through_this_io(self):
        d = self.FakeDap()
        cti.Cti(cti.ApbIo(d, 1), 0x80420000, **WIRING).write(cti.CTICONTROL, 1)
        self.assertEqual(d.log, [("w", 1, 0x80420000 + cti.CTILAR, cti.UNLOCK_KEY), ("w", 1, 0x80420000, 1)])


class ZynqA9Behaviour(unittest.TestCase):
    """What was measured on the PYNQ-Z2 (2026-10-28), modelled: a pulse does nothing, a held channel halts every core,
    output 1 does not restart, and the debug acknowledge is trigger input 5."""

    def a9(self, mode="level"):
        ctm, sync = build(halt_mode=mode, n_cores=2, pulses=False, restart_works=False)
        sync.arm(event_sync_trigin=0)
        return ctm, sync

    def test_a_held_channel_halts_both_cores(self):
        ctm, sync = self.a9("level")
        sync.halt_all()
        self.assertEqual(halted(ctm), [True, True])

    def test_a_pulse_halts_nothing_on_such_a_board_and_the_caller_sees_it(self):
        ctm, sync = self.a9("pulse")
        sync.halt_all()
        self.assertEqual(halted(ctm), [False, False])

    def test_release_drops_the_held_channel_and_acknowledges_the_request(self):
        ctm, sync = self.a9("level")
        sync.halt_all()
        sync.release()
        self.assertEqual(ctm.ctis[0].appset, 0)
        self.assertEqual([c.dbgrq for c in ctm.cores], [False, False])
        self.assertEqual(halted(ctm), [True, True], "dropping the request does not restart the cores")

    def test_restart_through_the_cti_does_not_work_there_and_the_cores_stay_halted(self):
        ctm, sync = self.a9("level")
        sync.halt_all()
        sync.release()
        sync.restart_all()
        self.assertEqual(halted(ctm), [True, True])

    def test_the_halt_mode_has_no_default_and_must_be_one_of_the_two(self):
        ctm = SimCtm()
        ctis = [cti.Cti(ctm, b, **WIRING) for b in sorted(ctm.base)]
        for bad in (None, "set", ""):
            with self.assertRaises(cti.CtiError):
                cti.CrossHalt(ctis, halt_mode=bad, ack_timeout_polls=ACK)

    def test_the_debug_acknowledge_input_makes_the_other_core_follow_a_core_that_stops_by_itself(self):
        ctm, sync = build(halt_mode="level", n_cores=2)
        sync.arm(event_sync_trigin=5)                           # any input number (the PYNQ-Z2 uses 0, see plugins/zynq_cti.py)
        ctm.core_halts_by_itself(0, trigin=5)
        self.assertEqual(halted(ctm), [True, True])
        ctm2, sync2 = build(halt_mode="level", n_cores=2)
        sync2.arm(event_sync_trigin=5)
        ctm2.core_halts_by_itself(0, trigin=0)                  # another input: nothing follows
        self.assertEqual(halted(ctm2), [True, False])


if __name__ == "__main__":
    unittest.main()
