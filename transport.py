"""复用 python-can、can-isotp 与 udsoncan，不实现标准传输协议。"""
from contextlib import contextmanager
import copy
import json
import threading
import time

DID = 0xf180


class UdsClient:
    def __init__(self, client):
        self.client = client

    def info(self):
        return json.loads(self.client.read_data_by_identifier_first(DID))

    def programming(self):
        self.client.change_session(2)

    def manifest(self, payload):
        self.client.write_data_by_identifier(DID, payload)

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

    class BytesCodec(DidCodec):
        def encode(self, value):
            return bytes(value)

        def decode(self, value):
            return bytes(value)

        def __len__(self):
            raise self.ReadAllRemainingData()

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
        config.update(data_identifiers={DID: BytesCodec()}, request_timeout=120,
                      p2_timeout=2, p2_star_timeout=120, use_server_timing=False)
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
