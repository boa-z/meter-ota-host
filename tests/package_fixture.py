"""仅供测试的公开合成包构造器，不用于生产打包。"""
import struct
import zlib


def entry(name, data, *, mode=0o100644, links=1, magic=b"070702"):
    name = name.encode("ascii") + b"\0"
    values = [1, mode, 0, 0, links, 0, len(data), 0, 0, 0, 0, len(name), sum(data) & 0xffffffff]
    head = magic + b"".join(f"{value:08x}".encode() for value in values) + name
    head += bytes(-len(head) % 4)
    return head + data + bytes(-len(data) % 4)


def package(*, os_data=bytes(range(256)) + b"tail", os_name="d13x_os.itb", mapping="os",
            version="v2", extra=(), declared_size=None, info_override=None, os_mode=0o100644,
            os_links=1, trailer=True):
    body = entry(os_name, os_data, mode=os_mode, links=os_links) + b"".join(extra)
    tail = entry("TRAILER!!!", b"", mode=0) if trailer else b""
    total = len(entry("ota_info.bin", bytes(512))) + len(body) + len(tail)
    total += -total % 512
    text = (b"[image]\0" + f'size = "{declared_size if declared_size is not None else total}";'.encode() +
            b"\0" + f'version = "{version}";'.encode() + b"\0[file]\0ota_info.bin:file;\0" +
            f"{os_name}:{mapping};".encode() + b"\0\0")
    text += b"\xff" * (508 - len(text))
    metadata = struct.pack("<I", zlib.crc32(text)) + text if info_override is None else info_override
    result = entry("ota_info.bin", metadata) + body + tail
    return result + bytes(-len(result) % 512)
