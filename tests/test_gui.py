"""升级界面：后台任务（无界面）与 Tk 界面各一层，设备用假板。"""
from contextlib import contextmanager
import json
import time
import pytest
from test_update import Board, case  # noqa: F401  (case 是 fixture)
from ota.client import UpdateError
from ota.gui.controller import Job, Settings, summarize_package


def fake_connect(board):
    @contextmanager
    def connect(channel, interface, bitrate, log):
        log.write_text("asc", encoding="utf-8")
        yield board
    return connect


def drain(job, timeout=30):
    assert job.wait(timeout), "job did not finish"
    messages = []
    while not job.messages.empty():
        messages.append(job.messages.get_nowait())
    return messages


def result_of(messages):
    done = [value for kind, value in messages if kind == "done"]
    assert len(done) == 1
    return done[0]


def new_job(board, directory, tmp_path, kind="update", **kwargs):
    return Job(kind, Settings(), directory, connect=fake_connect(board), evidence_root=tmp_path / "evidence", **kwargs)


def test_package_summary_reports_what_is_wrong_in_chinese(case, tmp_path):
    directory, manifest = case
    assert summarize_package(directory)["version"] == "v2"
    assert summarize_package(directory / "ota.cpio")["size"] == manifest.size
    with pytest.raises(UpdateError, match="找不到升级包"):
        summarize_package(tmp_path / "nothing")
    (directory / "ota.manifest.json").unlink()
    with pytest.raises(UpdateError, match="没有 ota.manifest.json"):
        summarize_package(directory)


def test_package_summary_detects_a_truncated_file(case):
    directory, manifest = case
    (directory / "ota.cpio").write_bytes((directory / "ota.cpio").read_bytes()[:-1])
    with pytest.raises(UpdateError, match="不符"):
        summarize_package(directory)


@pytest.mark.inspector  # 升级在后台线程调用校验器，跳过必须在收集时决定
def test_update_job_streams_progress_and_keeps_evidence(case, tmp_path):
    directory, manifest = case
    board = Board(manifest)
    job = new_job(board, directory, tmp_path, ask=lambda summary: True)
    job.start()
    messages = drain(job)
    result = result_of(messages)
    assert result["ok"] and result["summary"] == "升级完成"
    lines = [value for kind, value in messages if kind == "line"]
    assert any("[6/6]" in line for line in lines)
    progress = [value for kind, value in messages if kind == "progress"]
    assert progress and progress[-1] == 100 and progress == sorted(progress)
    events = [json.loads(line) for line in (job.evidence / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert events[-1]["event"] == "gui_result" and events[-1]["ok"] is True
    assert (job.evidence / "can.asc").is_file()


def test_probe_job_only_reads(case, tmp_path):
    directory, manifest = case
    board = Board(manifest)
    job = new_job(board, None, tmp_path, kind="probe")
    job.start()
    messages = drain(job)
    assert result_of(messages)["ok"] and board.calls == []
    assert any("synthetic" in value for kind, value in messages if kind == "line")


@pytest.mark.inspector  # 升级在后台线程调用校验器，跳过必须在收集时决定
def test_declined_confirmation_changes_nothing(case, tmp_path):
    directory, manifest = case
    board = Board(manifest)
    job = new_job(board, directory, tmp_path, ask=lambda summary: False)
    job.start()
    result = result_of(drain(job))
    assert not result["ok"] and "没有被改动" in result["summary"] and board.calls == []


@pytest.mark.inspector  # 升级在后台线程调用校验器，跳过必须在收集时决定
def test_cancel_during_the_maintenance_wait_leaves_the_device_alone(case, tmp_path):
    directory, manifest = case
    board = Board(manifest, maintenance=False)
    board.grants = False
    job = new_job(board, directory, tmp_path, ask=lambda summary: True)
    job.start()
    time.sleep(0.6)
    job.cancel()
    result = result_of(drain(job))
    assert not result["ok"] and "已取消" in result["summary"]
    assert board.calls == ["session"]


@pytest.mark.inspector  # 升级在后台线程调用校验器，跳过必须在收集时决定
def test_cancel_during_the_write_asks_the_device_to_abort(case, tmp_path):
    directory, manifest = case
    board = Board(manifest)
    job = new_job(board, directory, tmp_path, ask=lambda summary: True)
    original = board.block

    def block(sequence, data):
        original(sequence, data)
        job.cancel()
    board.block = block
    job.start()
    result = result_of(drain(job))
    assert not result["ok"] and "已取消" in result["summary"]
    assert "abort" in board.calls and "activate" not in board.calls


def test_adapter_failure_is_explained_not_a_traceback(case, tmp_path):
    directory, manifest = case

    @contextmanager
    def broken(channel, interface, bitrate, log):
        raise OSError("PCAN_USBBUS1 not available")
        yield

    job = Job("update", Settings(), directory, connect=broken, evidence_root=tmp_path / "evidence")
    job.start()
    result = result_of(drain(job))
    assert not result["ok"] and "无法打开 CAN 接口" in result["summary"] and "PCAN_USBBUS1" in result["summary"]


def test_communication_error_keeps_the_hint_and_the_log(case, tmp_path):
    directory, manifest = case
    board = Board(manifest)
    board.info = lambda: (_ for _ in ()).throw(TimeoutError("no response"))
    job = new_job(board, directory, tmp_path, ask=lambda summary: True)
    job.start()
    result = result_of(drain(job))
    assert not result["ok"] and "通信失败" in result["summary"] and "TimeoutError" in result["summary"]
    assert (job.evidence / "events.jsonl").read_text(encoding="utf-8").strip()


@pytest.mark.inspector  # 升级在后台线程调用校验器，跳过必须在收集时决定
def test_tk_window_drives_a_full_update(case, tmp_path, monkeypatch):
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip("no display: %s" % exc)
    from tkinter import messagebox
    from ota.gui.app import App
    directory, manifest = case
    monkeypatch.setattr(messagebox, "askyesno", lambda *args, **kwargs: True)
    monkeypatch.setattr(messagebox, "showwarning", lambda *args, **kwargs: None)
    root.withdraw()
    try:
        board = Board(manifest)
        app = App(root, connect=fake_connect(board), evidence_root=tmp_path / "evidence", config={}, remember=False)
        app.package.set(str(directory))
        assert app.refresh_package() and "v2" in app.summary.get()
        app.start_update()
        assert str(app.start_button.cget("state")) == "disabled" and str(app.cancel_button.cget("state")) == "normal"
        end = time.monotonic() + 30
        while (app.job is None or not app.job.finished.is_set() or app.status.get() != "升级完成") and time.monotonic() < end:
            root.update()
            time.sleep(0.02)
        assert app.status.get() == "升级完成"
        assert float(app.progress["value"]) == 100
        assert str(app.start_button.cget("state")) == "normal" and str(app.evidence_button.cget("state")) == "normal"
        assert "[6/6]" in app.log.get("1.0", "end")
        app.package.set(str(tmp_path / "missing"))
        assert not app.refresh_package() and app.summary.get().startswith("⚠")
    finally:
        root.destroy()
        # Tk 变量必须在主线程释放；留给垃圾回收会在别的线程（例如 subprocess 的管道线程）里析构并卡住 Tcl。
        app = board = None
        import gc
        gc.collect()
