"""ファームを書き込んで走らせ、出力を流し、終わり方で合否を決める（CI 用。`python -m fprobe run firmware.elf`）。

    書き込み（RAM か Flash。auto は ELF の番地で選ぶ）→ 変数の設定（--set）→ 実行 →
    出力（RTT のチャンネル 0・セミホスティング・SWO の ITM のポート）を標準出力へ流し、試験の結果の行を読む →
    終わり方で判定:
      セミホスティングの SYS_EXIT        終了コードをそのまま（0 でも試験の行に失敗があれば 1）
      故障（HardFault 等・LOCKUP）       125。故障の解析・呼び出し履歴・スナップショット・コアファイルを残す
      BKPT 命令で止まった（probe-rs 流） 試験の行に失敗があれば 1、無ければ 0
      試験の結果のまとめの行             その合否（ターゲットは走らせたまま）
      ほかの理由で止まった               125（スナップショットを残す）
      時間切れ                           124（止めて、呼び出し履歴とスナップショットを残す）
      プローブ・書き込みの失敗           3

試験の結果の行は Unity（`file.c:12:test_x:PASS`）、Zephyr ztest（` PASS - test_x in 0.001 seconds`）、
defmt-test（文字にした後の "(1/3) running `x`..."・`all tests passed!`）、TAP（`ok 1 - x`）と、利用者の正規表現
（--pass / --fail）を読む。結果は out_dir の result.json・output.log と、任意で JUnit XML。

Runner は Probe（fprobe.mcp_server）の公開の道具だけを使う（偽の Probe で試せる）。
"""

from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .rtt import rtt_address_from_elf
from .symbols import Symbols

__all__ = ["EXIT_ABNORMAL", "EXIT_ERROR", "EXIT_FAILED", "EXIT_OK", "EXIT_TIMEOUT", "RunOptions", "RunResult",
           "Runner", "TestCase", "TestLog", "choose_load", "junit_xml", "run_firmware"]

EXIT_OK = 0
EXIT_FAILED = 1            # 試験の失敗・ターゲットの終了コードが 0 以外（下位 8 ビットが 0 のとき）
EXIT_ERROR = 3             # プローブ・接続・書き込みの失敗（ターゲットを走らせる前）
EXIT_TIMEOUT = 124         # 時間切れ（timeout(1) と同じ）
EXIT_ABNORMAL = 125        # 故障・LOCKUP・思いがけない停止
SRAM_REGION = (0x20000000, 0x40000000)    # Cortex-M のメモリマップの SRAM の領域（メモリマップが分からないとき）
SEMIHOSTING_BKPT = 0xBEAB                  # BKPT 0xAB


# --- 試験の結果の行 -----------------------------------------------------------------------------

@dataclass
class TestCase:
    __test__ = False                  # pytest に試験の組と見なさせない
    name: str
    status: str                       # passed / failed / skipped / error / running
    message: str = ""
    suite: str = ""
    file: str | None = None
    line: int | None = None
    time_s: float | None = None


_UNITY = re.compile(r"^(?P<file>[^:\s][^:]*):(?P<line>\d+):(?P<name>[^:]+):(?P<status>PASS|FAIL|IGNORE)(?::\s?(?P<msg>.*))?$")
_UNITY_SUMMARY = re.compile(r"^(\d+) Tests (\d+) Failures (\d+) Ignored\s*$")
_ZTEST_SUITE = re.compile(r"^Running TESTSUITE (\S+)")
_ZTEST = re.compile(r"^\s*(PASS|FAIL|SKIP) - (\S+) in ([\d.]+) seconds")
_ZTEST_END = re.compile(r"PROJECT EXECUTION (SUCCESSFUL|FAILED)")
_DEFMT_RUNNING = re.compile(r"^\((\d+)/(\d+)\) running `([^`]+)`\.\.\.")
_DEFMT_PASSED = re.compile(r"^all tests passed!")
_TAP_PLAN = re.compile(r"^1\.\.(\d+)")
_TAP = re.compile(r"^(not )?ok\b\s*(\d+)?\s*(?:-\s*)?(.*?)\s*(?:#\s*(SKIP|TODO)\b\s*(.*))?$", re.IGNORECASE)
_STATUS = {"PASS": "passed", "FAIL": "failed", "IGNORE": "skipped", "SKIP": "skipped"}


class TestLog:
    """出力を行に組み立て（出どころごと）、試験の結果の行を読む。finished と verdict（passed / failed）は
    まとめの行（Unity の OK / FAIL、ztest の PROJECT EXECUTION、defmt-test の all tests passed!、TAP の計画の数、
    利用者の正規表現）で決まる。"""

    __test__ = False

    def __init__(self, pass_pattern: str | None = None, fail_pattern: str | None = None):
        self.cases: list[TestCase] = []
        self.finished = False
        self.verdict: str | None = None
        self.failure = ""                                         # 失敗の正規表現に当たった行
        self._pass = re.compile(pass_pattern) if pass_pattern else None
        self._fail = re.compile(fail_pattern) if fail_pattern else None
        self._partial: dict[str, str] = {}
        self._unity_summary: tuple[int, int, int] | None = None
        self._suite = ""
        self._tap_plan: int | None = None
        self._tap_count = 0

    def feed(self, source: str, text: str) -> None:
        buffer = self._partial.get(source, "") + text
        *lines, self._partial[source] = buffer.split("\n")
        for line in lines:
            self._line(line.rstrip("\r"))

    def flush(self) -> None:
        """出し切った: 改行の来ていない最後の行も読む。"""
        for source, rest in list(self._partial.items()):
            if rest:
                self._line(rest.rstrip("\r"))
            self._partial[source] = ""

    def _finish(self, verdict: str) -> None:
        if not self.finished:
            self.finished, self.verdict = True, verdict

    def _running(self) -> TestCase | None:
        return next((c for c in reversed(self.cases) if c.status == "running"), None)

    def _line(self, line: str) -> None:
        if self._fail is not None and self._fail.search(line):
            self.failure = line
            self._finish("failed")
            return
        if self._pass is not None and self._pass.search(line):
            self._finish("failed" if self.failures() else "passed")
            return
        if self._unity_summary is not None:                       # まとめの次の行が OK / FAIL
            if line.strip() in ("OK", "FAIL"):
                self._finish("passed" if line.strip() == "OK" else "failed")
                return
        if m := _UNITY.match(line):
            self.cases.append(TestCase(m["name"], _STATUS[m["status"]], (m["msg"] or "").strip(), Path(m["file"]).stem,
                                       m["file"], int(m["line"])))
        elif m := _UNITY_SUMMARY.match(line):
            self._unity_summary = (int(m[1]), int(m[2]), int(m[3]))
        elif m := _ZTEST_SUITE.match(line):
            self._suite = m[1]
        elif m := _ZTEST.match(line):
            self.cases.append(TestCase(m[2], _STATUS[m[1]], suite=self._suite, time_s=float(m[3])))
        elif m := _ZTEST_END.search(line):
            self._finish("passed" if m[1] == "SUCCESSFUL" else "failed")
        elif m := _DEFMT_RUNNING.match(line):
            if (running := self._running()) is not None:
                running.status = "passed"                         # 次の試験が始まった = 前の試験は通った
            self.cases.append(TestCase(m[3], "running"))
        elif _DEFMT_PASSED.match(line):
            if (running := self._running()) is not None:
                running.status = "passed"
            self._finish("failed" if self.failures() else "passed")
        elif m := _TAP_PLAN.match(line):
            self._tap_plan = int(m[1])
            self._tap_done()
        elif m := _TAP.match(line):
            directive = (m[4] or "").upper()
            status = "skipped" if directive == "SKIP" else "failed" if m[1] and directive != "TODO" else "passed"
            self.cases.append(TestCase(m[3] or f"test {m[2]}", status, (m[5] or "").strip()))
            self._tap_count += 1
            self._tap_done()

    def _tap_done(self) -> None:
        if self._tap_plan is not None and self._tap_count >= self._tap_plan:
            self._finish("failed" if self.failures() else "passed")

    def failures(self) -> list[TestCase]:
        return [c for c in self.cases if c.status in ("failed", "error")]

    def abort(self, message: str) -> None:
        """実行が異常に終わった: 走っていた試験を error にする。"""
        if (running := self._running()) is not None:
            running.status, running.message = "error", message


# --- JUnit XML -------------------------------------------------------------------------------------

_CONTROL = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _clean(text: str) -> str:
    """XML 1.0 に入れられない制御文字と、端末の色の指定を除く。"""
    return _CONTROL.sub("", text)


def junit_xml(suite: str, cases: list[TestCase], output: str = "", duration_s: float = 0.0) -> str:
    counts = {k: sum(c.status == k for c in cases) for k in ("failed", "error", "skipped")}
    root = ET.Element("testsuites")
    s = ET.SubElement(root, "testsuite", name=_clean(suite), tests=str(len(cases)), failures=str(counts["failed"]),
                      errors=str(counts["error"]), skipped=str(counts["skipped"]), time=f"{duration_s:.3f}")
    for c in cases:
        attrs = {"name": _clean(c.name), "classname": _clean(c.suite or suite)}
        if c.time_s is not None:
            attrs["time"] = f"{c.time_s:.3f}"
        if c.file:
            attrs["file"] = _clean(c.file)
        if c.line is not None:
            attrs["line"] = str(c.line)
        e = ET.SubElement(s, "testcase", attrs)
        tag = {"failed": "failure", "error": "error", "skipped": "skipped", "running": "error"}.get(c.status)
        if tag:
            ET.SubElement(e, tag, message=_clean(c.message or c.status))
    ET.SubElement(s, "system-out").text = _clean(output)
    return ET.tostring(root, encoding="unicode", xml_declaration=True)


# --- 実行 -------------------------------------------------------------------------------------------

@dataclass
class RunOptions:
    elf: Path
    out_dir: Path = Path("fprobe-run")
    target: str = "cortex_m"
    frequency_hz: int = 4_000_000
    connect_mode: str = "halt"
    connect: bool = True                     # False: 接続済みの Probe を使う
    load: str = "auto"                       # auto / ram / flash / none（書き込まない）
    reset: bool = True                       # flash / none: リセットして最初から（ram は読み込みで必ず最初から）
    set_values: dict[str, str] = field(default_factory=dict)    # 走らせる前に書く変数（C の式 → 値）
    set_at: str | None = None                # set_values を書く場所（起動のコードの後。省略時は main があれば main）
    catch_faults: bool = True                # 故障の入口でコアを止める（ベクタキャッチ）: 故障のハンドラが回り続けても分かる
    timeout_s: float = 60.0
    rtt: bool | None = None                  # None: ELF に _SEGGER_RTT があれば
    rtt_address: int | None = None
    rtt_retry_s: float = 0.1                 # RTT の制御ブロックが見つかるまで付け直す間隔
    swo_baud: int | None = None
    swo_port: int = 0
    pass_pattern: str | None = None
    fail_pattern: str | None = None
    stop_on_summary: bool = True             # 試験のまとめの行で終える（ターゲットは走らせたまま）
    junit: Path | None = None
    snapshot: bool = True                    # 故障・時間切れ・思いがけない停止でスナップショットを残す
    poll_s: float = 0.02


@dataclass
class RunResult:
    outcome: str                             # exit / fault / bkpt / tests / halted / timeout / error
    exit_code: int
    reason: str
    duration_s: float
    target_exit_code: int | None = None
    output: str = ""
    cases: list[TestCase] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    stop: dict | None = None
    elf: str = ""

    def junit_cases(self) -> list[TestCase]:
        """JUnit の試験: 読んだ試験の結果。無ければ（または失敗の試験が無いのに異常に終わったら）実行そのものを 1 件。"""
        cases = list(self.cases)
        failed = any(c.status in ("failed", "error") for c in cases)
        if not cases or (self.exit_code != EXIT_OK and not failed):
            status = "passed" if self.exit_code == EXIT_OK else "error" if self.outcome in (
                "fault", "timeout", "halted", "error") else "failed"
            cases.append(TestCase(Path(self.elf).stem or "run", status, "" if status == "passed" else self.reason,
                                  suite="fprobe.run"))
        return cases

    def as_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if k != "output"}


def choose_load(elf: Path, ram_ranges: list[tuple[int, int]]) -> str:
    """ELF の読み込む区画がすべて RAM（メモリマップ。無ければ Cortex-M の SRAM の領域）なら ram、そうでなければ flash。"""
    ranges = ram_ranges or [(SRAM_REGION[0], SRAM_REGION[1] - SRAM_REGION[0])]
    segments = Symbols(elf).segments
    inside = all(any(start <= s.address and s.address + len(s.data) <= start + length for start, length in ranges)
                 for s in segments)
    return "ram" if segments and inside else "flash"


def _value(text: str) -> int | float | str:
    for convert in (lambda t: int(t, 0), float):
        try:
            return convert(text)
        except ValueError:
            pass
    return text                                                    # 列挙子の名前等


class Runner:
    def __init__(self, probe, options: RunOptions, echo: Callable[[str], None] | None = None):
        self.probe = probe
        self.o = options
        self.echo = echo or (lambda text: None)
        self.log = TestLog(options.pass_pattern, options.fail_pattern)
        self.output: list[str] = []
        self.warnings: list[str] = []
        self._rtt_wanted = False
        self._rtt_on = False
        self._rtt_next = 0.0

    # --- 全体 ----------------------------------------------------------------------------------

    def run(self) -> RunResult:
        started = time.monotonic()
        self.o.out_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._prepare()
        except Exception as e:
            result = RunResult("error", EXIT_ERROR, f"{type(e).__name__}: {e}", time.monotonic() - started)
        else:
            result = self._execute(started)
        result.duration_s = round(time.monotonic() - started, 3)
        result.output, result.cases, result.elf = "".join(self.output), self.log.cases, str(self.o.elf)
        result.warnings = self.warnings + result.warnings
        self._write(result)
        return result

    def _prepare(self) -> None:
        p, o = self.probe, self.o
        if o.connect:
            p.target_connect(o.target, frequency_hz=o.frequency_hz, connect_mode=o.connect_mode)
        load = o.load if o.load != "auto" else choose_load(o.elf, self._ram_ranges())
        if load == "ram":
            p.program_ram(str(o.elf))
        elif load == "flash":
            p.flash(str(o.elf))
            p.target_reset(halt=True)                         # 書き込み後は走っている: 最初から見るため止める
        elif load == "none":
            p.elf_load(str(o.elf))
            if o.reset:
                p.target_reset(halt=True)
        else:
            raise ValueError(f"load は auto / ram / flash / none（{o.load!r}）")
        if o.catch_faults:
            try:
                p.vector_catch(["hard_fault", "mem_manage", "bus_fault", "state_error", "check_error",
                                "no_coprocessor", "interrupt_error"])
            except Exception as e:                                # 偽の Probe・ベクタキャッチの無いコア
                self.warnings.append(f"故障のベクタキャッチを設定できなかった: {e}")
        if o.set_values:
            self._write_values()
        if o.swo_baud:
            p.swo_start(o.swo_baud)
        self._rtt_wanted = o.rtt if o.rtt is not None else self._elf_has_rtt()

    def _write_values(self) -> None:
        """--set の値を書く。起動のコードは .bss を 0 にし .data を写すので、そこの変数は起動の後に書かないと消える。
        set_at（省略時は main があれば main）まで走らせてから書く。どちらも無ければ今書いて注意を残す。"""
        p, o = self.probe, self.o
        at = o.set_at
        if at is None and self._has_symbol("main"):
            at = "main"
        if at is not None:
            p.breakpoint_set(at)
            try:
                stop = p.run_until_halt(timeout_s=min(o.timeout_s, 10.0))
            finally:
                p.breakpoint_clear(at)
            if stop.get("reason") != "breakpoint":
                raise RuntimeError(f"--set の場所 {at} に着かなかった（{stop.get('reason')} at {stop.get('where')}）")
        else:
            self.warnings.append("--set を起動のコードの前に書いた: .bss・.data の変数は起動のコードが上書きする"
                                 "（--set-at で起動の後の場所を指定する）")
        for expression, value in o.set_values.items():
            p.write_expression(expression, _value(value))

    def _has_symbol(self, name: str) -> bool:
        try:
            Symbols(self.o.elf).symbol(name)
            return True
        except KeyError:
            return False

    def _ram_ranges(self) -> list[tuple[int, int]]:
        try:
            memory_map = self.probe.target.memory_map or []
        except Exception:                                         # 偽の Probe・メモリマップの無いターゲット
            return []
        return [(r.start, r.length) for r in memory_map if getattr(r, "is_ram", False)]

    def _elf_has_rtt(self) -> bool:
        try:
            rtt_address_from_elf(self.o.elf)
            return True
        except KeyError:
            return False

    # --- 走らせて待つ ----------------------------------------------------------------------------

    def _execute(self, started: float) -> RunResult:
        p, o = self.probe, self.o
        deadline = started + o.timeout_s
        p.target_resume()
        while True:
            self._pump()
            if self.log.finished and o.stop_on_summary:
                return self._by_summary()
            status = p.target_status()
            if status["state"] in ("HALTED", "LOCKUP") and not self._semihosting_pending(status):
                self._drain()
                return self._stopped(status)
            if time.monotonic() >= deadline:
                status = p.target_halt()
                self._drain()
                self.log.abort(f"時間切れ（{o.timeout_s:g} 秒）")
                result = RunResult("timeout", EXIT_TIMEOUT, f"{o.timeout_s:g} 秒で終わらなかった", 0, stop=status)
                self._keep_state(result, fault=False)
                return result
            if not self._rtt_on:
                time.sleep(o.poll_s)

    def _pump(self) -> None:
        """出力を読み、流し、試験の行を読む。RTT は付くまで（ファームが制御ブロックを作るまで）付け直す。"""
        p, o = self.probe, self.o
        if self._rtt_wanted and not self._rtt_on and time.monotonic() >= self._rtt_next:
            try:
                p.rtt_start(address=o.rtt_address)
                self._rtt_on = True
            except Exception:
                self._rtt_next = time.monotonic() + o.rtt_retry_s
        self._take("semihosting", p.rtt_read(channel="semihosting")["data"])
        if self._rtt_on:
            self._take("rtt", p.rtt_read(channel=0, timeout_s=o.poll_s)["data"])
        if o.swo_baud:
            lines = p.swo_text(port=o.swo_port, timeout_s=0.0)["lines"]
            self._take("swo", "".join(line + "\n" for line in lines))

    def _take(self, source: str, text: str) -> None:
        if text:
            self.output.append(text)
            self.echo(text)
            self.log.feed(source, text)

    def _drain(self) -> None:
        """止まった後の残り（RTT は背景の読みが届くまで少し待つ）。"""
        for _ in range(3):
            before = len(self.output)
            self._pump()
            if len(self.output) == before:
                break
        self.log.flush()

    # --- 判定 ----------------------------------------------------------------------------------

    def _by_summary(self) -> RunResult:
        failed = self.log.verdict == "failed"
        reason = self.log.failure or self._failures_text() if failed else "試験のまとめの行: 合格"
        return RunResult("tests", EXIT_FAILED if failed else EXIT_OK, reason or "試験のまとめの行: 不合格", 0)

    def _failures_text(self) -> str:
        names = [c.name for c in self.log.failures()]
        return f"失敗した試験: {', '.join(names)}" if names else ""

    def _stopped(self, status: dict) -> RunResult:
        p = self.probe
        if status["state"] == "LOCKUP":
            status = p.target_halt()
        exited = p.semihosting_status().get("exit")
        if exited is not None:
            code = exited["code"]
            if code:
                return RunResult("exit", code & 0xFF or EXIT_FAILED, f"ターゲットが終了コード {code} で終わった", 0,
                                 target_exit_code=code, stop=status)
            if self.log.failures() or self.log.verdict == "failed":
                return RunResult("exit", EXIT_FAILED, self._failures_text() or self.log.failure or "試験の失敗", 0,
                                 target_exit_code=0, stop=status)
            return RunResult("exit", EXIT_OK, "ターゲットが終了コード 0 で終わった", 0, target_exit_code=0, stop=status)
        report = self._fault()
        if report.get("faulted") or status["state"] == "LOCKUP":
            causes = ", ".join(c["bit"] for c in report.get("causes", []))
            name = (report.get("exception") or {}).get("name") or "LOCKUP"
            at = ((report.get("frame") or {}).get("pc") or {}).get("where") or status.get("where", "")
            reason = f"{name}（{causes}）at {at}" if causes else f"{name} at {at}"
            self.log.abort(reason)
            result = RunResult("fault", EXIT_ABNORMAL, reason, 0, stop=status)
            self._keep_state(result, fault=report)
            return result
        if self._at_bkpt(status):
            if self.log.failures() or self.log.verdict == "failed":
                return RunResult("bkpt", EXIT_FAILED, self._failures_text() or "試験の失敗", 0, stop=status)
            return RunResult("bkpt", EXIT_OK, "BKPT 命令で止まった（終わり）", 0, stop=status)
        reason = f"思いがけない停止 at {status.get('where', hex(status.get('pc', 0)))}"
        self.log.abort(reason)
        result = RunResult("halted", EXIT_ABNORMAL, reason, 0, stop=status)
        self._keep_state(result, fault=False)
        return result

    def _semihosting_pending(self, status: dict) -> bool:
        """止まったのはセミホスティングの要求（BKPT 0xAB）で、背景の見張りがまだ処理していない（すぐ再開する）。"""
        if status["state"] != "HALTED":
            return False
        sh = self.probe.semihosting_status()
        return sh.get("exit") is None and bool(sh.get("watching")) and self._instruction(status) == SEMIHOSTING_BKPT

    def _instruction(self, status: dict) -> int | None:
        try:
            return int.from_bytes(bytes.fromhex(self.probe.read_memory(status["pc"], 2)), "little")
        except Exception:
            return None

    def _fault(self) -> dict:
        try:
            return self.probe.target_fault()
        except Exception as e:
            self.warnings.append(f"故障の解析ができない: {type(e).__name__}: {e}")
            return {"faulted": False}

    def _at_bkpt(self, status: dict) -> bool:
        half = self._instruction(status)
        return half is not None and half & 0xFF00 == 0xBE00

    # --- 残すもの --------------------------------------------------------------------------------

    def _keep_state(self, result: RunResult, fault: dict | bool) -> None:
        """故障・時間切れ・思いがけない停止: 故障の解析・呼び出し履歴・スナップショット・コアファイルを out_dir に残す。"""
        p, out = self.probe, self.o.out_dir
        steps: list[tuple[str, Callable[[], str]]] = []
        if fault:
            steps.append(("fault", lambda: self._json(out / "fault.json", fault)))
        steps.append(("backtrace", lambda: self._json(out / "backtrace.json", p.backtrace(variables=True))))
        if self.o.snapshot:
            steps.append(("snapshot", lambda: p.snapshot_save(path=str(out / "snapshot.fsnap"),
                                                              note=f"fprobe run: {result.reason}")["path"]))
            steps.append(("core", lambda: p.snapshot_export_core(str(out / "snapshot.fsnap"),
                                                                 str(out / "snapshot.core"))["path"]))
        for name, step in steps:
            try:
                result.artifacts[name] = str(step())
            except Exception as e:
                result.warnings.append(f"{name} を残せなかった: {type(e).__name__}: {e}")

    @staticmethod
    def _json(path: Path, value) -> str:
        path.write_text(json.dumps(value, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        return str(path)

    def _write(self, result: RunResult) -> None:
        out = self.o.out_dir
        (out / "output.log").write_text(result.output, encoding="utf-8", newline="")
        self._json(out / "result.json", result.as_dict())
        if self.o.junit is not None:
            self.o.junit.parent.mkdir(parents=True, exist_ok=True)
            self.o.junit.write_text(junit_xml(self.o.elf.stem, result.junit_cases(), result.output, result.duration_s),
                                    encoding="utf-8")


def run_firmware(options: RunOptions, probe_args: dict, echo: Callable[[str], None] | None = None) -> RunResult:
    """Probe を開き（probe_args: serial / port / eth）、Runner で走らせて閉じる。"""
    from .mcp_server import Probe
    probe = Probe(**probe_args)
    try:
        return Runner(probe, options, echo).run()
    finally:
        try:
            probe.close()
        except Exception:
            pass
