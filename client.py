"""有界升级客户端；包结构与候选安装仍必须由设备验证。"""
from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
import re
import time
from .package import inspect_stream


class UpdateError(RuntimeError):
    """预检、能力门禁或升级后置条件未满足。"""


@dataclass(frozen=True)
class Manifest:
    product: str
    hardware: str
    version: str
    size: int
    sha256: str

    def __post_init__(self):
        for name, width in (("product", 32), ("hardware", 32), ("version", 48)):
            value = getattr(self, name)
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.+-]+", value) or len(value) >= width:
                raise UpdateError(f"invalid {name}: use 1..{width - 1} ASCII identity characters")
        if type(self.size) is not int or not 0 < self.size <= 0xffffffff:
            raise UpdateError("invalid package size")
        if not isinstance(self.sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", self.sha256):
            raise UpdateError("sha256 must contain 64 lowercase hexadecimal characters")

    @classmethod
    def load(cls, path):
        with Path(path).open("rb") as stream:
            data = stream.read(4097)
        if len(data) > 4096:
            raise UpdateError("manifest exceeds 4096 bytes")
        try:
            return cls(**json.loads(data))
        except (ValueError, TypeError) as exc:
            raise UpdateError(f"invalid manifest: {exc}") from exc

    def encode(self):
        return (self.product.encode().ljust(32, b"\0") + self.hardware.encode().ljust(32, b"\0") +
                self.version.encode().ljust(48, b"\0") + self.size.to_bytes(4, "big") + bytes.fromhex(self.sha256))


def check_stream(stream, manifest, package_policy):
    digest = hashlib.sha256()
    size = 0
    while chunk := stream.read(65536):
        size += len(chunk)
        if size > manifest.size:
            raise UpdateError("package longer than manifest")
        digest.update(chunk)
    if size != manifest.size or digest.hexdigest() != manifest.sha256:
        raise UpdateError("package length or SHA256 mismatch")
    stream.seek(0)
    try:
        archive = inspect_stream(stream, manifest, package_policy)
    except (ValueError, OSError) as exc:
        raise UpdateError(str(exc)) from exc
    return {"product": manifest.product, "hardware": manifest.hardware, "version": manifest.version,
            "size": size, "sha256": digest.hexdigest(), "integrity": "PASS",
            "archive_validation": "HOST_PASS_DEVICE_REQUIRED", "archive": archive, "signature": "NOT_PROVIDED"}


def preflight(path, manifest, package_policy):
    with Path(path).open("rb") as stream:
        return check_stream(stream, manifest, package_policy)


def require_device(info, manifest=None):
    if info.get("backend_supported") is not True:
        raise UpdateError("device backend unavailable: " + str(info.get("backend_reason", "capability not advertised")))
    if info.get("maintenance") not in (True, 1):
        raise UpdateError("Product maintenance admission is required")
    if manifest and (info.get("product") != manifest.product or info.get("hardware") != manifest.hardware):
        raise UpdateError("device Product/Hardware mismatch")


def download(client, path, manifest, emit=lambda event: None, block_size=512, *, package_policy=None):
    if type(block_size) is not int or not 1 <= block_size <= 512:
        raise UpdateError("block size must be in 1..512")
    with Path(path).open("rb") as stream:
        checked = check_stream(stream, manifest, package_policy)
        before = client.info()
        require_device(before, manifest)
        if (before.get("os_file") != package_policy.os_file or
                type(before.get("candidate_capacity")) is not int or
                before["candidate_capacity"] < package_policy.candidate_capacity):
            raise UpdateError("device candidate partition differs from the inspected package policy")
        if before.get("state") not in ("IDLE", "FAILED", "ABORTED"):
            raise UpdateError("device already owns an update session")
        emit({"event": "preflight", "package": checked, "device": before})
        started = False
        begin = time.monotonic()
        try:
            client.programming()
            client.manifest(manifest.encode())
            started = True
            limit = client.begin(manifest.size)
            if type(limit) is not int or limit < 3:
                raise UpdateError("invalid device block-length limit")
            chunk_size = min(block_size, limit - 2)
            offset, sequence = 0, 1
            digest = hashlib.sha256()
            while offset < manifest.size:
                chunk = stream.read(min(chunk_size, manifest.size - offset))
                if not chunk:
                    raise UpdateError("package changed during transfer")
                client.block(sequence, chunk)
                digest.update(chunk)
                offset += len(chunk)
                sequence = (sequence + 1) & 255
                emit({"event": "progress", "received": offset, "total": manifest.size})
            if stream.read(1) or digest.hexdigest() != manifest.sha256:
                raise UpdateError("package changed during transfer")
            client.finish()
            after = client.info()
            if (after.get("state") != "CANDIDATE_READY" or after.get("target") != manifest.version or
                    after.get("received") != manifest.size or after.get("error") != 0):
                emit({"event": "verify_mismatch", "device": after,
                      "want": {"state": "CANDIDATE_READY", "target": manifest.version,
                               "received": manifest.size, "error": 0}})
                raise UpdateError("device did not validate the expected candidate")
            elapsed = time.monotonic() - begin
            result = {"event": "candidate", "device": after, "elapsed_s": elapsed,
                      "bytes_per_second": manifest.size / elapsed if elapsed else None}
            emit(result)
            return result
        except BaseException:
            if started:
                try:
                    client.abort()
                    emit({"event": "abort", "result": "requested"})
                except Exception as exc:
                    emit({"event": "abort", "result": "failed", "reason": str(exc)})
            raise


def activate(client, version, reboot=False):
    info = client.info()
    require_device(info)
    if info.get("target") != version or info.get("state") not in ("CANDIDATE_READY", "WAIT_DURABLE"):
        raise UpdateError("expected validated candidate is not present")
    client.programming()
    client.activate()
    result = client.info()
    if result.get("state") != "ACTIVATED" or result.get("target") != version or result.get("error") != 0:
        raise UpdateError("activation was not confirmed by the device")
    if reboot:
        client.reboot()
    return {"event": "activated", "device": result, "reboot_requested": reboot,
            "new_firmware_confirmation": "NOT_VERIFIED"}


def confirm(client, version):
    """确认试运行镜像：仅当设备报告目标版本且处于未确认试运行槽位时清除回退计数。"""
    info = client.info()
    if info.get("version") != version:
        raise UpdateError(f"device runs {info.get('version')!r}, expected {version!r}")
    if info.get("slot") not in ("A", "B") or info.get("slot") != info.get("next"):
        raise UpdateError("device boot slot is not settled")
    if info.get("trial") == 0:
        # SDK absystem_os.c 在 INIT_ENV 阶段已自动清除 upgrade_available；如实报告而非重复确认。
        return {"event": "confirmed", "mode": "sdk_auto_confirm", "device": info}
    if info.get("trial") != 1:
        raise UpdateError("device did not report its trial state")
    client.programming()
    client.confirm()
    after = client.info()
    if after.get("trial") != 0 or after.get("slot") != info.get("slot") or after.get("version") != version:
        raise UpdateError("confirmation was not persisted by the device")
    return {"event": "confirmed", "mode": "application", "before": info, "device": after}
