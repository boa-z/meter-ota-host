"""仪表固件升级界面（Tkinter，标准库，无额外依赖）。

界面只做三件事：收集参数、显示 controller 发来的中文进度、在改动设备之前让用户确认。
升级逻辑在 ota.update.run，后台线程在 controller.Job。
"""
import json
import os
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from ..client import UpdateError
from .controller import Job, Settings, default_evidence_root, summarize_package

INTERFACES = ("pcan", "kvaser", "vector", "socketcan")
BITRATES = ("125000", "250000", "500000", "1000000")
CONFIG = Path.home() / ".meter-ota-gui.json"
COLORS = {"idle": "#555555", "busy": "#0b5cad", "ok": "#1a7f37", "error": "#b42318"}


def load_config():
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(values):
    try:
        CONFIG.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass  # 记不住上次的选择不影响使用


class App:
    def __init__(self, root, *, connect=None, evidence_root=None, config=None, remember=True):
        self.root = root
        self.connect = connect
        self.evidence_root = evidence_root or default_evidence_root()
        self.remember = remember
        self.job = None
        self.evidence = None
        self.questions = queue.Queue()
        config = load_config() if config is None else config
        root.title("仪表固件升级")
        root.minsize(720, 560)
        root.protocol("WM_DELETE_WINDOW", self.close)

        self.package = tk.StringVar(value=config.get("package", ""))
        self.interface = tk.StringVar(value=config.get("interface", "pcan"))
        self.channel = tk.StringVar(value=config.get("channel", "PCAN_USBBUS1"))
        self.bitrate = tk.StringVar(value=str(config.get("bitrate", 500000)))
        self.summary = tk.StringVar(value="请选择升级包。")
        self.status = tk.StringVar(value="就绪")

        frame = ttk.Frame(root, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(6, weight=1)

        ttk.Label(frame, text="升级包").grid(row=0, column=0, sticky="w")
        entry = ttk.Entry(frame, textvariable=self.package)
        entry.grid(row=0, column=1, sticky="ew", padx=6)
        entry.bind("<FocusOut>", lambda _event: self.refresh_package())
        buttons = ttk.Frame(frame)
        buttons.grid(row=0, column=2)
        ttk.Button(buttons, text="选择目录…", command=self.pick_directory).pack(side="left")
        ttk.Button(buttons, text="选择文件…", command=self.pick_file).pack(side="left", padx=(4, 0))
        ttk.Label(frame, textvariable=self.summary, foreground="#333333", wraplength=640, justify="left"
                  ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 8))

        can = ttk.LabelFrame(frame, text="CAN 适配器", padding=8)
        can.grid(row=2, column=0, columnspan=3, sticky="ew")
        ttk.Label(can, text="接口").grid(row=0, column=0)
        ttk.Combobox(can, textvariable=self.interface, values=INTERFACES, width=10).grid(row=0, column=1, padx=6)
        ttk.Label(can, text="通道").grid(row=0, column=2)
        ttk.Entry(can, textvariable=self.channel, width=18).grid(row=0, column=3, padx=6)
        ttk.Label(can, text="波特率").grid(row=0, column=4)
        ttk.Combobox(can, textvariable=self.bitrate, values=BITRATES, width=9).grid(row=0, column=5, padx=6)

        actions = ttk.Frame(frame)
        actions.grid(row=3, column=0, columnspan=3, sticky="w", pady=10)
        self.probe_button = ttk.Button(actions, text="读取设备", command=self.start_probe)
        self.probe_button.pack(side="left")
        self.start_button = ttk.Button(actions, text="开始升级", command=self.start_update)
        self.start_button.pack(side="left", padx=6)
        self.cancel_button = ttk.Button(actions, text="取消", command=self.cancel, state="disabled")
        self.cancel_button.pack(side="left")
        self.evidence_button = ttk.Button(actions, text="打开取证目录", command=self.open_evidence, state="disabled")
        self.evidence_button.pack(side="left", padx=6)

        self.progress = ttk.Progressbar(frame, maximum=100)
        self.progress.grid(row=4, column=0, columnspan=3, sticky="ew")
        self.status_label = tk.Label(frame, textvariable=self.status, anchor="w", font=("Microsoft YaHei UI", 12, "bold"),
                                     fg=COLORS["idle"])
        self.status_label.grid(row=5, column=0, columnspan=3, sticky="ew", pady=6)
        log_frame = ttk.Frame(frame)
        log_frame.grid(row=6, column=0, columnspan=3, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log = tk.Text(log_frame, height=14, state="disabled", wrap="word", font=("Consolas", 10))
        scroll = ttk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")

        self.refresh_package()
        root.after(100, self.poll)

    # ---- 参数 ----
    def settings(self):
        try:
            bitrate = int(self.bitrate.get())
        except ValueError:
            raise UpdateError("波特率必须是数字，例如 500000。")
        return Settings(interface=self.interface.get().strip(), channel=self.channel.get().strip(), bitrate=bitrate)

    def refresh_package(self):
        text = self.package.get().strip()
        if not text:
            self.summary.set("请选择升级包。")
            return False
        try:
            info = summarize_package(text)
        except UpdateError as exc:
            self.summary.set("⚠ " + str(exc))
            return False
        self.summary.set("产品 %(product)s / 硬件 %(hardware)s，版本 %(version)s，%(size)d 字节" % info)
        return True

    def pick_directory(self):
        chosen = filedialog.askdirectory(title="选择 pack 生成的升级包目录")
        if chosen:
            self.package.set(chosen)
            self.refresh_package()

    def pick_file(self):
        chosen = filedialog.askopenfilename(title="选择 ota.cpio", filetypes=[("OTA 升级包", "*.cpio"), ("所有文件", "*.*")])
        if chosen:
            self.package.set(chosen)
            self.refresh_package()

    # ---- 操作 ----
    def busy(self, running):
        state = "disabled" if running else "normal"
        self.probe_button.configure(state=state)
        self.start_button.configure(state=state)
        self.cancel_button.configure(state="normal" if running else "disabled")

    def set_status(self, text, kind):
        self.status.set(text)
        self.status_label.configure(fg=COLORS[kind])

    def append(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def launch(self, kind, package=None):
        try:
            settings = self.settings()
        except UpdateError as exc:
            messagebox.showerror("参数错误", str(exc))
            return
        self.save()
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.progress["value"] = 0
        self.set_status("正在读取设备…" if kind == "probe" else "正在升级…", "busy")
        self.busy(True)
        self.job = Job(kind, settings, package, connect=self.connect, evidence_root=self.evidence_root,
                       ask=self.ask_user)
        self.job.start()

    def start_probe(self):
        self.launch("probe")

    def start_update(self):
        if not self.refresh_package():
            messagebox.showwarning("无法升级", self.summary.get().lstrip("⚠ ") or "请先选择有效的升级包。")
            return
        self.launch("update", self.package.get().strip())

    def cancel(self):
        if self.job and not self.job.finished.is_set():
            self.job.cancel()
            self.set_status("正在取消…", "busy")

    def ask_user(self, summary):
        """后台线程调用：Tk 不是线程安全的，这里只把问题放进队列，由界面线程的 poll 弹框，然后等待回答。"""
        request = {"summary": summary, "answered": threading.Event(), "ok": False}
        self.questions.put(request)
        request["answered"].wait()
        return request["ok"]

    def answer_questions(self):
        try:
            while True:
                request = self.questions.get_nowait()
                request["ok"] = messagebox.askyesno(
                    "确认升级", "即将" + request["summary"] + "。\n\n升级过程中请保持设备供电和 CAN 连接，继续吗？")
                request["answered"].set()
        except queue.Empty:
            pass

    def open_evidence(self):
        if self.evidence and os.path.isdir(self.evidence) and hasattr(os, "startfile"):
            os.startfile(self.evidence)  # 仅 Windows；其他系统请直接在文件管理器里打开

    def poll(self):
        self.answer_questions()
        job = self.job
        if job:
            try:
                while True:
                    kind, value = job.messages.get_nowait()
                    if kind == "line":
                        self.append(value)
                    elif kind == "progress":
                        self.progress["value"] = value
                    elif kind == "done":
                        self.finish(value)
            except queue.Empty:
                pass
        self.root.after(100, self.poll)

    def finish(self, result):
        self.busy(False)
        self.evidence = result.get("evidence")
        if self.evidence:
            self.evidence_button.configure(state="normal")
        if result["ok"]:
            self.progress["value"] = 100
        self.set_status(result["summary"], "ok" if result["ok"] else "error")
        self.append(("✔ " if result["ok"] else "✘ ") + result["summary"])

    def save(self):
        if self.remember:
            save_config({"package": self.package.get().strip(), "interface": self.interface.get(),
                         "channel": self.channel.get(), "bitrate": self.bitrate.get()})

    def close(self):
        if self.job and not self.job.finished.is_set():
            if not messagebox.askyesno("升级进行中", "升级还在进行，现在关闭会中断它。确定要关闭吗？"):
                return
            self.job.cancel()
            self.job.wait(5)
        self.root.destroy()


def main():
    try:  # Windows 高分屏：声明 DPI 感知，否则界面会被系统放大而发虚
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError, ImportError):
        pass
    root = tk.Tk()
    App(root)
    root.mainloop()
