"""复用 python-can、can-isotp 与 udsoncan，不实现标准传输协议。

DID 契约见固件 protocols/uds/meter_uds_did.h；这里只做定长解码，不做旧协议兼容。
"""
from contextlib import contextmanager
import copy
import threading
import time

DID_MANIFEST = 0xFD10
DID_STATUS = 0xFD11
DID_ENDPOINT = 0xFD12
DID_SESSION = 0xF186
DID_HARDWARE = 0xF192
DID_SOFTWARE = 0xF195

MANIFEST_SIZE = 148
STATUS_SIZE = 12
ENDPOINT_SIZE = 164
HARDWARE_SIZE = 32
SOFTWARE_SIZE = 48

# 与固件 state_code 表一一对应；编号是线协议的一部分，不能随固件枚举重排。
STATE_NAMES = ("IDLE", "DOWNLOADING", "TRANSFERRED", "VERIFYING", "CANDIDATE_READY", "WAIT_DURABLE",
               "ACTIVATING", "ACTIVATED", "CONFIRMED", "ABORTED", "FAILED")

FLAG_BACKEND_SUPPORTED = 1 << 0
FLAG_MAINTENANCE = 1 << 1
FLAG_EXCLUSIVE = 1 << 2
FLAG_ADMISSION = 1 << 3
FLAG_MANIFEST_PRESENT = 1 << 4
FLAG_CANDIDATE_VERIFIED = 1 << 5


def _text(raw):
    """定长 ASCII 字段：NUL 终止，其余为填充。"""
    return raw.split(b"\0", 1)[0].decode("ascii", "replace")


def _manifest(raw):
    return {"product": _text(raw[0:32]), "hardware": _text(raw[32:64]), "version": _text(raw[64:112]),
            "size": int.from_bytes(raw[112:116], "big"), "sha256": raw[116:148].hex()}


def _status(raw):
    flags = raw[2]
    return {"state": STATE_NAMES[raw[0]] if raw[0] < len(STATE_NAMES) else "UNKNOWN",
            "state_code": raw[0], "error": raw[1],
            "backend_supported": bool(flags & FLAG_BACKEND_SUPPORTED),
            "maintenance": bool(flags & FLAG_MAINTENANCE),
            "exclusive": bool(flags & FLAG_EXCLUSIVE),
            "admission": bool(flags & FLAG_ADMISSION),
            "manifest_present": bool(flags & FLAG_MANIFEST_PRESENT),
            "candidate_verified": bool(flags & FLAG_CANDIDATE_VERIFIED),
            "received": int.from_bytes(raw[4:8], "big"), "total": int.from_bytes(raw[8:12], "big")}


def _endpoint(raw):
    return {"product": _text(raw[0:32]), "hardware": _text(raw[32:64]), "version": _text(raw[64:112]),
            "os_file": _text(raw[112:128]),
            "candidate_capacity": int.from_bytes(raw[128:132], "big"),
            "backend_reason": _text(raw[132:164])}


class UdsClient:
    def __init__(self, client):
        self.client = client

    def info(self):
        """低频：一次多 DID 请求取回身份、状态与候选 Manifest。"""
        response = self.client.read_data_by_identifier([DID_ENDPOINT, DID_STATUS, DID_MANIFEST])
        values = response.service_data.values
        info = _endpoint(values[DID_ENDPOINT])
        info.update(_status(values[DID_STATUS]))
        manifest = _manifest(values[DID_MANIFEST])
        info["target"] = manifest["version"]
        info["size"] = manifest["size"]
        info["sha256"] = manifest["sha256"]
        info["manifest_product"] = manifest["product"]
        info["manifest_hardware"] = manifest["hardware"]
        return info

    def status(self):
        """高频：只读 12B 状态，不拉 Manifest 与 Endpoint。"""
        return _status(self.client.read_data_by_identifier_first(DID_STATUS))

    def software_version(self):
        """重启后轻量确认当前运行的软件版本。"""
        return _text(self.client.read_data_by_identifier_first(DID_SOFTWARE))

    def programming(self):
        self.client.change_session(2)

    def manifest(self, payload):
        self.client.write_data_by_identifier(DID_MANIFEST, payload)

    def begin(self, size):
        from udsoncan import MemoryLocation
        response = self.client.request_download(MemoryLocation(0, size, address_format=32, memorysize_format=32))
        return response.service_data.max_length

    def block(self, sequence, data):
        self.client.transfer_data(sequence, data)

    def finish(self):
        self.client.request_transfer_exit()

    def abort(self):
        self.client.start_routine(0xf002)

    def activate(self):
        self.client.start_routine(0xf001)

    def confirm(self):
        self.client.start_routine(0xf003)

    def reboot(self):
        self.client.ecu_reset(1)


@contextmanager
def connect(channel, interface, bitrate, evidence, request_id=0x7e0, response_id=0x7e8, *, periodic=()):
    import can
    import isotp
    from udsoncan import DidCodec
    from udsoncan.client import Client
    from udsoncan.configs import default_client_config
    from udsoncan.connections import PythonIsoTpConnection

    class FixedCodec(DidCodec):
        """定长 DID：长度即契约，禁止用 ReadAllRemainingData 猜多 DID 边界。"""

        def __init__(self, size):
            self.size = size

        def encode(self, value):
            data = bytes(value)
            if len(data) != self.size:
                raise ValueError("DID payload must be exactly %d bytes" % self.size)
            return data

        def decode(self, value):
            data = bytes(value)
            if len(data) != self.size:
                raise ValueError("DID response must be exactly %d bytes, got %d" % (self.size, len(data)))
            return data

        def __len__(self):
            return self.size

    bus = None
    tasks = []
    writer = can.ASCWriter(evidence.open("x", encoding="utf-8"))
    lock = threading.Lock()
    try:
        bus = can.Bus(interface=interface, channel=channel, bitrate=bitrate, receive_own_messages=False)

        def receive(timeout):
            message = bus.recv(timeout)
            if message is None:
                return None
            with lock:
                entry = copy.copy(message)
                # RX 保留 backend 原生时间戳；TX 仅记录本机提交时间。
                entry.channel = 0
                writer.on_message_received(entry)
            if message.is_error_frame or message.is_remote_frame or message.is_fd:
                return None
            return isotp.CanMessage(arbitration_id=message.arbitration_id, data=message.data,
                                    dlc=message.dlc, extended_id=message.is_extended_id)

        def transmit(message):
            frame = can.Message(arbitration_id=message.arbitration_id, data=message.data,
                                is_extended_id=message.is_extended_id, timestamp=time.time(), channel=0, is_rx=False)
            bus.send(frame, timeout=1)
            with lock:
                writer.on_message_received(frame)

        # 测试周期帧与升级共用同一通道，避免 PCAN 重复初始化/关闭。
        def record_periodic(message):
            entry = copy.copy(message)
            entry.timestamp = time.time()
            entry.channel = 0
            entry.is_rx = False
            with lock:
                writer.on_message_received(entry)
        for frame, period in periodic:
            tasks.append(bus.send_periodic(frame, period, modifier_callback=record_periodic))

        stack = isotp.TransportLayer(rxfn=receive, txfn=transmit,
            address=isotp.Address(isotp.AddressingMode.Normal_11bits, txid=request_id, rxid=response_id),
            params={"stmin": 0, "blocksize": 8, "tx_padding": 0, "max_frame_size": 1024,
                    "rate_limit_enable": False, "rate_limit_max_bitrate": 500000,
                    "rate_limit_window_size": 0.1})
        config = dict(default_client_config)
        # 同步连接等待完整 ISO-TP PDU；固定有界预算覆盖 1024 字节和 5 ms STmin。
        config.update(data_identifiers={DID_MANIFEST: FixedCodec(MANIFEST_SIZE),
                                        DID_STATUS: FixedCodec(STATUS_SIZE),
                                        DID_ENDPOINT: FixedCodec(ENDPOINT_SIZE),
                                        DID_SESSION: FixedCodec(1),
                                        DID_HARDWARE: FixedCodec(HARDWARE_SIZE),
                                        DID_SOFTWARE: FixedCodec(SOFTWARE_SIZE)},
                      request_timeout=120, p2_timeout=2, p2_star_timeout=120, use_server_timing=False)
        with Client(PythonIsoTpConnection(stack), config=config) as client:
            yield UdsClient(client)
    finally:
        for task in tasks:
            task.stop()
            thread = getattr(task, "thread", None)
            if thread is not None:
                thread.join(timeout=2)
        if bus is not None:
            bus.shutdown()
        writer.stop()
