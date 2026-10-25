"""PYNQ-Z2 への据え付け（`python -m fprobe_gw pynq install|status`）。

install: 中継（host/pynq/fprobe-relay）と書き込みの補助（host/pynq/fprobe-pl）を送り、PYNQ の上で make して
/usr/local/bin に置き、systemd のサービス fprobe-relay を有効にし、補助だけをパスワード無しの sudo で呼べるようにする
（PynqLoader が書き込みの前後に sudo -n で呼ぶ）。管理者の権限が要るのはここだけで、sudo のパスワードは環境変数
FPROBE_PYNQ_SUDO_PASSWORD で渡す（リポジトリに書かない）。
"""

import os
import subprocess
from pathlib import Path

from .toolchain.pynq_loader import PL_HELPER, PynqError, PynqLoader, run_checked

__all__ = ["RELAY_SOURCES", "install", "reserve", "status"]

HOST_PYNQ = Path(__file__).resolve().parents[2] / "host" / "pynq"
RELAY_SOURCES = ("fprobe-relay/main.c", "fprobe-relay/relay.c", "fprobe-relay/relay.h", "fprobe-relay/Makefile",
                 "fprobe-relay/fprobe-relay.service", "fprobe-pl")
REMOTE_DIR = "fprobe/relay"
SUDO_ENV = "FPROBE_PYNQ_SUDO_PASSWORD"

_ROOT_STEPS = " && ".join((
    f"install -m 755 {REMOTE_DIR}/fprobe-relay /usr/local/bin/fprobe-relay",
    f"install -m 755 {REMOTE_DIR}/fprobe-pl {PL_HELPER}",
    f"install -m 644 {REMOTE_DIR}/fprobe-relay.service /etc/systemd/system/fprobe-relay.service",
    f"echo '{{user}} ALL=(root) NOPASSWD: {PL_HELPER}' > /tmp/fprobe.sudoers",
    "visudo -cf /tmp/fprobe.sudoers",
    "install -m 440 /tmp/fprobe.sudoers /etc/sudoers.d/fprobe",
    "systemctl daemon-reload",
    "systemctl enable fprobe-relay",
))


def _password(env, what: str) -> str:
    password = env.get(SUDO_ENV)
    if not password:
        raise PynqError(f"{what}には PYNQ の sudo のパスワードが要る（環境変数 {SUDO_ENV}）")
    return password


def _scp(loader: PynqLoader, files, run) -> None:
    r = run_checked(run, ["scp", "-i", str(loader.key), "-o", "BatchMode=yes", *map(str, files),
                          f"{loader.user}@{loader.host}:{REMOTE_DIR}/"],
                    what="PYNQ へのファイルの送り", timeout=60, capture_output=True, text=True, encoding="utf-8",
                    errors="replace")
    if r.returncode != 0:
        raise PynqError(f"PYNQ へのファイルの送りが失敗: {(r.stderr or '').strip()}")


def _sudo(loader: PynqLoader, steps: str, password: str, run, *, what: str, ok=(0,)) -> None:
    """管理者の手順 steps を sudo -S で走らせる（パスワードは標準入力。引数に出さない）。steps は呼び手が作る固定の文字列。"""
    r = run_checked(run, ["ssh", "-i", str(loader.key), "-o", "BatchMode=yes", f"{loader.user}@{loader.host}",
                          f"sudo -S -p '' sh -c \"{steps}\""],
                    what=what, timeout=120, input=password + "\n", capture_output=True, text=True, encoding="utf-8",
                    errors="replace")
    if r.returncode not in ok:
        raise PynqError(f"{what}が失敗: {(r.stderr or r.stdout or '').strip()}")


def install(loader: PynqLoader, *, run=subprocess.run, env=os.environ) -> None:
    password = _password(env, "据え付け")
    loader.ssh(f"mkdir -p {REMOTE_DIR}")
    _scp(loader, [HOST_PYNQ / s for s in RELAY_SOURCES], run)
    loader.ssh(f"cd {REMOTE_DIR} && sed -i 's/\\r$//' fprobe-pl && make", timeout=120)
    _sudo(loader, _ROOT_STEPS.format(user=loader.user), password, run, what="PYNQ の上の据え付け（管理者の手順）")


def status(loader: PynqLoader) -> str:
    return loader.ssh("systemctl is-active fprobe-relay; cat /run/fprobe/pl-design 2>/dev/null || echo '(印なし)'; "
                      "journalctl -u fprobe-relay -n 5 --no-pager 2>/dev/null || true")


def reserve(loader: PynqLoader, *, run=subprocess.run, env=os.environ, undo: bool = False) -> None:
    """P3 の深いバッファの領域を予約して再起動する（undo = True なら予約を外して再起動する）。

    /boot/uEnv.txt（host/pynq/uEnv.txt: 今の起動の引数 + mem=479M）で DDR の上端 33 MB（Linux のリングの 1 MB 0x1DF00000〜 + 深いバッファの 32 MB）を Linux から外し、
    /etc/fprobe/mem（host/pynq/fprobe-mem）で中継に領域を知らせる。再起動の前に中継を止め、レベルシフタを切る。
    再起動の後、PL は未構成（boot.py は空）なので、fprobe の回路を書き直す（build fprobe_pynq_deep --program）。
    起動しなくなったら、SD を PC に挿して uEnv.txt を消す。"""
    password = _password(env, "予約")
    if undo:
        steps = f"{PL_HELPER} before; rm -f /boot/uEnv.txt /etc/fprobe/mem; sync; reboot"
    else:
        _scp(loader, [HOST_PYNQ / "uEnv.txt", HOST_PYNQ / "fprobe-mem"], run)
        steps = (f"{PL_HELPER} before; install -m 644 {REMOTE_DIR}/uEnv.txt /boot/uEnv.txt && "
                 f"mkdir -p /etc/fprobe && install -m 644 {REMOTE_DIR}/fprobe-mem /etc/fprobe/mem && sync && reboot")
    # reboot で SSH が切れる（終了コード 255）のは成功のうち
    _sudo(loader, steps, password, run, what="PYNQ の予約の手順", ok=(0, 255))
