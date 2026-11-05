"""linux.ring.read の読み口（止めずに読む・止めて読む）と、読みの統計・時刻の不確かさ。"""
from __future__ import annotations


def new_ring_stats() -> dict:
    return {"reads": 0, "halted_ms_total": 0.0, "halted_ms_max": 0.0, "records": 0, "lost": 0, "torn": 0, "corrupt": 0,
            "header_errors": 0, "error": None}


def count_ring_read(stats: dict, out: dict, records: list[dict], halted_ms: float, header_error: bool) -> None:
    """1 回の linux.ring.read の結果を ring_stats に足す（t.lock の中で）。"""
    stats["reads"] += 1
    stats["halted_ms_total"] += halted_ms
    stats["halted_ms_max"] = max(stats["halted_ms_max"], halted_ms)
    stats["records"] += sum(not r["duplicate"] for r in records)
    stats["lost"] += sum(b - a for a, b in out["lost"])
    stats["torn"] += len(out["torn"])
    stats["corrupt"] += len(out["corrupt"])
    stats["header_errors"] += header_error


def sync_uncertainty(sync: dict, hz: float, map_unc: float) -> float | None:
    """対象の時刻を PC の時刻へ換算した出来事の記録の不確かさ（タイムベース）: 往復の半分 + 記録の時計の写し。
    組の範囲の外（outside）へ延ばした時刻は、組の外の時計の比のゆれが分からず上限が無いので None（分からない。
    範囲の内と同じ幅を付けない。sync の outside・nearest_pair_ns が延ばした事実）。"""
    if sync.get("outside"):
        return None
    return sync["half_rtt_ns"] / 1e9 * hz + map_unc


class MemapReader:
    """止めずに読む口を、止めて読む CPU と同じ形で使う（止める・再開は何もしない）"""

    index = None

    def __init__(self, memap):
        self.memap = memap

    def halt(self) -> None:
        pass

    def resume(self) -> None:
        pass

    def read_words(self, address: int, count: int) -> list[int]:
        return self.memap.read_words(address, count)


class ArmACpu:
    """道 B の止めて読む CPU（fprobe.arm_a_target.ArmATarget の上）。再開に失敗したら、誤りの印を消して再開を求め直す。"""

    def __init__(self, target):
        self.target = target
        self.index = getattr(target, "index", None)

    def state(self) -> str:
        return self.target.core.state()

    def halt(self) -> None:
        self.target.halt()

    def resume(self) -> None:
        """再開する（借りたレジスタを戻してから）。一時的な誤りなら、誤りの印を消して同じ再開をもう 1 度。それでも失敗したら、
        止めたまま残さないために生の再開の要求を出し、レジスタを戻せていないかもしれない事実を誤りで返す。"""
        try:
            self.target.resume()
            return
        except Exception as first:                           # noqa: BLE001
            port = self.target.core.port
            try:
                port.dap.clear_errors()
            except Exception:                                # noqa: BLE001
                pass
            try:
                self.target.resume()
                return
            except Exception:                                # noqa: BLE001
                pass
            try:
                port.restart_request()
            except Exception as e:                           # noqa: BLE001
                raise RuntimeError(f"コアを再開できない（{first}; 生の再開の要求も失敗: {e}）。コアが止まったまま") from first
            raise RuntimeError(f"再開の手順が失敗した（{first}）。生の再開の要求で再開したが、借りたレジスタ（r0・r1）を"
                               "戻せていないかもしれない") from first

    def read_words(self, address: int, count: int) -> list[int]:
        return self.target.read_memory_block32(address, count)
