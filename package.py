"""调用与固件共用的只读校验器，不在 Python 重复实现 CPIO 解析。"""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess


@dataclass(frozen=True)
class PackagePolicy:
    os_file: str
    candidate_capacity: int

    def __post_init__(self):
        import re
        if not re.fullmatch(r"[A-Za-z0-9_.+-]{1,27}\.itb", self.os_file):
            raise ValueError("invalid OS member name")
        if type(self.candidate_capacity) is not int or not 4096 <= self.candidate_capacity <= 0xffffffff:
            raise ValueError("invalid candidate capacity")


def inspector_path():
    override = os.environ.get("METER_OTA_INSPECTOR")
    if override:
        path = Path(override)
        if not path.is_file():
            raise ValueError("METER_OTA_INSPECTOR executable does not exist")
        return path.resolve()
    root = Path(__file__).resolve().parents[2]
    suffix = ".exe" if os.name == "nt" else ""
    for directory in ("build-package", "build-framework-headless", "build-update", "build"):
        path = root / directory / ("meter-ota-inspect" + suffix)
        if path.is_file():
            return path
    raise ValueError("build meter-ota-inspect or set METER_OTA_INSPECTOR")


def inspect_stream(stream, manifest, policy):
    if not isinstance(policy, PackagePolicy):
        raise ValueError("explicit OS-only package policy is required")
    stream.seek(0)
    try:
        result = subprocess.run([str(inspector_path()), str(manifest.size), str(policy.candidate_capacity),
                                 policy.os_file, manifest.version], stdin=stream, capture_output=True,
                                timeout=120, check=False)
        if result.returncode != 0:
            detail = result.stdout.decode("utf-8", "replace").strip()
            raise ValueError("OS-only package rejected: " + (detail or "invalid policy or inspector failure"))
        report = json.loads(result.stdout)
        if report.get("result") != "OK" or report.get("received") != manifest.size:
            raise ValueError("invalid package inspector result")
        return {"profile": "aic-os-only-crc-cpio-v1", "os_file": policy.os_file,
                "candidate_capacity": policy.candidate_capacity, **report}
    finally:
        stream.seek(0)
