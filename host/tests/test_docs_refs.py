"""公開向けの文書（README.md と docs/guide/*.md）の参照の検査。壊れた参照（無い道具名・無いリンク先・無い CLI）を試験で落とす。

- (a) バッククォートの識別子で道具の形のもの（`_` を含む小文字）は、API の仕様（host/api/fprobe_api.json）から作れる道具名に実在する
- (b) 相対リンクの先のファイルが実在する
- (c) `fprobe ...` / `python -m fprobe_gw ...` の第 1 サブコマンドが実在する
- 公開してはいけないもの（機器の USB シリアル番号・家の LAN のアドレス・個人のパス）が無い
"""
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCS = [ROOT / "README.md", *sorted((ROOT / "docs" / "guide").glob("*.md"))]

# 道具の形ではないが `_` を含む識別子（設計名・generic・環境変数・関数名・引数名・レジスタ名など）。文書に出るものだけを列挙し、
# 増やすときは理由が説明できるものに限る
NOT_TOOLS = {
    "fprobe_pynq", "fprobe_pynq_deep", "fprobe_core_m", "fprobe_la", "fprobe_eth", "fprobe_jtag", "fprobe_serial",
    "fprobe_nano9k", "fprobe_core", "fprobe_obs", "fprobe_gw", "obs_target_primer", "obs_core", "obs_soft_core",
    "obs_soft_tap", "obs_snap_core", "obs_tck_sampler", "obs_async_fifo", "obs_match", "obs_mark", "obs_jtag",
    "ir_length_source",                                   # jtag.scan の応答の項目名（道具名ではない）
    "fpga_obs", "target_ip", "half_period", "id_scans", "auto_reopen", "sync_period", "ir_len", "ir_before", "ir_after",
    "dr_before", "dr_after", "user_ir", "id_ok", "id_timeout", "time_us", "time_basis", "target_cycles", "previous_ratio",
    "nominal_ratio", "window_s", "gap_s", "post_s", "rate_hz", "frequencies_hz", "design_switch", "causes_total",
    "fpga_hidden", "fpga_lost", "reads_missing", "read_failed", "pins_missing", "events_error", "recording_dropped",
    "watch_running", "events_errors", "lost_lines", "lost_frames", "ONE_CLOCK", "FIFO_DEPTH", "CLOCK_HZ", "LOST_BITS",
    "SYNC_DELAY", "SAMPLE_DELAY", "EVENTS_WRITTEN", "FPROBE_GOWIN_HOME", "FPROBE_VIVADO_HOME", "FPROBE_GHDL_HOME",
    "FPROBE_PYNQ_HOST", "FPROBE_PYNQ_SUDO_PASSWORD", "user_ir", "tdo_oe", "tck_ce", "value_untimed", "gap_untimed",
    "group_untimed", "g_errors", "g_scenario", "ui_state", "max_coverage", "no_values", "lost_bits", "stop_after",
    "is_available", "open_eth", "write_vcd", "dut_dbg", "la_inputs", "ps_axi", "sample_hz", "decimate_after_s",
    "segment_s", "max_bytes", "halt_la", "cmd_pending", "overflow_seen", "cmd_dropped", "inputs_sha256",
    "read_transitions", "rp2350_trace", "zmk_health", "zynq_frame_link", "file_root", "probe_window",
    "clock_hz", "fifo_depth", "sample_delay", "sync_delay", "span_s", "t0_us", "t1_us", "sld_virtual_jtag",   # 応答の項目・ベンダーのプリミティブ名
    "before_beats", "after_beats", "filter_channels", "filter_ns", "external_only", "vcd_path", "timeout_s",   # la_* の引数名
    "la_layout", "la_sample_hz", "la_filter", "la_pulse", "capacity_beats", "sample_period_ns", "trigger_time_span",   # ケイパビリティ・応答の項目名
    "trigger_index", "first_edges_ns", "first_edges_truncated", "constant_low", "duration_s", "delay_ns",
    "cs_active_low", "data_bits", "errors_only", "lsb_first", "msb_first", "stop_bits", "word_bits",   # la_decode の設定・引数の名前
    "la_in", "sys_clock_hz", "trigger_time",                                  # ボード定義の資源名・ケイパビリティ・応答の項目名
    "probe_drives_swdio", "dap_busy",                                         # ロジアナの内部の ch の名前
    # Python の関数・プラグイン名・RTL の entity 名
}

# 公開してはいけない値: 機器の USB シリアル番号は、リポジトリ直下の CLAUDE.md の機器の表から試験のときに読む（この試験ファイル
# 自体には書かない）。CLAUDE.md が無い環境ではシリアル番号の検査は飛ばす。家の LAN のアドレスと個人のパスは常に検査する
FORBIDDEN_RE = [re.compile(r"192\.168\.0\.\d+"), re.compile(r"[Cc]:[/\\]+Users[/\\]+M[/\\]")]


def _private_serials() -> set[str]:
    path = ROOT / "CLAUDE.md"
    if not path.exists():
        return set()
    return set(re.findall(r"[0-9A-F]{4}:[0-9A-F]{4} / `([^`]+)`", path.read_text(encoding="utf-8")))


def _tool_names() -> set[str]:
    spec = json.loads((ROOT / "host" / "api" / "fprobe_api.json").read_text(encoding="utf-8"))
    return {name.replace(".", "_") for name in spec["methods"]}


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _outside_fences(text: str) -> str:
    """コードの囲み（```）の外だけ。"""
    return re.sub(r"```.*?```", "", text, flags=re.S)


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_tool_names_exist(doc):
    tools = _tool_names()
    text = _text(doc)
    candidates = set(re.findall(r"`([a-z][a-z0-9]*(?:_[a-z0-9]+)+)`", _outside_fences(text)))
    # コードの囲みの中の関数呼び出し `name(` も道具として確かめる
    for block in re.findall(r"```.*?```", text, flags=re.S):
        candidates |= set(re.findall(r"^\s*([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\(", block, flags=re.M))
    missing = sorted(n for n in candidates if n not in tools and n not in NOT_TOOLS)
    assert not missing, f"{doc.name}: API に無い道具名（または NOT_TOOLS に足す理由が要る識別子）: {missing}"


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_relative_links_exist(doc):
    broken = []
    for target in re.findall(r"\]\(([^)\s]+)\)", _text(doc)):
        if re.match(r"^[a-z]+:", target) or target.startswith("#"):
            continue
        path = target.split("#", 1)[0]
        if path and not (doc.parent / path).exists():
            broken.append(target)
    assert not broken, f"{doc.name}: リンク先が無い: {broken}"


def _cli_subcommands() -> tuple[set[str], set[str]]:
    import contextlib
    import io

    from fprobe.__main__ import main as host_main
    from fprobe_gw.cli import main as gw_main

    def choices(main) -> set[str]:
        """`--help` の `{a,b,c}` から第 1 サブコマンドの集合を取る（機器には触れない）。"""
        out = io.StringIO()
        with contextlib.redirect_stdout(out), pytest.raises(SystemExit):
            main(["--help"])
        m = re.search(r"\{([a-z0-9_,-]+)\}", out.getvalue())
        return set(m.group(1).split(",")) if m else set()

    return choices(host_main), choices(gw_main)


def test_cli_first_subcommands_exist():
    host_cmds, gw_cmds = _cli_subcommands()
    assert host_cmds and gw_cmds, "CLI のサブコマンドの一覧が取れない（build_parser が無い？）"
    bad = []
    for doc in DOCS:
        for line in _text(doc).splitlines():
            line = line.strip().removeprefix("$ ")
            m = re.search(r"python -m fprobe_gw\s+([a-z][a-z0-9_-]*)", line)
            if m and m.group(1) not in gw_cmds:
                bad.append((doc.name, "fprobe_gw", m.group(1)))
            # fprobe の CLI: `fprobe [--オプション 値]... <サブコマンド>`（fprobe-mcp・fprobe-daemon は別のコマンド）
            m = re.search(r"(?:^|[\s`(])(?:python -m )?fprobe((?:\s+--[a-z]+(?:\s+[^\s-][^\s]*)?)*)\s+([a-z][a-z0-9_]*)\b", line)
            if m and line.lstrip("`").startswith(("fprobe ", ".venv/Scripts/python -m fprobe ", "python -m fprobe ")) \
                    and m.group(2) not in host_cmds:
                bad.append((doc.name, "fprobe", m.group(2)))
    assert not bad, f"無い CLI のサブコマンド: {bad}"


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_nothing_private_is_published(doc):
    text = _text(doc)
    found = ["<シリアル番号>" for s in _private_serials() if s in text] + [r.pattern for r in FORBIDDEN_RE if r.search(text)]
    assert not found, f"{doc.name}: 公開してはいけない値: {found}"
