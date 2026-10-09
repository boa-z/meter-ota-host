"""使用真实 GNU cpio 和 mkenvimage 验证成包，不使用生产打包器替身。"""
import hashlib
import os
import shutil
import subprocess
from pathlib import Path
import pytest
from ota.client import Manifest, UpdateError, preflight
from ota.pack import pack
from ota.package import PackagePolicy

POLICY = PackagePolicy("d13x_os.itb", 4096)


@pytest.fixture
def native_tools():
    result = {key: os.environ.get("METER_OTA_" + key.upper(), key) for key in ("cpio", "mkenvimage")}
    if not all(shutil.which(value) for value in result.values()):
        pytest.skip("GNU cpio and mkenvimage are required; Linux CI installs both")
    return result


def test_native_pack_roundtrip_without_source_mutation(tmp_path, native_tools):
    source = tmp_path / "source.itb"
    data = bytes(range(256)) * 8 + b"tail"
    source.write_bytes(data)
    result = pack(source, tmp_path / "deliverable", "synthetic", "reference-board", "v2", POLICY, **native_tools)
    output = Path(result["directory"])
    manifest = Manifest.load(output / "ota.manifest.json")
    assert source.read_bytes() == data
    assert preflight(output / "ota.cpio", manifest, POLICY)["archive"]["os_size"] == 4096
    assert result["sha256"] == hashlib.sha256((output / "ota.cpio").read_bytes()).hexdigest()
    with (output / "ota.cpio").open("rb") as stream:
        verified = subprocess.run([native_tools["cpio"], "-i", "--only-verify-crc", "--quiet"],
                                  stdin=stream, capture_output=True, cwd=tmp_path, timeout=10)
    assert verified.returncode == 0, verified.stderr
    assert sorted(x.name for x in output.iterdir()) == ["ota.cpio", "ota.manifest.json", "package-report.json"]
    with pytest.raises(UpdateError, match="already exists"):
        pack(source, output, "synthetic", "reference-board", "v2", POLICY, **native_tools)


def test_failed_native_tool_publishes_no_package(tmp_path, native_tools):
    source = tmp_path / "source.itb"
    source.write_bytes(b"os")
    destination = tmp_path / "output"
    # 将真实 cpio 用作不兼容的元数据工具，验证进程失败传播和临时目录回收。
    with pytest.raises(UpdateError, match="mkenvimage failed"):
        pack(source, destination, "synthetic", "reference-board", "v2", POLICY,
             cpio=native_tools["cpio"], mkenvimage=native_tools["cpio"])
    assert not destination.exists()
    assert not list(tmp_path.glob("meter-ota-*"))


@pytest.mark.parametrize("length,version", [(0, "v2"), (4097, "v2"), (2, "v" * 32)])
def test_pack_rejects_bounds_before_starting_native_tools(tmp_path, length, version):
    source = tmp_path / "source.itb"
    source.write_bytes(b"x" * length)
    with pytest.raises(UpdateError):
        pack(source, tmp_path / "out", "synthetic", "reference-board", version, POLICY,
             cpio="missing-program", mkenvimage="missing-program")
    assert not (tmp_path / "out").exists()


def test_explicit_nand_alignment(tmp_path, native_tools):
    source = tmp_path / "source.itb"
    data = b"sample-fit" * 205
    source.write_bytes(data)
    result = pack(source, tmp_path / "out", "synthetic", "reference-board", "v2", POLICY,
                  pad_os_to=4096, **native_tools)
    assert result["archive"]["os_size"] == 4096
    assert source.read_bytes() == data
    with (tmp_path / "out/ota.cpio").open("rb") as stream:
        extracted = subprocess.run([native_tools["cpio"], "-i", "--to-stdout", "d13x_os.itb"],
                                   stdin=stream, capture_output=True, cwd=tmp_path, timeout=10)
    assert extracted.returncode == 0
    assert extracted.stdout == data + bytes([255]) * (4096 - len(data))


def test_default_pack_uses_nand_alignment(tmp_path, native_tools):
    source = tmp_path / "source.itb"
    source.write_bytes(b"fit" * 701)
    result = pack(source, tmp_path / "out", "synthetic", "reference-board", "v2", POLICY,
                  cpio=native_tools["cpio"], mkenvimage=native_tools["mkenvimage"])
    assert result["archive"]["os_size"] % 4096 == 0


def test_metadata_config_is_written_with_lf_only(tmp_path, monkeypatch):
    """ota-subimgs.cfg 必须逐字节为 LF：Windows 文本模式会混入 CR，导致包被检查器拒绝。"""
    import subprocess as real_subprocess
    import ota.pack as pack_module
    seen = {}

    def fake_run(command, **kwargs):
        seen["config"] = (Path(kwargs["cwd"]) / "ota-subimgs.cfg").read_bytes()
        return real_subprocess.CompletedProcess(command, 1, b"", b"stop after the config is written")

    monkeypatch.setattr(pack_module.subprocess, "run", fake_run)
    source = tmp_path / "source.itb"
    source.write_bytes(b"os")
    program = os.path.realpath(shutil.which("python") or shutil.which("python3"))
    with pytest.raises(UpdateError, match="mkenvimage failed"):
        pack(source, tmp_path / "out", "synthetic", "reference-board", "v2", POLICY,
             cpio=program, mkenvimage=program)
    assert seen["config"].count(bytes([13])) == 0
    assert seen["config"].startswith(b"[image]" + bytes([10]))
