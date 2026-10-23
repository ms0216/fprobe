"""記録の VCD の書き出しに FPGA の観測の信号（記録の信号の定義で。定義の無い組は 16 ビットの組の値）。"""

from array import array

from fprobe.la import Transitions
from fprobe.recording import Recorder, Recording

HZ = 100_000_000                                     # 1 刻み = 10 ns
DEFS = {"groups": {"0": [{"name": "mark", "lsb": 0, "width": 1},
                         {"name": "count", "lsb": 8, "width": 8, "format": "dec"}]}}


def test_fpga_signals_are_written_beside_the_waveform(tmp_path):
    rec = Recorder(tmp_path / "r", timebase_hz=HZ, meta={"fpga.signals": DEFS})
    rec.add_waveform(Transitions(array("H", [0, 1]), array("Q", [0, 50]), 100, 0, 0, 1e-8, 1), 0)
    rec.add("fpga", "value", 20, a=0, b=0x0301)
    rec.add("fpga", "value", 80, a=0, b=0x0400)
    rec.add("fpga", "value", 30, a=2, b=0xBEEF)                                  # 定義の無い組
    rec.add("fpga", "gap", 90, a=5)
    rec.flush()
    vcd = tmp_path / "out.vcd"
    Recording.open(rec.directory).export_vcd(vcd)
    text = vcd.read_text(encoding="ascii")
    head, body = text.split("$enddefinitions $end")
    ids = {line.split()[4]: line.split()[3] for line in head.splitlines() if line.startswith("$var")}
    assert {"ch0", "fpga_0_mark", "fpga_0_count", "fpga_2"} <= set(ids)
    assert "$var wire 8 " + ids["fpga_0_count"] + " fpga_0_count $end" in head
    assert "$var wire 16 " + ids["fpga_2"] + " fpga_2 $end" in head
    blocks: dict[int, list[str]] = {}                                           # 時刻の行（#数字）ごとの変化
    for line in body.strip().splitlines():
        if line.startswith("#") and line[1:].isdigit():
            current = blocks.setdefault(int(line[1:]), [])
        else:
            current.append(line)
    times = list(blocks)
    assert times == sorted(times) and times == [0, 200, 300, 500, 800, 900]      # ns（波形と信号が時刻の順に交じる）
    assert f"x{ids['fpga_0_mark']}" in blocks[900]                               # 溢れの切れ目: 信号は不明
    after = blocks.__getitem__
    assert f"1{ids['fpga_0_mark']}" in after(200) and f"b00000011 {ids['fpga_0_count']}" in after(200)
    assert f"b1011111011101111 {ids['fpga_2']}" in after(300)
    assert f"1{ids['ch0']}" in after(500)
    assert f"0{ids['fpga_0_mark']}" in after(800) and f"b00000100 {ids['fpga_0_count']}" in after(800)


def test_signal_names_become_unique_ascii_identifiers(tmp_path):
    # 日本語の名前・記号の名前でも書ける（ASCII の VCD）。置き換えて同じになる名前は番号で分ける
    defs = {"groups": {"0": [{"name": "状態", "lsb": 0, "width": 2}, {"name": "a-b", "lsb": 2, "width": 1},
                             {"name": "a_b", "lsb": 3, "width": 1}]}}
    rec = Recorder(tmp_path / "r", timebase_hz=HZ, meta={"fpga.signals": defs})
    rec.add("fpga", "value", 10, a=0, b=0b1011)
    rec.flush()
    vcd = tmp_path / "out.vcd"
    Recording.open(rec.directory).export_vcd(vcd)
    names = [line.split()[4] for line in vcd.read_text(encoding="ascii").splitlines() if line.startswith("$var")]
    assert len(names) == len(set(names)) == 3 and all(n.isascii() for n in names)
    assert "fpga_0_a_b" in names and "fpga_0_a_b_2" in names


def test_unobserved_signals_become_x(tmp_path):
    defs = {"groups": {"0": [{"name": "a", "lsb": 0, "width": 1}], "1": [{"name": "b", "lsb": 0, "width": 4}]}}
    rec = Recorder(tmp_path / "r", timebase_hz=HZ, meta={"fpga.signals": defs})
    rec.add("fpga", "value", 10, a=0, b=1)
    rec.add("fpga", "group", 50, a=1)
    rec.add("fpga", "value", 50, a=1, b=3)
    rec.flush()
    vcd = tmp_path / "out.vcd"
    Recording.open(rec.directory).export_vcd(vcd)
    head, body = vcd.read_text(encoding="ascii").split("$enddefinitions $end")
    ids = {line.split()[4]: line.split()[3] for line in head.splitlines() if line.startswith("$var")}
    assert f"x{ids['fpga_0_a']}" in body.split("#500")[1]                     # 組 1 に切り替えた: 組 0 の a は不明
