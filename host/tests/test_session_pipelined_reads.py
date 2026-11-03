"""MEM-AP のブロック読み出しの先送り版は、開いたセッションの AP にだけ付ける（pyOCD のクラスを書き換えない。C4）。

前は Session.debug が pyOCD の MEM_AP._read_memory_block32 をクラスごと置き換えていた。同じプロセスで fprobe 以外の
プローブ（ST-LINK 等）を pyOCD で開いても置き換わった版が使われ、どこで変わったかがクラスを見ないと分からなかった。
"""

from types import SimpleNamespace

from pyocd.coresight.ap import MEM_AP

from fprobe import session as session_mod


class _Ap(MEM_AP):
    """MEM_AP の初期化（DP・探索）を通さずに、ブロック読み出しの束ね方だけを持つ AP。"""

    def __init__(self, accelerated: bool = False):  # noqa: D107 - 試験用
        self.read_memory_block32 = self._accelerated_read_memory_block32 if accelerated else self._read_memory_block32


def _pyocd_session(*aps):
    return SimpleNamespace(target=SimpleNamespace(aps={i: ap for i, ap in enumerate(aps)}))


def test_debug_does_not_rewrite_the_pyocd_class():
    original = MEM_AP.__dict__["_read_memory_block32"]
    s = session_mod.Session(SimpleNamespace(close=lambda: None), lambda: None, lambda: None)
    try:
        s.debug(target_override="cortex_m")
    except Exception:  # noqa: BLE001 - 接続口が無いので pyOCD のセッションは作れなくてよい。見たいのはクラスが変わらないこと
        pass
    assert MEM_AP.__dict__["_read_memory_block32"] is original
    assert not hasattr(session_mod, "_install_pipelined_reads")


def test_the_pipelined_read_is_bound_only_to_the_aps_of_the_opened_session():
    mine, other = _Ap(), _Ap()
    session_mod._attach_pipelined_reads(_pyocd_session(mine))
    assert mine.read_memory_block32.__func__ is session_mod._pipelined_read_memory_block32
    assert mine.read_memory_block32.__self__ is mine
    assert other.read_memory_block32.__func__ is MEM_AP._read_memory_block32     # ほかのセッションの AP はそのまま


def test_an_ap_that_uses_the_probes_accelerated_interface_is_left_as_is():
    ap = _Ap(accelerated=True)
    session_mod._attach_pipelined_reads(_pyocd_session(ap))
    assert ap.read_memory_block32.__func__ is MEM_AP._accelerated_read_memory_block32


def test_an_ap_that_is_not_a_mem_ap_is_left_as_is():
    plain = SimpleNamespace(read_memory_block32="untouched")
    session_mod._attach_pipelined_reads(_pyocd_session(plain))
    assert plain.read_memory_block32 == "untouched"


def test_the_session_from_debug_attaches_after_open(monkeypatch):
    """Session.debug の返すセッションは、開いた（探索で AP ができた）後に付ける。"""
    attached = []
    monkeypatch.setattr(session_mod, "_attach_pipelined_reads", attached.append)
    monkeypatch.setattr(session_mod.PyocdSession, "open", lambda self, init_board=True: None)
    cls = session_mod._PipelinedPyocdSession
    s = cls.__new__(cls)
    s.open()
    assert attached == [s]
