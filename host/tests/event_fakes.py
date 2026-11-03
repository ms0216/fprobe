"""イベント記録（fprobe.events.EventLog の口）の偽物。FakeDapLink の転送を覗き、FPGA の dap_monitor と同じ形の
イベント（種類 1。DRW はホストが書いた TAR から求めたメモリの番地、それ以外はレジスタの番地）を時刻つきで積む。

時刻は転送ごとに ticks_per_transfer だけ進む（試験が上限の値を正確に確かめられるように）。
AP の読みは線の上と同じくポストされた形で積む: 読みを頼んだ転送（値は積まない）の直後に、値を返す DP の RDBUFF の読みを積む。
"""
from fprobe.events import ACK_OK, KIND_HOST, DapEvent

MASK48 = (1 << 48) - 1


class RecordingEventLog:
    def __init__(self, link, *, ticks_per_transfer: int, start: int = 0, capacity: int = 1 << 16):
        self.link = link
        self.ticks = ticks_per_transfer
        self.t = start
        self.capacity = capacity
        self.events: list[DapEvent] = []
        self.lost = 0
        self.overwritten = 0
        self.records = 0
        self.recording = True
        self.reads = 0                                  # read の回数（試験が「読んだか」を確かめる）
        self._tar: dict[int, int] = {}                  # AP → TAR（自動増加なし: RawDap は CSW を増加なしにする）
        write, read = link.write_reg, link.read_reg

        def write_reg(reg_id, value, dap_index=0):
            write(reg_id, value, dap_index=dap_index)
            self._note(reg_id.value, False, value)

        def read_reg(reg_id, dap_index=0, now=True):
            out = read(reg_id, dap_index=dap_index, now=now)
            value = out() if callable(out) else out
            self._note(reg_id.value, True, 0)
            if reg_id.value >= 4:                       # 線の上では AP の読みはポストされる: 値は続く RDBUFF の読みが返す
                self._note(0x3, True, value)            # DP の 0xC（RDBUFF）
            return out
        link.write_reg, link.read_reg = write_reg, read_reg

    def _note(self, reg: int, rnw: bool, value: int) -> None:
        if not self.recording:
            return
        self.t += self.ticks
        ap = reg >= 4
        a = (reg & 3) << 2
        select = self.link.select
        register = (select & 0xF0 | a) if ap else a
        address = a
        if ap and select & 0xF0 == 0 and a == 0x4 and not rnw:
            self._tar[select >> 24] = value
        if ap and select & 0xF0 == 0 and a == 0xC:
            address = self._tar.get(select >> 24, a)
        self.events.append(DapEvent(time=self.t & MASK48, kind=KIND_HOST, ap=ap, read=rnw, register=register & 0xC,
                                    ack=ACK_OK, parity_error=False, address=address, data=value))

    # --- EventLog の口 ---
    def running(self) -> bool:
        return self.recording

    def counts(self) -> tuple[int, int]:
        return len(self.events), self.lost

    def read(self, last=None, end=None):
        self.reads += 1
        end = len(self.events) if end is None else end
        n = end if last is None else min(last, end)
        self.overwritten = 0
        out = list(self.events[end - n:end])
        self.records = len(out)  # EventLog.records と同じ（この偽物は 1 記録 1 イベント）
        return out
