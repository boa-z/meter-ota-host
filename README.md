# meter-ota-host

> [中文版](README.zh-CN.md)

Host-side CAN OTA tool for ArtInChip D13x meters: a UDS-over-ISO-TP client, an OS-only package packer, a one-command update flow and a Tk GUI. The device endpoint lives in the firmware projects; this repository holds only the PC side.

It is used as a Git submodule mounted at `tools/ota` by:

- [forklift-meter-platform](https://github.com/boa-z/forklift-meter-platform): `python -m tools.ota ...`
- [lvgl-aic-smoke](https://github.com/boa-z/lvgl-aic-smoke): `python -m ota ...` from its `tools/` directory

The repository root is the Python package, so every module uses relative imports and works under any mount name.

## Install

```sh
python -m pip install -r tools/ota/requirements.txt
```

Packing needs GNU `cpio` and `mkenvimage` (the Luban-Lite SDK ships both in `tools/scripts/`; MSYS2: `pacman -S cpio uboot-tools`).

## Package inspector

Package checks run `meter-ota-inspect`, which the host project compiles from the same `meter_package.c` parser as the firmware. The tool looks for `METER_OTA_INSPECTOR` first, then `build-package/`, `build-update/` and `build/` in the host project root (two levels above this package).

## Usage

```sh
python -m tools.ota --interface pcan --channel PCAN_USBBUS1 --bitrate 500000 info
python -m tools.ota --interface pcan --channel PCAN_USBBUS1 --bitrate 500000 update <package-dir>
python -m tools.ota.gui
```

Other actions: `pack`, `preflight`, `download`, `activate`, `confirm`, `abort`. Run `python -m tools.ota <action> --help` for options. The device must be in maintenance mode before a download; the update flow waits for it and prints the console command.

## Tests

```sh
python -m pip install pytest -r requirements.txt
METER_OTA_INSPECTOR=/path/to/meter-ota-inspect python -m pytest
```

Without an inspector, tests that need one are skipped.

## License

Apache-2.0, see [LICENSE](LICENSE).
