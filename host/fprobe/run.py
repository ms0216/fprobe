"""ファームを書き込んで走らせ、出力を流し、終わり方で合否を決める（CI 用。`python -m fprobe run firmware.elf`）。

    書き込み（RAM か Flash。auto は ELF の番地で選ぶ）→ 変数の設定（--set）→ 実行 →
    出力（RTT のチャンネル 0・セミホスティング・SWO の ITM のポート）を標準出力へ流し、試験の結果の行を読む →
    終わり方で判定:
      セミホスティングの SYS_EXIT        終了コードをそのまま（0 でも試験の行に失敗があれば 1）
      故障（HardFault 等・LOCKUP）       125。故障の解析・呼び出し履歴・スナップショット・コアファイルを残す
      終わりの BKPT（--end-bkpt の即値）  試験の行に失敗があれば 1、無ければ 0（probe-rs 流の約束。即値を渡したときだけ）
      試験の結果のまとめの行             その合否（ターゲットは走らせたまま）
      ほかの理由で止まった               125（スナップショットを残す。ほかの即値の BKPT・即値を渡さない BKPT も）
      時間切れ                           124（止めて、呼び出し履歴とスナップショットを残す）
      プローブ・書き込みの失敗           3

試験の結果の行は Unity（`file.c:12:test_x:PASS`）、Zephyr ztest（` PASS - test_x in 0.001 seconds`）、
defmt-test（文字にした後の "(1/3) running `x`..."・`all tests passed!`）、TAP（`ok 1 - x`）と、利用者の正規表現
（--pass / --fail）を読む。結果は out_dir の result.json・output.log と、任意で JUnit XML。

Runner は Probe（fprobe.mcp_server）の公開の道具だけを使う（偽の Probe で試せる）。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .rtt import rtt_address_from_elf
from .run_testlog import TestCase, TestLog, junit_xml
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


# --- 実行 -------------------------------------------------------------------------------------------

@dataclass
class RunOptions:
    elf: Path
    out_dir: Path = Path("fprobe-run")
    # つなぐ（connect=True）ときは使う側が明示する（既定を持たない。target.connect と同じ。CLAUDE.md 2026-11-04）
    target: str | None = None
    frequency_hz: int | None = None
    connect_mode: str = "halt"
    connect: bool = True                     # False: 接続済みの Probe を使う
    load: str = "auto"                       # auto / ram / flash / none（書き込まない）
    reset: bool = True                       # flash / none: リセットして最初から（ram は読み込みで必ず最初から）
    set_values: dict[str, str] = field(default_factory=dict)    # 走らせる前に書く変数（C の式 → 値）
    set_at: str | None = None                # set_values を書く場所（起動のコードの後。省略時は今すぐ書く: main を探さない）
    vector_table: int | None = None          # load ram の起動に使うベクタ表の番地（省略時は ELF の入口 e_entry から）
    set_timeout_s: float | None = None       # set_at まで待つ秒（省略時は timeout_s）
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
    end_bkpt: int | None = None              # 終わりの合図とする BKPT の即値（ファームとの約束。None: BKPT で終わりとしない）


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
    bkpt: int | None = None                  # 止まった所の命令が BKPT ならその即値（測った事実）
    settings: dict = field(default_factory=dict)   # 使った設定（既定のときも。target・frequency_hz・timeout_s 等）

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
        o = self.o
        result.settings = {"target": o.target, "frequency_hz": o.frequency_hz, "connect_mode": o.connect_mode,
                           "load": o.load, "reset": o.reset, "timeout_s": o.timeout_s, "end_bkpt": o.end_bkpt,
                           "rtt": o.rtt, "swo_baud": o.swo_baud, "swo_port": o.swo_port,
                           "stop_on_summary": o.stop_on_summary, "catch_faults": o.catch_faults}
        result.warnings = self.warnings + result.warnings
        self._write(result)
        return result

    def _prepare(self) -> None:
        p, o = self.probe, self.o
        if o.connect:
            if o.target is None or o.frequency_hz is None:
                raise ValueError("つなぐには target と frequency_hz を明示する（既定は持たない。例 cortex_m・4000000）")
            p.target_connect(o.target, frequency_hz=o.frequency_hz, connect_mode=o.connect_mode)
        load = o.load if o.load != "auto" else choose_load(o.elf, self._ram_ranges())
        if load == "ram":
            p.program_ram(str(o.elf), vector_table=o.vector_table)
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
        set_at（使う側が明示する場所。名前の慣習で main 等を探さない）まで走らせてから書く。待つのは set_timeout_s
        （省略時は timeout_s）秒。set_at が無ければ今書いて注意を残す。"""
        p, o = self.probe, self.o
        at = o.set_at
        if at is not None:
            wait_s = o.timeout_s if o.set_timeout_s is None else o.set_timeout_s
            p.breakpoint_set(at)
            try:
                stop = p.run_until_halt(timeout_s=wait_s)
            finally:
                p.breakpoint_clear(at)
            if stop.get("reason") != "breakpoint":
                raise RuntimeError(f"--set の場所 {at} に {wait_s:g} 秒で着かなかった（{stop.get('reason')} at "
                                   f"{stop.get('where')}）")
        else:
            self.warnings.append("--set を起動のコードの前に書いた: .bss・.data の変数は起動のコードが上書きする"
                                 "（--set-at で起動の後の場所を指定する）")
        for expression, value in o.set_values.items():
            p.write_expression(expression, _value(value))

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
        bkpt = self._bkpt_immediate(status)
        if bkpt is not None and bkpt == self.o.end_bkpt:
            if self.log.failures() or self.log.verdict == "failed":
                return RunResult("bkpt", EXIT_FAILED, self._failures_text() or "試験の失敗", 0, stop=status, bkpt=bkpt)
            return RunResult("bkpt", EXIT_OK, f"終わりの BKPT 0x{bkpt:02X} で止まった", 0, stop=status, bkpt=bkpt)
        what = "思いがけない停止" if bkpt is None else f"BKPT 0x{bkpt:02X} で止まった（終わりの合図として渡した即値ではない）"
        reason = f"{what} at {status.get('where', hex(status.get('pc', 0)))}"
        self.log.abort(reason)
        result = RunResult("halted", EXIT_ABNORMAL, reason, 0, stop=status, bkpt=bkpt)
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

    def _bkpt_immediate(self, status: dict) -> int | None:
        """止まった所の命令が BKPT（0xBExx）ならその即値。"""
        half = self._instruction(status)
        return half & 0xFF if half is not None and half & 0xFF00 == 0xBE00 else None

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
