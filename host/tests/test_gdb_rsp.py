"""fprobe.gdb.rsp（RSP の枠）と fprobe.gdb.description（target.xml・メモリマップ・スレッドの XML）。"""

import xml.etree.ElementTree as ET

from fprobe.gdb import description as d
from fprobe.gdb import rsp


# --- 枠 -------------------------------------------------------------------------------------------

def test_frame_has_the_checksum_and_escapes_special_bytes():
    assert rsp.frame(b"OK") == b"$OK#9a"
    framed = rsp.frame(b"a$b#c}d*e")
    body, check = framed[1:-3], framed[-2:]
    assert body == b"a}\x04b}\x03c}]d}\x0ae"
    assert int(check, 16) == sum(body) & 0xFF


def test_reader_splits_packets_acks_and_interrupts():
    r = rsp.PacketReader()
    events = r.feed(b"+$qSupported:swbreak+#")
    assert events == [rsp.ACK]
    events = r.feed(b"%02x" % (sum(b"qSupported:swbreak+") & 0xFF))       # 検査和は次の読みで届く
    assert events == [rsp.Packet(b"qSupported:swbreak+")]
    assert r.feed(b"\x03-") == [rsp.INTERRUPT, rsp.NAK]


def test_reader_reports_a_bad_checksum_and_unescapes_binary():
    r = rsp.PacketReader()
    assert r.feed(b"$m0,4#00") == [rsp.CORRUPT]
    payload = b"X0,2:}\x03}]"
    wire = b"$" + payload + b"#%02x" % (sum(payload) & 0xFF)
    assert r.feed(wire) == [rsp.Packet(b"X0,2:#}")]


def test_reader_ignores_noise_between_packets():
    r = rsp.PacketReader()
    assert r.feed(b"junk$g#67") == [rsp.Packet(b"g")]


def test_little_endian_hex():
    assert rsp.le_hex(0x12345678, 32) == "78563412"
    assert rsp.from_le_hex("78563412") == 0x12345678
    assert rsp.le_hex(None, 32) == "xxxxxxxx"


# --- 記述 -----------------------------------------------------------------------------------------

M4F = {f"r{i}" for i in range(13)} | {"sp", "lr", "pc", "xpsr", "msp", "psp", "primask", "basepri", "faultmask",
                                       "control", "fpscr"} | {f"s{i}" for i in range(32)}
M0 = {f"r{i}" for i in range(13)} | {"sp", "lr", "pc", "xpsr", "msp", "psp", "primask", "control"}


def test_layout_of_a_cortex_m4f():
    regs = d.layout(M4F)
    names = [r.name for r in regs]
    assert names[:17] == [f"r{i}" for i in range(13)] + ["sp", "lr", "pc", "xpsr"]
    assert names[17:23] == ["msp", "psp", "primask", "basepri", "faultmask", "control"]
    assert names[23:] == [f"d{i}" for i in range(16)] + ["fpscr"]
    assert d.regnum(regs, "pc") == 15 and d.regnum(regs, "sp") == 13
    assert next(r for r in regs if r.name == "d3").bits == 64


def test_layout_of_a_cortex_m0_leaves_out_what_the_core_lacks():
    names = [r.name for r in d.layout(M0)]
    assert "basepri" not in names and "faultmask" not in names and "d0" not in names and "fpscr" not in names


def test_target_xml_names_the_gdb_features():
    root = ET.fromstring(d.target_xml(d.layout(M4F)))
    assert root.find("architecture").text == "arm"
    features = {f.get("name"): [r.get("name") for r in f.findall("reg")] for f in root.findall("feature")}
    assert set(features) == {"org.gnu.gdb.arm.m-profile", "org.gnu.gdb.arm.m-system", "org.gnu.gdb.arm.vfp"}
    regnums = [int(r.get("regnum")) for f in root.findall("feature") for r in f.findall("reg")]
    assert regnums == list(range(len(regnums)))


def test_memory_map_xml():
    xml = d.memory_map_xml([d.Region("flash", 0x0800_0000, 0x8_0000, 0x4000), d.Region("ram", 0x2000_0000, 0x1_8000)])
    root = ET.fromstring(xml.split("\n", 2)[2])          # DOCTYPE を外して読む
    flash, ram = root.findall("memory")
    assert flash.get("type") == "flash" and flash.get("start") == "0x8000000" and flash.get("length") == "0x80000"
    assert flash.find("property").get("name") == "blocksize" and flash.find("property").text == "0x4000"
    assert ram.get("type") == "ram" and ram.find("property") is None


def test_flash_without_a_known_blocksize_is_not_given_a_guessed_one():
    # 消去の単位が分からない Flash に 1 KB 等を推さない。GDB の DTD は flash に blocksize が要るので、書けない rom として
    # 申告する（GDB は書き込みを断る。誤った単位で消して書くよりよい）
    xml = d.memory_map_xml([d.Region("flash", 0x0800_0000, 0x8_0000)])
    flash, = ET.fromstring(xml.split("\n", 2)[2]).findall("memory")
    assert flash.get("type") == "rom" and flash.find("property") is None and "0x400" not in xml


def test_gaps_are_filled_with_ram_so_gdb_can_read_peripherals():
    flash = d.Region("flash", 0x0800_0000, 0x8_0000, 0x4000)
    ram = d.Region("ram", 0x2000_0000, 0x1_8000)
    alias = d.Region("rom", 0x0800_1000, 0x1000)                 # 重なり: 先のものを残す
    assert d.fill_gaps([ram, flash, alias]) == [
        d.Region("ram", 0, 0x0800_0000), flash, d.Region("ram", 0x0808_0000, 0x2000_0000 - 0x0808_0000), ram,
        d.Region("ram", 0x2001_8000, (1 << 32) - 0x2001_8000)]
    assert d.fill_gaps([]) == [d.Region("ram", 0, 1 << 32)]


def test_threads_xml_escapes_names():
    root = ET.fromstring(d.threads_xml([{"id": 0x2000_0100, "name": "a<b", "extra": "running"}]))
    t = root.find("thread")
    assert t.get("id") == "20000100" and t.get("name") == "a<b" and t.text == "running"
