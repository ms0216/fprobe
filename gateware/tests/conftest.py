import pytest


def pytest_addoption(parser):
    parser.addoption("--hil", action="store_true", default=False,
                     help="実機が必要なテスト（hil マーカー）を実行する")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--hil"):
        return
    skip = pytest.mark.skip(reason="実機テストは --hil 指定時のみ実行")
    for item in items:
        if "hil" in item.keywords:
            item.add_marker(skip)
