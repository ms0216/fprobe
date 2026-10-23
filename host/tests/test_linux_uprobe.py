from pathlib import Path

import pytest

from fprobe.linux.uprobe import Site, UprobeRefused, parse_trace_line, site_of, uprobe_line

DATA = Path(__file__).parent / "data" / "linux"          # tick.c を PYNQ の gcc で -marm / -mthumb で作ったもの


def test_site_of_symbol_arm_and_thumb():
    arm = site_of(DATA / "tick_arm", "/tmp/tick_arm", symbol="tick")
    assert arm == Site("/tmp/tick_arm", 0x5cc, 0x5cc, False, "EM_ARM")
    thumb = site_of(DATA / "tick_thumb", "/tmp/tick_thumb", symbol="tick")
    assert thumb.thumb is True and thumb.offset == 0x5cc and thumb.vaddr == 0x5cc


def test_site_of_address_needs_isa_on_arm32():
    with pytest.raises(ValueError, match="isa"):
        site_of(DATA / "tick_arm", "/tmp/tick_arm", address=0x5cc)
    assert site_of(DATA / "tick_arm", "/tmp/tick_arm", address=0x5cc, isa="arm").offset == 0x5cc


def test_site_of_unknown_symbol():
    with pytest.raises(ValueError, match="nosuch"):
        site_of(DATA / "tick_arm", "/tmp/tick_arm", symbol="nosuch")


def test_uprobe_line_text():
    line = uprobe_line("enter", Site("/usr/bin/app", 0x1234, 0x11234, False, "EM_ARM"), ["a=%r0:s32"])
    assert line == "p:fprobe/enter /usr/bin/app:0x1234 a=%r0:s32"
    assert uprobe_line("leave", Site("/a", 0x10, 0x10, False, "EM_AARCH64"), [], ret=True) == "r:fprobe/leave /a:0x10"


def test_uprobe_refuses_thumb_on_arm32():
    with pytest.raises(UprobeRefused, match="32 ビットの ARM の uprobes は ARM 命令だけ"):
        uprobe_line("enter", Site("/a", 0x10, 0x10, True, "EM_ARM"), [])


def test_bad_name():
    with pytest.raises(ValueError):
        uprobe_line("a b", Site("/a", 0x10, 0x10, False, "EM_ARM"), [])


def test_parse_trace_line():
    r = parse_trace_line("            app-812     [001] d..1.  1234.567890: enter: (0x10450) a=5")
    assert r == {"comm": "app", "pid": 812, "cpu": 1, "t_ns": 1234567890000, "event": "enter", "args": {"a": "5"},
                 "ip": "0x10450", "text": "enter: (0x10450) a=5",
                 "raw": "            app-812     [001] d..1.  1234.567890: enter: (0x10450) a=5"}


def test_parse_ignores_other():
    assert parse_trace_line("# tracer: nop") is None
    assert parse_trace_line("") is None


def test_parse_tracepoint_line():
    """Pi 2B の sched_switch の行（2026-10-23。番地の括弧が無い）"""
    line = ("          <idle>-0       [001] d....  2432.515105: sched_switch: prev_comm=swapper/1 prev_pid=0 prev_prio=120 "
            "prev_state=R ==> next_comm=rcu_sched next_pid=15 next_prio=120")
    r = parse_trace_line(line)
    assert (r["comm"], r["pid"], r["cpu"], r["t_ns"], r["event"]) == ("<idle>", 0, 1, 2432515105000, "sched_switch")
    assert r["args"]["prev_pid"] == "0" and r["args"]["next_comm"] == "rcu_sched" and r["args"]["next_pid"] == "15"
    assert "ip" not in r or r["ip"] is None


def test_parse_syscall_lines_from_pi():
    """Pi 2B の syscalls の行（2026-10-23）: 「sys_x(引数)」と「sys_x -> 戻り値」"""
    enter = parse_trace_line("              sh-4212    [000] .....  3201.462675: sys_openat(dfd: 0xffffff9c, filename: 0x24d60, "
                             "flags: 0x20000, mode: 0)")
    leave = parse_trace_line("              sh-4212    [000] .....  3201.462724: sys_openat -> 0x0")
    assert (enter["event"], enter["pid"], enter["ip"]) == ("sys_openat", 4212, None) and "flags: 0x20000" in enter["text"]
    assert (leave["event"], leave["args"]) == ("sys_openat", {"ret": "0x0"})
