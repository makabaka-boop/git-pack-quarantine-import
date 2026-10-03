"""测试辅助：手工构造/篡改 PACK v2 文件的工具函数。"""

from __future__ import annotations

import hashlib
import struct
import zlib

# 对象类型编号
COMMIT, TREE, BLOB, TAG = 1, 2, 3, 4
OFS_DELTA, REF_DELTA = 6, 7


def varint(n: int) -> bytes:
    """差量头使用的小端 7 位变长整数。"""
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def obj_header(type_id: int, size: int) -> bytes:
    """对象条目的类型/长度变长头。"""
    out = bytearray()
    b = (type_id << 4) | (size & 0x0F)
    size >>= 4
    while size:
        out.append(b | 0x80)
        b = size & 0x7F
        size >>= 7
    out.append(b)
    return bytes(out)


def encode_ofs(distance: int) -> bytes:
    """OFS_DELTA 的基对象距离编码（与解析器互逆）。"""
    out = bytearray([distance & 0x7F])
    distance >>= 7
    while distance:
        distance -= 1
        out.append(0x80 | (distance & 0x7F))
        distance >>= 7
    return bytes(reversed(out))


def build_pack(entries) -> tuple[bytes, list[int]]:
    """构造合法 PACK v2。

    entries: [(type_id, extra, payload), ...]，extra 为 OFS 偏移编码或
    REF 的 20 字节基对象 ID（普通对象为空字节串）。
    返回 (pack_bytes, 各条目起始偏移列表)。
    """
    out = bytearray(b"PACK" + struct.pack(">II", 2, len(entries)))
    offsets = []
    for type_id, extra, payload in entries:
        offsets.append(len(out))
        out += obj_header(type_id, len(payload))
        out += extra
        out += zlib.compress(payload)
    out += hashlib.sha1(bytes(out)).digest()
    return bytes(out), offsets


def copy_cmd(offset: int, length: int) -> bytes:
    """差量拷贝指令：从基对象 offset 处拷贝 length 字节。"""
    cmd = 0x80
    args = bytearray()
    for i in range(4):
        b = (offset >> (8 * i)) & 0xFF
        if b:
            cmd |= 1 << i
            args.append(b)
    if length != 0x10000:
        for i in range(3):
            b = (length >> (8 * i)) & 0xFF
            if b:
                cmd |= 0x10 << i
                args.append(b)
    # length == 0x10000 时长度位全留空，解码端自动映射
    return bytes([cmd]) + bytes(args)


def insert_cmd(data: bytes) -> bytes:
    """差量插入指令（1..127 字节字面量）。"""
    assert 0 < len(data) <= 127
    return bytes([len(data)]) + data


def make_delta(base: bytes, ops: bytes, tgt_size: int) -> bytes:
    """组装差量程序：源长度 + 目标长度 + 指令序列。"""
    return varint(len(base)) + varint(tgt_size) + ops


def chain_pack(depth: int) -> bytes:
    """构造 base + depth 层 OFS_DELTA 链的包（每层追加一个字节）。"""
    base = b"A" * 10
    out = bytearray(b"PACK" + struct.pack(">II", 2, depth + 1))
    offsets = []

    def add(type_id, extra, payload):
        offsets.append(len(out))
        out.extend(obj_header(type_id, len(payload)))
        out.extend(extra)
        out.extend(zlib.compress(payload))

    add(BLOB, b"", base)
    cur = base
    for _ in range(depth):
        delta = make_delta(cur, copy_cmd(0, len(cur)) + insert_cmd(b"x"),
                           len(cur) + 1)
        add(OFS_DELTA, encode_ofs(len(out) - offsets[-1]), delta)
        cur = cur + b"x"
    out += hashlib.sha1(bytes(out)).digest()
    return bytes(out)
