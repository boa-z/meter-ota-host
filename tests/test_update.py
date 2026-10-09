"""一键升级流程：步骤顺序、等待维护模式、断点续做、失败不改动设备。"""
import hashlib
import json
import pytest
from package_fixture import package as make_package
from ota.client import Manifest, UpdateError
from ota.update import locate, run

CAPACITY = 4096


class Clock:
    """假时钟：sleep 只推进时间，测试不真正等待。"""

    def __init__(self):
        self.now = 0.0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Board:
    def __init__(self, manifest, **info):
        self.m = manifest
        self.calls = []
        self.received = bytearray()
        self.silent_polls = 0
        self.maintenance_after = 0
        self.grants = True  # 编程会话请求是否带来维护模式
        self.session_error = None
        self.uart_after = 0  # 第 N 次读取状态时，操作员在串口打开了维护模式
        self.polls = 0
        self.status = {"backend_supported": True, "maintenance": True, "product": manifest.product,
                       "hardware": manifest.hardware, "version": "v1", "state": "IDLE", "error": 0,
                       "os_file": "d13x_os.itb", "candidate_capacity": CAPACITY}
        self.status.update(info)

    def info(self):
        self.polls += 1
        if self.silent_polls > 0 and "reboot" in self.calls:
            self.silent_polls -= 1
            raise TimeoutError("no response while rebooting")
        status = dict(self.status)
        if self.uart_after and self.polls >= self.uart_after:
            status["maintenance"] = True
        if self.maintenance_after and self.polls <= self.maintenance_after:
            status["maintenance"] = False
        return status

    def programming(self):
        self.calls.append("session")
        if self.session_error:
            error, self.session_error = self.session_error, None  # 只拒绝第一次请求
            raise error
        if self.grants:
            self.status["maintenance"] = True

    def manifest(self, data):
        self.calls.append("manifest")

    def begin(self, size):
        self.calls.append("begin")
        return 258

    def block(self, sequence, data):
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
        self.status.update(version=self.m.version, state="IDLE", maintenance=False)


def write_package(directory, data, manifest):
    directory.mkdir()
    (directory / "ota.cpio").write_bytes(data)
    (directory / "ota.manifest.json").write_text(json.dumps(manifest.__dict__), encoding="utf-8")


@pytest.fixture
def case(tmp_path):
    data = make_package()
    manifest = Manifest("synthetic", "reference-board", "v2", len(data), hashlib.sha256(data).hexdigest())
    write_package(tmp_path / "pkg", data, manifest)
    return tmp_path / "pkg", manifest


def go(board, directory, **options):
    lines, events, time = [], [], Clock()
    package, manifest = locate(directory)
    options.setdefault("os_file", "d13x_os.itb")
    result = run(board, package, manifest, say=lines.append, emit=events.append,
                 clock=time.clock, sleep=time.sleep, **options)
    return result, lines, events


def test_happy_path_runs_every_step_in_order_with_chinese_progress(case):
    directory, manifest = case
    board = Board(manifest)
    result, lines, events = go(board, directory)
    assert result["event"] == "verified" and result["warnings"] == []
    assert board.calls == ["session", "manifest", "begin", "finish", "session", "activate", "reboot"]
    steps = [line.split("]")[0] for line in lines if line.startswith("[")]
    assert steps == ["[1/6", "[2/6", "[3/6", "[4/6", "[5/6", "[6/6"]
    assert any("写入 100%" in line for line in lines) and any("升级完成" in line for line in lines)
    assert bytes(board.received) == (directory / "ota.cpio").read_bytes()
    assert [e["event"] for e in events if e["event"] in ("package", "verified")] == ["package", "verified"]


def test_already_running_target_changes_nothing_unless_forced(case):
    directory, manifest = case
    board = Board(manifest, version="v2")
    result, lines, _ = go(board, directory)
    assert result["event"] == "up_to_date" and board.calls == []
    result, _, _ = go(board, directory, force=True)
    assert result["event"] == "verified" and "begin" in board.calls


def test_identity_mismatch_stops_before_any_request(case):
    directory, manifest = case
    board = Board(manifest, hardware="other-board")
    with pytest.raises(UpdateError, match="不匹配"):
        go(board, directory)
    assert board.calls == []


def test_waits_for_maintenance_and_tells_the_user_the_serial_command(case):
    directory, manifest = case
    board = Board(manifest)
    board.grants = False
    board.maintenance_after = 6
    result, lines, _ = go(board, directory)
    assert result["event"] == "verified"
    assert any("meter_update maintenance on" in line for line in lines)


def test_maintenance_is_requested_over_can_without_the_serial_port(case):
    directory, manifest = case
    board = Board(manifest, maintenance=False)
    result, lines, _ = go(board, directory)
    assert result["event"] == "verified" and board.calls[0] == "session"
    assert any("经 CAN 编程会话" in line for line in lines)
    assert not any("串口" in line for line in lines)


def test_rejected_session_falls_back_to_the_serial_prompt(case):
    directory, manifest = case
    board = Board(manifest, maintenance=False)
    board.session_error = RuntimeError("negative response 0x22")
    board.uart_after = 8
    result, lines, _ = go(board, directory)
    assert result["event"] == "verified"
    assert any("没有接受 CAN 编程会话" in line for line in lines)
    assert any("meter_update maintenance on" in line for line in lines)
    stuck = Board(manifest, maintenance=False)
    stuck.session_error = RuntimeError("negative response 0x22")
    with pytest.raises(UpdateError, match="维护模式"):
        go(stuck, directory, wait_maintenance=6)
    assert stuck.calls == ["session"]


def test_maintenance_hint_follows_the_endpoint_and_can_be_overridden(case, tmp_path):
    directory, manifest = case
    smoke_manifest = Manifest("lvgl-aic-smoke", manifest.hardware, manifest.version, manifest.size, manifest.sha256)
    write_package(tmp_path / "smoke", (directory / "ota.cpio").read_bytes(), smoke_manifest)
    smoke = Board(smoke_manifest)
    smoke.grants = False
    smoke.maintenance_after = 6
    _, lines, _ = go(smoke, tmp_path / "smoke")
    assert any("lv_aic_can_ota maintenance on" in line for line in lines)
    board = Board(manifest)
    board.grants = False
    board.maintenance_after = 6
    _, lines, _ = go(board, directory, maintenance_command="my_cmd on")
    assert any("my_cmd on" in line for line in lines)


def test_maintenance_timeout_leaves_the_device_untouched(case):
    directory, manifest = case
    board = Board(manifest, maintenance=False)
    board.grants = False
    with pytest.raises(UpdateError, match="维护模式"):
        go(board, directory, wait_maintenance=5)
    assert board.calls == ["session"]  # 只请求了会话，没有写任何东西


def test_declined_confirmation_cancels_before_writing(case):
    directory, manifest = case
    board = Board(manifest)
    seen = []
    result, lines, _ = go(board, directory, approve=lambda summary: seen.append(summary) or False)
    assert result["event"] == "cancelled" and board.calls == []
    assert "v1" in seen[0] and "v2" in seen[0] and any("已取消" in line for line in lines)


def test_resumes_from_a_validated_candidate_without_rewriting(case):
    directory, manifest = case
    board = Board(manifest, state="CANDIDATE_READY", target="v2", received=manifest.size)
    result, lines, _ = go(board, directory)
    assert result["event"] == "verified"
    assert "begin" not in board.calls and board.calls[-2:] == ["activate", "reboot"]
    assert any("跳过" in line for line in lines)


def test_resumes_after_activation_by_only_rebooting(case):
    directory, manifest = case
    board = Board(manifest, state="ACTIVATED", target="v2")
    result, _, _ = go(board, directory)
    assert result["event"] == "verified" and board.calls == ["session", "reboot"]


def test_foreign_session_is_not_taken_over(case):
    directory, manifest = case
    board = Board(manifest, state="RECEIVING", target="other")
    with pytest.raises(UpdateError, match="abort"):
        go(board, directory)
    assert "begin" not in board.calls


def test_silent_reboot_is_waited_out_and_a_stuck_one_times_out(case):
    directory, manifest = case
    board = Board(manifest)
    board.silent_polls = 5
    result, lines, _ = go(board, directory)
    assert result["event"] == "verified"
    stuck = Board(manifest)
    stuck.reboot = lambda: stuck.calls.append("reboot")  # 设备没有换成新版本
    with pytest.raises(UpdateError, match="回到 CAN 总线"):
        go(stuck, directory, boot_timeout=15)


def test_failed_post_check_is_an_error_and_open_maintenance_is_a_warning(case):
    directory, manifest = case
    board = Board(manifest)
    board.reboot = lambda: board.status.update(version="v2", state="IDLE", error=7)
    with pytest.raises(UpdateError, match="校验未通过"):
        go(board, directory)
    board = Board(manifest)
    board.reboot = lambda: board.status.update(version="v2", state="IDLE", maintenance=True)
    result, lines, _ = go(board, directory)
    assert result["warnings"] and any("注意" in line and "maintenance off" in line for line in lines)


def test_unsettled_trial_slot_is_confirmed(case):
    directory, manifest = case
    board = Board(manifest)
    board.reboot = lambda: board.status.update(version="v2", state="IDLE", maintenance=False,
                                               slot="B", next="B", trial=1)
    board.confirm = lambda: board.status.update(trial=0)
    result, lines, _ = go(board, directory)
    assert "confirmation" in result and any("试运行确认" in line for line in lines)


def test_cli_update_end_to_end_over_virtual_can(case, tmp_path, capsys):
    """真实传输栈（python-can + can-isotp + udsoncan）加一个模拟的 UDS 设备，走完整的 update 命令。"""
    import threading
    import time
    import uuid
    import can
    import isotp
    from ota.__main__ import main
    directory, manifest = case
    channel = "ota-update-" + str(uuid.uuid4())
    bus = can.Bus(interface="virtual", channel=channel)
    stack = isotp.CanStack(bus=bus, address=isotp.Address(isotp.AddressingMode.Normal_11bits, txid=0x7e8, rxid=0x7e0),
                           params={"stmin": 0, "blocksize": 8})
    status = {"backend_supported": True, "maintenance": False, "product": manifest.product,
              "hardware": manifest.hardware, "version": "v1", "state": "IDLE", "error": 0,
              "os_file": "d13x_os.itb", "candidate_capacity": CAPACITY}
    stop, errors, received = threading.Event(), [], bytearray()
    silent_until = [0.0]

    def respond(request):
        if request == bytes.fromhex("1002"):
            status["maintenance"] = True  # 编程会话请求维护模式，设备端无准入限制
            return bytes.fromhex("5002009604b0")
        if request == bytes.fromhex("22f180"):
            return bytes.fromhex("62f180") + json.dumps(status).encode()
        if request[:3] == bytes.fromhex("2ef180"):
            status["target"] = request[3 + 64:3 + 112].rstrip(bytes(1)).decode()
            return bytes.fromhex("6ef180")
        if request[0] == 0x34:
            return bytes.fromhex("742001") + bytes([2])
        if request[0] == 0x36:
            received.extend(request[2:])
            return bytes([0x76, request[1]])
        if request[0] == 0x37:
            status.update(state="CANDIDATE_READY", received=len(received))
            return bytes([0x77])
        if request == bytes.fromhex("3101f001"):
            status["state"] = "ACTIVATED"
            return bytes.fromhex("7101f001")
        if request == bytes.fromhex("1101"):
            status.update(version=status["target"], state="IDLE", maintenance=False)
            silent_until[0] = time.monotonic() + 1.0  # 重启期间不应答
            return bytes.fromhex("5101")
        raise AssertionError("unexpected request " + request.hex())

    def server():
        try:
            while not stop.is_set():
                request = stack.recv(block=True, timeout=0.05)
                if request is not None and time.monotonic() >= silent_until[0]:
                    stack.send(respond(bytes(request)))
        except BaseException as exc:
            errors.append(exc)

    stack.start()
    thread = threading.Thread(target=server)
    thread.start()
    try:
        code = main(["--interface", "virtual", "--channel", channel, "--evidence", str(tmp_path / "evidence"),
                     "update", str(directory), "--yes", "--boot-timeout", "30"])
    finally:
        stop.set()
        thread.join(timeout=2)
        stack.stop()
        bus.shutdown()
    output = capsys.readouterr().out
    assert not errors, errors
    assert code == 0, output
    assert "[6/6]" in output and "升级完成" in output and "取证目录" in output
    assert "经 CAN 编程会话" in output and "串口" not in output
    assert bytes(received) == (directory / "ota.cpio").read_bytes()
    events = [json.loads(line) for line in (tmp_path / "evidence/events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert events[-1]["event"] == "verified" and (tmp_path / "evidence/can.asc").stat().st_size > 0
    assert '{"event"' not in output  # 屏幕上只有中文进度，JSON 事件只写入取证日志
