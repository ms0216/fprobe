"""共通の時間軸の記録（fprobe.recording）を、ほかのツールの形式へ書き出す。

- perfetto: Chrome JSON のトレースイベント（Perfetto UI・trace_processor・chrome://tracing で開ける）。
- profile_formats: プロファイル・カバレッジ（gmon.out・callgrind・lcov・gcov・dot）。
"""

from .perfetto import write_trace

__all__ = ["write_trace"]
