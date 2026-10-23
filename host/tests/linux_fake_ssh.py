# 試験の偽の ssh: 命令の文字で振り分ける。fprobe_agent を含めば偽のエージェント、caps の台本（sh -s）なら PYNQ の見本、
# それ以外は FPROBE_FAKE_SSH_REPLIES（JSON の {部分の文字: 返す stdout}）から返し、受けた命令を FPROBE_FAKE_SSH_LOG に書く。
import json
import os
import runpy
import sys
from pathlib import Path

cmd = sys.argv[-1]
log = os.environ.get("FPROBE_FAKE_SSH_LOG")
if log:
    with open(log, "a", encoding="utf-8") as f:
        f.write(cmd + "\n")
here = Path(__file__).parent
if cmd.startswith("sudo -S -k -v"):                    # パスワードの確かめ
    sys.stdin.buffer.readline()
    if os.environ.get("FPROBE_FAKE_SUDO_BAD"):
        sys.stderr.write("Sorry, try again.\n")
        sys.exit(1)
    sys.exit(0)
if "sudo -S" in cmd:                                   # 本物の sudo -S と同じく、stdin の最初の行をパスワードとして読む
    password = sys.stdin.buffer.readline().decode().rstrip("\n")
    if os.environ.get("FPROBE_FAKE_SUDO_LOG"):
        with open(os.environ["FPROBE_FAKE_SUDO_LOG"], "a", encoding="utf-8") as f:
            f.write(password + "\n")
if "fprobe_agent" in cmd and "gcc" not in cmd and "sha256sum" not in cmd:
    sys.argv = [str(here / "linux_fake_agent.py")]
    runpy.run_path(sys.argv[0], run_name="__main__")
    sys.exit(0)
if "gdbserver" in cmd:                                 # 偽の gdbserver: 受けたバイトを大文字にして返す
    while True:
        data = sys.stdin.buffer.read1(4096) if hasattr(sys.stdin.buffer, "read1") else sys.stdin.buffer.read(1)
        if not data:
            break
        sys.stdout.buffer.write(data.upper())
        sys.stdout.buffer.flush()
    sys.exit(0)
if cmd == "sh -s":
    sys.stdin.read()
    from test_linux_caps import PYNQ                     # noqa: E402
    sys.stdout.write(PYNQ)
    sys.exit(0)
replies = json.loads(os.environ.get("FPROBE_FAKE_SSH_REPLIES", "{}"))
if os.environ.get("FPROBE_FAKE_SSH_FAIL") and os.environ["FPROBE_FAKE_SSH_FAIL"] in cmd:   # 試験から: その命令は失敗する
    sys.stderr.write("Failed to open journal\n")
    sys.exit(1)
for part, text in replies.items():
    if part in cmd:
        sys.stdout.write(text)
        sys.stdout.flush()
        if os.environ.get("FPROBE_FAKE_SSH_DELAY"):
            import time
            time.sleep(float(os.environ["FPROBE_FAKE_SSH_DELAY"]))
        sys.exit(0)
sys.exit(0)
