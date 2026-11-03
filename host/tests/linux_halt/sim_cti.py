"""A register-level model of per-core CTIs on one cross trigger matrix, for lxdbg/cti.py. It is a model of how the
sequence OpenOCD uses on ARMv8 is meant to behave, not of any real chip: gates are modelled as acting on both the way
into and out of the matrix (as OpenOCD's use implies); the trigger numbers are the constructor's.

The register offsets are written out here (from OpenOCD's arm_cti.h), NOT taken from lxdbg/cti.py: a wrong constant
there must make the model stop answering.

The point of the model: a halt request stays asserted until acknowledged (a core restarted with a stale request halts
again), a closed gate keeps a core out of the matrix, and a restart only works on cores that are halted and whose
restart channel is open. A driver that gets the order wrong fails here the way it would on hardware."""


class SimCore:
    def __init__(self, n):
        self.n, self.halted, self.restarts, self.failed_restarts = n, False, 0, 0
        self.dbgrq = False                    # level; cleared by the CTI acknowledge

    def _check(self):
        if self.dbgrq:
            self.halted = True


class SimCti:
    def __init__(self, ctm, core, halt_out=0, restart_out=1):
        self.ctm, self.core, self.halt_out, self.restart_out = ctm, core, halt_out, restart_out
        self.control = self.gate = 0
        self.appset = 0
        self.inen, self.outen = {}, {}
        self.unlocked = False
        self.writes_while_locked = 0

    # --- register access
    def write(self, off, v):
        if off == 0xFB0:
            self.unlocked = v == 0xC5ACCE55
            return
        if not self.unlocked:
            self.writes_while_locked += 1
            return
        if off == 0x000:
            self.control = v & 1
        elif off == 0x140:
            self.gate = v
        elif off == 0x01C:
            if self.control and self.ctm.pulses:                   # (the PYNQ-Z2's A9s ignore pulses: pulses=False)
                self.ctm.channel_event(self, v)
        elif off == 0x014:                                         # CTIAPPSET: the channel is held
            if self.control:
                self.appset |= v
                self.ctm.channel_event(self, v)
        elif off == 0x018:                                         # CTIAPPCLEAR
            self.appset &= ~v
        elif off == 0x010:
            if v & (1 << self.halt_out):
                self.core.dbgrq = False
        elif 0x020 <= off < 0x020 + 0x40:
            assert off % 4 == 0, "unaligned CTI register 0x%X" % off
            self.inen[(off - 0x020) // 4] = v
        elif 0x0A0 <= off < 0x0A0 + 0x40:
            assert off % 4 == 0, "unaligned CTI register 0x%X" % off
            self.outen[(off - 0x0A0) // 4] = v

    def read(self, off):
        if off == 0x140:
            return self.gate
        if off == 0x134:
            return (1 << self.halt_out) if self.core.dbgrq else 0
        if off == 0x000:
            return self.control
        if 0x0A0 <= off < 0x0A0 + 0x40:
            return self.outen.get((off - 0x0A0) // 4, 0)
        if 0x020 <= off < 0x020 + 0x40:
            return self.inen.get((off - 0x020) // 4, 0)
        return 0

    # --- matrix side
    def deliver(self, chans):
        """Channel events arriving from the matrix: drive the trigger outputs mapped to them (if the gate is open)."""
        if not self.control:
            return
        for out, mask in self.outen.items():
            m = mask & chans & self.gate
            if not m:
                continue
            if out == self.halt_out:
                self.core.dbgrq = True
                self.core._check()
            elif out == self.restart_out:
                if not self.ctm.restart_works:                    # (the PYNQ-Z2's A9s: output 1 does not restart)
                    return
                c = self.core
                if c.halted and not c.dbgrq:
                    c.halted = False
                    c.restarts += 1
                else:
                    c.failed_restarts += 1


class SimCtm:
    def __init__(self, n_cores=4, halt_out=0, restart_out=1, pulses=True, restart_works=True):
        self.pulses, self.restart_works = pulses, restart_works
        self.cores = [SimCore(i) for i in range(n_cores)]
        self.ctis = [SimCti(self, c, halt_out, restart_out) for c in self.cores]
        self.base = {0x1000 * (i + 1): s for i, s in enumerate(self.ctis)}

    def channel_event(self, source, chans):
        chans &= source.gate                    # the pulsing CTI's own gate
        if chans:
            for c in self.ctis:
                c.deliver(chans)

    def core_halts_by_itself(self, n, trigin=0):
        """A breakpoint: the core enters debug state and raises its debug-acknowledge trigger input `trigin`."""
        c = self.cores[n]
        c.halted = True
        s = self.ctis[n]
        if s.control and s.inen.get(trigin, 0) & s.gate:
            self.channel_event(s, s.inen[trigin])

    # --- the io the driver uses
    def read32(self, addr):
        base = addr & ~0xFFF
        return self.base[base].read(addr & 0xFFF)

    def write32(self, addr, v):
        self.base[addr & ~0xFFF].write(addr & 0xFFF, v)
