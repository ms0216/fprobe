"""連続試験を無人で動かす起動役。セッション（Claude Code など）から切り離して動かす用。
- 実行中だけ Windows のスリープを防ぐ（SetThreadExecutionState。システムの電源設定は変えない。終わると元に戻る）。
- 引数はそのまま soak.py に渡す（例: --host <PYNQ の IP> --until 06:30 --log build/soak/soak.jsonl）。
"""

import ctypes
import os
import runpy
import sys

ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001

if os.name == "nt":
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)

sys.argv = [str(os.path.join(os.path.dirname(__file__), "soak.py")), *sys.argv[1:]]
runpy.run_path(sys.argv[0], run_name="__main__")
