"""调用成熟打包工具生成受限包，不改写 SDK 配置或已经生成的归档。"""
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from .client import Manifest, UpdateError, preflight
from .package import PackagePolicy


def _tool(value):
    found = shutil.which(str(value))
    if not found:
        raise UpdateError(f"packaging tool not found: {value}")
    return str(Path(found).resolve())


def pack(os_image, destination, product, hardware, version, policy, *, cpio="cpio", mkenvimage="mkenvimage", pad_os_to=4096):
    """固定工作副本、两遍成包确定长度，最后校验后才发布结果。"""
    if not isinstance(policy, PackagePolicy):
        raise UpdateError("explicit package policy is required")
    Manifest(product, hardware, version, 1, "0" * 64)
    if len(version) >= 32:
        raise UpdateError("native package version must fit 31 ASCII characters")
    if type(pad_os_to) is not int or pad_os_to not in (1, 4096):
        raise UpdateError("OS alignment must be 1 or 4096")
    source = Path(os_image).resolve(strict=True)
    destination = Path(destination).absolute()
    if not source.is_file() or source.stat().st_size == 0:
        raise UpdateError("OS image must be a nonempty regular file")
    if source.stat().st_size > policy.candidate_capacity - policy.candidate_capacity % 4096:
        raise UpdateError("OS image exceeds aligned candidate capacity")
    if destination.exists():
        raise UpdateError("package output directory already exists")
    programs = {"cpio": _tool(cpio), "mkenvimage": _tool(mkenvimage)}
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="meter-ota-", dir=destination.parent) as temporary:
        work = Path(temporary)
        shutil.copyfile(source, work / policy.os_file)
        # 仅填充临时 OS 副本；填充值计入 CPIO 校验及完整包 SHA256。
        padding = -source.stat().st_size % pad_os_to
        with (work / policy.os_file).open("ab") as image:
            image.write(b"\xff" * padding)
        # SDK 元数据固定为 512 字节；先确定归档长度，再重新生成元数据和归档。
        def build(size):
            config = chr(10).join(("[image]", f'size = "{size}";', f'version = "{version}";',
                                   "", "[file]", "ota_info.bin:file;", f"{policy.os_file}:os;", ""))
            # 二进制换行：文本模式在 Windows 会把 LF 写成 CRLF，CR 混入值后元数据与检查器逐字节比对失败。
            with (work / "ota-subimgs.cfg").open("w", encoding="ascii", newline=chr(10)) as handle:
                handle.write(config)
            result = subprocess.run([programs["mkenvimage"], "-s", "512", "-o", "ota_info.bin", "ota-subimgs.cfg"],
                                    cwd=work, capture_output=True, timeout=30)
            if result.returncode:
                raise UpdateError("mkenvimage failed: " + result.stderr.decode("utf-8", "replace"))
            with (work / "ota.cpio").open("wb") as output:
                result = subprocess.run([programs["cpio"], "-o", "-H", "crc", "--quiet"],
                                        input=("ota_info.bin" + chr(10) + policy.os_file + chr(10)).encode("ascii"),
                                        stdout=output, stderr=subprocess.PIPE, cwd=work, timeout=120)
            if result.returncode:
                raise UpdateError("cpio failed: " + result.stderr.decode("utf-8", "replace"))
            return (work / "ota.cpio").stat().st_size

        size = build(1)
        if build(size) != size:
            raise UpdateError("package length changed between passes")
        digest = hashlib.sha256()
        with (work / "ota.cpio").open("rb") as stream:
            while block := stream.read(65536):
                digest.update(block)
        manifest = Manifest(product, hardware, version, size, digest.hexdigest())
        result = preflight(work / "ota.cpio", manifest, policy)
        (work / "ota.manifest.json").write_text(json.dumps(asdict(manifest), indent=2) + chr(10), encoding="utf-8")
        (work / "package-report.json").write_text(json.dumps(result, indent=2) + chr(10), encoding="utf-8")
        # 只发布完成预检的交付文件；保留源镜像，不向 SDK 写入任何文件。
        destination.mkdir(exist_ok=False)
        for name in ("ota.cpio", "ota.manifest.json", "package-report.json"):
            shutil.copyfile(work / name, destination / name)
    return {"event": "package", "directory": str(destination), **result}
