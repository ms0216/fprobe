"""Probe の道具: プロファイル・カバレッジの集計と書き出し（U18）。時間の区間に限る集計、関数のグループ、CPU 使用率の
推移、gmon.out・callgrind・lcov・gcov・dot への書き出し（PC サンプリングとテストポイントの両方）。

Probe（fprobe.probe。共通の部品の型は fprobe.probe_host.ProbeHost）に混ぜて使う mixin。ProfileTools（いまのプロファイル `_current_profile`・ベクタの名前
`_vector_names`）、InspectTools（ELF の逆アセンブラ `_disassembler`）、TestpointTools（いまの受け取り `_tp_current`）、
StackTools（静的な呼び出しグラフ `_call_graph`）を使う。機器に触るのは必ず _lock の中。

時刻: PC サンプルの時刻は方法 pcsr ならタイムベース（記録・`recording_events` と同じ時間軸。t0 / t1 で指定できる）、
方法 itm ならローカルのタイムスタンプ（コアのクロック数。timestamps が要る）。t0_s / t1_s は最初のサンプルからの秒。
テストポイントの t0_s / t1_s は `testpoint_calls` と同じ（最初の印からの秒）。

仕様: docs/superpowers/specs/2026-10-18-u18-u26-profile-export-stack-design.md
"""

from pathlib import Path

from .debug import Debugger
from .export.profile_formats import (callgrind_from_profile, callgrind_from_tree, coverage_from_profile,
                                     coverage_from_testpoint, dot_from_profile, dot_from_tree, dwarf_declarations,
                                     gmon_bytes, render_lcov, tree_edges, write_gcov)
from .profile import PcSamples, Profiler, coverage
from .profile.aggregate import Grouping, call_stats, group_profile, group_rows, timeline, window
from .symbols import is_network_path

__all__ = ["PROFILE_FORMATS", "ProfileExportTools", "TESTPOINT_FORMATS"]

PROFILE_FORMATS = ("gmon", "callgrind", "lcov", "gcov", "dot")
TESTPOINT_FORMATS = ("callgrind", "lcov", "gcov", "dot", "gmon")
_PLAIN_BY = ("function", "line", "instruction", "exception")


def _out_path(path: str) -> Path:
    if is_network_path(path):
        raise ValueError(f"外の共有には書かない: {path}")
    return Path(path)


def _check_format(fmt: str, allowed: tuple[str, ...]) -> None:
    if fmt not in allowed:
        raise ValueError(f"format は {', '.join(allowed)} のどれか: {fmt!r}")


class ProfileExportTools:
    """プロファイル・カバレッジの集計と書き出し（Probe の mixin）。"""

    # --- PC サンプリング: 区間 -------------------------------------------------------------------

    def _sample_clock(self, result) -> float | None:
        """サンプルの時刻の 1 秒あたりの数（itm: コアのクロック、pcsr: タイムベース）。"""
        if result.run.method == "itm":
            return result.run.core_clock_hz
        with self._lock:
            return self.session.device.capabilities().timebase_hz

    def _windowed(self, t0_s: float | None, t1_s: float | None, t0: int | None, t1: int | None):
        """(結果, 区間で切ったサンプル, 区間の説明, 時刻の 1 秒あたりの数)。"""
        result = self._current_profile()
        samples = result.samples
        hz = self._sample_clock(result)
        if (t0 is not None or t1 is not None) and result.run.method != "pcsr":
            raise ValueError("t0 / t1（タイムベース）は方法 pcsr だけ。itm は t0_s / t1_s（最初のサンプルからの秒）")
        if (t0 is not None or t1 is not None) and (t0_s is not None or t1_s is not None):
            raise ValueError("t0 / t1 と t0_s / t1_s は一緒に使わない")
        lo, hi = t0, t1
        origin = next((s.t for s in samples.samples if s.t is not None), None)
        if t0_s is not None or t1_s is not None:
            if origin is None or hz is None:
                raise ValueError("サンプルに時刻が無い（itm は profile_start の timestamps）: 区間で切れない")
            lo = None if t0_s is None else origin + t0_s * hz
            hi = None if t1_s is None else origin + t1_s * hz
        chosen = window(samples, lo, hi)
        info = {"samples": len(chosen), "t0": lo if lo is not None else origin, "t1": hi,
                "t0_s": t0_s if t0_s is not None else (0.0 if lo is None or origin is None or not hz
                                                        else (lo - origin) / hz),
                "t1_s": t1_s if t1_s is not None else (None if hi is None or origin is None or not hz
                                                        else (hi - origin) / hz),
                "time_unit": "timebase" if result.run.method == "pcsr" else "core_cycles"}
        return result, chosen, info, hz

    def _profiler(self) -> Profiler:
        with self._lock:
            disassembler, _ = self._disassembler(from_elf=True)
        return Profiler(self._symbols, disassembler)

    def profile_aggregate(self, by: str = "function", groups: dict[str, list[str]] | None = None,
                          t0_s: float | None = None, t1_s: float | None = None, t0: int | None = None,
                          t1: int | None = None, top: int = 20, function: str | None = None,
                          separator: str | None = None) -> dict:
        """プロファイルを区間（t0_s〜t1_s 秒、pcsr はタイムベースの t0〜t1 も）に限って集計する。by: function / line /
        instruction / exception（profile_result と同じ表）、file / directory / prefix（関数名の separator の最初の
        出現より前。separator は使う側が明示する）/ groups（{名前: [関数名の glob か "file:" + ファイルの glob]}）の
        グループ。rows_total（グループは groups_total）= top で切る前の行の数。"""
        result, chosen, info, _ = self._windowed(t0_s, t1_s, t0, t1)
        prof = self._profiler().profile(chosen, names=self._vector_names(result.run.vector_table))
        if by in _PLAIN_BY:
            table = prof.table(by, function)
            return {"by": by, "window": info, "total": prof.total, "active": prof.active,
                    "categories": prof.category_table(), "rows": table[:top], "rows_total": len(table),
                    "notes": prof.notes()}
        out = group_profile(prof, Grouping(by, groups, separator), top=top)
        out["window"] = info
        return out

    def profile_timeline(self, bucket_s: float = 0.1, by: str = "function", groups: dict[str, list[str]] | None = None,
                         top: int = 8, t0_s: float | None = None, t1_s: float | None = None,
                         separator: str | None = None) -> dict:
        """CPU 使用率（load = 眠り以外の割合）とグループの割合の推移（bucket_s 秒ごと。全体で多い top 個のグループ、
        残りは "(other)"）。サンプルの時刻が要る（pcsr、または itm の timestamps）。"""
        result, chosen, info, hz = self._windowed(t0_s, t1_s, None, None)
        if hz is None:
            raise ValueError("サンプルの時刻の単位が分からない")
        out = timeline(chosen, self._profiler(), bucket=bucket_s * hz, grouping=Grouping(by, groups, separator),
                       top=top)
        origin = next((s.t for s in result.samples.samples if s.t is not None), 0)
        for b in out["buckets"]:
            b["t0_s"] = round((b["t0"] - origin) / hz, 12)
        out.update(bucket_s=bucket_s, window=info)
        return out

    # --- PC サンプリング: 書き出し ---------------------------------------------------------------

    def profile_export(self, format: str, path: str, t0_s: float | None = None, t1_s: float | None = None,
                       t0: int | None = None, t1: int | None = None, source_root: str | None = None,
                       test_name: str = "fprobe_pc_sampling", low: int | None = None, high: int | None = None,
                       min_fraction: float = 0.0, static_calls: bool = True) -> dict:
        """プロファイルを書き出す（区間で切れる）。gmon（gprof。番地の度数。low / high で範囲）/ callgrind
        （KCacheGrind。関数・行の自分のサンプル）/ lcov（近似のカバレッジ。SF は source_root を前に付ける）/ gcov
        （path はディレクトリ。ファイルごとの .gcov）/ dot（関数の割合と、static_calls なら逆アセンブルの静的な呼び出し）。"""
        _check_format(format, PROFILE_FORMATS)
        out_path = _out_path(path)
        result, chosen, info, hz = self._windowed(t0_s, t1_s, t0, t1)
        if format == "gmon":
            rate, source = self._sample_rate(result, chosen, hz)
            data, meta = gmon_bytes([s.pc for s in chosen.samples if s.pc is not None], rate_hz=rate, low=low,
                                    high=high)
            out_path.write_bytes(data)
            return {"format": format, "path": str(out_path), "window": info, "rate_source": source, **meta}
        prof = self._profiler().profile(chosen, names=self._vector_names(result.run.vector_table))
        out = {"format": format, "path": str(out_path), "window": info, "total": prof.total}
        if format == "callgrind":
            out_path.write_text(callgrind_from_profile(prof, cmd=self._symbols.path.name), encoding="utf-8",
                                newline="\n")
        elif format in ("lcov", "gcov"):
            with self._lock:
                disassembler, _ = self._disassembler(from_elf=True)
            info = Debugger(None, self._symbols).debug_info()
            if info is None:
                raise ValueError("lcov / gcov には ELF の DWARF が要る（関数の所在は DW_AT_decl_file・decl_line）")
            files, undeclared = coverage_from_profile(coverage(prof, disassembler, all_functions=True), prof,
                                                      dwarf_declarations(info))
            out["files"] = len(files)
            out["notes"] = ["PC サンプリングによる近似: 回数 0 の行が実行されなかったとは限らない"]
            if undeclared:
                out["undeclared_functions"] = undeclared
                out["notes"].append(f"DWARF に宣言の場所（DW_AT_decl_file・decl_line）の無い関数 {len(undeclared)} 個は"
                                    "関数の記録（FN）を書いていない（undeclared_functions）")
            if format == "lcov":
                out_path.write_text(render_lcov(files, test_name, source_root=source_root), encoding="utf-8",
                                    newline="\n")
            else:
                out["written"] = [str(p) for p in write_gcov(files, out_path, source_root=source_root)]
        else:
            graph = self._call_graph() if static_calls else None
            out_path.write_text(dot_from_profile(prof, graph, min_fraction=min_fraction), encoding="utf-8",
                                newline="\n")
        return out

    @staticmethod
    def _sample_rate(result, chosen: PcSamples, hz: float | None) -> tuple[float, str]:
        """1 秒あたりのサンプル数: 時刻があれば実測（（数 − 1）/ 最初から最後まで）、無ければ計画の値。"""
        times = [s.t for s in chosen.samples if s.t is not None]
        if hz and len(times) >= 2 and times[-1] > times[0]:
            return (len(times) - 1) / ((times[-1] - times[0]) / hz), "measured"
        return result.run.plan.rate_hz, "plan"

    # --- テストポイント ----------------------------------------------------------------------------

    def _tp_names(self, table) -> tuple[dict[str, str], dict[str, int], dict[str, str]]:
        """表示の名前 → (ファイル, 行, 関数の元の名前)。"""
        names = table.function_names()
        files, lines, raw = {}, {}, {}
        starts = {(f.file, f.name): f.line for f in table.functions}
        for (file, function), shown in names.items():
            files[shown], raw[shown] = file, function
            entry = [p.line for p in table.find(function=function, kind="entry", file=file)]
            lines[shown] = starts.get((file, function), min(entry) if entry else 0)
        return files, lines, raw

    def testpoint_aggregate(self, t0_s: float | None = None, t1_s: float | None = None, by: str = "function",
                            groups: dict[str, list[str]] | None = None, top: int = 50,
                            separator: str | None = None) -> dict:
        """テストポイントの関数の時間を区間（最初の印からの t0_s〜t1_s 秒）に限って集計: inclusive_s（子を含む。区間で
        切る。再帰は外側だけ）・exclusive_s（自分だけ）・started（区間で始まった回数）・active（区間に掛かった回数）。
        by が function 以外なら file / directory / prefix / groups のグループ（exclusive_s と started の合計）。
        使うのは持っている最近の呼び出し（dropped はそれより前に捨てた数）。"""
        run = self._tp_current()
        report = run.live.report(final=False)
        calls, dropped = run.live.calls()
        hz, origin = report.hz, report.t_first or 0
        lo = None if t0_s is None else origin + t0_s * hz
        hi = None if t1_s is None else origin + t1_s * hz
        rows = call_stats(calls, lo, hi)
        out = {"running": run.running, "hz": hz, "t0_s": t0_s, "t1_s": t1_s, "dropped": dropped,
               "kept_from_s": (min(c.t0 for c in calls) - origin) / hz if calls else None, "by": by}
        if by == "function":
            out["rows"] = [{"function": r["function"], "inclusive_s": r["inclusive"] / hz,
                            "exclusive_s": r["exclusive"] / hz, "started": r["started"], "active": r["active"]}
                           for r in rows[:top]]
            total = len(rows)
        else:
            files, _, _ = self._tp_names(run.live.table)
            grouped = group_rows(rows, Grouping(by, groups, separator), files.get)
            out["rows"] = [{"group": g["group"], "exclusive_s": g["exclusive"] / hz, "started": g["started"],
                            "functions": g["functions"]} for g in grouped[:top]]
            total = len(grouped)
        out.update(rows_total=total, rows_truncated=total > top, top=top)    # 切ったことが分かる
        if dropped:
            out["notes"] = [f"古い呼び出し {dropped} 件は持っていない（kept_from_s より前は数えていない。全部は記録の "
                            "stream \"testpoint\"）"]
        return out

    def testpoint_export(self, format: str, path: str, source_root: str | None = None,
                         test_name: str = "fprobe_testpoint", min_fraction: float = 0.0) -> dict:
        """テストポイントの結果を書き出す: callgrind（呼び出しの木。呼び出しの回数と含む時間）/ lcov（C0 の行・関数・C1 の
        分岐 BRDA。SF は対応表の相対の道に source_root を前に付ける）/ gcov（path はディレクトリ）/ dot（呼び出しの木）/
        gmon（gprof の呼び出しの組 arc だけ。番地は読み込んだ ELF から）。"""
        _check_format(format, TESTPOINT_FORMATS)
        out_path = _out_path(path)
        run = self._tp_current()
        report = run.live.report(final=False)
        table = run.live.table
        files, lines, raw = self._tp_names(table)
        out = {"format": format, "path": str(out_path), "running": run.running}
        if format == "callgrind":
            out_path.write_text(callgrind_from_tree(report.tree, report.hz, file_of=files.get, line_of=lines.get),
                                encoding="utf-8", newline="\n")
        elif format in ("lcov", "gcov"):
            cov = coverage_from_testpoint(report, table)
            out["files"] = len(cov)
            if format == "lcov":
                out_path.write_text(render_lcov(cov, test_name, source_root=source_root), encoding="utf-8",
                                    newline="\n")
            else:
                out["written"] = [str(p) for p in write_gcov(cov, out_path, source_root=source_root)]
        elif format == "dot":
            out_path.write_text(dot_from_tree(report.tree, report.functions, report.hz, min_fraction=min_fraction),
                                encoding="utf-8", newline="\n")
        else:
            if self._symbols is None:
                raise RuntimeError("gmon の呼び出しの組には関数の番地が要る（debug_elf_load）")
            arcs, missing = [], set()
            for (caller, callee), (count, _) in sorted(tree_edges(report.tree).items()):
                ends = []
                for shown in (caller, callee):
                    try:
                        ends.append(self._symbols.symbol(raw.get(shown, shown)).address)
                    except KeyError:
                        missing.add(shown)
                if len(ends) == 2:
                    arcs.append((ends[0], ends[1], count))
            data, meta = gmon_bytes([], rate_hz=1, arcs=arcs)
            out_path.write_bytes(data)
            out.update(arcs=meta["arcs"], missing=sorted(missing))
        return out
