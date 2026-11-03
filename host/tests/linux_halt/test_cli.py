import pytest

from fprobe.linux_halt import cli
from fprobe.linux_halt.kernel import Kernel
from .test_kernel import kernel_with, layout

COMMON = ["--jtag-khz", "4000", "--offsets", "offsets.json", "--page-offset", "0x80000000", "--phys-offset", "0",
          "--linear-map-end", "0xBB400000",
          "--max-tasks", "100000", "--max-threads", "4096", "--mmu-format", "short", "--journal", "j.json"]
ATTACH = ["--pid", "123", "--port", "3340", "--skew-limit-ns", "68", "--thread-id", "stack", "--kernel-isa", "arm",
          "--max-step-retries", "8", "--bp-unmapped", "error"]
OPENOCD = ["--backend", "openocd", "--ocd-board", "rpi2b", "--ocd-port", "6666", "--ocd-timeout-s", "60", "--hw-slots", "5", "--watch-slots", "4"]
FPROBE = ["--backend", "fprobe", "--part", "bcm2836"]


def argv(*parts):
    return ["attach", *[x for p in parts for x in p]]


def drop(args, flag):
    i = args.index(flag)
    return args[:i] + args[i + 2:]


def test_a_complete_attach_command_line_parses_for_both_backends():
    for backend in (OPENOCD, FPROBE):
        a = cli.parse(argv(backend, COMMON, ATTACH))
        assert a.pid == 123 and a.jtag_khz == 4000 and a.page_offset == 0x80000000


@pytest.mark.parametrize("flag", ["--jtag-khz", "--offsets", "--page-offset", "--phys-offset", "--linear-map-end", "--max-tasks",
                                  "--max-threads",
                                  "--mmu-format", "--journal"])
def test_every_common_setting_is_required(flag):
    with pytest.raises(SystemExit):
        cli.parse(argv(OPENOCD, drop(COMMON, flag), ATTACH))


@pytest.mark.parametrize("flag", ["--pid", "--port", "--skew-limit-ns", "--thread-id", "--kernel-isa", "--max-step-retries",
                                  "--bp-unmapped"])
def test_every_attach_setting_is_required(flag):
    with pytest.raises(SystemExit):
        cli.parse(argv(OPENOCD, COMMON, drop(ATTACH, flag)))


@pytest.mark.parametrize("flag", ["--ocd-board", "--ocd-port", "--ocd-timeout-s", "--hw-slots", "--watch-slots"])
def test_the_openocd_backend_needs_its_own_settings(flag):
    with pytest.raises(SystemExit):
        cli.parse(argv(drop(OPENOCD, flag), COMMON, ATTACH))


def test_the_fprobe_backend_needs_the_part_name():
    with pytest.raises(SystemExit):
        cli.parse(argv(["--backend", "fprobe"], COMMON, ATTACH))


def test_the_clock_must_be_in_range():
    with pytest.raises(SystemExit):
        cli.parse(argv(OPENOCD, ["--jtag-khz", "0"] + drop(COMMON, "--jtag-khz"), ATTACH))
    with pytest.raises(SystemExit):
        cli.parse(argv(OPENOCD, ["--jtag-khz", "100001"] + drop(COMMON, "--jtag-khz"), ATTACH))


def test_list_processes_skips_the_idle_task_and_marks_kernel_threads():
    k0, m = kernel_with([(100, [100], "app"), (200, [200], "kworker")])
    k = Kernel(m.read, layout(), max_tasks=100, max_threads=16)
    m.w32(k.find_leader(200).addr + 0x28, 0)                         # mm = 0: a kernel thread
    rows = cli.list_processes(k)
    assert [(r.tgid, r.comm, r.has_mm) for r in rows] == [(100, "app", True), (200, "kworker", False)]


def test_status_is_a_read_only_subcommand_with_the_common_settings():
    a = cli.parse(["status", *OPENOCD, *COMMON])
    assert a.cmd == "status"
    with pytest.raises(SystemExit):
        cli.parse(["status", *OPENOCD, *drop(COMMON, "--journal")])


def test_the_openocd_board_must_be_a_known_one():
    with pytest.raises(SystemExit):
        cli.parse(argv(["--ocd-board", "nosuch"] + drop(OPENOCD, "--ocd-board"), COMMON, ATTACH))
    assert cli.parse(argv(OPENOCD, COMMON, ATTACH)).ocd_board == "rpi2b"


def test_the_probe_can_be_the_pynq_over_ethernet_but_not_both():
    a = cli.parse(argv(FPROBE, ["--probe-eth", "<家の LAN のアドレス>", "--probe-eth-link-up-wait", "15"], COMMON, ATTACH))
    assert a.probe_eth == "<家の LAN のアドレス>" and a.probe_eth_link_up_wait == 15.0
    with pytest.raises(SystemExit):
        cli.parse(argv(FPROBE, ["--probe-eth", "<家の LAN のアドレス>", "--probe-eth-link-up-wait", "15", "--probe-serial", "X"],
                       COMMON, ATTACH))


def test_the_wait_for_the_ethernet_link_is_given_with_the_probe_eth_and_has_no_default():
    with pytest.raises(SystemExit):
        cli.parse(argv(FPROBE, ["--probe-eth", "<家の LAN のアドレス>"], COMMON, ATTACH))           # 待つ時間が無い
    with pytest.raises(SystemExit):
        cli.parse(argv(FPROBE, ["--probe-eth-link-up-wait", "15"], COMMON, ATTACH))        # Ethernet でないのに待つ時間


def test_the_fpga_halt_watch_needs_a_stated_bound_and_poll_interval():
    a = cli.parse(argv(FPROBE, ["--fpga-halt-watch-bound-ns", "277000", "--fpga-halt-watch-poll-s", "0.001"], COMMON, ATTACH))
    assert (a.fpga_halt_watch_bound_ns, a.fpga_halt_watch_poll_s) == (277000, 0.001)
    a = cli.parse(argv(FPROBE, ["--fpga-halt-watch-bound-ns", "unmeasured", "--fpga-halt-watch-poll-s", "0"], COMMON, ATTACH))
    assert a.fpga_halt_watch_bound_ns is None and a.fpga_halt_watch_poll_s == 0.0
    for extra in (["--fpga-halt-watch-bound-ns", "277000"], ["--fpga-halt-watch-poll-s", "0.001"]):
        with pytest.raises(SystemExit):
            cli.parse(argv(FPROBE, extra, COMMON, ATTACH))                     # 片方だけは不可
    with pytest.raises(SystemExit):
        cli.parse(argv(OPENOCD, ["--fpga-halt-watch-bound-ns", "1", "--fpga-halt-watch-poll-s", "0"], COMMON, ATTACH))


def test_the_fpga_request_times_are_asked_for_explicitly_and_belong_to_the_fprobe_backend_without_a_cti():
    assert cli.parse(argv(FPROBE, ["--fpga-request-times"], COMMON, ATTACH)).fpga_request_times is True
    assert cli.parse(argv(FPROBE, [], COMMON, ATTACH)).fpga_request_times is False      # 頼まなければ PC の時計の上限だけ
    a = cli.parse(argv(FPROBE, ["--fpga-request-times", "--fpga-halt-watch-bound-ns", "1", "--fpga-halt-watch-poll-s", "0"],
                       COMMON, ATTACH))
    assert a.fpga_request_times and a.fpga_halt_watch                  # 見張りの無い停止（gdb の中断等）に使う
    with pytest.raises(SystemExit):
        cli.parse(argv(OPENOCD, ["--fpga-request-times"], COMMON, ATTACH))
    with pytest.raises(SystemExit):
        cli.parse(argv(FPROBE, CTI + ["--fpga-request-times"], COMMON, ATTACH))


CTI = ["--cti-board", "zynq7000", "--cti-request-bound-ns", "1000", "--cti-event-bound-ns", "unmeasured",
       "--cti-hw-wait-s", "0.05", "--cti-ack-polls", "100"]


def test_the_cti_needs_every_value_stated_and_belongs_to_the_fprobe_backend():
    a = cli.parse(argv(FPROBE, CTI, COMMON, ATTACH))
    assert (a.cti_board, a.cti_request_bound_ns, a.cti_event_bound_ns, a.cti_hw_wait_s, a.cti_ack_polls) ==         ("zynq7000", 1000, None, 0.05, 100)
    assert a.cti is True
    assert cli.parse(argv(FPROBE, [], COMMON, ATTACH)).cti is False
    for i in range(0, len(CTI), 2):                       # 1 つでも欠けたら断る（既定を持たない）
        with pytest.raises(SystemExit):
            cli.parse(argv(FPROBE, CTI[:i] + CTI[i + 2:], COMMON, ATTACH))
    with pytest.raises(SystemExit):
        cli.parse(argv(OPENOCD, CTI, COMMON, ATTACH))
    with pytest.raises(SystemExit):
        cli.parse(argv(FPROBE, CTI + ["--fpga-halt-watch-bound-ns", "1", "--fpga-halt-watch-poll-s", "0"], COMMON, ATTACH))


def test_the_subcommands_of_the_api_operations_take_the_common_settings_and_build_the_same_options():
    for cmd in ("ps", "halt", "resume", "recover", "rescue", "status"):
        a = cli.parse([cmd, *FPROBE, *COMMON])
        assert a.cmd == cmd and a.options.part == "bcm2836" and a.options.fpga_request_times is False
    a = cli.parse(["threads", *FPROBE, *COMMON, "--pid", "812"])
    assert a.pid == 812
    with pytest.raises(SystemExit):
        cli.parse(["threads", *FPROBE, *COMMON])                         # --pid は必須


def test_unmeasured_reaches_the_options_as_the_word_and_the_namespace_as_none():
    a = cli.parse(argv(FPROBE, CTI, COMMON, ATTACH))
    assert a.options.cti_event_bound_ns == "unmeasured" and a.cti_event_bound_ns is None
