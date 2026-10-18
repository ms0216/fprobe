import fprobe_gw


def test_package_exposes_version():
    assert isinstance(fprobe_gw.__version__, str)
    assert fprobe_gw.__version__
