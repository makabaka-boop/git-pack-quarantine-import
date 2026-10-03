"""Git SHA-1 PACK v2 解析与差量重建。

完全自实现，不调用 ``git index-pack``。解析阶段核对：

* 包尾 SHA-1 摘要；
* 每个对象的 zlib 压缩流边界（流必须恰好结束于下一对象起点）；
* 对象头声明长度与实际解压长度一致；
* OFS_DELTA 相对偏移必须指向包内已有对象起点；
* 差量程序的源长度、目标长度、拷贝范围、插入字节数。

重建阶段强制：差量链不超过 ``MAX_DELTA_DEPTH`` 层、单对象重建结果
不超过 ``MAX_OBJECT_SIZE``；缺失基对象或依赖环会导致整批拒绝。
"""

from __future__ import annotations

import hashlib
import hmac
import struct
import zlib
from dataclasses import dataclass

PACK_SIGNATURE = b"PACK"
PACK_VERSION = 2
HEADER_SIZE = 12
TRAILER_SIZE = 20  # SHA-1 摘要长度

MAX_PACK_SIZE = 8 * 1024 * 1024    # 包文件上限：8 MB
MAX_OBJECTS = 200                  # 单包对象数上限
MAX_DELTA_DEPTH = 8                # 差量链最大层数
MAX_OBJECT_SIZE = 1 * 1024 * 1024  # 单对象重建上限：1 MB

OBJ_COMMIT = 1
OBJ_TREE = 2
OBJ_BLOB = 3
OBJ_TAG = 4
OBJ_OFS_DELTA = 6
OBJ_REF_DELTA = 7

TYPE_NAMES = {
    OBJ_COMMIT: "commit",
    OBJ_TREE: "tree",
    OBJ_BLOB: "blob",
    OBJ_TAG: "tag",
}


class PackError(Exception):
    """包校验或重建失败；调用方应整批拒绝。"""


@dataclass
class RawEntry:
    """解析阶段产出的原始条目（差量尚未重建）。"""

    offset: int               # 条目在包内的起始偏移
    type_id: int              # 对象类型编号
    declared_size: int        # 对象头声明的解压长度
    data: bytes               # 解压后的载荷（差量条目为差量程序）
    base_offset: int = -1     # OFS_DELTA：基对象在包内的起始偏移
    base_id: bytes = b""      # REF_DELTA：基对象 SHA-1（20 字节）


@dataclass
class ResolvedObject:
    """重建完成的规范对象。"""

    oid: str
    type_name: str
    data: bytes
    depth: int                # 差量链层数（非差量对象为 0）


def compute_oid(type_name: str, data: bytes) -> str:
    """按 Git 规则计算对象 ID：sha1("<type> <len>\\0" + data)。"""
    h = hashlib.sha1(f"{type_name} {len(data)}\0".encode("ascii"))
    h.update(data)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------

def _parse_obj_header(buf: bytes, pos: int, end: int) -> tuple[int, int, int]:
    """解析对象类型/长度变长头，返回 (type_id, size, new_pos)。"""
    if pos >= end:
        raise PackError("对象头截断")
    b = buf[pos]
    pos += 1
    type_id = (b >> 4) & 0x07
    size = b & 0x0F
    shift = 4
    while b & 0x80:
        if pos >= end:
            raise PackError("对象头截断")
        b = buf[pos]
        pos += 1
        size |= (b & 0x7F) << shift
        shift += 7
        if shift > 63:
            raise PackError("对象长度字段过大")
    return type_id, size, pos


def _parse_ofs_distance(buf: bytes, pos: int, end: int) -> tuple[int, int]:
    """解析 OFS_DELTA 的基对象距离编码，返回 (distance, new_pos)。"""
    if pos >= end:
        raise PackError("OFS_DELTA 偏移字段截断")
    b = buf[pos]
    pos += 1
    distance = b & 0x7F
    while b & 0x80:
        if pos >= end:
            raise PackError("OFS_DELTA 偏移字段截断")
        b = buf[pos]
        pos += 1
        distance = ((distance + 1) << 7) | (b & 0x7F)
        if distance >= 1 << 63:
            raise PackError("OFS_DELTA 偏移字段过大")
    return distance, pos


def _inflate(buf: bytes, pos: int, end: int, expected: int) -> tuple[bytes, int]:
    """解压一个 zlib 流并确定其精确边界。

    返回 (数据, 流结束位置)。校验：解压长度必须等于声明长度，且流必须
    在输入内正常结束（既未截断也不越界）。
    """
    if pos > end:
        raise PackError("压缩流起点越界")
    decomp = zlib.decompressobj()
    source = buf[pos:end]
    try:
        # 限长解压：超过声明长度 +1 即视为不符，避免解压炸弹
        out = decomp.decompress(source, expected + 1)
        while not decomp.eof and decomp.unconsumed_tail and len(out) <= expected:
            out += decomp.decompress(decomp.unconsumed_tail, expected + 1 - len(out))
    except zlib.error as exc:
        raise PackError(f"zlib 流损坏: {exc}") from exc
    if len(out) != expected:
        raise PackError(f"解压长度 {len(out)} 与声明长度 {expected} 不符")
    if not decomp.eof:
        raise PackError("zlib 流截断")
    consumed = len(source) - len(decomp.unused_data)
    return out, pos + consumed


def parse_pack(buf: bytes) -> list[RawEntry]:
    """解析并校验整个 PACK v2 文件，返回原始条目列表。"""
    if len(buf) > MAX_PACK_SIZE:
        raise PackError(f"包大小 {len(buf)} 字节超过上限 {MAX_PACK_SIZE}")
    if len(buf) < HEADER_SIZE + TRAILER_SIZE:
        raise PackError("包文件过小")
    if buf[:4] != PACK_SIGNATURE:
        raise PackError("缺少 PACK 签名")
    version, count = struct.unpack(">II", buf[4:12])
    if version != PACK_VERSION:
        raise PackError(f"不支持的包版本 {version}")
    if count > MAX_OBJECTS:
        raise PackError(f"对象数 {count} 超过上限 {MAX_OBJECTS}")

    # 包摘要：尾部 20 字节须等于此前全部字节的 SHA-1
    expect = hashlib.sha1(buf[:-TRAILER_SIZE]).digest()
    if not hmac.compare_digest(expect, buf[-TRAILER_SIZE:]):
        raise PackError("包摘要校验失败")

    pos = HEADER_SIZE
    end = len(buf) - TRAILER_SIZE
    entries: list[RawEntry] = []
    seen_offsets: set[int] = set()
    for _ in range(count):
        start = pos
        if start >= end:
            raise PackError("包数据截断：对象头越界")
        type_id, size, pos = _parse_obj_header(buf, pos, end)
        base_offset = -1
        base_id = b""
        if type_id == OBJ_OFS_DELTA:
            distance, pos = _parse_ofs_distance(buf, pos, end)
            if distance <= 0 or distance > start - HEADER_SIZE:
                raise PackError(f"OFS_DELTA 相对偏移非法: {distance}")
            base_offset = start - distance
            if base_offset not in seen_offsets:
                raise PackError("OFS_DELTA 基对象偏移未指向包内已有对象")
        elif type_id == OBJ_REF_DELTA:
            if pos + 20 > end:
                raise PackError("REF_DELTA 基对象 ID 截断")
            base_id = buf[pos:pos + 20]
            pos += 20
        elif type_id not in TYPE_NAMES:
            raise PackError(f"未知对象类型 {type_id}")
        if type_id in TYPE_NAMES and size > MAX_OBJECT_SIZE:
            raise PackError(f"对象声明长度 {size} 超过上限 {MAX_OBJECT_SIZE}")
        data, pos = _inflate(buf, pos, end, size)
        entries.append(RawEntry(start, type_id, size, data, base_offset, base_id))
        seen_offsets.add(start)
    if pos != end:
        raise PackError("对象区与包摘要之间存在多余或缺失字节")
    return entries


# ---------------------------------------------------------------------------
# 差量重建
# ---------------------------------------------------------------------------

def _read_delta_varint(data: bytes, pos: int) -> tuple[int, int]:
    """读取差量头中的小端 7 位变长整数。"""
    result = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise PackError("差量头截断")
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
        if shift > 63:
            raise PackError("差量长度字段过大")
    return result, pos


def apply_delta(base: bytes, delta: bytes) -> bytes:
    """应用差量程序，返回重建结果；全程核对拷贝范围与长度。"""
    src_size, pos = _read_delta_varint(delta, 0)
    if src_size != len(base):
        raise PackError(
            f"差量源长度 {src_size} 与基对象长度 {len(base)} 不符")
    tgt_size, pos = _read_delta_varint(delta, pos)
    if tgt_size > MAX_OBJECT_SIZE:
        raise PackError(f"差量目标长度 {tgt_size} 超过上限 {MAX_OBJECT_SIZE}")
    out = bytearray()
    while pos < len(delta):
        cmd = delta[pos]
        pos += 1
        if cmd & 0x80:
            # 从基对象拷贝：低位 4 比特标记偏移字节，次 3 比特标记长度字节
            offset = 0
            length = 0
            for i in range(4):
                if cmd & (1 << i):
                    if pos >= len(delta):
                        raise PackError("差量拷贝指令截断")
                    offset |= delta[pos] << (8 * i)
                    pos += 1
            for i in range(3):
                if cmd & (0x10 << i):
                    if pos >= len(delta):
                        raise PackError("差量拷贝指令截断")
                    length |= delta[pos] << (8 * i)
                    pos += 1
            if length == 0:
                length = 0x10000
            if offset + length > len(base):
                raise PackError(
                    f"差量拷贝范围 [{offset}, {offset + length}) "
                    f"超出基对象长度 {len(base)}")
            out += base[offset:offset + length]
        elif cmd:
            if pos + cmd > len(delta):
                raise PackError("差量插入指令截断")
            out += delta[pos:pos + cmd]
            pos += cmd
        else:
            raise PackError("差量操作码 0 非法")
        if len(out) > tgt_size:
            raise PackError("差量输出超过声明的目标长度")
    if len(out) != tgt_size:
        raise PackError(
            f"差量结果长度 {len(out)} 与目标长度 {tgt_size} 不符")
    return bytes(out)


def resolve_all(
    entries: list[RawEntry],
    external=None,
) -> list[ResolvedObject]:
    """重建全部对象，返回按包内顺序排列的 ResolvedObject 列表。

    ``external``：可选回调 ``external(oid_hex) -> (type_name, data) | None``，
    用于解析基对象不在包内的 REF_DELTA（瘦包）。缺失基对象、依赖环、
    差量链超过 MAX_DELTA_DEPTH 层均抛出 PackError。
    """
    n = len(entries)
    by_offset = {e.offset: i for i, e in enumerate(entries)}
    types: list[str | None] = [None] * n
    datas: list[bytes | None] = [None] * n
    oids: list[str | None] = [None] * n
    depths = [0] * n
    oid_to_index: dict[str, int] = {}

    def finish(i: int, type_name: str, data: bytes) -> None:
        types[i] = type_name
        datas[i] = data
        oids[i] = compute_oid(type_name, data)
        oid_to_index[oids[i]] = i

    # 非差量对象直接落定
    for i, e in enumerate(entries):
        if e.type_id in TYPE_NAMES:
            finish(i, TYPE_NAMES[e.type_id], e.data)

    remaining = [i for i in range(n) if types[i] is None]
    while remaining:
        progressed = False
        for i in list(remaining):
            e = entries[i]
            base = None
            base_depth = 0
            if e.type_id == OBJ_OFS_DELTA:
                j = by_offset.get(e.base_offset)
                if j is None:
                    raise PackError(
                        f"偏移 {e.offset} 处对象的基对象缺失")
                if types[j] is not None:
                    base = (types[j], datas[j])
                    base_depth = depths[j]
            else:  # REF_DELTA
                hex_id = e.base_id.hex()
                j = oid_to_index.get(hex_id)
                if j is not None:
                    base = (types[j], datas[j])
                    base_depth = depths[j]
                elif external is not None:
                    found = external(hex_id)
                    if found is not None:
                        base = found
            if base is None:
                continue
            if base_depth + 1 > MAX_DELTA_DEPTH:
                raise PackError(
                    f"偏移 {e.offset} 处对象的差量链超过 "
                    f"{MAX_DELTA_DEPTH} 层")
            result = apply_delta(base[1], e.data)
            finish(i, base[0], result)
            depths[i] = base_depth + 1
            remaining.remove(i)
            progressed = True
        if not progressed:
            remaining_set = set(remaining)
            for i in remaining:
                e = entries[i]
                if e.type_id == OBJ_OFS_DELTA:
                    j = by_offset.get(e.base_offset)
                    if j is None or j not in remaining_set:
                        raise PackError(
                            f"偏移 {e.offset} 处对象的基对象缺失")
                else:
                    hex_id = e.base_id.hex()
                    raise PackError(
                        f"REF_DELTA 基对象 {hex_id} 缺失或不可解析")
            raise PackError("差量对象之间存在依赖环")

    return [
        ResolvedObject(oids[i], types[i], datas[i], depths[i])
        for i in range(n)
    ]
