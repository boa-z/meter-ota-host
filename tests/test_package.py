"""校验器使用与固件一致的 C 实现，测试不依赖 SDK 修改。"""
import hashlib
import json
import subprocess
import pytest
from package_fixture import entry, package
from ota.client import Manifest, UpdateError, preflight
from ota.package import PackagePolicy, inspector_path

POLICY = PackagePolicy("d13x_os.itb", 4096)


def inspect(data, *, chunk=512, capacity=4096, version="v2"):
    result = subprocess.run([str(inspector_path()), str(len(data)), str(capacity), POLICY.os_file, version, str(chunk)],
                            input=data, capture_output=True, timeout=5)
    return result.returncode, json.loads(result.stdout) if result.stdout else {}


@pytest.mark.parametrize("chunk", [1, 2, 3, 4, 7, 8, 109, 110, 111, 127, 128, 255, 256, 511, 512])
def test_arbitrary_input_boundaries(chunk):
    code, report = inspect(package(), chunk=chunk)
    assert code == 0 and report["result"] == "OK"
    assert report["os_size"] == 260 and report["guard_bytes"] < 1024


@pytest.mark.parametrize("data", [
    package(mapping="env"), package(mapping="os_r"), package(mapping="blk_data"),
    package(os_name="../os.itb"), package(os_name="rodata.fatfs"),
    package(os_data=b""), package(os_mode=0o120777), package(os_links=2),
    package(version="wrong"), package(declared_size=1), package(declared_size=2**32),
    package(extra=[entry("d13x_os.itb", b"second")]),
    package(extra=[entry("data.fatfs", b"data")]), package(trailer=False),
    package(info_override=bytes(512)),
], ids=["env", "active-slot", "data-map", "traversal", "rodata", "empty-os", "symlink", "hardlink",
        "version", "wrong-size", "size-overflow", "duplicate", "extra-member", "missing-trailer", "bad-metadata"])
def test_reject_unsafe_structures(data):
    code, _ = inspect(data)
    assert code != 0


@pytest.mark.parametrize("offset", [0, 6, 94, 109, 110, 122, 200, 800, -1])
def test_modified_bytes_rejected(offset):
    data = bytearray(package())
    if offset == 109:
        data[offset] = ord("0") if data[offset] != ord("0") else ord("1")
    else:
        data[offset] ^= 0x20
    assert inspect(data)[0] != 0


def test_capacity_checks_flash_alignment():
    data = package(os_data=b"x" * 4097)
    assert inspect(data, capacity=4096)[0] != 0
    assert inspect(data, capacity=4097)[0] != 0
    assert inspect(data, capacity=8192)[0] == 0


def test_truncation_and_excess_zero_tail():
    data = package()
    assert inspect(data[:-512])[0] != 0
    assert inspect(data + bytes(512))[0] != 0


def test_matching_hash_does_not_admit_random_bytes(tmp_path):
    data = b"x" * 1536
    path = tmp_path / "bad.cpio"
    path.write_bytes(data)
    m = Manifest("synthetic", "reference-board", "v2", len(data), hashlib.sha256(data).hexdigest())
    with pytest.raises(UpdateError, match="package rejected"):
        preflight(path, m, POLICY)


def test_invalid_hex_and_oversized_name():
    data = bytearray(package())
    data[94:102] = b"ffffffff"
    assert inspect(data)[0] != 0
    data[94:102] = b"0000000g"
    assert inspect(data)[0] != 0
