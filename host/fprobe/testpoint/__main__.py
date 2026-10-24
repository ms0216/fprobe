"""テストポイントの挿入のコマンド。

    python -m fprobe.testpoint instrument SRC_DIR OUT_DIR [--var NAME[:novalue]] [--map testpoints.json]
           [--only-changed] [--baseline OLD.json] [--include GLOB] [--exclude GLOB] [--no-branches] [--renumber]
    python -m fprobe.testpoint header OUT_DIR       # fprobe_tp.h だけを置く

--map の対応表が既にあれば、それを前回として読み、指紋が同じ関数の ID を保つ。結果の要約を JSON で出す。
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

from .instrument import Options, WatchVar, header_path, instrument_tree
from .probes import ProbeTable


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m fprobe.testpoint", description="テストポイントの自動挿入")
    sub = ap.add_subparsers(dest="command", required=True)
    ins = sub.add_parser("instrument", help="C のソースに挿入した写しを作る")
    ins.add_argument("src", type=Path)
    ins.add_argument("out", type=Path)
    ins.add_argument("--map", type=Path, help="対応表（既定 OUT/testpoints.json。あれば前回として ID を保つ）")
    ins.add_argument("--var", action="append", default=[], help="書き込みを記録する変数（name または name:novalue）")
    ins.add_argument("--include", action="append", default=[], help="挿入する関数名の glob")
    ins.add_argument("--exclude", action="append", default=[], help="挿入しない関数名の glob")
    ins.add_argument("--only-changed", action="store_true", help="前回から変わった関数にだけ入れる")
    ins.add_argument("--baseline", type=Path, help="--only-changed の比較の元の対応表（既定は前回）")
    ins.add_argument("--no-branches", action="store_true", help="分岐に入れない（関数の入口・出口と変数だけ）")
    ins.add_argument("--renumber", action="store_true", help="ID を 1 から振り直す")
    ins.add_argument("--non-type-word", action="append", default=[],
                     help="戻り値の型の中の型でない語（記憶域のマクロ等。一時変数の型から除く。繰り返せる）")
    hdr = sub.add_parser("header", help="fprobe_tp.h を置く")
    hdr.add_argument("out", type=Path)
    args = ap.parse_args(argv)

    if args.command == "header":
        args.out.mkdir(parents=True, exist_ok=True)
        shutil.copy(header_path(), args.out / "fprobe_tp.h")
        return 0
    map_path = args.map or args.out / "testpoints.json"
    previous = ProbeTable.load(map_path) if map_path.exists() else None
    options = Options(branches=not args.no_branches, variables=tuple(WatchVar.parse(v) for v in args.var),
                      include=tuple(args.include), exclude=tuple(args.exclude), only_changed=args.only_changed,
                      non_type_words=tuple(args.non_type_word))
    baseline = ProbeTable.load(args.baseline) if args.baseline else None
    result = instrument_tree(args.src, args.out, options=options, previous=previous, baseline=baseline,
                             map_path=map_path, renumber=args.renumber)
    summary = {
        "probes": result.probe_count, "map": str(map_path), "written": [str(p) for p in result.written],
        "removed": result.removed,
        "files": [{"path": f.path, "instrumented": f.instrumented, "reused": f.reused, "changed": f.changed,
                   "skipped": [{"function": s.name, "line": s.line, "reason": s.reason} for s in f.skipped],
                   "warnings": f.warnings, "error": f.error, "probes": f.probes} for f in result.files],
    }
    json.dump(summary, sys.stdout, ensure_ascii=False, indent=1)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
