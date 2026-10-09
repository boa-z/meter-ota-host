"""上位机升级核心和真实第三方传输栈的回归。"""
import hashlib
import json
import threading
import uuid
from pathlib import Path
import pytest
from package_fixture import package as make_package
from ota.package import PackagePolicy
from ota.client import Manifest, UpdateError, activate, confirm, download, preflight


POLICY = PackagePolicy("d13x_os.itb", 4096)


class Device:
    def __init__(self, manifest):
        self.m = manifest
        self.calls = []
        self.received = bytearray()
        self.status = {"backend_supported": True, "maintenance": True, "product": manifest.product,
                       "hardware": manifest.hardware, "state": "IDLE", "error": 0,
                       "os_file": POLICY.os_file, "candidate_capacity": POLICY.candidate_capacity}
        self.fail_block = False

    def info(self):
        return dict(self.status)

    def programming(self):
        self.calls.append("session")

    def manifest(self, data):
        self.calls.append("manifest")
        assert len(data) == 148
        assert data[112:116] == self.m.size.to_bytes(4, "big")
        assert data[116:] == bytes.fromhex(self.m.sha256)

    def begin(self, size):
        self.calls.append("begin")
        assert size == self.m.size
        return 3

    def block(self, sequence, data):
        assert sequence == (len(self.received) + 1) & 255
        if self.fail_block:
            raise TimeoutError("no transfer response")
        self.received.extend(data)

    def finish(self):
        self.calls.append("finish")
        self.status.update(state="CANDIDATE_READY", target=self.m.version, received=len(self.received))

    def abort(self):
        self.calls.append("abort")

    def activate(self):
        self.calls.append("activate")
        self.status["state"] = "ACTIVATED"

    def reboot(self):
        self.calls.append("reboot")


@pytest.fixture
def package(tmp_path):
    data = make_package()
    path = tmp_path / "ota.cpio"
    path.write_bytes(data)
    manifest = Manifest("synthetic", "reference-board", "v2", len(data), hashlib.sha256(data).hexdigest())
    return path, manifest


def test_download_wrap_and_explicit_activation(package):
    path, m = package
    device = Device(m)
    events = []
    result = download(device, path, m, events.append, package_policy=POLICY)
    assert device.received == path.read_bytes()
    assert result["device"]["state"] == "CANDIDATE_READY"
    assert "activate" not in device.calls and "reboot" not in device.calls
    assert len([x for x in events if x["event"] == "progress"]) == m.size
    assert activate(device, "v2")["new_firmware_confirmation"] == "NOT_VERIFIED"
    assert "reboot" not in device.calls


@pytest.mark.parametrize("field,value", [("product", "wrong"), ("hardware", "wrong"),
    ("backend_supported", False), ("backend_supported", None), ("maintenance", False), ("state", "DOWNLOADING"),
    ("os_file", "other.itb"), ("os_file", None), ("candidate_capacity", 4095),
    ("candidate_capacity", True), ("candidate_capacity", None)])
def test_admission_before_any_mutation(package, field, value):
    path, m = package
    d = Device(m)
    d.status[field] = value
    with pytest.raises(UpdateError):
        download(d, path, m, package_policy=POLICY)
    assert not d.calls


@pytest.mark.parametrize("suffix", [b"extra", b""])
def test_corruption_before_bus_requests(package, suffix):
    path, m = package
    path.write_bytes(b"x" * m.size + suffix)
    d = Device(m)
    with pytest.raises(UpdateError):
        download(d, path, m, package_policy=POLICY)
    assert not d.calls


def test_timeout_requests_abort(package):
    path, m = package
    d = Device(m)
    d.fail_block = True
    with pytest.raises(TimeoutError):
        download(d, path, m, package_policy=POLICY)
    assert d.calls[-1] == "abort" and "finish" not in d.calls


def test_changed_package_aborts_before_verification(package):
    path, m = package
    d = Device(m)
    original = d.begin
    def changed(size):
        path.write_bytes(b"x" * m.size)
        return original(size)
    d.begin = changed
    with pytest.raises(UpdateError, match="changed"):
        download(d, path, m, package_policy=POLICY)
    assert d.calls[-1] == "abort" and "finish" not in d.calls


def test_wrong_activation_target_and_missing_candidate(package):
    _, m = package
    d = Device(m)
    with pytest.raises(UpdateError):
        activate(d, "v2", True)
    assert not d.calls
    d.status.update(state="CANDIDATE_READY", target="v3")
    with pytest.raises(UpdateError):
        activate(d, "v2", True)
    assert not d.calls


@pytest.mark.parametrize("field,value", [("version", ""), ("product", "a" * 32),
    ("hardware", "board\x00other"), ("sha256", "g" * 64), ("size", True), ("size", 0), ("size", 2**32)])
def test_manifest_contract(package, field, value):
    _, m = package
    fields = dict(m.__dict__)
    fields[field] = value
    with pytest.raises(UpdateError):
        Manifest(**fields)


def test_preflight_still_requires_device_validation(package):
    path, m = package
    result = preflight(path, m, POLICY)
    assert result["integrity"] == "PASS"
    assert result["archive_validation"] == "HOST_PASS_DEVICE_REQUIRED"
    assert result["signature"] == "NOT_PROVIDED"


def test_virtual_can_roundtrip_and_evidence(tmp_path):
    import can
    import isotp
    from ota.transport import connect
    channel = "ota-" + str(uuid.uuid4())
    bus = can.Bus(interface="virtual", channel=channel)
    stack = isotp.CanStack(bus=bus, address=isotp.Address(isotp.AddressingMode.Normal_11bits,
                         txid=0x7e8, rxid=0x7e0), params={"stmin": 5, "blocksize": 8})
    stop = threading.Event()
    errors = []
    expected = {"product": "synthetic", "backend_supported": False,
                "backend_reason": "fixture" * 90, "state": "IDLE"}
    def responder():
        try:
            while not stop.is_set():
                payload = stack.recv(block=True, timeout=0.1)
                if payload is not None:
                    if payload == bytes.fromhex("1002"):
                        stack.send(bytes.fromhex("5002009604b0"))
                        continue
                    assert payload == bytes.fromhex("22f180")
                    stack.send(bytes.fromhex("62f180") + json.dumps(expected).encode())
        except BaseException as exc:
            errors.append(exc)
    stack.start()
    thread = threading.Thread(target=responder)
    thread.start()
    log = tmp_path / "can.asc"
    try:
        with connect(channel, "virtual", 500000, log, periodic=[(can.Message(arbitration_id=0x123, is_extended_id=False, data=[1]), .05)]) as client:
            client.programming()
            assert client.info() == expected
    finally:
        stop.set()
        thread.join(timeout=2)
        stack.stop()
        bus.shutdown()
    assert not thread.is_alive() and not errors
    with can.ASCReader(log) as reader:
        frames = list(reader)
    flow = [x for x in frames if x.arbitration_id == 0x7e0 and x.data and x.data[0] == 0x30]
    assert flow and all(x.data[1:3] == bytes([8, 0]) for x in flow)
    assert any(x.arbitration_id == 0x7e0 and not x.is_rx for x in frames)
    assert any(x.arbitration_id == 0x7e8 and x.is_rx for x in frames)


    assert any(x.arbitration_id == 0x123 and not x.is_rx for x in frames)
    assert max(x.timestamp for x in frames) - min(x.timestamp for x in frames) < 10


class Trial:
    """试运行确认只对报告槽位与试运行状态的端点有意义；其余端点必须如实失败而不是假装确认。"""

    def __init__(self, **info):
        self.status = {"version": "v2", "slot": "B", "next": "B", "trial": 1}
        self.status.update(info)
        self.calls = []

    def info(self):
        return dict(self.status)

    def programming(self):
        self.calls.append("session")

    def confirm(self):
        self.calls.append("confirm")
        self.status["trial"] = 0


def test_confirm_clears_an_unconfirmed_trial():
    device = Trial()
    result = confirm(device, "v2")
    assert result["mode"] == "application" and device.calls == ["session", "confirm"]


def test_confirm_reports_sdk_auto_confirm_without_a_routine_call():
    device = Trial(trial=0)
    assert confirm(device, "v2")["mode"] == "sdk_auto_confirm" and device.calls == []


@pytest.mark.parametrize("info", [{"version": "other"}, {"slot": "A"}, {"slot": None, "next": None}, {"trial": 2}])
def test_confirm_refuses_an_unsettled_or_wrong_device(info):
    device = Trial(**info)
    with pytest.raises(UpdateError):
        confirm(device, "v2")
    assert device.calls == []


def test_endpoint_without_trial_state_cannot_be_confirmed():
    """框架固件的端点不报告槽位/试运行状态：confirm 必须明确拒绝。"""
    class Plain(Trial):
        def __init__(self):
            super().__init__()
            self.status = {"version": "v2"}
    device = Plain()
    with pytest.raises(UpdateError):
        confirm(device, "v2")
    assert device.calls == []

