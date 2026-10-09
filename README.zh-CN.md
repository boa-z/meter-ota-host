# meter-ota-host

> [English](README.md)

匠芯创 D13x 仪表的 CAN OTA 上位机：UDS over ISO-TP 客户端、仅含 OS 的升级包打包器、一键升级流程和 Tk 图形界面。设备端升级端点在各固件工程里，本仓库只放 PC 端。

它以 Git submodule 的形式挂载在 `tools/ota`，被以下工程使用：

- [forklift-meter-platform](https://github.com/boa-z/forklift-meter-platform)：`python -m tools.ota ...`
- [lvgl-aic-smoke](https://github.com/boa-z/lvgl-aic-smoke)：在其 `tools/` 目录执行 `python -m ota ...`

仓库根目录本身就是 Python 包，所有模块都用相对导入，挂载成任何名字都能用。

## 安装

```sh
python -m pip install -r tools/ota/requirements.txt
```

打包需要 GNU `cpio` 和 `mkenvimage`（Luban-Lite SDK 的 `tools/scripts/` 自带两者；MSYS2：`pacman -S cpio uboot-tools`）。

## 升级包校验器

升级包检查调用 `meter-ota-inspect`，它由宿主工程用与固件相同的 `meter_package.c` 解析器编译。查找顺序：先看 `METER_OTA_INSPECTOR`，再找宿主工程根目录（本包往上两级）下的 `build-package/`、`build-update/`、`build/`。

## 用法

```sh
python -m tools.ota --interface pcan --channel PCAN_USBBUS1 --bitrate 500000 info
python -m tools.ota --interface pcan --channel PCAN_USBBUS1 --bitrate 500000 update <升级包目录>
python -m tools.ota.gui
```

其他动作：`pack`、`preflight`、`download`、`activate`、`confirm`、`abort`，参数见 `python -m tools.ota <动作> --help`。写入前设备必须处于维护模式；一键升级会等待并提示需要在串口执行的命令。

## 测试

```sh
python -m pip install pytest -r requirements.txt
METER_OTA_INSPECTOR=/path/to/meter-ota-inspect python -m pytest
```

没有校验器时，依赖它的用例会被跳过。

## 许可证

Apache-2.0，见 [LICENSE](LICENSE)。
