"""試験の結果の行の読み取り（Unity・Zephyr ztest・defmt-test・TAP・利用者の正規表現）と JUnit XML の書き出し。
fprobe.run の Runner が出力をここ（TestLog）へ流し、終わり方の判定に結果を使う。ターゲットにもプローブにも触れない。"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

__all__ = ["TestCase", "TestLog", "junit_xml"]


# --- 試験の結果の行 -----------------------------------------------------------------------------

@dataclass
class TestCase:
    __test__ = False                  # pytest に試験の組と見なさせない
    name: str
    status: str                       # passed / failed / skipped / error / running / returned（defmt-test: 次の試験が
                                      # 始まった = 関数は戻った。合否の行が無いので passed とは書かない）
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
                running.status = "returned"                       # 次の試験が始まった = 前の試験の関数が戻った（合否の行は無い）
            self.cases.append(TestCase(m[3], "running"))
        elif _DEFMT_PASSED.match(line):
            for case in self.cases:                               # defmt-test のまとめの行: 全部が通った
                if case.status in ("running", "returned"):
                    case.status = "passed"
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
        # returned（defmt-test の戻った試験）は JUnit に当たる状態が無いので印を付けない（result.json に元の状態がある）
        tag = {"failed": "failure", "error": "error", "skipped": "skipped", "running": "error"}.get(c.status)
        if tag:
            ET.SubElement(e, tag, message=_clean(c.message or c.status))
    ET.SubElement(s, "system-out").text = _clean(output)
    return ET.tostring(root, encoding="unicode", xml_declaration=True)
