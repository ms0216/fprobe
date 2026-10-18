"""DWARF によるソースの言葉のデバッグ: 型・値・変数の場所（位置式）・C の式・CFI による呼び出し履歴の巻き戻し。

ターゲットには依存しない。メモリとレジスタは `TargetAccess`（read_memory / read_register）で受け取る。

    info = DebugInfo("app.elf")
    insp = Inspector(info, access, symbols=Symbols("app.elf"))
    insp.backtrace(variables=True)
    insp.read("head->samples[1]")
"""

from .access import AccessError, ImageAccess, TargetAccess, WritableAccess
from .cexpr import ExpressionError, Value
from .info import DebugInfo, FrameView, Function, Scope, Variable
from .inspector import Inspector
from .location import NotAvailable
from .unwind import CallFrameTable, FrameState, Unwinder, is_exc_return

__all__ = ["AccessError", "CallFrameTable", "DebugInfo", "ExpressionError", "FrameState", "FrameView", "Function",
           "ImageAccess", "Inspector", "NotAvailable", "Scope", "TargetAccess", "Unwinder", "Value", "Variable",
           "WritableAccess", "is_exc_return"]
