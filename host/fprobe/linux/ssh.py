"""SSH・scp を子のプロセスで使う（Windows 標準の OpenSSH。鍵は利用者の ~/.ssh と ssh-agent。新しい依存を足さない）。

実行するファイルは環境変数 FPROBE_SSH / FPROBE_SCP で差し替えられる（.py なら Python で起動する: 試験の偽物）。"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

CONNECT_TIMEOUT_S = 10                 # 接続を待つ上限（argv に見える）


class SshTimeout(RuntimeError):
    """時間切れ。途中までの stdout・stderr を持つ（捨てない）"""

    def __init__(self, message: str, stdout: bytes, stderr: bytes):
        super().__init__(message)
        self.stdout, self.stderr = stdout or b"", stderr or b""


def _exe(env: str, default: str) -> list[str]:
    path = os.environ.get(env, default)
    return [sys.executable, path] if path.endswith(".py") else [path]


@dataclass(frozen=True)
class SshTarget:
    host: str
    user: str
    port: int = 22
    key: str | None = None

    def _options(self, port_flag: str) -> list[str]:
        out = ["-o", "BatchMode=yes", "-o", f"ConnectTimeout={CONNECT_TIMEOUT_S}", port_flag, str(self.port)]
        return out + (["-i", self.key] if self.key else [])

    def argv(self, remote_cmd: str) -> list[str]:
        return _exe("FPROBE_SSH", "ssh") + self._options("-p") + [f"{self.user}@{self.host}", remote_cmd]

    def run(self, remote_cmd: str, input: bytes | None = None, timeout: float = 30) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(self.argv(remote_cmd), input=input, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise SshTimeout(f"{self.user}@{self.host} が {timeout} 秒で終わらなかった: {remote_cmd[:80]}",
                             e.stdout, e.stderr) from e

    def open(self, remote_cmd: str) -> subprocess.Popen:
        return subprocess.Popen(self.argv(remote_cmd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)

    def _scp(self, src: str, dst: str, timeout: float) -> None:
        try:
            r = subprocess.run(_exe("FPROBE_SCP", "scp") + self._options("-P") + [src, dst],
                               capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"scp が {timeout} 秒で終わらなかった: {src} → {dst}") from e
        if r.returncode:
            raise RuntimeError(f"scp が失敗した（{r.returncode}）: {r.stderr.decode(errors='replace').strip()}")

    def put(self, local: Path, remote: str, timeout: float = 60) -> None:
        self._scp(str(local), f"{self.user}@{self.host}:{remote}", timeout)

    def get(self, remote: str, local: Path, timeout: float = 120) -> None:
        local.parent.mkdir(parents=True, exist_ok=True)
        self._scp(f"{self.user}@{self.host}:{remote}", str(local), timeout)
