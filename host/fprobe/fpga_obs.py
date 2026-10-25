"""対象の FPGA の観測（docs/superpowers/specs/2026-10-20-fpga-observation-v2-design.md）: 対象の FPGA に入れた観測の
回路 v2（target_ip/fprobe_obs）を fprobe が対象の JTAG で読み続け（obs_jtag_engine）、記録をイベント 8、印をイベント 9 に
する。ここは対象の回路の設定・操作（FpgaObs）。イベントから「fprobe の時刻の値の並び」を作り直す復号は obs_decoder、
条件（FPGA に書く一致への翻訳・ホストで数える評価）は obs_condition。互換のため、どちらもここから再公開する。
以下は復号が読む記録の形式。

記録（32 ビット、31:30 が種類）。時刻のある記録は前の時刻のある記録からの対象の clk の周期数 delta を 29:16 に持つ:
  0 値 delta | value(15:0)（delta 0 = 鎖の始め）、1 刻み delta | 0・印 delta | 1 | 番号(14:7) | late(6:0)、
  2 溢れ lost(15:0)（鎖が切れる）、3 制御 種類(29:26): 0 組(7:0)・1 応答 番地(19:16) | 値(15:0)。
時刻は数えない: delta を足し合わせた「鎖の時刻」を、印（番号つき。fprobe がその Update-DR の時刻をイベント 9 にする）
で fprobe の時刻と結ぶ。鎖ごとに印の組を直線に合わせる（組が 1 つなら申告の CLOCK_HZ の比を傾きにする）。

標本型（obs_snap_core。極小の FPGA 向け）: 記録の語 = 0xA7 | 前の標本からの変化の回数(23:16) | 値(15:0)。時刻は
fprobe がその Capture-DR を出た瞬間（イベントの時刻そのもの）。標本の間の見えない変化は切れ目（失った数）にする。
"""

import time

from . import generated as g
from .obs_condition import compile_match
from .obs_decoder import WIDTH, ObsDecoder, ObsSample, SyncSegment, fit_line, fit_sync, samples

__all__ = ["CHAIN_FIELDS", "FpgaObs", "ObsDecoder", "ObsSample", "SyncSegment", "compile_match", "fit_line", "fit_sync",
           "samples"]

OP_WRITE, OP_READ = 1, 2
REG_ID_LO, REG_ID_HI, REG_VERSION, REG_GROUPS, REG_FIFO_DEPTH, REG_CLOCK_LO, REG_CLOCK_HI = range(7)
REG_SYNC_DELAY, REG_SAMPLE_DELAY, REG_LOST_BITS = 7, 8, 9    # 申告の遅れ（半周期の単位・周期）と失った数の幅
REG_CONTROL, REG_SELECT = 14, 15
OBS_ID = 0x464F4253                               # "FOBS"
SNAP_ID = 0x534E4150                              # 標本型（エンジンが id に出す "SNAP"）
CHAIN_FIELDS = {"ir_len": g.OBS_CHAIN_IR_LEN, "ir_before": g.OBS_CHAIN_IR_BEFORE, "ir_after": g.OBS_CHAIN_IR_AFTER,
                "dr_before": g.OBS_CHAIN_DR_BEFORE, "dr_after": g.OBS_CHAIN_DR_AFTER}


class FpgaObs:
    """fprobe の FPGA の観測の口（ケイパビリティ FPGA_OBS がある構成だけ）。"""

    def __init__(self, device):
        caps = device.capabilities()
        if not getattr(caps, "fpga_obs", False):
            raise RuntimeError("この構成には FPGA の観測が無い（ケイパビリティ FEATURES の FPGA_OBS。PYNQ 等の大きい構成だけ）")
        self.dev = device
        self.sync_delay = 0.0                          # 観測の回路の申告（open で読む。周期）
        self.sample_delay = 0.0
        self.clock_hz = 0
        self.mode = "stream"                           # open で決まる: "stream"（記録型）・"snapshot"（標本型）
        self.sys_clock_hz = caps.sys_clock_hz or 100_000_000
        self.scan_s = 0.0                              # 1 スキャンの時間の見積もり（open で。待ちの長さに使う）

    def samples(self, events, nominal_ratio: float | None = None) -> list[ObsSample]:
        """samples（モジュールの関数）を、open で読んだ観測の回路の遅れで。"""
        return samples(events, nominal_ratio, sync_delay=self.sync_delay, sample_delay=self.sample_delay)

    def open(self, chain: dict | None = None, user_ir: int = 2, half_period: int = 10, words: int = 64,
             sync_period: int = 100, timeout_s: float = 1.0, id_scans: int = 0) -> dict:
        """対象の JTAG の鎖での位置（chain: CHAIN_FIELDS のうち要るもの。ir_len の既定は 4）・USER の命令・TCK の半周期
        （sys の周期数。1 クロック版の観測の回路は TCK ≤ 対象の clk / 8）・1 スキャンの記録の語数・印の間隔（スキャンの
        数）を設定して始め、観測の回路の申告を返す。id_scans = 始めに ID の応答を待つスキャンの上限（使う側が渡す。
        0 = 上限なし。上限まで来なければ回路が stuck・id_timeout で止める）。"""
        unknown = set(chain or {}) - set(CHAIN_FIELDS)
        if unknown:
            raise ValueError(f"chain に知らない項目: {sorted(unknown)}（使えるのは {sorted(CHAIN_FIELDS)}）")
        if not 2 <= half_period <= 0xFFFF or not 1 <= words <= 256 or not 0 <= sync_period <= 0xFFFF:
            raise ValueError("half_period は 2〜65535、words は 1〜256、sync_period は 0〜65535")
        if not 0 <= id_scans <= 0xFF:
            raise ValueError("id_scans は 0〜255（0 = 上限なし）")
        fields = {"ir_len": 4, **(chain or {})}
        if not 1 <= fields["ir_len"] <= 32:
            raise ValueError(f"chain の ir_len = {fields['ir_len']} は 1〜32（エンジンは範囲の外を黙って丸める）")
        packed = 0
        for name, value in fields.items():
            lsb, width = CHAIN_FIELDS[name]
            if not 0 <= value < 1 << width:
                raise ValueError(f"chain の {name} = {value} は {width} ビットに入らない")
            packed |= value << lsb
        self.close()
        self.scan_s = self._scan_estimate(half_period, words)
        # ID の応答を待つ上限（id_scans）まで待つ。0（上限なし）のときは、使う側が渡す timeout_s（と 40 スキャン）まで
        timeout_s = max(timeout_s, (id_scans + 8 if id_scans else 40) * self.scan_s)
        try:
            return self._open(half_period, packed, user_ir, words, sync_period, id_scans, timeout_s)
        except BaseException:                          # 失敗したままエンジンを走らせない（線を駆動し続け、他の道具が JTAG を使えない）
            try:
                self.close()
            except Exception:                          # noqa: BLE001  後始末の失敗で、元の例外を隠さない
                pass
            raise

    def _scan_estimate(self, half_period: int, words: int) -> float:
        """1 スキャン = 見出し 1 語 + 記録 words 語（と鎖・状態の移りの分）の TCK。命令の間の分を見込んで 1.5 倍。"""
        return (1 + words) * 32 * 2 * half_period / self.sys_clock_hz * 1.5 + 1e-4

    def scan_seconds(self) -> float:
        """今のエンジンの設定（CSR の TCK の半周期・語数）での 1 スキャンの長さ（秒）。開いた FpgaObs でなくても分かる。"""
        if self.scan_s:
            return self.scan_s
        half_period, words = self.dev.read32(g.OBS_HALF_PERIOD), self.dev.read32(g.OBS_WORDS)
        return self._scan_estimate(half_period, words) if half_period and words else 0.0

    def _open(self, half_period: int, packed: int, user_ir: int, words: int, sync_period: int, id_scans: int,
              timeout_s: float) -> dict:
        self.dev.write32(g.OBS_HALF_PERIOD, half_period)
        self.dev.write32(g.OBS_CHAIN, packed)
        self.dev.write32(g.OBS_USER_IR, user_ir)
        self.dev.write32(g.OBS_WORDS, words)
        self.dev.write32(g.OBS_SYNC_PERIOD, sync_period)
        self.dev.write32(g.OBS_ID_SCANS, id_scans)
        self.dev.write32(g.OBS_CONTROL, 1)
        self._wait(lambda s: s["id_ok"] or s["stuck"], timeout_s, "ID を読めない")
        status = self.status()
        if not status["id_ok"] and status["id_timeout"]:
            self.close()
            raise RuntimeError(f"観測の回路の ID の応答が id_scans = {id_scans} スキャンで来なかった（ID {status['id']:#010x}）")
        if not status["id_ok"]:
            self.close()
            raise RuntimeError(f"観測の回路が答えない（ID {status['id']:#010x}。鎖の位置・USER の命令・配線・TCK の速さ"
                               "（1 クロック版は対象の clk の 1/8 以下）を確かめる）")
        if status["id"] == SNAP_ID:
            # 標本型: レジスタが無い（組 1・幅 16。時刻は fprobe の Capture-DR の瞬間）
            self.mode = "snapshot"
            self.sync_delay = self.sample_delay = 0.0
            self.clock_hz = 0
            return {"id": status["id"], "mode": "snapshot", "version": 2, "width": WIDTH, "groups": 1,
                    "fifo_depth": 0, "clock_hz": 0, "sync_delay": 0.0, "sample_delay": 0.0, "lost_bits": 8}
        self.mode = "stream"
        self.sync_delay = self.register_read(REG_SYNC_DELAY) / 2
        self.sample_delay = float(self.register_read(REG_SAMPLE_DELAY))
        self.clock_hz = self.register_read(REG_CLOCK_LO) | self.register_read(REG_CLOCK_HI) << 16
        return {"id": status["id"], "mode": "stream", "version": self.register_read(REG_VERSION), "width": WIDTH,
                "groups": self.register_read(REG_GROUPS), "fifo_depth": self.register_read(REG_FIFO_DEPTH),
                "clock_hz": self.clock_hz, "sync_delay": self.sync_delay, "sample_delay": self.sample_delay,
                "lost_bits": self.register_read(REG_LOST_BITS)}

    def close(self, timeout_s: float = 1.0) -> None:
        """止める（今のスキャンを終え、TAP を Test-Logic-Reset に戻して線を手放す）。手放すまで待つ（その後なら別の
        道具を対象の JTAG につないでよい）。"""
        self.dev.write32(g.OBS_CONTROL, 0)
        deadline = time.monotonic() + max(timeout_s, 20 * self.scan_seconds())
        while self.dev.read32(g.OBS_STATUS) >> g.OBS_STATUS_DRIVING[0] & 1:
            if time.monotonic() > deadline:
                raise TimeoutError("観測の線を手放さない（エンジンが止まらない）")

    def status(self) -> dict:
        s = self.dev.read32(g.OBS_STATUS)
        out = {name.lower(): bool(s >> getattr(g, f"OBS_STATUS_{name}")[0] & 1)
               for name in ("RUNNING", "ID_OK", "STUCK", "CMD_PENDING", "OVERFLOW_SEEN", "CMD_DROPPED", "TARGET_RUNNING",
                            "DRIVING", "ID_TIMEOUT")}
        out["scans"] = self.dev.read32(g.OBS_SCANS)
        out["id"] = self.dev.read32(g.OBS_ID)
        return out

    def register_read(self, addr: int, timeout_s: float | None = None) -> int:
        """観測の回路のレジスタ（16 ビット）を読む。応答は記録の列で来て、エンジンが read_value（番号 | 番地 | 値）に
        写す。番号が変わり番地が合うまで待つ。"""
        self._need_registers()
        before = self.dev.read32(g.OBS_READ_VALUE) >> 24
        self._command(OP_READ, addr, 0, timeout_s)
        deadline = time.monotonic() + self._timeout(timeout_s)
        while True:
            v = self.dev.read32(g.OBS_READ_VALUE)
            if v >> 24 != before and v >> 16 & 0xF == addr:
                return v & 0xFFFF
            if self.status()["stuck"]:
                raise RuntimeError("観測の回路が答えなくなった（レジスタの読み）")
            if time.monotonic() > deadline:
                raise TimeoutError(f"レジスタ {addr} の応答が来ない")

    def register_write(self, addr: int, value: int, timeout_s: float | None = None) -> None:
        self._need_registers()
        self._command(OP_WRITE, addr, value, timeout_s)
        start = self.dev.read32(g.OBS_SCANS)
        self._wait(lambda s: (s["scans"] - start) % (1 << 32) >= 1, self._timeout(timeout_s), "スキャンが進まない")

    def select(self, group: int) -> None:
        """観測する 16 ビットの組（0〜GROUPS − 1。範囲外は観測の回路が無視する。標本型は 0 だけ）。記録中は、観測の回路が
        変化の無い周期（か溢れの立て直し）に組の記録を書くまで前の組を見続ける（記録の組の印が切り替えの瞬間）。"""
        if self.mode == "snapshot" and group == 0:
            return
        self.register_write(REG_SELECT, group)

    def start(self) -> None:
        """観測の回路が記録を始める（始め直すと溢れの印も消える。標本型はいつも動いているので何もしない）。"""
        if self.mode != "snapshot":
            self.register_write(REG_CONTROL, 1)

    def stop(self) -> None:
        if self.mode != "snapshot":
            self.register_write(REG_CONTROL, 0)

    def _need_registers(self) -> None:
        if self.mode == "snapshot":
            raise RuntimeError("標本型の観測の回路（obs_snap_core）にはレジスタが無い")

    def _timeout(self, timeout_s: float | None) -> float:
        return max(1.0, 40 * self.scan_s) if timeout_s is None else timeout_s

    def _command(self, op: int, addr: int, data: int, timeout_s: float | None) -> None:
        """命令（op(3:0) | 番地(7:4) | データ(15:8)）を待ち行列に積み、スキャンに載るまで待つ。"""
        if not 0 <= addr <= 15 or not 0 <= data <= 0xFF:
            raise ValueError("番地は 0〜15、データは 0〜255")
        self.dev.write32(g.OBS_CMD, op | addr << 4 | data << 8)
        self.dev.write32(g.OBS_CMD_PUSH, 1)
        if self.status()["cmd_dropped"]:
            raise RuntimeError("命令の待ち行列が一杯で捨てられた（観測が止まっていないか確かめる）")
        self._wait(lambda s: not s["cmd_pending"], self._timeout(timeout_s), "命令が出ない")

    def _wait(self, done, timeout_s: float, what: str) -> None:
        deadline = time.monotonic() + timeout_s
        while True:
            s = self.status()
            if done(s):
                return
            if s["stuck"]:
                raise RuntimeError(f"観測の回路が答えなくなった（{what}）")
            if time.monotonic() > deadline:
                raise TimeoutError(f"{what}（{timeout_s} 秒）")
