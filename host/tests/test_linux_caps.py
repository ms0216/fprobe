from fprobe.linux.caps import CONFIG_NAMES, parse_caps

# 2026-10-22 に PYNQ-Z2（PynqLinux 3.0）で CAPS_SCRIPT を走らせた出力（config は関係する行だけ残した）
PYNQ = """@@kernel
6.6.10-xilinx-v2024.1-g3c0eca68c652
@@machine
armv7l
@@config_source
/proc/config.gz
@@config
CONFIG_BPF_SYSCALL=y
# CONFIG_BPF_JIT is not set
CONFIG_BPF_UNPRIV_DEFAULT_OFF=y
CONFIG_PERF_EVENTS=y
# CONFIG_KPROBES is not set
CONFIG_HAVE_HW_BREAKPOINT=y
CONFIG_DEVMEM=y
CONFIG_DEBUG_FS=y
# CONFIG_STRICT_DEVMEM is not set
@@tracefs
@@perf_event_paranoid
2
@@ptrace_scope
@@tools
gcc /usr/bin/gcc
gdbserver
gdb /usr/bin/gdb
perf
bpftrace
@@uid
1000
@@sudo
no
"""


def test_parse_pynq_sample():
    c = parse_caps(PYNQ)
    assert c["kernel"].startswith("6.6.10-xilinx") and c["machine"] == "armv7l"
    assert c["config_source"] == "/proc/config.gz"
    assert c["config"]["CONFIG_UPROBE_EVENTS"] == "n"          # FTRACE が無いので設定の一覧に出ない = 入っていない
    assert c["config"]["CONFIG_KPROBES"] == "n"
    assert c["config"]["CONFIG_BPF_SYSCALL"] == "y"
    assert c["config"]["CONFIG_HAVE_HW_BREAKPOINT"] == "y"
    assert c["tracefs"] is None and c["uprobe_events"] is False and c["trace_clocks"] is None
    assert c["perf_event_paranoid"] == 2 and c["ptrace_scope"] is None
    assert c["tools"] == {"gcc": "/usr/bin/gcc", "gdbserver": None, "gdb": "/usr/bin/gdb", "perf": None, "bpftrace": None}
    assert c["uid"] == 1000 and c["sudo_nopasswd"] is False


def test_unreadable_config_is_unknown():
    c = parse_caps("@@kernel\n6.1\n@@tools\n")
    assert c["config_source"] is None
    assert c["config"] == {name: None for name in CONFIG_NAMES}


def test_trace_clocks_listed():
    c = parse_caps("@@tracefs\n/sys/kernel/tracing\n@@events\nuprobe_events\n@@trace_clock\n[local] global counter mono_raw\n")
    assert c["uprobe_events"] is True and c["trace_clocks"] == ["local", "global", "counter", "mono_raw"]


def test_tracefs_without_uprobes():
    """Pi 2B（2026-10-23）: tracefs と kprobe_events はあるが uprobe_events は無い。tracefs を「無い」と言わない"""
    c = parse_caps("@@tracefs\n/sys/kernel/tracing\n@@events\nkprobe_events\n@@trace_clock\n[local] mono_raw\n")
    assert c["tracefs"] == "/sys/kernel/tracing" and c["uprobe_events"] is False and c["kprobe_events"] is True
