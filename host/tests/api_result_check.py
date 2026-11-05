"""pytest の差し込み: 試験の中で呼ばれた API のメソッドの応答を、仕様（host/api/fprobe_api.json）の result で確かめる。

実装（Probe の impl のメソッド。session.* はデーモン）を包み、呼ばれた応答を daemon.jsonable を通した形にして
Spec.validate_result で照合する。合わなければその呼び出しで InvalidResult を投げ（その試験が落ちる）、試験が例外を
握りつぶしても、終わりの要約に必ず出して全体を失敗にする（実装と仕様のずれを黙って通さない）。

全部の試験を走らせたとき（-k・-m・ファイルの指定なし）は、どのメソッドも 1 回は照合されたことを確かめる。pytest-xdist で
並べたとき（-n）は、働く子の照合の数とずれを親が集め、親が最後に 1 回だけ数える。試験から
呼べないメソッドは UNCOVERED に理由を書く（呼べるようになったら消す: 照合されたのに残っていれば失敗）。

照合は 1 つの試験の中のメソッドごとに最初の CHECKS_PER_TEST 回（同じ試験で同じメソッドを何千回も呼ぶ性能の試験の
時間を、照合で変えないため。形は呼び方の道筋で変わるので、試験ごとには必ず照合する）。

環境変数 FPROBE_API_RESULT_DUMP=ファイル: 照合の代わりに、応答を 1 行 1 件の JSON（{"method", "test", "result"}）で
書き出す（新しいメソッドの result を書くときに、試験が実際に返す形を見るため。照合はしない）。
"""

import functools
import json
import os
from collections import Counter
from pathlib import Path

import pytest

from fprobe.api.daemon import Daemon, jsonable
from fprobe.api.spec import InvalidResult, load_spec
from fprobe.probe import Probe
from fprobe.probe_service import delegate
from api_result_uncovered import UNCOVERED

CHECKS_PER_TEST = 3
TESTS_DIR = Path(__file__).resolve().parent

SPEC = load_spec()
_checked: Counter = Counter()             # メソッド → 照合した回数（全体）
_per_test: Counter = Counter()            # (試験, メソッド) → 照合した回数
_violations: list[tuple[str, str]] = []   # (試験, 誤り)
_current = {"test": "(試験の外)"}
_DUMP = os.environ.get("FPROBE_API_RESULT_DUMP")


def check(name: str, result) -> None:
    """name の応答 result（実装が返したそのもの）を照合する。"""
    key = (_current["test"], name)
    if _per_test[key] >= CHECKS_PER_TEST:
        return
    _per_test[key] += 1
    method = SPEC.methods[name]
    if method.binary:
        result = getattr(result, "result", result)            # BinaryResult の JSON の部分（区画は形を持たない）
    if _DUMP:
        try:
            line = json.dumps({"method": name, "test": key[0], "result": jsonable(result)}, ensure_ascii=False)
        except TypeError as e:
            line = json.dumps({"method": name, "test": key[0], "error": str(e)}, ensure_ascii=False)
        with open(_DUMP, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        _checked[name] += 1
        return
    try:
        SPEC.validate_result(name, jsonable(result))
    except (InvalidResult, TypeError) as e:
        message = f"{e}" if isinstance(e, InvalidResult) else f"{name}: JSON にできない: {e}"
        _violations.append((_current["test"], message))
        raise InvalidResult(message) from None
    _checked[name] += 1


def _wrap(name: str, fn):
    @functools.wraps(fn)
    def checked(*args, **kwargs):
        result = fn(*args, **kwargs)
        check(name, result)
        return result
    checked.__api_result_check__ = True
    return checked


def _install() -> None:
    for m in SPEC.methods.values():
        if not m.impl:
            continue
        # 定義した mixin のクラスで包む（Probe そのものに足すと、mixin の名前の重なりを見る試験に引っかかる）。サービスに
        # 委ねた名前（fprobe.probe_service.delegate）は、そのサービスのクラスで包む
        owner = next(c for c in Probe.__mro__ if m.impl in vars(c))
        if isinstance(vars(owner)[m.impl], delegate):
            # サービスが混ぜ込む部品（mixin）から継いだ名前は、それを定義した部品のクラスで包む
            service = vars(owner)[m.impl].service
            owner = next(c for c in service.__mro__ if m.impl in vars(c))
        fn = vars(owner)[m.impl]
        if getattr(fn, "__api_result_check__", False):
            continue
        setattr(owner, m.impl, _wrap(m.name, fn))
    dispatch = Daemon._dispatch

    @functools.wraps(dispatch)
    async def checked_dispatch(self, conn, method, params, quiet=False):
        result = await dispatch(self, conn, method, params, quiet)
        if isinstance(method, str) and method.startswith("session.") and method in SPEC.methods:
            check(method, result)
        return result
    Daemon._dispatch = checked_dispatch


_install()


@pytest.fixture(autouse=True)
def _api_result_test_name(request):
    _current["test"] = request.node.nodeid
    yield
    _current["test"] = "(試験の外)"


def _full_run(session) -> bool:
    """全部の試験を選んだ走りか（このときだけ「全メソッドを照合した」を確かめる）。"""
    opt = session.config.option
    if getattr(opt, "keyword", "") or getattr(opt, "markexpr", "") or getattr(opt, "lf", False):
        return False
    if getattr(opt, "deselect", None) or session.testsfailed and getattr(opt, "maxfail", 0):
        return False
    args = [Path(a.split("::")[0]).resolve() for a in session.config.args] or [Path.cwd().resolve()]
    return all(a in (TESTS_DIR, TESTS_DIR.parent) for a in args)


def coverage_problems() -> list[str]:
    names = set(SPEC.methods)
    uncovered = names - set(_checked)
    problems = [f"{n}: 試験で 1 回も照合されない（呼ぶ試験を足すか、api_result_uncovered.UNCOVERED に理由を書く）"
                for n in sorted(uncovered - set(UNCOVERED))]
    problems += [f"{n}: 照合されたので api_result_uncovered.UNCOVERED から消す" for n in sorted(set(UNCOVERED) & set(_checked))]
    problems += [f"{n}: 仕様に無いメソッドが api_result_uncovered.UNCOVERED にある" for n in sorted(set(UNCOVERED) - names)]
    return problems


def _is_worker(config) -> bool:
    """pytest-xdist の働く子か（-n で並べたとき。子は試験の一部だけを走らせる）。"""
    return hasattr(config, "workerinput")


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node, error):
    """pytest-xdist の親: 働く子が終わったら、その子の照合の数とずれを足す（全部の子の分をまとめてから、親が 1 回だけ数える）。"""
    out = getattr(node, "workeroutput", {}).get("api_result_check")
    if out is None:
        return
    _checked.update(out["checked"])
    _violations.extend((test, message) for test, message in out["violations"])


def pytest_sessionfinish(session, exitstatus):
    if _is_worker(session.config):
        # 子は自分の分を親へ渡すだけ（「全メソッドを照合したか」は全部の子の分を合わせた親が数える。子ごとに数えると、
        # ほかの子が照合したメソッドを漏れと誤って出す）
        session.config.workeroutput["api_result_check"] = {"checked": dict(_checked),
                                                           "violations": [list(v) for v in _violations]}
        return
    problems = [f"{test}: {message}" for test, message in _violations]
    if _full_run(session) and not _DUMP and session.testscollected and not session.shouldstop:
        problems += coverage_problems()
    session.config._api_result_problems = problems
    if problems and session.exitstatus == 0:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    if _is_worker(config):
        return
    problems = getattr(config, "_api_result_problems", [])
    if problems:
        terminalreporter.section("API の応答と仕様のずれ（tests/api_result_check.py）", red=True)
        for line in problems:
            terminalreporter.write_line(line)
    else:
        terminalreporter.write_line(f"API の応答の照合: {len(_checked)} メソッド・{sum(_checked.values())} 回、ずれなし")
