"""プロセス（とその子）の作業セットの合計の最大を、別スレッドで 0.2 秒ごとに測る。"""

import threading

import psutil


class MemWatch:
    def __init__(self, find):
        """find(): 測るプロセス（psutil.Process）の一覧を返す関数。"""
        self._find, self.peak_mb, self._stop = find, 0.0, threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.wait(0.2):
            total = 0
            for p in self._find():
                try:
                    total += p.memory_info().rss
                except psutil.Error:
                    pass
            self.peak_mb = max(self.peak_mb, total / 2**20)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join()


def tree(pid: int) -> list[psutil.Process]:
    try:
        p = psutil.Process(pid)
        return [p, *p.children(recursive=True)]
    except psutil.Error:
        return []


def by_cmdline(marker: str) -> list[psutil.Process]:
    out = []
    for p in psutil.process_iter(["cmdline"]):
        try:
            if any(marker in a for a in (p.info["cmdline"] or [])):
                out.append(p)
        except psutil.Error:
            pass
    return out
