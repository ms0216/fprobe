"""停止の時刻の「根拠」の文（StopReport.basis・assumptions に事実として載せる固定の文）。ArmACores と OpenOCD の道が使う。"""
SEQ_BASIS = "host-timed: from the last instant each core was known not to have halted until all were seen halted"
SAME_HALT_TIME = ("every core takes the same time from its DBGDRCR write to its halt (the target's APB transfer and the "
                  "core's entry to debug state; not measured)")
FPGA_BASIS = (
    "fpga-timed: the FPGA's timestamps of the accepted halt-request writes (DBGDRCR.HRQ; DAP events of kind 1, ack OK), "
    "first to last core: %d ticks at %d Hz, rounded up to ns; from the moment each write reaches the target (JTAG "
    "Update-DR) to its timestamp the FPGA takes the same number of cycles for every write (docs/linux-halt.md; on a "
    "device declaring DAP_LINE_TIME the timestamp is that Update-DR point itself), which adds "
    "0; every core reported a halt request as its entry reason (DSCR.MOE); assumes every core takes the same time from "
    "its DBGDRCR write to its halt (the target's APB transfer and the core's entry to debug state; not measured); "
    "host clock: %s (without that assumption, %s)")
REACH = ("each core halted by a halt request halts at the moment the request's write reaches it (the target's APB "
        "transfer and the core's entry to debug state after the write are not measured)")
FPGA_EVENT_BASIS = (
    "fpga-timed (stop by a core): cores %s were found halted by the host and the others were sent halt requests; from "
    "the earliest moment any core could have halted (core %d: %s, FPGA timestamp of that transfer) to the last accepted "
    "halt-request write (DBGDRCR.HRQ): %d ticks at %d Hz, rounded up to ns; assumes %s; without that assumption (until "
    "each core was read halted, FPGA timestamps): %s; host clock: %s")
FPGA_WATCH_BASIS = (
    "fpga-timed: from the FPGA's last sample that read core %d running (its read request on the wire, DAP_LINE_TIME) "
    "to the FPGA's last halt-request write reaching the target (DBGDRCR.HRQ, Update-DR): %d ticks at %d Hz, rounded up "
    "to ns; every other core reported a halt request as its entry reason (DSCR.MOE); assumes %s; caller's bound: %s; "
    "host clock: %s")
