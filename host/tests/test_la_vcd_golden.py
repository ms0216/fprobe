"""VCD の出力のバイト列を固定する（一括の書き出しに直す前の実装で取った sha256 と比べる）。

numpy の道（一括）と numpy の無い道（変化点ごと）の両方で、乱数の場面（ずれ・欠け・前の値の無い先頭・同じ値の続き・
トリガの印・時刻の単位の丸め）のファイルが、直す前の実装と 1 バイトも違わないことを確かめる。
"""

import hashlib
import random
from array import array

import pytest

from fprobe import la, la_vcd, la_wave
from fprobe.la import Capture, Transitions, VcdWriter, write_vcd

PERIODS = (1e-8, 1.25e-9, 3.3e-9, 1e-6)
SHIFT_CHOICES = (0.0, 0.0, 0.5, 3.0, -1.25, 0.37, 7.0)


@pytest.fixture(params=["numpy", "python"])
def vcd_mode(request, monkeypatch):
    if request.param == "python":
        monkeypatch.setattr(la_wave, "_np", None)
        monkeypatch.setattr(la, "_np", None)
        monkeypatch.setattr(la_vcd, "_np", None)
    return request.param


def _transitions(rng, count, channels, start, dedup):
    values, starts, t, previous = [], [], start, None
    for _ in range(count):
        v = rng.getrandbits(channels) if rng.random() < 0.7 else rng.choice((0, (1 << channels) - 1, 1))
        if rng.random() < 0.1:
            v |= rng.getrandbits(16)                       # ch の外のビット（無視される）
        if dedup and v == previous:
            continue
        values.append(v)
        starts.append(t)
        previous = v
        t += rng.choice((1, 1, 2, 7, 150, rng.randint(1, 1 << 20)))
    return values, starts, t


def _stream_vcd(path, seed, skewed):
    rng = random.Random(seed)
    channels = rng.choice((1, 2, 3, 8, 16))
    period = rng.choice(PERIODS)
    shifts = [rng.choice(SHIFT_CHOICES) for _ in range(channels)] if skewed else ()
    unknown = sorted(rng.sample(range(channels), rng.randint(0, min(2, channels)))) if skewed else ()
    names = {0: "clk"} if rng.random() < 0.5 else None
    t = rng.randint(0, 1000)
    with VcdWriter(path, channels, period, names, shifts=shifts, unknown=unknown) as vcd:
        for _ in range(rng.randint(1, 12)):
            kind = rng.random()
            if kind < 0.15:
                vcd.gap(t)
                t += rng.choice((0, 1, 5, 1000))
                continue
            values, starts, t = _transitions(rng, rng.choice((0, 1, 2, 5, 40, 300)), channels, t, rng.random() < 0.7)
            vcd.write(array("H", [v & 0xFFFF for v in values]), array("Q", starts))


def _capture_vcd(path, seed, as_transitions):
    rng = random.Random(seed)
    channels = rng.choice((1, 2, 8, 10, 16))
    period = rng.choice(PERIODS)
    n = rng.randint(1, 400)
    samples = [rng.getrandbits(channels) if rng.random() < 0.3 else 0 for _ in range(n)]
    for i in range(1, n):
        if rng.random() < 0.6:
            samples[i] = samples[i - 1]
    trigger = rng.choice((0, n - 1, rng.randrange(n)))
    skew = tuple(rng.choice((0.0, 1.0, None, 2.5, -0.5)) for _ in range(channels)) if rng.random() < 0.6 else ()
    timebase_hz = rng.choice((None, 100e6, 1e9)) if skew else None
    cap = Capture(array("H", samples), trigger, 0, period, channels, channel_skew=skew)
    write_vcd(Transitions.from_capture(cap) if as_transitions else cap, path, {1: "data"}, timebase_hz=timebase_hz)


CASES = ([("stream", s, False) for s in range(24)] + [("stream", s, True) for s in range(100, 124)]
         + [("capture", s, False) for s in range(200, 216)] + [("capture", s, True) for s in range(300, 316)])

# 直す前の実装（086f4e9f）で取ったもの。write_vcd は直す前は文字の書き込み（Path.write_text）で、Windows では改行が
# CRLF になっていた（VcdWriter と Linux では LF）。今はどこでも LF なので、直す前の出力の CRLF を LF にしたものの sha256
GOLDEN = {
    'stream-0-False': '05d5cf3a6879317f13b7b1227db968f7c9f7c60f1bf464423c26cd4c1969b298',
    'stream-1-False': '7e5f3a4186c068a383483eb71702027e8c3fd883e70ee564649866b63e243fa5',
    'stream-2-False': '391ac8d5c28338d3e20e354a05c19911fc85072dca06598ac22dd97a8b924962',
    'stream-3-False': '5aadf9dab0b95a5144fd5938329baa96795821efed4480571fc805a1a4fc6e29',
    'stream-4-False': '573a387974c30e4d243ab0f3b55c645501357e994055ef513238161b352e0cad',
    'stream-5-False': 'e7481109fdc109338fb7ffaf51a979dd1b01674a9d10982fa77bbf4d2e35e3f9',
    'stream-6-False': '2f5963dee38fe9802c28f8073dceb4e3c66f2c58b5d3f7b5e8b945f7418ea45f',
    'stream-7-False': '18bef6bf5c9fd8ce19dabd229433c34ba194e13caf586d3caf8891c2d12067f9',
    'stream-8-False': '5af19b185ebef64989b522f291a004ce204dee65e7f485eead5f9a334008f026',
    'stream-9-False': '548106645206958a56a8b6497c31cf5514fd6854f201c4ad12a56b1563bfe46a',
    'stream-10-False': '2369b428aa7c5279445d55406c40a454eaef169bd6cc229523211d704dba0a22',
    'stream-11-False': '2f9215356bfd9120ae15d5c22affa972bbf1e4971ff040747877108b31639d98',
    'stream-12-False': '8a68e57c84f0900ced46989a285f810036316b45c1f6ec7dd3d86583f0da039c',
    'stream-13-False': 'b7b9eb0cd2d4e51ea59320694ba3ed953cd4ee0f31fe410d0b6e855b2e1bfeca',
    'stream-14-False': '9a59226aecd7bf6cc0e88ca2762dbb34ab873caaf54a4dbb4496a339b585ea3b',
    'stream-15-False': '66ea59296550f5b34e8e5b86eb865c28c96c2d36df4366b200a76ccbc7e54d08',
    'stream-16-False': 'da998c02c30261b0cb9897edb3d7eef04a33d03c7f55da175a5d3084ebc65951',
    'stream-17-False': 'a3680f739dbb2168e0005734ecc64839664682d6a94a9ecb3ea1ae26b37efecd',
    'stream-18-False': '6320be06577fe76735502cd43d75e120b495beb23e277b25d59034772451a707',
    'stream-19-False': 'ffd89f20e3cad658264f4e8355f685af365c4cf74595e9c26c9954add6bf410e',
    'stream-20-False': 'f746ed0bb0a57be77a3347e29d3b39e84be4e5e3482ec664e4a62f56cb2af03a',
    'stream-21-False': 'a72e2b7c3255fc5173678c362e209429a72b630d81763bcd7d28d55afb836a2e',
    'stream-22-False': 'b64ad99250681542210d39227ba7b3581efdd9c56db231a18744fa412d02ae77',
    'stream-23-False': '10eb7e9c682da388438b920388d2b517ede078b6eebae317c1d330c21a8e8f08',
    'stream-100-True': '587bc7d8c626f5d4eab6085d27dfd82ff177f4c0bb88eb68c1a240b215bdc002',
    'stream-101-True': 'c6fcfa20a4f4d00cfdc691dd74aff81f9cde5bf8bbc5a07efaffaa406159daa1',
    'stream-102-True': '32f9027183e449e826ea0fcf160a90b18d44869652969725761447a845b2bd15',
    'stream-103-True': 'a165727bd35f1f50efdbc1f17a07d6cf3f7729a463ecf824b7826f1980a0c2f8',
    'stream-104-True': 'f5e9d2fa78d45b73e4200579d72297a321996bb2cd4d97cf6afb9bacc1d57406',
    'stream-105-True': '897825ecda8735dd3b6a9cef38cc5ebf64ce9404bebe1e732428f39b40484add',
    'stream-106-True': 'f7a08192331ffd32593268c09f28ec8e5e1fa8b726ac8e78b19956b08fd1960d',
    'stream-107-True': 'ad7f260d315e3953616b065893277a3a5d0d0c211dc041598db377f4b27f98d1',
    'stream-108-True': '1d4c45bd16fe32c09397cc7a226d9575335f7987f72adb8a42a4fbfa4e36f8ac',
    'stream-109-True': '571c71e4ac02114ff4b74861ad97d73c679d5df6de0306d1676e69c46678d781',
    'stream-110-True': '079fcdc092f96579dda0360c7920de01fe9b4f460ec15f220dc0c364c7a10e3c',
    'stream-111-True': '3a318fa44986237dc0af0c591b0c543ce95f32b72a8edccd7be3eafa4ea619cc',
    'stream-112-True': '0225b2bfcd9111e2679571fd05ab59078a231666cd154ef9d66f370cda441ee5',
    'stream-113-True': '3fd894fcf132fad0bb9c3612e61d4f45ffd2a14fea85579acd447792c8ed89b5',
    'stream-114-True': '547621ed39edfc2a51ee6ab2cef305ea3a234ce69601297fd62000d90a7b9a20',
    'stream-115-True': '106b392bf2b35d00c970feef5b5c3ca717248366cd66f6021912dad78bcedfa2',
    'stream-116-True': '61faf7b05977857a1d52c69d6f48feaeadeaa0dd361d3768d763f17d93123d40',
    'stream-117-True': 'a5366dbb3179b52de500eca741c409403490406362c13b576a615847d2ebdd3a',
    'stream-118-True': '7130bdb91f3192ca618ade80056ae344706fe5ca16be9dc60a96ee2a3065d858',
    'stream-119-True': '7d9d7a26d514383fad1469619d256c986eb30f5dcf439dce3c95bd481fe88c7e',
    'stream-120-True': '1429cb4b15a8f174430db167bacc661fe903706fe80d97ae166332bc74d82fc3',
    'stream-121-True': '8ba8b39ce3cbd5f3ee740b95b63f513c73e7ef3a48358216acc7a2adff5733eb',
    'stream-122-True': '03fd53e1c9c0da43bee2860722e9bda2202196e9fd9ed9a2a947929b389af5c3',
    'stream-123-True': '3d3dd9cb5389d3ec4256362e9447ae35854687145b1290613ef9ef583c0beba4',
    'capture-200-False': '9c4dd58576d0df9dea5034357524a2ffc13cee7b806be9e0ed9e54613ccb21ca',
    'capture-201-False': '4afd55e53bd122ff36b8c457f1b3e3a012beeec3d3ce4a557b532df15c2dbd87',
    'capture-202-False': '90fd167d9273549a4f4a3193957b2b00457cbd14d74049b26c2bb813ecdb0cdc',
    'capture-203-False': '488005ba1548e66122e1aaf6759a78632c916ca267d6aabd93aa6db65aa005ed',
    'capture-204-False': '64bc38f07984a8d955d0e9c7aec872f541f4621af22a95911ca50def22a10cfd',
    'capture-205-False': '3e1d38063d212d98e54b8efe54cb2c45500a7b4366028d477c95712ef3942c4e',
    'capture-206-False': '7e881419624b63ac6098165998a3bdd73c01f77c180e9113504d8aa0d5706926',
    'capture-207-False': '61b4ea0b7b8ed2edaa15d7cd8bddcabadfade6324a32fdf449b7052237d2ad0b',
    'capture-208-False': '68c60b244f260ee117ab27bfa533efbe6009cdddee98d480cea0777e3e80cafd',
    'capture-209-False': '73b702d69a0e1cc9f17a30d2f6bafe6c0ce60d9b1f4eb51705861a9ed16c8b9c',
    'capture-210-False': '48c2f0fa341ff1f8091d76fd0e2d857544c1d263d74b8b9adbaa167d4a139c64',
    'capture-211-False': 'fb9561ea9a8860a55bd7fe1c32effcfba3e2937599f0d14d3d39169a089a58e7',
    'capture-212-False': '4ab260732c45f3c097a4d7a148408949efc04aea0a6e0f8ec34eb0eea1c0d30e',
    'capture-213-False': 'f64da58fe0e9625915f0a48d66041b30fb243e5921eb81a2456de7b7cf213198',
    'capture-214-False': 'b889d87ccf31ec1e4b4a13a1e027ce114ad252bd08c5f66e3cf035c91497c8ad',
    'capture-215-False': '99985750f12f89c8a58c6b6b9440f9386ec696ffb7f93fc5d61eacb0adf0c1a8',
    'capture-300-True': 'a55d5a05371e6deee099b826513960703bf8399ecab176ec79a5831b6d493662',
    'capture-301-True': '876690ab005435cad5cc37593dc02491ce729288a61f0faf1d0521abd089ca51',
    'capture-302-True': '39fc4d4f75307b29bf7e89caad7e04b98d62c8e0150637ae50612482cd4fbb3a',
    'capture-303-True': 'd4390f798fe088fd25a1e52094ac57d7e0b7aa011d99fa9ebc9b4efb84caebea',
    'capture-304-True': 'f36e424039797f950bbaba3a70a2f69d6b15770e36c012ec42024774cf72afa9',
    'capture-305-True': '2596b46160250f281c242701ff845513b8009e0e5d1f50078d4432a163238080',
    'capture-306-True': '773be75a8700883fcc492e022628c1102cc7342c9fc5af7c4b6aa1c299791120',
    'capture-307-True': '59f59f232a3b00b76dc3d4f7fde690458891b91034be92333a2c0334482cfad8',
    'capture-308-True': '5068873ff618e02966e428459e19cdda40a44a26e01c5030f9a71a10829c92fd',
    'capture-309-True': '6f76dd9808e55543c025794d84664223c01ef7f56b76542d9c4ca7ffe09e7e26',
    'capture-310-True': '30bf132c1c7961ee4ea0fa1e433e767e7cdb19f2fad8a70332b1649a6d2abfe9',
    'capture-311-True': '98d4ed7955fd8f6889d5ac22ce54621a4861f77db8f65ab7d57efac9a98e46de',
    'capture-312-True': '45649c3fb2519c36d27e82a3fdb339e087693fde69404ea9d80e5d00caca0d9d',
    'capture-313-True': '8fa958100b90ff69f4af382a39fecba8909b2d7dbebbc70afe80f7e1927c48e1',
    'capture-314-True': 'bcec6a2c35c9ddadec53eaf2cdf2e0e27c5358d50c26db5b13577ec7a807af03',
    'capture-315-True': '7a645c94f602c5538c3eae8b8afe834e6e218490184a73dd804463b2cd0be250',
}


def _make(path, case):
    kind, seed, flag = case
    (_stream_vcd if kind == "stream" else _capture_vcd)(path, seed, flag)


@pytest.mark.parametrize("case", CASES, ids=lambda c: f"{c[0]}-{c[1]}-{c[2]}")
def test_vcd_bytes_are_those_of_the_reference_implementation(case, tmp_path, vcd_mode):
    path = tmp_path / "x.vcd"
    _make(path, case)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert digest == GOLDEN[f"{case[0]}-{case[1]}-{case[2]}"]


@pytest.mark.parametrize("piece, uniform, workers", [(3, 2, 4), (1, 1, 1), (5, 1000, 3), (1 << 16, 2, 1)])
@pytest.mark.parametrize("case", CASES[::3], ids=lambda c: f"{c[0]}-{c[1]}-{c[2]}")
def test_the_bulk_path_is_the_same_whatever_its_pieces_and_threads(case, piece, uniform, workers, tmp_path,
                                                                    monkeypatch):
    # 描くかたまりの大きさ（境目の「前の位置」）・同じ形の続きの扱い・スレッドの数（欠けの行と描きかけの順）を変えても同じ
    pytest.importorskip("numpy")
    monkeypatch.setattr(la_vcd, "_PIECE", piece)
    monkeypatch.setattr(la_vcd, "_UNIFORM_MIN", uniform)
    monkeypatch.setattr(la_vcd, "default_workers", lambda: workers)
    path = tmp_path / "x.vcd"
    _make(path, case)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == GOLDEN[f"{case[0]}-{case[1]}-{case[2]}"]


@pytest.mark.parametrize("workers", [1, 4])
def test_write_copies_its_inputs_before_it_returns(workers, tmp_path):
    # 描くのは後で別のスレッドで: 渡した配列を write の後に書き換えても、書いた中身は渡したときのもの
    np = pytest.importorskip("numpy")
    values, starts = np.array([1, 2, 3], dtype=np.uint16), np.array([0, 5, 9], dtype=np.uint64)
    with VcdWriter(tmp_path / "a.vcd", 2, 1e-8, workers=workers) as vcd:
        vcd.write(values, starts)
        values[:] = 0
        starts[:] = 7
    with VcdWriter(tmp_path / "b.vcd", 2, 1e-8, workers=1) as vcd:
        vcd.write(array("H", [1, 2, 3]), array("Q", [0, 5, 9]))
    assert (tmp_path / "a.vcd").read_bytes() == (tmp_path / "b.vcd").read_bytes()


def test_an_error_while_rendering_reaches_the_caller(tmp_path, monkeypatch):
    pytest.importorskip("numpy")

    def broken(*args):
        raise RuntimeError("render failed")
    monkeypatch.setattr(la_vcd, "_render", broken)
    vcd = VcdWriter(tmp_path / "e.vcd", 2, 1e-8, workers=3)
    vcd.write(array("H", [1, 2]), array("Q", [0, 5]))
    with pytest.raises(RuntimeError, match="render failed"):
        vcd.close()


def _golden() -> dict:
    """GOLDEN を作る（直す前の実装で 1 回だけ走らせた）。"""
    import tempfile
    from pathlib import Path
    out = {}
    with tempfile.TemporaryDirectory() as d:
        for case in CASES:
            path = Path(d) / "x.vcd"
            _make(path, case)
            out[f"{case[0]}-{case[1]}-{case[2]}"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


if __name__ == "__main__":
    for k, v in _golden().items():
        print(f"    {k!r}: {v!r},")
