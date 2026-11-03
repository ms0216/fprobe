"""CoreSight Cross Trigger Interface (CTI): stop and restart several cores by one write. (Ported from the lxdbg prototype.)

STATUS: **verified on hardware for stopping, on one board**: PYNQ-Z2 (Zynq-7020, two Cortex-A9), 2026-10-28, with
the prototype's tools/zynq_cti_probe.py through Vivado's xsdb (NOT through this driver or fprobe yet). There, trigger output 0 of each per-core CTI is the external debug request
(both cores halt with MOE = 4), trigger input 5 of a CTI is its core's debug acknowledge (DBGACK), input 4 is always
high, and channel 0 reaches the other CTI through the matrix. Three things differ from what OpenOCD does on ARMv8:
  * a one-cycle pulse (CTIAPPPULSE) did NOT halt the cores; holding the channel (CTIAPPSET) did. Hence `halt_mode`
    ("level" there), which the caller must name;
  * trigger output 1 asserted, but did NOT restart the halted cores (neither pulsed nor held): restart them per core with
    the debug unit's DBGDRCR restart request (a restart skew does not change what was seen at the stop);
  * ARM's recommended numbers are right for output 0 only.
Not verified: BCM2836 has no CTI (never used); other boards; the stop skew itself (needs the Linux layer on the board);
the register layout is from OpenOCD's arm_cti.h and read back correctly on the PYNQ.

Why it matters: the debug request goes to every core through the cross trigger matrix in nanoseconds, instead of
one JTAG write per core (about 1 ms each through OpenOCD).

    io  = anything with read32(addr) and write32(addr, value)
    cti = Cti(io, base, "cti0", halt_out=0, restart_out=1, halt_chan=0, restart_chan=1)     # one per core; no defaults
    sync = CrossHalt([cti0, cti1, ...], halt_mode="level", ack_timeout_polls=1000)     # "pulse" or "level": see above
    sync.arm()          # once: enable, route channel 0 -> halt request (and, with event_sync_trigin, DBGACK -> channel 0)
    sync.halt_all()     # one write; every core with an open halt gate gets the request
    ...                 # wait until the cores report halted (DSCR), read state
    sync.release()      # before resuming: drop the channel, acknowledge the halt request
    ...                 # then restart each core (DBGDRCR on the Zynq A9s; sync.restart_all() only where it is known to work)
"""

# Register offsets (OpenOCD arm_cti.h)
CTICONTROL, CTIINTACK, CTIAPPSET, CTIAPPCLEAR, CTIAPPPULSE = 0x000, 0x010, 0x014, 0x018, 0x01C
CTIINEN, CTIOUTEN = 0x020, 0x0A0              # + 4 * trigger number
CTITRIGINSTATUS, CTITROUTSTATUS, CTICHINSTATUS, CTICHOUTSTATUS, CTIGATE = 0x130, 0x134, 0x138, 0x13C, 0x140
CTILAR, UNLOCK_KEY = 0xFB0, 0xC5ACCE55


class CtiError(Exception):
    pass


class Cti:
    def __init__(self, io, base, name="cti", *, halt_out, restart_out, halt_chan, restart_chan):
        """halt_out / restart_out: the trigger OUTPUTS wired to the core's debug request and restart; halt_chan /
        restart_chan: the channels used for them. They depend on the SoC's integration (see the module docstring), so there
        are NO defaults (ARM's recommended 0 / 1 / 0 / 1 are right for output 0 only on the PYNQ-Z2)."""
        self.io, self.base, self.name = io, base, name
        self.halt_out, self.restart_out = halt_out, restart_out
        self.halt_chan, self.restart_chan = halt_chan, restart_chan

    def write(self, reg, value):
        self.io.write32(self.base + CTILAR, UNLOCK_KEY)        # as OpenOCD does before every access
        self.io.write32(self.base + reg, value & 0xFFFFFFFF)

    def read(self, reg):
        return self.io.read32(self.base + reg)

    def modify(self, reg, mask, value):
        self.write(reg, (self.read(reg) & ~mask) | (value & mask))

    def arm(self):
        """Enable; every channel gated; channel `halt_chan` raises the halt request, `restart_chan` the restart."""
        self.write(CTICONTROL, 1)
        self.write(CTIGATE, 0)
        self.write(CTIOUTEN + 4 * self.halt_out, 1 << self.halt_chan)
        self.write(CTIOUTEN + 4 * self.restart_out, 1 << self.restart_chan)

    def ungate(self, chan):
        self.modify(CTIGATE, 1 << chan, 0xFFFFFFFF)

    def gate(self, chan):
        self.modify(CTIGATE, 1 << chan, 0)

    def pulse(self, chan):
        self.write(CTIAPPPULSE, 1 << chan)

    def set_channel(self, chan):
        """Hold the channel (CTIAPPSET) until clear_channel."""
        self.write(CTIAPPSET, 1 << chan)

    def clear_channel(self, chan):
        self.write(CTIAPPCLEAR, 1 << chan)

    def ack_halt(self, wait, timeout_polls):
        """Acknowledge the halt request (it stays asserted until then and would stop the core again right after a
        restart) and wait until the trigger output reads clear."""
        bit = 1 << self.halt_out
        self.write(CTIINTACK, bit)
        for _ in range(timeout_polls):
            if not self.read(CTITROUTSTATUS) & bit:
                return
            if wait:
                wait()
        raise CtiError("%s: the halt request is still asserted after the acknowledge" % self.name)

    def route_halt_event(self, trigin):
        """Make the core's own debug entry (trigger input `trigin`, DBGACK in ARM's recommended wiring) a
        channel event: when one core stops by itself, the others get the halt request in hardware."""
        self.modify(CTIINEN + 4 * trigin, 1 << self.halt_chan, 0xFFFFFFFF)

    def status(self):
        return {"trigin": self.read(CTITRIGINSTATUS), "trigout": self.read(CTITROUTSTATUS),
                "chin": self.read(CTICHINSTATUS), "chout": self.read(CTICHOUTSTATUS), "gate": self.read(CTIGATE)}


class CrossHalt:
    """The halt/restart protocol over one CTI per core (they must share a cross trigger matrix)."""

    def __init__(self, ctis, wait=None, halt_mode=None, *, ack_timeout_polls):
        """halt_mode: "pulse" (CTIAPPPULSE; OpenOCD on ARMv8) or "level" (CTIAPPSET held; what the PYNQ-Z2's
        Cortex-A9s need). It depends on how the board is wired, so there is no default."""
        if not ctis:
            raise CtiError("no CTI given")
        if halt_mode not in ("pulse", "level"):
            raise CtiError("halt_mode must be 'pulse' or 'level' (the board decides, see the module docstring), "
                           "not %r" % (halt_mode,))
        self.ctis, self.wait, self.halt_mode, self.ack_timeout_polls = list(ctis), wait, halt_mode, ack_timeout_polls

    def arm(self, event_sync_trigin=None):
        """event_sync_trigin: if given, a core that stops by itself (breakpoint) halts the others in hardware."""
        for c in self.ctis:
            c.arm()
            c.ungate(c.halt_chan)                               # let halt requests through
            if event_sync_trigin is not None:
                c.route_halt_event(event_sync_trigin)

    def halt_all(self):
        c = self.ctis[0]
        if self.halt_mode == "pulse":
            c.pulse(c.halt_chan)
        else:
            c.set_channel(c.halt_chan)

    def release(self):
        """Before any core resumes. Order as in OpenOCD: acknowledge, open the restart channel, close the halt
        channel (so the cores do not halt again from a stale request). In level mode the held channel is dropped first."""
        if self.halt_mode == "level":
            self.ctis[0].clear_channel(self.ctis[0].halt_chan)
        for c in self.ctis:
            c.ack_halt(self.wait, self.ack_timeout_polls)
            c.ungate(c.restart_chan)
            c.gate(c.halt_chan)

    def restart_all(self):
        """One pulse on the restart channel. NOT working on the PYNQ-Z2's Cortex-A9s (measured): the cores stayed halted."""
        self.ctis[0].pulse(self.ctis[0].restart_chan)

    def rearm_halt(self):
        """After the restart: let halt requests through again (and close the restart channel)."""
        for c in self.ctis:
            c.ungate(c.halt_chan)
            c.gate(c.restart_chan)


class ApbIo:
    """read32/write32 on a MEM-AP of the DAP fprobe has opened (`RawDap.mem_read32/mem_write32`). A CTI is a CoreSight
    component next to the cores' debug units on the APB-AP; UNVERIFIED: which APB addresses the PYNQ-Z2's CTIs have when the
    DAP is reached through PJTAG (the addresses in plugins/zynq_cti.py are the system addresses the AHB-AP shows)."""

    def __init__(self, dap, ap):
        self.dap, self.ap = dap, ap

    def read32(self, addr):
        return self.dap.mem_read32(self.ap, addr)

    def write32(self, addr, value):
        self.dap.mem_write32(self.ap, addr, value)
