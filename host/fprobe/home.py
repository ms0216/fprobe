"""利用者ごとの保存先（FPROBE_HOME。既定は ~/.fprobe）。デーモン・MCP・Probe の保存物（記録・スナップショット・名前の表等）が共有する。"""

import os
from pathlib import Path

__all__ = ["fprobe_home"]


def fprobe_home() -> Path:
    return Path(os.environ.get("FPROBE_HOME", Path.home() / ".fprobe"))
