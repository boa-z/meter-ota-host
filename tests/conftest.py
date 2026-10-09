"""把仓库根目录注册为包 ota，测试与挂载路径无关（tools/ota、独立检出目录名都可以）。"""
import importlib.util
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ota", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
package = importlib.util.module_from_spec(spec)
sys.modules["ota"] = package
spec.loader.exec_module(package)

from ota.package import inspector_path  # noqa: E402


def _inspector_available():
    try:
        inspector_path()
        return True
    except ValueError:
        return False


def pytest_collection_modifyitems(config, items):
    """校验器 meter-ota-inspect 由宿主工程用固件同一份解析器编译；没有时跳过依赖它的用例。"""
    if _inspector_available():
        return
    skip = pytest.mark.skip(reason="meter-ota-inspect not found; build it in the host project "
                                   "or set METER_OTA_INSPECTOR")
    for item in items:
        if "inspector" in item.keywords:
            item.add_marker(skip)


def pytest_configure(config):
    config.addinivalue_line("markers", "inspector: needs the meter-ota-inspect executable")
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")


@pytest.fixture(autouse=True)
def _skip_without_inspector(request, monkeypatch):
    """没有校验器时，用例在第一次调用校验器处跳过，而不是报失败。"""
    if _inspector_available():
        return

    def missing():
        pytest.skip("meter-ota-inspect not found; build it in the host project or set METER_OTA_INSPECTOR")
    import ota.package
    monkeypatch.setattr(ota.package, "inspector_path", missing)
    if hasattr(request.module, "inspector_path"):
        monkeypatch.setattr(request.module, "inspector_path", missing)
