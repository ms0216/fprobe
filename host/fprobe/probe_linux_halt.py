"""JTAG で全コアを止めて Linux のアプリをデバッグする（API の linux.halt.*。Probe のサービス）。中身は fprobe.linux_halt.service で、
CLI `python -m fprobe.linux_halt` と同じものを呼ぶ。設計: docs/superpowers/specs/2026-11-01-linux-halt-api-ui-design.md。

占有: fprobe のバックエンドで開いている間、プローブの生の DAP をこの口が使う。ターゲットの接続（target.connect）・線を直接
動かす操作（jtag.scan・dp/ap・pins 等、`_raw`）・止めずに読む口の JTAG（linux.memap.connect_jtag）・構成の書き替えは断る
（`require_dap_free`。TargetService・LowLevelService・DesignService が呼ぶ）。コアの操作は 1 回ずつ機器の錠（核の lock）を取る
（gdb のスレッドと他の操作が同じ USB の口を混ぜない）。

頼る相手: 核（fprobe.probe_service.ProbeCore の lock・session）、ターゲットに接続していないことの確かめ（require_disconnected）・
線を直接動かす道具の DAP の接続口を閉じる（close_lowlevel。どちらも LowLevelService）、構成の書き替え（ensure_design。
DesignService）、止めずに読む口の Linux のターゲット（linux_targets。LinuxService）。
"""
from __future__ import annotations


import time

from .api import NotConnected
from .linux_halt.service import FileLog, HaltOptions, HaltSession, journal_facts
from .linux_halt.view import LinuxDebugView, prefetch_libraries, prefetch_sources
from .probe_service import HostView

__all__ = ["DapOccupied", "LinuxHaltService", "OWNER"]

OWNER = "linux.halt"


class DapOccupied(RuntimeError):
    """プローブの生の DAP を linux.halt が使っている。"""


class LinuxHaltService:
    """JTAG で全コアを止めて Linux のアプリをデバッグする口。"""

    API = ("linux_halt_open", "linux_halt_close", "linux_halt_status", "linux_halt_halt_all", "linux_halt_resume_all",
           "linux_halt_processes", "linux_halt_threads", "linux_halt_recover", "linux_halt_rescue",
           "linux_halt_gdb_serve", "linux_halt_gdb_status", "linux_halt_gdb_stop",
           "linux_halt_debug_attach", "linux_halt_debug_status", "linux_halt_debug_break_insert",
           "linux_halt_debug_break_remove", "linux_halt_debug_run", "linux_halt_debug_halt", "linux_halt_debug_threads",
           "linux_halt_debug_read", "linux_halt_debug_memory", "linux_halt_debug_step", "linux_halt_debug_select",
           "linux_halt_debug_close")

    # ほかの領域（Probe の委譲）に出す名前: DAP の占有の確かめ（ターゲット・低レベル・構成）と、後始末の段 linux_halt
    # _linux_view: attach している間、マイコンのデバッガの API を Linux のデバッグで答える層（各サービスの入口が使う）
    # request_halt_now・target_status_unqueued: デーモンが作業スレッドの順番を待たずに呼ぶ（止める要求・見張りの読み）
    SHARED = ("_close_linux_halt", "_linux_attaching", "_linux_halt_opened", "_linux_halted_reason", "_linux_view",
              "_require_dap_free",
              "_require_halt_budget", "read_unqueued", "request_halt_now", "target_status_unqueued")
    NEEDS = ("_close_lowlevel", "_ensure_design", "_linux", "_lock", "_require_disconnected", "linux_core_fetch", "session",
             "system_status")

    def __init__(self, host):
        self._host = HostView(host, self.NEEDS, type(self).__name__)
        self._halt_session: HaltSession | None = None
        self._halt_by: dict | None = None
        self._view: LinuxDebugView | None = None
        self._attach_target: str | None = None          # attach の fetch_from_target（止めているのはこの対象と分かる）
        self._attach_options: dict | None = None        # attach に渡した設定（debug.status の options。画面が欄に映す）
        self._attaching: dict | None = None             # 進行中の attach（pid・始めた時刻）。状態の読みが待たずに返す

    def _linux_view(self) -> LinuxDebugView | None:
        """attach している（HaltDebugger が開いていて閉じていない）間だけ、その層。事実（持ち主の有無）で道を選ぶ。"""
        view = self._view
        if view is None or view.d.closed or self._halt_session is None or self._halt_session._debug is not view.d:
            return None
        return view

    def _linux_halted_reason(self, target: str) -> str | None:
        """linux.connect のターゲット target が、いま linux.halt で全コアを止められていると分かれば、その事実の文。分かる
        のは attach の fetch_from_target に渡した対象だけ（ほかの対象との対応は知らない。推さない）。"""
        view = self._linux_view()
        if view is None or target != self._attach_target or view.d.engine.running:
            return None
        return (f"対象 {target} は linux.halt で全コアを止めている（pid {view.d.proc.tgid} に attach 中）: ssh・エージェントは"
                "答えないので使わない（target.resume / linux.halt.debug.run で走らせるか、linux.halt.debug.close してから）")

    def _require_halt_budget(self, what: str, seconds: float) -> None:
        """機器の錠を seconds 秒まで持ち続けうる操作（ロジアナの取り込み・流し）を、attach で全コアを止めている間、
        放置しない見張り（max_halt_s）の残りより長ければ断る（錠を待つ見張りが全コアを走らせられなくなる）。事実の比較だけ。"""
        view = self._linux_view()
        left = view.d.halt_budget_left() if view is not None else None
        if left is not None and seconds > left:
            raise RuntimeError(f"{what}: 機器の錠を最大 {seconds} 秒持つ。linux.halt で全コアを止めていて、止めたまま放置しない"
                               f"見張り（max_halt_s {view.d.max_halt_s} 秒）が走らせるまで残り {max(left, 0.0):.3f} 秒: その間"
                               "見張りが錠を取れない（走らせてから、か上限を短くして）")

    def request_halt_now(self) -> None:
        """attach している間、走らせたままの待ちを破る止める要求を立てる（錠を取らない。すぐ返る）。"""
        view = self._linux_view()
        if view is not None:
            view.d.request_halt()

    def _linux_attaching(self) -> dict | None:
        """進行中の linux.halt.debug.attach の事実（pid・始めた UNIX 秒）。無ければ None。"""
        a = self._attaching
        return None if a is None else dict(a)

    def _linux_halt_opened(self) -> dict | None:
        """linux.halt が開いていて、プロセスへの attach はまだ（進行中でもない）の事実（backend）。無ければ None。
        マイコンのターゲットは未接続のまま（DISCONNECTED）なので、画面が「開いている」を同じ場所に出す（backlog 80 番）。"""
        session = self._halt_session
        if session is None or self._linux_view() is not None or self._attaching is not None:
            return None
        return {"backend": session.options.backend}

    def read_unqueued(self, method: str, params: dict):
        """作業スレッドを通さない状態の読み（デーモンの UNQUEUED_READS）。Linux のプロセスに attach している・attach が進行中
        の間だけ答える（それ以外は None: いつもの道）。機器の錠だけを短く取るので、attach の取り寄せ・長い run の後ろに並ばない。
        target.status: attach 中は待たずに NotConnected（attach が進行中の事実）。system.status: そのまま（錠を短く取る）。"""
        attaching = self._linux_attaching()
        view = self._linux_view()
        if attaching is None and view is None:
            return None
        if method == "target.status":
            if view is None:
                raise NotConnected(f"linux.halt.debug.attach が進行中（pid {attaching['pid']}、"
                                   f"{attaching['since_unix_s']:.3f} から）: まだ止めて調べる対象が無い")
            return view.target_status_unqueued()
        if method == "system.status":
            return self._host.system_status(**params)
        return None

    def target_status_unqueued(self) -> dict | None:
        """attach している間の target.status（作業スレッドの外から読む。鍵を待たない: 取れなければ最後に分かった事実の
        写し）。無ければ None（デーモンはいつもの道で読む）。"""
        view = self._linux_view()
        if view is None:
            attaching = self._linux_attaching()
            if attaching is not None:
                return {"state": "DISCONNECTED", "linux_attaching": attaching}
            opened = self._linux_halt_opened()
            # プローブの DAP を使う backend では開いている間 target.connect を断るので、マイコンは未接続（事実）。openocd の
            # backend はマイコンの接続と並びうるので、いつもの道に任せる
            if opened is not None and opened["backend"] == "fprobe":
                return {"state": "DISCONNECTED", "linux_halt_open": opened}
            return None
        try:
            return view.target_status_unqueued()
        except Exception:  # noqa: BLE001 - 閉じた直後等: いつもの道に任せる
            return None

    def _close_linux_halt(self) -> dict | None:
        """閉じる（gdb のサーバの後始末を待つ）。デーモンの終了・機器の開き直しでも呼ぶ。錠を持ったまま呼ばない（gdb の
        スレッドが錠を待っていると、後始末が終わらない）。"""
        session, self._halt_session, self._halt_by, self._view = self._halt_session, None, None, None
        self._attach_options = None
        if session is None:
            return None
        return session.close()

    def _require_dap_free(self, what: str) -> None:
        """プローブの生の DAP を linux.halt が使っていれば what を断る（DapOccupied）。"""
        session = self._halt_session
        if session is not None and session.options.backend == "fprobe":
            raise DapOccupied(f"{what}: プローブの DAP は linux.halt が使っている（linux.halt.close してから）")

    def _halt(self) -> HaltSession:
        if self._halt_session is None:
            raise RuntimeError("linux.halt が開いていない（linux.halt.open）")
        return self._halt_session

    def _occupancy(self) -> dict:
        """他の操作との排他の事実（識別子。言葉にするのは画面・読む側。仕様の description が意味を持つ）。"""
        if self._halt_session.options.backend == "fprobe":
            return {"probe_dap": True,
                    "refused_while_open": ["target.connect", "lowlevel", "linux.memap.connect_jtag", "probe.design.switch"],
                    "device_lock": "per_call"}
        return {"probe_dap": False, "refused_while_open": [], "device_lock": "openocd_path"}

    def linux_halt_open(self, backend: str, jtag_khz: int, offsets: str, page_offset: int, phys_offset: int,
                        linear_map_end: int, max_tasks: int, max_threads: int, mmu_format: str, journal: str,
                        fpga_request_times: bool, part: str | None = None,
                        fpga_halt_watch_bound_ns: int | str | None = None, fpga_halt_watch_poll_s: float | None = None,
                        cti_board: str | None = None, cti_request_bound_ns: int | None = None,
                        cti_event_bound_ns: int | str | None = None, cti_hw_wait_s: float | None = None,
                        cti_ack_polls: int | None = None, ocd_board: str | None = None, ocd_port: int | None = None,
                        ocd_timeout_s: float | None = None, hw_slots: int | None = None, watch_slots: int | None = None,
                        caller: dict | None = None) -> dict:
        """JTAG で全コアを止めて Linux のアプリをデバッグする口を開く（CLI の共通の引数と一対一。既定の値は無い）。"""
        if self._halt_session is not None:
            raise RuntimeError("linux.halt はもう開いている（linux.halt.close してから開き直す）")
        options = HaltOptions(backend=backend, jtag_khz=jtag_khz, offsets=offsets, page_offset=page_offset,
                              phys_offset=phys_offset, linear_map_end=linear_map_end, max_tasks=max_tasks,
                              max_threads=max_threads, mmu_format=mmu_format, journal=journal,
                              fpga_request_times=fpga_request_times, part=part,
                              fpga_halt_watch_bound_ns=fpga_halt_watch_bound_ns,
                              fpga_halt_watch_poll_s=fpga_halt_watch_poll_s, cti_board=cti_board,
                              cti_request_bound_ns=cti_request_bound_ns, cti_event_bound_ns=cti_event_bound_ns,
                              cti_hw_wait_s=cti_hw_wait_s, cti_ack_polls=cti_ack_polls, ocd_board=ocd_board,
                              ocd_port=ocd_port, ocd_timeout_s=ocd_timeout_s, hw_slots=hw_slots,
                              watch_slots=watch_slots).check()
        switch = None
        if backend == "openocd":
            session = HaltSession.open(options, lock=self._host._lock)
        else:
            self._host._require_disconnected("linux.halt（プローブの DAP を使う）")
            jtag = [name for name, t in self._host._linux.items()
                    if t.memap is not None and getattr(t.memap, "info", {}).get("via") == "probe_jtag"]
            if jtag:
                raise DapOccupied(f"linux.halt: 止めずに読む口（JTAG）が DAP を使っている（{', '.join(jtag)}。"
                                  "linux.memap.disconnect してから）")
            switch = self._host._ensure_design("jtag")
            with self._host._lock:
                self._host._close_lowlevel()                   # 線を直接動かす道具が開いたままの DAP の接続口を閉じる
            session = HaltSession.open(options, dap_link=self._host.session.dap_access, device=self._host.session.device,
                                       lock=self._host._lock)
        self._halt_session, self._halt_by = session, caller
        out = {**session.info, "options": options.as_dict(), "journal": journal_facts(journal),
               "occupies": self._occupancy(), "opened_by": caller}
        if switch is not None:
            out["design_switch"] = switch
        return out

    def linux_halt_close(self) -> dict:
        if self._halt_session is None:
            return {"closed": False, "open": False}
        return self._close_linux_halt()

    def linux_halt_status(self) -> dict:
        if self._halt_session is None:
            return {"open": False}
        return {"open": True, **self._halt_session.status(), "occupies": self._occupancy(), "opened_by": self._halt_by}

    def linux_halt_halt_all(self) -> dict:
        return self._halt().halt_all()

    def linux_halt_resume_all(self) -> dict:
        return self._halt().resume_all()

    def linux_halt_processes(self) -> dict:
        return self._halt().processes()

    def linux_halt_threads(self, pid: int) -> dict:
        return self._halt().threads(pid)

    def linux_halt_recover(self) -> dict:
        return self._halt().recover()

    def linux_halt_rescue(self) -> dict:
        return self._halt().rescue()

    def linux_halt_gdb_serve(self, pid: int, port: int, skew_limit_ns: int, thread_id: str, kernel_isa: str,
                             max_step_retries: int, bp_unmapped: str, log: str | None = None) -> dict:
        """RSP サーバをデーモンの中で開く（127.0.0.1:port。手元の gdb が target remote でつなぐ）。log はパケットのログのファイル。"""
        session = self._halt()
        logger = FileLog(log) if log is not None else None
        try:
            return session.gdb_serve(pid=pid, port=port, skew_limit_ns=skew_limit_ns, thread_id=thread_id,
                                     kernel_isa=kernel_isa, max_step_retries=max_step_retries, bp_unmapped=bp_unmapped,
                                     log=logger)
        except BaseException:
            if logger is not None:
                logger.close()
            raise

    def linux_halt_gdb_status(self) -> dict:
        return {"gdb": self._halt().gdb_status()}

    def linux_halt_gdb_stop(self) -> dict:
        return {"gdb": self._halt().gdb_stop()}

    # --- linux.halt.debug.*（gdb を通さずに AI が全コア停止で止めて調べる。仕様 2026-11-03-linux-halt-ai-tools-design.md）---

    def linux_halt_debug_attach(self, pid: int, elf: str, skew_limit_ns: int, thread_id: str, kernel_isa: str,
                                max_step_retries: int, bp_unmapped: str, max_halt_s: float,
                                fetch_from_target: str | None = None, breakpoint_kind: str | None = None,
                                step_max_instructions: int | None = None, step_timeout_s: float | None = None) -> dict:
        """プロセス pid を止めて調べる持ち主を開く（全コアを止めたまま返す）。fetch_from_target（linux.connect のターゲットの
        名前）を渡すと、先にそのターゲットの /proc/<pid>/exe を elf に写す（linux.core.fetch と同じ道）。PC に無いソースも
        そのターゲットから取り寄せる（debug.source.read）。breakpoint_kind（sw / hw）は種類を渡さない置き方
        （画面の debug.breakpoint.set）で使う種類（渡さなければその置き方は断る。既定の選び方を持たない）。
        step_max_instructions・step_timeout_s は画面の道のステップ（debug.step・target.step・Run to Cursor）の上限
        （同じく渡さなければ断る）。"""
        if breakpoint_kind not in (None, "sw", "hw"):
            raise ValueError(f"breakpoint_kind は sw か hw（{breakpoint_kind!r}）")
        self._attaching = {"pid": pid, "since_unix_s": time.time()}
        try:
            return self._debug_attach(pid, elf, skew_limit_ns, thread_id, kernel_isa, max_step_retries, bp_unmapped,
                                      max_halt_s, fetch_from_target, breakpoint_kind, step_max_instructions,
                                      step_timeout_s)
        finally:
            self._attaching = None

    def _debug_attach(self, pid, elf, skew_limit_ns, thread_id, kernel_isa, max_step_retries, bp_unmapped, max_halt_s,
                      fetch_from_target, breakpoint_kind, step_max_instructions, step_timeout_s) -> dict:
        session = self._halt()
        fetched, sources = None, None
        if fetch_from_target is not None:
            fetched = self._host.linux_core_fetch(target=fetch_from_target, path=f"/proc/{pid}/exe", local_path=elf)
            # PC に無いソースもいま（コアを止める前）取り寄せる: 止まった対象の ssh は答えない（2026-11-03 実機）
            sources = prefetch_sources(elf, fetch_from_target, lambda target, path, local: self._host.linux_core_fetch(
                target=target, path=path, local_path=local))
            # 共有ライブラリ（libc など）も、いま取り寄せる（関数の名前・ARM / Thumb の別を引くため）
            libraries = prefetch_libraries(elf, fetch_from_target, pid, lambda target, path, local: self._host.linux_core_fetch(
                target=target, path=path, local_path=local))
        else:
            libraries = None
        out = session.debug_attach(pid=pid, elf=elf, skew_limit_ns=skew_limit_ns, thread_id=thread_id,
                                   kernel_isa=kernel_isa, max_step_retries=max_step_retries, bp_unmapped=bp_unmapped,
                                   max_halt_s=max_halt_s, libraries=libraries)
        self._attach_target = fetch_from_target
        self._attach_options = {"pid": pid, "elf": elf, "skew_limit_ns": skew_limit_ns, "thread_id": thread_id,
                                "kernel_isa": kernel_isa, "max_step_retries": max_step_retries, "bp_unmapped": bp_unmapped,
                                "max_halt_s": max_halt_s, "fetch_from_target": fetch_from_target,
                                "breakpoint_kind": breakpoint_kind, "step_max_instructions": step_max_instructions,
                                "step_timeout_s": step_timeout_s}
        self._view = LinuxDebugView(session._debug, breakpoint_kind=breakpoint_kind, sources=sources,
                                    step_max_instructions=step_max_instructions, step_timeout_s=step_timeout_s)
        return {**out, "fetched": fetched, "breakpoint_kind": breakpoint_kind,
                "step_max_instructions": step_max_instructions, "step_timeout_s": step_timeout_s,
                "sources_fetched": [{"name": n, **v} for n, v in sorted((sources or {}).items())]}

    def linux_halt_debug_status(self) -> dict:
        return {**self._halt().debug_status(), "options": self._attach_options}

    def linux_halt_debug_break_insert(self, location: str, kind: str) -> dict:
        return self._halt().debug_break_insert(location=location, kind=kind)

    def linux_halt_debug_break_remove(self, id: int) -> dict:  # noqa: A002 - API の引数名
        return self._halt().debug_break_remove(id=id)

    def linux_halt_debug_run(self, wait_s: float) -> dict:
        return self._halt().debug_run(wait_s=wait_s)

    def linux_halt_debug_halt(self) -> dict:
        return self._halt().debug_halt()

    def linux_halt_debug_threads(self, variables: bool, expressions: list[str], max_elements: int) -> dict:
        return self._halt().debug_threads(variables=variables, expressions=expressions, max_elements=max_elements)

    def linux_halt_debug_read(self, expressions: list[str], tid: int, frame: int, max_elements: int) -> dict:
        return self._halt().debug_read(expressions=expressions, tid=tid, frame=frame, max_elements=max_elements)

    def linux_halt_debug_memory(self, address: int, length: int) -> dict:
        return self._halt().debug_memory(address=address, length=length)

    def linux_halt_debug_step(self, tid: int, mode: str, max_instructions: int, timeout_s: float) -> dict:
        return self._halt().debug_step(tid=tid, mode=mode, max_instructions=max_instructions, timeout_s=timeout_s)

    def linux_halt_debug_select(self, tid: int) -> dict:
        """今のスレッドを選ぶ（画面のデバッガ: レジスタ・呼び出し履歴・変数・ステップがこのスレッドで答える。次の止まりまで）。"""
        return self._halt().debug_select(tid=tid)

    def linux_halt_debug_close(self) -> dict:
        return self._halt().debug_close()
