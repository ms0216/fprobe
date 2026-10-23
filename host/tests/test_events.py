"""DAP イベントの復号と、ポストされた読みの対応付け（ハードウェア不要）。"""

from fprobe.events import KIND_HOST, decode_events, resolve


def event(t, *, ap, read, reg, address=0, data=0, ack=1, parity=0, kind=KIND_HOST):
    request = int(ap) | int(read) << 1 | (reg >> 2) << 2
    v = (t | address << 48 | data << 80 | ack << 112 | request << 115 | parity << 119 | kind << 120)
    return v.to_bytes(16, "little")


def test_decode_fields():
    (e,) = decode_events(event(123, ap=True, read=False, reg=0xC, address=0x20000010, data=0xDEADBEEF))
    assert (e.time, e.ap, e.read, e.register, e.address, e.data, e.ok, e.kind) == (
        123, True, False, 0xC, 0x20000010, 0xDEADBEEF, True, KIND_HOST)


def test_posted_reads_are_paired_like_dap_c():
    data = b"".join([
        event(1, ap=False, read=False, reg=0x8, data=0),                        # SELECT
        event(2, ap=True, read=False, reg=0x4, address=0x4, data=0x20000000),   # TAR
        event(3, ap=True, read=True, reg=0xC, address=0x20000000, data=0x99),   # 読み 1（値は前の読み = 不定）
        event(4, ap=True, read=True, reg=0xC, address=0x20000004, data=0x11),   # 読み 2（値 = 読み 1）
        event(5, ap=False, read=True, reg=0xC, data=0x22),                      # RDBUFF（値 = 読み 2）
        event(6, ap=False, read=True, reg=0x4, data=0xF0000040),                # CTRL/STAT
        event(7, ap=True, read=False, reg=0xC, address=0x20000008, data=0x33),  # 書き込み
        event(8, ap=True, read=True, reg=0xC, address=0x20000000, ack=2),       # WAIT
    ])
    accesses = resolve(decode_events(data))
    assert [(a.kind, a.address, a.value) for a in accesses] == [
        ("dp_write", 0x0, 0), ("ap_write", 0x4, 0x20000000),
        ("ap_read", 0x20000000, 0x11), ("ap_read", 0x20000004, 0x22),
        ("dp_read", 0x4, 0xF0000040), ("ap_write", 0x20000008, 0x33), ("fail", 0x20000000, 0)]


def test_uart_and_swo_bytes_do_not_break_posted_reads():
    from fprobe.events import KIND_SWO, KIND_UART_RX, KIND_UART_TX
    data = b"".join([
        event(1, ap=True, read=True, reg=0xC, address=0x20000000, data=0x99),   # 読み 1
        event(2, ap=False, read=False, reg=0, data=0x41, ack=0, kind=KIND_UART_RX),
        event(3, ap=False, read=False, reg=0, data=0x42, ack=0, kind=KIND_UART_TX),
        event(4, ap=False, read=False, reg=0, data=0x43, ack=0, kind=KIND_SWO),
        event(5, ap=False, read=True, reg=0xC, data=0x11),                      # RDBUFF（値 = 読み 1）
    ])
    accesses = resolve(decode_events(data))
    assert [(a.time, a.kind, a.value) for a in accesses] == [
        (2, "uart_rx", 0x41), (3, "uart_tx", 0x42), (4, "swo", 0x43), (5, "ap_read", 0x11)]


def inject_event(t, transfers, address, data):
    from fprobe.events import KIND_INJECT_TRANSFERS
    v = t | address << 48 | data << 80 | transfers << 112 | KIND_INJECT_TRANSFERS << 120
    return v.to_bytes(16, "little")


def test_injected_writes_are_their_own_kind_with_the_transfer_count():
    # R20: 割り込み書き込みは、ホストの書き込み（ap_write）と区別する。種類 11 は転送数（CSW・TAR の控えがあれば 4、
    # 無ければ読んで戻す転送が足される。WAIT の繰り返しも数える）を持つ。旧い種類 2 は転送数が分からない（None）
    from fprobe.events import KIND_INJECT
    old = event(5, ap=True, read=False, reg=0xC, address=0xE000EDF0, data=0xA05F0003, kind=KIND_INJECT)
    data = b"".join([
        event(1, ap=True, read=True, reg=0xC, address=0x20000000, data=0x99),   # ホストの読み（値はまだ返っていない）
        inject_event(2, 4, 0xE000EDF0, 0xA05F0003),
        event(3, ap=False, read=True, reg=0xC, data=0x22),                      # RDBUFF（値 = ホストの読み）
        old,
    ])
    (e2,) = decode_events(data[16:32])
    assert (e2.address, e2.data, e2.transfers, e2.ok) == (0xE000EDF0, 0xA05F0003, 4, True)
    assert [(a.kind, a.address, a.value, a.transfers) for a in resolve(decode_events(data))] == [
        ("inject", 0xE000EDF0, 0xA05F0003, 4), ("ap_read", 0x20000000, 0x22, None),
        ("inject", 0xE000EDF0, 0xA05F0003, None)]
