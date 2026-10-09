"""可自动化的 CAN OTA 命令行；默认只读预检，激活与重启显式请求。"""
import argparse
import json
from pathlib import Path
import sys
import time
from .package import PackagePolicy
from .client import Manifest, UpdateError, activate, confirm, download, preflight
from .update import DEFAULT_MAINTENANCE_COMMAND


def main(argv=None):
    parser = argparse.ArgumentParser(description="Framework CAN OTA client")
    parser.add_argument("--interface", default="pcan")
    parser.add_argument("--channel", default="PCAN_USBBUS1")
    parser.add_argument("--bitrate", type=int, default=500000)
    parser.add_argument("--evidence", type=Path, help="new directory for JSONL and CAN ASC evidence")
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ("preflight", "download"):
        command = sub.add_parser(name)
        command.add_argument("package", type=Path)
        command.add_argument("--manifest", type=Path, required=True)
        command.add_argument("--os-file", required=True)
        command.add_argument("--candidate-capacity", type=int, required=True)
    command = sub.add_parser("pack")
    command.add_argument("os_image", type=Path)
    command.add_argument("destination", type=Path)
    for field in ("product", "hardware", "version", "os-file"):
        command.add_argument("--" + field, required=True)
    command.add_argument("--candidate-capacity", type=int, required=True)
    # reference-board AIC OTA 后端按 4 KiB 写入候选分区；默认值必须满足该板级契约。
    command.add_argument("--pad-os-to", type=int, choices=(1, 4096), default=4096)
    command.add_argument("--cpio", default="cpio")
    command.add_argument("--mkenvimage", default="mkenvimage")
    sub.add_parser("info", aliases=["probe"])
    sub.add_parser("abort")
    command = sub.add_parser("activate")
    command.add_argument("--version", required=True)
    command.add_argument("--reboot", action="store_true")
    command = sub.add_parser("confirm")
    command.add_argument("--version", required=True)
    command = sub.add_parser("update", help="一键升级：检查升级包、等待维护模式、写入、激活重启并校验")
    command.add_argument("package", type=Path, help="pack 生成的目录，或其中的 ota.cpio")
    command.add_argument("--manifest", type=Path, help="默认取升级包同目录的 ota.manifest.json")
    command.add_argument("--os-file", default="d13x_os.itb")
    command.add_argument("--candidate-capacity", type=int, help="默认采用设备上报的候选分区容量")
    command.add_argument("--wait-maintenance", type=int, default=120, help="等待设备进入维护模式的秒数")
    command.add_argument("--boot-timeout", type=int, default=90, help="激活后等待设备以新版本上线的秒数")
    command.add_argument("--maintenance-command", help="提示用户在串口执行的命令，默认 " + DEFAULT_MAINTENANCE_COMMAND)
    command.add_argument("--force", action="store_true", help="设备已是目标版本时也重新升级")
    command.add_argument("--yes", action="store_true", help="不再询问确认（脚本或产线使用）")
    args = parser.parse_args(argv)
    log = None
    def emit(event):
        line = json.dumps(event, ensure_ascii=False)
        # update 面向人：屏幕上只显示中文进度，结构化事件只写入取证日志。
        if args.action != "update":
            print(line, flush=True)
        if log:
            log.write(line + "\n")
            log.flush()
    try:
        policy = PackagePolicy(args.os_file, args.candidate_capacity) if args.action in ("preflight", "download", "pack") else None
        if args.action == "update" and not args.evidence:
            args.evidence = Path("evidence") / time.strftime("update-%Y%m%d-%H%M%S")
        if args.action == "pack":
            from .pack import pack
            emit(pack(args.os_image, args.destination, args.product, args.hardware, args.version, policy,
                      cpio=args.cpio, mkenvimage=args.mkenvimage, pad_os_to=args.pad_os_to))
            return 0
        if args.action == "preflight":
            emit(preflight(args.package, Manifest.load(args.manifest), policy))
            return 0
        if not args.evidence:
            raise UpdateError("--evidence is required for CAN operations")
        args.evidence.mkdir(parents=True, exist_ok=False)
        log = (args.evidence / "events.jsonl").open("x", encoding="utf-8")
        manifest = None
        if args.action == "download":
            manifest = Manifest.load(args.manifest)
            emit({"event": "offline_preflight", **preflight(args.package, manifest, policy)})
        from .transport import connect
        with connect(args.channel, args.interface, args.bitrate, args.evidence / "can.asc") as client:
            if args.action in ("info", "probe"):
                emit({"event": "info", "device": client.info()})
            elif args.action == "download":
                download(client, args.package, manifest, emit, package_policy=policy)
            elif args.action == "activate":
                emit(activate(client, args.version, args.reboot))
            elif args.action == "confirm":
                emit(confirm(client, args.version))
            elif args.action == "update":
                from .update import locate, run
                package, manifest_path = locate(args.package, args.manifest)

                def approve(summary):
                    if args.yes:
                        return True
                    if not sys.stdin.isatty():
                        raise UpdateError("非交互环境下需要加 --yes 才会开始升级。")
                    return input("即将" + summary + "。继续吗？[y/N] ").strip().lower() in ("y", "yes")
                print("取证目录：%s" % args.evidence, flush=True)
                result = run(client, package, manifest_path, os_file=args.os_file,
                             candidate_capacity=args.candidate_capacity, emit=emit,
                             approve=approve, wait_maintenance=args.wait_maintenance,
                             boot_timeout=args.boot_timeout, maintenance_command=args.maintenance_command,
                             force=args.force)
                return 1 if result["event"] == "cancelled" else 0
            elif args.action == "abort":
                client.programming()
                client.abort()
                emit({"event": "abort", "device": client.info()})
        return 0
    except KeyboardInterrupt:
        emit({"event": "error", "reason": "interrupted"})
        if args.action == "update":
            print("已中断。如果写入已经开始，设备会保留未完成的会话，可用 abort 命令中止。", flush=True)
        return 130
    except Exception as exc:
        emit({"event": "error", "reason": str(exc), "type": type(exc).__name__})
        if args.action == "update":
            print("升级失败：%s" % exc, flush=True)
        return 1
    finally:
        if log:
            log.close()


if __name__ == "__main__":
    sys.exit(main())
