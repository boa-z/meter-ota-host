"""升级界面的后台逻辑：与 Tk 无关，界面只负责显示这里产生的消息。

一次操作（读取设备或一键升级）在独立线程里运行，通过队列向界面发消息：
  ("line", 文本)        一行中文进度
  ("progress", 百分比)  写入进度 0..100
  ("done", 结果字典)    结束：ok、summary、evidence
升级流程本身是 ota.update.run，界面不重复实现任何升级逻辑。
"""
from contextlib import ExitStack
from dataclasses import dataclass
import json
from pathlib import Path
import queue
import threading
import time
from ..client import Manifest, UpdateError
from ..update import describe, locate, run


class Cancelled(UpdateError):
    """用户在界面上点了取消。"""


@dataclass
class Settings:
    interface: str = "pcan"
    channel: str = "PCAN_USBBUS1"
    bitrate: int = 500000
    os_file: str = "d13x_os.itb"
    wait_maintenance: int = 120
    boot_timeout: int = 90


def default_evidence_root():
    return Path.home() / "meter-ota-evidence"


def summarize_package(path, manifest_path=None):
    """读取升级包旁边的清单，给界面显示；清单缺失或损坏时抛 UpdateError（中文）。"""
    package, manifest_file = locate(path, manifest_path)
    if not package.is_file():
        raise UpdateError("找不到升级包文件：%s" % package)
    if not manifest_file.is_file():
        raise UpdateError("升级包旁边没有 ota.manifest.json：%s" % manifest_file)
    try:
        manifest = Manifest.load(manifest_file)
    except UpdateError as exc:
        raise UpdateError("升级清单无法使用：%s" % exc) from exc
    if package.stat().st_size != manifest.size:
        raise UpdateError("升级包大小（%d 字节）与清单（%d 字节）不符，文件可能不完整。" % (
            package.stat().st_size, manifest.size))
    return {"product": manifest.product, "hardware": manifest.hardware, "version": manifest.version,
            "size": manifest.size, "sha256": manifest.sha256}


class Job:
    """一次后台操作。kind 为 "update" 或 "probe"。"""

    def __init__(self, kind, settings, package=None, *, connect=None, evidence_root=None, ask=None):
        self.kind = kind
        self.settings = settings
        self.package = package
        self.connect = connect
        self.evidence_root = Path(evidence_root) if evidence_root else default_evidence_root()
        self.ask = ask
        self.messages = queue.Queue()
        self.evidence = None
        self.finished = threading.Event()
        self._cancel = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._work, name="meter-ota-" + self.kind, daemon=True)
        self._thread.start()

    def cancel(self):
        self._cancel.set()

    def wait(self, timeout=None):
        return self.finished.wait(timeout)

    def _check_cancel(self):
        if self._cancel.is_set():
            raise Cancelled("已取消")

    def _sleep(self, seconds):
        # 分小段睡眠，取消按钮在等待阶段也能及时生效。
        end = time.monotonic() + seconds
        while True:
            self._check_cancel()
            left = end - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(0.1, left))

    def _work(self):
        result = {"ok": False, "summary": "未知错误", "evidence": None}
        log = None
        try:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            self.evidence = self.evidence_root / ("%s-%s" % (self.kind, stamp))
            self.evidence.mkdir(parents=True, exist_ok=False)
            result["evidence"] = str(self.evidence)
            log = (self.evidence / "events.jsonl").open("x", encoding="utf-8")

            def emit(event):
                log.write(json.dumps(event, ensure_ascii=False) + "\n")
                log.flush()
                if event.get("event") == "progress" and event.get("total"):
                    self.messages.put(("progress", event["received"] * 100 // event["total"]))
                self._check_cancel()  # 写入过程中取消：download 会通知设备中止本次升级

            say = lambda text: self.messages.put(("line", text))
            say("取证目录：%s" % self.evidence)
            connect = self.connect
            if connect is None:
                from ..transport import connect
            with ExitStack() as stack:
                try:
                    client = stack.enter_context(connect(self.settings.channel, self.settings.interface,
                                                         self.settings.bitrate, self.evidence / "can.asc"))
                except Exception as exc:
                    raise UpdateError("无法打开 CAN 接口（%s / %s）：%s。请确认适配器已连接、驱动已安装且没有被别的程序占用。" % (
                        self.settings.interface, self.settings.channel, exc)) from exc
                if self.kind == "probe":
                    info = client.info()
                    say("设备：" + describe(info))
                    say("维护模式：%s；升级后端：%s；候选分区容量：%s 字节" % (
                        "已开启" if info.get("maintenance") in (True, 1) else "未开启",
                        "可用" if info.get("backend_supported") is True else "不可用",
                        info.get("candidate_capacity", "未知")))
                    result.update(ok=True, summary="已读取设备信息")
                else:
                    package, manifest = locate(self.package)
                    outcome = run(client, package, manifest, os_file=self.settings.os_file,
                                  say=say, emit=emit, approve=self.ask,
                                  wait_maintenance=self.settings.wait_maintenance,
                                  boot_timeout=self.settings.boot_timeout, sleep=self._sleep)
                    kind = outcome["event"]
                    result.update(
                        ok=kind in ("verified", "up_to_date"),
                        summary={"verified": "升级完成", "up_to_date": "设备已是该版本，无需升级",
                                 "cancelled": "已取消，设备没有被改动"}.get(kind, kind))
        except Cancelled:
            result.update(ok=False, summary="已取消。如果升级已经开始，请重新读取设备确认当前状态。")
        except UpdateError as exc:
            result.update(ok=False, summary=str(exc))
        except Exception as exc:  # 通信超时等：给出可操作的提示，细节留在取证日志
            result.update(ok=False, summary="通信失败：%s（%s）。请检查线缆、波特率，并确认设备已上电。" % (
                exc or type(exc).__name__, type(exc).__name__))
        finally:
            if log:
                log.write(json.dumps({"event": "gui_result", **result}, ensure_ascii=False) + "\n")
                log.close()
            self.messages.put(("done", result))
            self.finished.set()
