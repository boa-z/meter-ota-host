"""一键升级：读取状态 → 检查升级包 → 维护模式 → 写入 → 激活并重启 → 校验。

把分散的 info / preflight / download / activate / probe 串成一次操作，供命令行和以后的界面共用。
本模块不碰总线细节：调用方传入已连接的 client，并提供 say（给人看的中文进度）与 emit（结构化事件）。
维护模式是设备端的准入闸门，只能在设备串口打开，主机只能等待并提示，不能代替。
"""
from pathlib import Path
import time
from .client import Manifest, UpdateError, activate, confirm, download, preflight
from .package import PackagePolicy

DEFAULT_MAINTENANCE_COMMAND = "meter_update maintenance on"
MAINTENANCE_COMMANDS = {"lvgl-aic-smoke": "lv_aic_can_ota maintenance on"}
IDLE_STATES = ("IDLE", "FAILED", "ABORTED")
READY_STATES = ("CANDIDATE_READY", "WAIT_DURABLE")
STEPS = 6


def locate(path, manifest=None):
    """接受 pack 生成的目录或其中的 ota.cpio；清单默认与升级包同目录。"""
    path = Path(path)
    package = path / "ota.cpio" if path.is_dir() else path
    return package, Path(manifest) if manifest else package.with_name("ota.manifest.json")


def describe(info):
    return "%s / %s，当前版本 %s，状态 %s" % (
        info.get("product", "?"), info.get("hardware", "?"), info.get("version", "未知"), info.get("state", "未知"))


def run(client, package, manifest_path, *, os_file, candidate_capacity=None, say=print, emit=lambda event: None,
        approve=None, wait_maintenance=120, boot_timeout=90, maintenance_command=None, force=False,
        clock=time.monotonic, sleep=time.sleep):
    """返回结果事件；失败抛 UpdateError。approve(摘要) 返回 False 时在任何写入之前取消。"""
    manifest = Manifest.load(manifest_path)

    def step(number, text):
        say("[%d/%d] %s" % (number, STEPS, text))
        emit({"event": "update_step", "step": number, "text": text})

    step(1, "连接设备并读取状态")
    info = client.info()
    say("      设备：" + describe(info))
    if info.get("product") != manifest.product or info.get("hardware") != manifest.hardware:
        raise UpdateError("设备是 %s / %s，升级包是 %s / %s，两者不匹配，已停止。" % (
            info.get("product"), info.get("hardware"), manifest.product, manifest.hardware))
    if info.get("version") == manifest.version and not force:
        say("设备已经运行 %s，无需升级。需要重刷请加 --force。" % manifest.version)
        return {"event": "up_to_date", "device": info}

    step(2, "检查升级包")
    capacity = candidate_capacity if candidate_capacity is not None else info.get("candidate_capacity")
    if type(capacity) is not int:
        raise UpdateError("设备没有上报候选分区容量，请用 --candidate-capacity 指定。")
    policy = PackagePolicy(os_file, capacity)
    checked = preflight(package, manifest, policy)
    say("      版本 %s，%d 字节，SHA256 %s…，主机侧包结构检查通过（最终以设备校验为准）" % (
        manifest.version, manifest.size, manifest.sha256[:12]))
    emit({"event": "package", **checked})
    if approve is not None and not approve("把 %s 从 %s 升级到 %s" % (
            info.get("product"), info.get("version", "未知版本"), manifest.version)):
        say("已取消，设备没有被改动。")
        return {"event": "cancelled"}

    resumed = info.get("target") == manifest.version
    state = info.get("state")
    if resumed and state == "ACTIVATED":
        step(3, "跳过：设备已激活该版本，只差重启")
        step(4, "跳过：升级包已在设备上")
        step(5, "请求重启")
        client.programming()
        client.reboot()
    else:
        step(3, "请求设备进入维护模式")
        command = maintenance_command or MAINTENANCE_COMMANDS.get(info.get("product"), DEFAULT_MAINTENANCE_COMMAND)
        _enter_maintenance(client, info, wait_maintenance, command, say, clock, sleep)
        if resumed and state in READY_STATES:
            step(4, "跳过：设备上已有校验通过的候选包")
        elif state in IDLE_STATES:
            step(4, "写入升级包")
            _download(client, package, manifest, policy, say, emit)
        else:
            raise UpdateError("设备升级状态是 %s，不能直接开始。先用 abort 命令中止后重试。" % state)
        step(5, "激活并重启")
        emit(activate(client, manifest.version, reboot=True))

    step(6, "等待设备以新版本重新上线并校验")
    after = _wait_new_version(client, manifest, boot_timeout, say, clock, sleep)
    return _verify(client, manifest, after, say, emit)


def _enter_maintenance(client, info, wait, command, say, clock, sleep):
    """先经 CAN 打开 UDS 编程会话请求维护模式（主流做法，无需串口）；设备或 Product 不接受时，
    回退到等待串口命令，两条路径共用同一个准入判断。"""
    if info.get("maintenance") in (True, 1):
        say("      维护模式已开启")
        return
    deadline = clock() + wait
    try:
        client.programming()
    except Exception as exc:
        say("      设备没有接受 CAN 编程会话请求（%s）" % exc)
    asked_at = clock()
    announced = False
    while True:
        info = client.info()  # 同时让编程会话保持有效
        if info.get("maintenance") in (True, 1):
            say("      维护模式已开启" + ("（经 CAN 编程会话）" if not announced else ""))
            return
        if not announced and clock() - asked_at >= 3:
            say("      设备没有自动进入维护模式（旧固件，或产品暂不允许）。"
                "请在设备串口执行：%s（最多再等待 %d 秒）" % (command, max(0, int(deadline - clock()))))
            announced = True
        if clock() >= deadline:
            raise UpdateError("等待 %d 秒仍未进入维护模式，已停止，设备没有被改动。" % wait)
        sleep(1)


def _download(client, package, manifest, policy, say, emit):
    last = [-1]

    def relay(event):
        emit(event)
        if event.get("event") == "progress":
            percent = event["received"] * 100 // event["total"]
            if percent // 10 != last[0] // 10 or percent == 100:
                say("      写入 %d%%（%d / %d 字节）" % (percent, event["received"], event["total"]))
            last[0] = percent
        elif event.get("event") == "abort":
            say("      写入中断，已请求设备中止本次升级")
        elif event.get("event") == "candidate":
            say("      写入完成，设备已校验候选包（%.1f 秒）" % event["elapsed_s"])

    download(client, package, manifest, relay, package_policy=policy)


def _wait_new_version(client, manifest, timeout, say, clock, sleep):
    deadline = clock() + timeout
    start = clock()
    sleep(3)  # 给设备留出开始重启的时间，避免读到重启前的旧状态
    told = 0
    while True:
        try:
            info = client.info()
            if info.get("version") == manifest.version:
                return info
        except Exception:
            pass  # 重启期间设备不应答是正常的
        waited = int(clock() - start)
        if waited - told >= 10:
            say("      仍在等待设备重启…（已 %d 秒）" % waited)
            told = waited
        if clock() >= deadline:
            raise UpdateError("设备在 %d 秒内没有以版本 %s 回到 CAN 总线。请查看串口启动日志，"
                              "确认是否已回退到旧版本。" % (timeout, manifest.version))
        sleep(1)


def _verify(client, manifest, info, say, emit):
    checks = [
        ("运行版本", info.get("version") == manifest.version, "设备上报 %s" % info.get("version")),
        ("产品与硬件", info.get("product") == manifest.product and info.get("hardware") == manifest.hardware,
         "%s / %s" % (info.get("product"), info.get("hardware"))),
        ("升级后端", info.get("backend_supported") is True, str(info.get("backend_reason", "可用"))),
        ("错误码", info.get("error") == 0, "error=%s" % info.get("error")),
    ]
    warnings = []
    if info.get("maintenance") in (True, 1):
        warnings.append("维护模式仍然开启，请在设备串口执行 meter_update maintenance off 退出。")
    result = {"event": "verified", "device": info, "checks": [
        {"name": name, "pass": ok, "detail": detail} for name, ok, detail in checks]}
    for name, ok, detail in checks:
        say("      %s：%s（%s）" % (name, "通过" if ok else "未通过", detail))
    if not all(ok for _, ok, _ in checks):
        emit(result)
        raise UpdateError("升级后校验未通过，详见上面的检查项。")
    if info.get("trial") == 1:
        # 端点上报了未确认的试运行槽位时才需要确认（smoke 端点）；其余端点由 SDK 自动确认。
        outcome = confirm(client, manifest.version)
        say("      试运行确认：" + ("SDK 已自动确认" if outcome.get("mode") == "sdk_auto_confirm" else "已确认"))
        result["confirmation"] = outcome
    for text in warnings:
        say("      注意：" + text)
    result["warnings"] = warnings
    emit(result)
    say("升级完成：设备现在运行 %s。" % manifest.version)
    say("上位机只能确认版本和升级通道状态；界面、触摸和正常 CAN 通信请在设备上目视确认。")
    return result
