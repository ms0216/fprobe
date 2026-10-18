"""ETM の命令トレース（Probe の mixin）: CoreSight の設定（trace.etm.start / stop）と、取ったトレースの解読
（trace.etm.decode）・パケットの一覧・命令の一覧・事後の命令履歴（U35・U36 の最小形）。

トレースのバイト列の取り方はここでは決めない（チップ・配線に依る）: 16 進・ファイル・ターゲットのメモリのリング
（RP2350 のトレース FIFO + DMA。手順はプラグイン fprobe.plugins.rp2350_trace）から渡す。FPGA でトレース端子を
受ける段（U34）になれば、その記録も同じ decode に渡る。
調査と段階: docs/reports/2026-10-19-etm-investigation.md
"""

from dataclasses import dataclass
from pathlib import Path

from .memview import TargetMemory
from .symbols import is_network_path
from .trace import decoder_for
from .trace import elements as el
from .trace.coresight import ETM_BASE, TPIU_BASE, Etmv4, Etmv4Info, Etmv4Setup, Funnel, Tpiu, enable_trace
from .trace.reconstruct import Flow, Reconstructor
from .trace.tpiu import deformat

__all__ = ["EtmTools", "HISTORY_NOTE"]

HISTORY_NOTE = ("ETM が出した命令の列（実行した命令は確実）。M-profile の ETM にはデータの値が無いので、レジスタ・"
                "メモリの過去の値は含まない（要るならスナップショットから前へ模擬した推定になる）。")


@dataclass
class _EtmSession:
    etm_base: int
    tpiu_base: int | None
    trace_id: int
    info: Etmv4Info


@dataclass
class _EtmResult:
    elements: list[el.Element]
    flow: Flow


class EtmTools:
    """trace.etm.*（Probe の mixin）。"""

    def _init_etm(self) -> None:
        self._etm_session: _EtmSession | None = None
        self._etm_result: _EtmResult | None = None

    # --- 設定 ---------------------------------------------------------------------------

    def etm_start(self, etm_base: int = ETM_BASE, trace_id: int = 0x10, tpiu_base: int | None = TPIU_BASE,
                  port_size: int = 4, formatter: bool = True, funnel_base: int | None = None, funnel_ports: int = 0xFF,
                  cycle_counting: bool = False, cycle_threshold: int = 256, timestamps: bool = False,
                  sync_period: int = 1024, fsync_frames: int = 64) -> dict:
        """ETMv4 の命令トレースを始める（CoreSight の標準の手順）: DEMCR.TRCENA、ファンネル（funnel_base があれば
        funnel_ports の入口を有効に）、TPIU（tpiu_base があれば port_size ビット・フォーマッタの継続モード）、ETM
        （trace_id、sync_period バイトごとに同期、全命令）。ETMv4 でなければ断る。"""
        setup = Etmv4Setup(trace_id, cycle_counting, cycle_threshold, timestamps, sync_period)
        with self._lock:
            regs = TargetMemory(self.target)
            etm = Etmv4(regs, etm_base)
            info = etm.info()
            if info.arch_major != 4:
                raise RuntimeError(f"0x{etm_base:08X} に ETMv4 が無い（TRCIDR1 の版 {info.version}。ETMv3.5 は未対応）")
            enable_trace(regs)
            out: dict = {"etm": info.as_dict(), "trace_id": trace_id}
            if funnel_base is not None:
                out["funnel"] = {"base": funnel_base, "ctrl": Funnel(regs, funnel_base).enable(funnel_ports)}
            if tpiu_base is not None:
                out["tpiu"] = {"base": tpiu_base, **Tpiu(regs, tpiu_base).configure(port_size, formatter, fsync_frames)}
            out["registers"] = etm.enable(setup)
            self._etm_session = _EtmSession(etm_base, tpiu_base, trace_id, info)
            return out

    def etm_stop(self, etm_base: int | None = None, tpiu_base: int | None = None) -> dict:
        """ETM を止め（IDLE を待つ）、TPIU のフォーマッタを出し切る。番地を省くと trace.etm.start のもの（無ければ
        ETM は Cortex-M の既定、TPIU は触らない）。"""
        session = self._etm_session
        etm_base = etm_base if etm_base is not None else session.etm_base if session else ETM_BASE
        tpiu_base = tpiu_base if tpiu_base is not None else session.tpiu_base if session else None
        with self._lock:
            regs = TargetMemory(self.target)
            idle = Etmv4(regs, etm_base).disable()
            flushed = Tpiu(regs, tpiu_base).flush() if tpiu_base is not None else None
            return {"idle": idle, "flushed": flushed}

    # --- 解読 ---------------------------------------------------------------------------

    def _etm_bytes(self, data_hex: str | None, path: str | None, address: int | None, length: int | None,
                   oldest: int) -> bytes:
        given = [x is not None for x in (data_hex, path, address)]
        if sum(given) != 1:
            raise ValueError("トレースは data_hex・path・address（と length）のどれか 1 つで渡す")
        if data_hex is not None:
            return bytes.fromhex(data_hex)
        if path is not None:
            if is_network_path(path):
                raise ValueError(f"外の共有のファイルは開かない: {path}")
            return Path(path).read_bytes()
        if length is None or length <= 0:
            raise ValueError("address には length（バイト数）が要る")
        if not 0 <= oldest < length:
            raise ValueError(f"oldest（最も古いバイトの位置）は 0〜{length - 1}: {oldest}")
        ring = bytes(self.target.read_memory_block8(address, length))
        return ring[oldest:] + ring[:oldest]                         # リングを古い順に並べ直す

    def _etm_code(self, from_elf: bool | None):
        """命令を読むもの: ELF があれば ELF（from_elf=False なら実機）、無ければ実機。(読み手, "elf" / "target")。"""
        image = self._elf_image()
        if from_elf is None:
            from_elf = image is not None
        if from_elf:
            if image is None:
                raise RuntimeError("ELF が無い（debug_elf_load）")
            return image, "elf"
        return TargetMemory(self.target), "target"

    def etm_decode(self, data_hex: str | None = None, path: str | None = None, address: int | None = None,
                   length: int | None = None, oldest: int = 0, formatted: bool = True, align: str = "auto",
                   trace_id: int | None = None, protocol: str = "etmv4", from_elf: bool | None = None) -> dict:
        """トレースのバイト列を解読し、命令の流れを再構成する（結果は trace.etm.packets / instructions / history）。
        formatted: TPIU のフォーマッタの枠付き（trace_id の流れを取り出す。省略時は trace.etm.start の ID、
        それが無ければ最も多い ID）か、ETM のバイト列そのものか。"""
        with self._lock:
            raw = self._etm_bytes(data_hex, path, address, length, oldest)
            out: dict = {"bytes": len(raw), "protocol": protocol}
            stream = raw
            if formatted:
                frames = deformat(raw, align)
                streams = frames.streams
                out["deformat"] = frames.as_dict()
                out["streams"] = out["deformat"].pop("streams")
                if trace_id is None:
                    session_id = self._etm_session.trace_id if self._etm_session else None
                    trace_id = session_id if session_id in streams else \
                        max(streams, key=lambda i: len(streams[i])) if streams else None
                stream = streams.get(trace_id, b"")
                out["trace_id"] = trace_id
            config = self._etm_session.info.decoder_config() if self._etm_session else None
            elements = decoder_for(protocol, config).decode(stream)
            memory, origin = self._etm_code(from_elf)
            flow = Reconstructor(memory, self._symbols).run(elements)
            self._etm_result = _EtmResult(elements, flow)
            addresses = flow.addresses()
            errors = list(flow.errors[:20])
            if formatted and trace_id not in streams:
                errors.insert(0, f"トレース ID {'' if trace_id is None else f'0x{trace_id:02X} '}の流れが無い"
                                 f"（あるのは {sorted(out['streams']) or 'なし'}。FSYNC が無ければ align を確かめる）")
            out.update({
                "code": origin, "packets": len(elements), "instructions": flow.count,
                "exceptions": sum(1 for m in flow.markers if m.kind == "exception"),
                "unresolved_atoms": flow.unresolved_atoms, "errors": errors,
                "first": {"address": addresses[0], **self._place_of(addresses[0])} if addresses else None,
                "last": {"address": addresses[-1], **self._place_of(addresses[-1])} if addresses else None,
                "next_address": flow.next_address,
            })
            return out

    def _etm(self) -> _EtmResult:
        if self._etm_result is None:
            raise RuntimeError("解読したトレースが無い（trace_etm_decode を先に）")
        return self._etm_result

    def etm_packets(self, offset: int = 0, limit: int = 200, kinds: list[str] | None = None) -> dict:
        """解読したパケット（共通の要素）の一覧: kind・トレースの中の位置 offset・バイト raw・中身。kinds で絞る。"""
        with self._lock:
            items = [e for e in self._etm().elements if kinds is None or e.kind in kinds]
            return {"total": len(items), "offset": offset, "packets": [e.as_dict() for e in items[offset:offset + limit]]}

    def etm_instructions(self, offset: int = 0, limit: int = 200, function: str | None = None) -> dict:
        """再構成した命令の一覧（通し番号 index・番地・命令・関数・ファイル:行、分岐の最後は branch = taken /
        not_taken / exception と行き先 target）と、その範囲の出来事（例外・時刻・途切れ）。function で関数を絞る。"""
        with self._lock:
            flow = self._etm().flow
            if function is None:
                rows, total = flow.instructions(offset, limit), flow.count
            else:
                matched = flow.instructions(function=function)
                rows, total = matched[offset:offset + limit], len(matched)
            return {"total": total, "offset": offset, "instructions": rows, "markers": self._etm_markers(flow, rows)}

    def etm_history(self, count: int = 32) -> dict:
        """事後の命令履歴: トレースの最後（止まった所の直前）の count 命令（古い順）と、その間の出来事。"""
        with self._lock:
            flow = self._etm().flow
            rows = flow.history(count)
            return {"total": flow.count, "instructions": rows, "markers": self._etm_markers(flow, rows),
                    "next_address": flow.next_address, "errors": flow.errors[:20], "note": HISTORY_NOTE}

    @staticmethod
    def _etm_markers(flow: Flow, rows: list[dict]) -> list[dict]:
        if not rows:
            return []
        first, last = rows[0]["index"], rows[-1]["index"]
        return [m.as_dict() for m in flow.markers if first <= m.index <= last + 1]
