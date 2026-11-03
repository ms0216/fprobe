"""中継（relay.c）の試験用の、PL の窓の「PS が DDR を直接読む流れ」のレジスタ（la_ps_port、窓の版 4）の模型。

番地とビットは fprobe.generated の WIN_*（gateware/fprobe_gw/ps_window.py が正）。約束（docs/superpowers/specs/2026-11-03-ps-stream-position-design.md）:
  - STREAM_EPOCH の読みは、公開された組（LA_WRITTEN・LA_WPTR・EV_WPOS・EV_START）を写す。以後の読みは写し。
  - 位置（LA_CONSUMED・EV_CONSUMED・STREAM_BEATS）は流れの番号（epoch）の値。番号が変わると 0 に戻る。書きは POS_TAG が今の番号の
    ときだけ受け、違えば捨てて stale_writes に数える。
  - 公開された番号（pub_epoch。既定は epoch と同じ）が epoch と違う間は CURRENT = 0（PL がまだ前の流れを公開している）。
  - 注入の口（D1）: on_read(addr, n) を、n 回目の読みの値を返す前に呼ぶ（1 回の中継の呼び出しの途中で PL の状態を変える）。
"""
from fprobe import generated as g

from tests.test_pynq_relay import ID, VERSION, PlModel  # noqa: F401

M32 = 1 << 32
EPOCH_MASK = (1 << g.WIN_EPOCH_BITS) - 1
SNAP = ("LA_WRITTEN", "LA_WPTR", "EV_WPOS", "EV_START")


class WindowModel(PlModel):
    """派生は LA_RING・EV_RING・MEM_BYTES を決める。DDR の書き方（put）は派生が決める。"""
    LA_RING = EV_RING = MEM_BYTES = 0

    def __init__(self):
        super().__init__()
        self.version = g.WIN_LINK_VERSION
        self._epoch = 1
        self._pub = None                                        # None = epoch と同じ
        self.active, self.writing, self.overrun = 1, 1, 0
        self.la_written = self.ev_wpos = self.ev_start = 0
        self.la_wptr_override = None
        self.total_start = 0                                    # 流れの開始の総数（2^32 の手前から数える試験）
        self.la_consumed = self.ev_consumed = self.stream_beats = 0
        self.tag = 0
        self.stale_writes = 0
        self.rd_epoch = self.rd_region = self.rd_first = self.rd_count = self.rd_ack = 0
        self.snap = dict.fromkeys(SNAP, 0)
        self.rd_snap = (0, 0, 0)
        self.reads, self.writes = [], []
        self.on_read = None

    # 流れの番号: 変えると位置が 0 に戻る（la_ps_port）
    @property
    def epoch(self):
        return self._epoch

    @epoch.setter
    def epoch(self, value):
        value &= EPOCH_MASK
        if value != self._epoch:
            self._epoch = value
            self.la_consumed, self.ev_consumed, self.stream_beats = self.total_start, 0, 0

    @property
    def pub_epoch(self):
        return self._epoch if self._pub is None else self._pub

    @pub_epoch.setter
    def pub_epoch(self, value):
        self._pub = value

    # 前の版の模型の書き方（mode・status）でも置ける
    @property
    def mode(self):
        return self.active

    @mode.setter
    def mode(self, value):
        self.active = int(bool(value))

    @property
    def status(self):
        return self.writing | self.overrun << 1 | self.active << 2

    @status.setter
    def status(self, value):
        """bit0 = 書き込み中、bit1 = overrun、bit2 = 読み出し中（PS が読み終えていない）。どれも 0 なら流れは終わった。"""
        self.writing, self.overrun = value & 1, (value >> 1) & 1
        self.active = int(bool(value & 5))

    @property
    def la_wptr(self):
        if self.la_wptr_override is not None:
            return self.la_wptr_override
        return ((self.la_written - self.total_start) % M32) % self.LA_RING

    def la_pos(self, n):
        """総数 n の拍のリングの位置。"""
        return ((n - self.total_start) % M32) % self.LA_RING

    def state_word(self):
        current = int(self.pub_epoch == self._epoch)
        return ((self.pub_epoch & EPOCH_MASK) | self.active << g.WIN_STATE_ACTIVE | self.writing << g.WIN_STATE_WRITING
                | self.overrun << g.WIN_STATE_OVERRUN | current << g.WIN_STATE_CURRENT)

    def rd(self, ctx, addr):
        self.reads.append(addr)
        if self.on_read is not None:
            self.on_read(addr, len(self.reads))
        if addr == g.WIN_STREAM_EPOCH:
            self.snap = {"LA_WRITTEN": self.la_written, "LA_WPTR": self.la_wptr, "EV_WPOS": self.ev_wpos,
                         "EV_START": self.ev_start}
            return self.state_word()
        for name in SNAP:
            if addr == getattr(g, f"WIN_{name}"):
                return self.snap[name] & 0xFFFFFFFF
        if addr == g.WIN_RD_EPOCH:
            self.rd_snap = (self.rd_region, self.rd_first, self.rd_count)
            return self.rd_epoch & 0xFF
        live = {g.WIN_MEM_BYTES: self.MEM_BYTES, g.WIN_LA_CONSUMED: self.la_consumed, g.WIN_EV_CONSUMED: self.ev_consumed,
                g.WIN_STREAM_BEATS: self.stream_beats, g.WIN_POS_TAG: self.tag, g.WIN_STALE_WRITES: self.stale_writes,
                g.WIN_RD_REGION: self.rd_snap[0], g.WIN_RD_FIRST: self.rd_snap[1], g.WIN_RD_COUNT: self.rd_snap[2],
                g.WIN_RD_ACK: self.rd_ack, VERSION: self.version}
        if addr in live:
            return live[addr] & 0xFFFFFFFF
        return super().rd(ctx, addr)

    def accept(self, addr, value):
        """位置の書きを受けたとき（派生が確かめを足す）。"""

    def wr(self, ctx, addr, value):
        self.writes.append((addr, value))
        if addr in (g.WIN_LA_CONSUMED, g.WIN_EV_CONSUMED, g.WIN_STREAM_BEATS):
            if self.tag != self._epoch:
                self.stale_writes += 1
                return
            self.accept(addr, value)
            if addr == g.WIN_LA_CONSUMED:
                self.la_consumed = value
            elif addr == g.WIN_EV_CONSUMED:
                self.ev_consumed = value
            else:
                self.stream_beats = value
        elif addr == g.WIN_POS_TAG:
            self.tag = value & EPOCH_MASK
        elif addr == g.WIN_RD_ACK:
            self.rd_ack = value
        else:
            super().wr(ctx, addr, value)
