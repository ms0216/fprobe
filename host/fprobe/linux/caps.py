"""ターゲットの Linux の能力の事実（カーネルの設定・tracefs・perf・ptrace・道具・権限）。

CAPS_SCRIPT をターゲットの /bin/sh で走らせ、その出力を parse_caps で読む。読めなかったものは None（不明）で返し、推測で埋めない。"""
from __future__ import annotations

CONFIG_NAMES = ("CONFIG_UPROBES", "CONFIG_UPROBE_EVENTS", "CONFIG_FTRACE", "CONFIG_KPROBES", "CONFIG_BPF_SYSCALL",
                "CONFIG_BPF_JIT", "CONFIG_DEBUG_INFO_BTF", "CONFIG_PERF_EVENTS", "CONFIG_HAVE_HW_BREAKPOINT",
                "CONFIG_DEVMEM", "CONFIG_STRICT_DEVMEM", "CONFIG_DEBUG_FS")
TOOLS = ("gcc", "gdbserver", "gdb", "perf", "bpftrace")
TRACEFS = ("/sys/kernel/tracing", "/sys/kernel/debug/tracing")

CAPS_SCRIPT = r"""
echo @@kernel; uname -r
echo @@machine; uname -m
if [ -r /proc/config.gz ]; then echo @@config_source; echo /proc/config.gz; echo @@config; zcat /proc/config.gz
elif [ -r /boot/config-$(uname -r) ]; then echo @@config_source; echo /boot/config-$(uname -r); echo @@config; cat /boot/config-$(uname -r)
fi
echo @@tracefs
for d in """ + " ".join(TRACEFS) + r"""; do if [ -e $d/trace_clock ] || sudo -n test -e $d/trace_clock 2>/dev/null; then echo $d; T=$d; break; fi; done
echo @@events
for f in uprobe_events kprobe_events; do if [ -n "$T" ] && { [ -e $T/$f ] || sudo -n test -e $T/$f 2>/dev/null; }; then echo $f; fi; done
for d in """ + " ".join(TRACEFS) + r"""; do if [ -e $d/trace_clock ] || sudo -n test -e $d/trace_clock 2>/dev/null; then echo @@trace_clock; cat $d/trace_clock 2>/dev/null || sudo -n cat $d/trace_clock; break; fi; done
echo @@perf_event_paranoid; cat /proc/sys/kernel/perf_event_paranoid 2>/dev/null
echo @@ptrace_scope; cat /proc/sys/kernel/yama/ptrace_scope 2>/dev/null
echo @@tools
for t in """ + " ".join(TOOLS) + r"""; do echo "$t $(command -v $t)"; done
echo @@uid; id -u
echo @@sudo; sudo -n true 2>/dev/null && echo yes || echo no
"""


def _sections(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    lines: list[str] | None = None
    for line in text.splitlines():
        if line.startswith("@@"):
            lines = out.setdefault(line[2:].strip(), [])
        elif lines is not None and line.strip():
            lines.append(line.rstrip())
    return out


def _int(lines: list[str] | None) -> int | None:
    try:
        return int(lines[0]) if lines else None
    except ValueError:
        return None


def _config(lines: list[str] | None) -> dict[str, str | None]:
    if not lines:
        return {name: None for name in CONFIG_NAMES}               # 設定が読めない: 不明
    found: dict[str, str] = {}
    for line in lines:
        if line.startswith("# CONFIG_") and line.endswith(" is not set"):
            found[line[2:-len(" is not set")]] = "n"
        elif line.startswith("CONFIG_") and "=" in line:
            k, v = line.split("=", 1)
            found[k] = v.strip('"')
    return {name: found.get(name, "n") for name in CONFIG_NAMES}   # 設定に無い名前 = 選べない（n）


def parse_caps(text: str) -> dict:
    s = _sections(text)
    first = lambda k: s[k][0] if s.get(k) else None              # noqa: E731
    tools = {}
    for line in s.get("tools", []):
        name, _, path = line.partition(" ")
        tools[name] = path.strip() or None
    clocks = s.get("trace_clock")
    return {
        "kernel": first("kernel"),
        "machine": first("machine"),
        "config_source": first("config_source"),
        "config": _config(s.get("config")),
        "tracefs": first("tracefs"),
        "uprobe_events": "uprobe_events" in s.get("events", []),
        "kprobe_events": "kprobe_events" in s.get("events", []),
        "trace_clocks": clocks[0].replace("[", "").replace("]", "").split() if clocks else None,
        "perf_event_paranoid": _int(s.get("perf_event_paranoid")),
        "ptrace_scope": _int(s.get("ptrace_scope")),
        "tools": {t: tools.get(t) for t in TOOLS},
        "uid": _int(s.get("uid")),
        "sudo_nopasswd": first("sudo") == "yes",
    }
