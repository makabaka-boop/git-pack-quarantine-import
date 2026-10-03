"""解析器与差量重建的单元测试（不依赖 git）。"""

from __future__ import annotations

import hashlib
import struct
import unittest
import zlib

from gitpack_import.packfile import (
    MAX_OBJECT_SIZE,
    OBJ_OFS_DELTA,
    OBJ_REF_DELTA,
    PackError,
    RawEntry,
    apply_delta,
    compute_oid,
    parse_pack,
    resolve_all,
)
from packutil import (
    BLOB,
    OFS_DELTA,
    REF_DELTA,
    build_pack,
    chain_pack,
    copy_cmd,
    encode_ofs,
    insert_cmd,
    make_delta,
    obj_header,
    varint,
)


def simple_pack(entries):
    """便捷包装：只要包字节。"""
    return build_pack(entries)[0]


class ParseValidTest(unittest.TestCase):
    def test_plain_objects_roundtrip(self):
        blobs = [b"hello world", b"", b"\x00\x01\x02" * 100]
        pack = simple_pack([(BLOB, b"", b) for b in blobs])
        entries = parse_pack(pack)
        self.assertEqual(len(entries), 3)
        objs = resolve_all(entries)
        self.assertEqual([o.data for o in objs], blobs)
        self.assertEqual(
            [o.oid for o in objs],
            [compute_oid("blob", b) for b in blobs])
        self.assertTrue(all(o.depth == 0 for o in objs))

    def test_empty_pack(self):
        pack = simple_pack([])
        self.assertEqual(parse_pack(pack), [])
        self.assertEqual(resolve_all(parse_pack(pack)), [])

    def test_ofs_delta_roundtrip(self):
        base = b"The quick brown fox jumps over the lazy dog"
        # "The quick " + "red" + " fox jumps over the lazy dog"
        expect = b"The quick red fox jumps over the lazy dog"
        delta = make_delta(base,
                           copy_cmd(0, 10) + insert_cmd(b"red")
                           + copy_cmd(15, len(base) - 15),
                           len(expect))
        # 手工构造：OFS 距离 = delta 条目起点 - 基对象条目起点
        out = bytearray(b"PACK" + struct.pack(">II", 2, 2))
        off0 = len(out)
        out += obj_header(BLOB, len(base)) + zlib.compress(base)
        off1 = len(out)
        out += (obj_header(OFS_DELTA, len(delta))
                + encode_ofs(off1 - off0) + zlib.compress(delta))
        out += hashlib.sha1(bytes(out)).digest()
        objs = resolve_all(parse_pack(bytes(out)))
        self.assertEqual(objs[1].data, expect)
        self.assertEqual(objs[1].type_name, "blob")
        self.assertEqual(objs[1].depth, 1)
        self.assertEqual(objs[1].oid, compute_oid("blob", expect))

    def test_ref_delta_roundtrip(self):
        base = b"base content for ref delta"
        base_oid = bytes.fromhex(compute_oid("blob", base))
        delta = make_delta(base, copy_cmd(0, 4) + insert_cmd(b"NEW"), 7)
        pack = simple_pack([(BLOB, b"", base), (REF_DELTA, base_oid, delta)])
        objs = resolve_all(parse_pack(pack))
        self.assertEqual(objs[1].data, b"baseNEW")
        self.assertEqual(objs[1].depth, 1)

    def test_ref_delta_external_base(self):
        base = b"external base object"
        ext_oid = compute_oid("blob", base)
        delta = make_delta(base, copy_cmd(0, 8) + insert_cmd(b"!"), 9)
        pack = simple_pack([(REF_DELTA, bytes.fromhex(ext_oid), delta)])
        seen = []

        def external(oid_hex):
            seen.append(oid_hex)
            return ("blob", base) if oid_hex == ext_oid else None

        objs = resolve_all(parse_pack(pack), external=external)
        self.assertEqual(objs[0].data, b"external!")
        self.assertEqual(seen, [ext_oid])

    def test_delta_chain_at_limit_ok(self):
        pack = chain_pack(8)  # 恰好 8 层
        objs = resolve_all(parse_pack(pack))
        self.assertEqual(objs[-1].depth, 8)
        self.assertEqual(objs[-1].data, b"A" * 10 + b"x" * 8)


class ParseCorruptTest(unittest.TestCase):
    def test_oversized_pack_rejected(self):
        with self.assertRaisesRegex(PackError, "超过上限"):
            parse_pack(b"\x00" * (8 * 1024 * 1024 + 1))

    def test_too_small(self):
        with self.assertRaisesRegex(PackError, "过小"):
            parse_pack(b"PACK")

    def test_bad_signature(self):
        pack = bytearray(simple_pack([(BLOB, b"", b"x")]))
        pack[0:4] = b"PAKC"
        with self.assertRaisesRegex(PackError, "签名"):
            parse_pack(bytes(pack))

    def test_bad_version(self):
        pack = bytearray(simple_pack([(BLOB, b"", b"x")]))
        pack[7] = 3  # version = 3
        # 修正摘要以隔离版本检查
        pack[-20:] = hashlib.sha1(bytes(pack[:-20])).digest()
        with self.assertRaisesRegex(PackError, "版本"):
            parse_pack(bytes(pack))

    def test_tampered_trailer(self):
        pack = bytearray(simple_pack([(BLOB, b"", b"x")]))
        pack[-1] ^= 0xFF
        with self.assertRaisesRegex(PackError, "摘要"):
            parse_pack(bytes(pack))

    def test_tampered_payload_detected_by_checksum(self):
        pack = bytearray(simple_pack([(BLOB, b"", b"some content")]))
        pack[20] ^= 0x01  # 改内容不改摘要
        with self.assertRaisesRegex(PackError, "摘要"):
            parse_pack(bytes(pack))

    def test_too_many_objects(self):
        entries = [(BLOB, b"", b"x")] * 201
        pack = simple_pack(entries)
        with self.assertRaisesRegex(PackError, "对象数"):
            parse_pack(pack)

    def test_declared_size_mismatch(self):
        payload = zlib.compress(b"hello")
        body = b"PACK" + struct.pack(">II", 2, 1) + obj_header(BLOB, 6) + payload
        pack = body + hashlib.sha1(body).digest()
        with self.assertRaisesRegex(PackError, "声明长度"):
            parse_pack(pack)

    def test_inflated_larger_than_declared(self):
        payload = zlib.compress(b"hello!")
        body = b"PACK" + struct.pack(">II", 2, 1) + obj_header(BLOB, 5) + payload
        pack = body + hashlib.sha1(body).digest()
        with self.assertRaisesRegex(PackError, "声明长度"):
            parse_pack(pack)

    def test_corrupt_zlib_stream(self):
        body = b"PACK" + struct.pack(">II", 2, 1) + obj_header(BLOB, 5) + b"\x00" * 10
        pack = body + hashlib.sha1(body).digest()
        with self.assertRaisesRegex(PackError, "zlib"):
            parse_pack(pack)

    def test_truncated_zlib_stream(self):
        payload = zlib.compress(b"hello")[:-3]
        body = b"PACK" + struct.pack(">II", 2, 1) + obj_header(BLOB, 5) + payload
        pack = body + hashlib.sha1(body).digest()
        with self.assertRaisesRegex(PackError, "截断"):
            parse_pack(pack)

    def test_trailing_garbage(self):
        body = bytearray(simple_pack([(BLOB, b"", b"x")])[:-20])
        body += b"GARBAGE"
        pack = bytes(body) + hashlib.sha1(bytes(body)).digest()
        with self.assertRaisesRegex(PackError, "多余或缺失"):
            parse_pack(pack)

    def test_unknown_object_type(self):
        pack = simple_pack([(5, b"", b"x")])
        with self.assertRaisesRegex(PackError, "未知对象类型"):
            parse_pack(pack)

    def test_object_over_1mb_rejected(self):
        big = b"\xab" * (MAX_OBJECT_SIZE + 1)
        pack = simple_pack([(BLOB, b"", big)])
        with self.assertRaisesRegex(PackError, "超过上限"):
            parse_pack(pack)

    def test_ofs_base_not_at_entry_start(self):
        # 基对象偏移指向包头部而非任何条目起点
        pack = simple_pack([(OFS_DELTA, encode_ofs(1), b"\x00\x00")])
        with self.assertRaisesRegex(PackError, "已有对象|非法"):
            parse_pack(pack)

    def test_ofs_distance_zero_is_self_reference(self):
        body = b"PACK" + struct.pack(">II", 2, 1)
        body += obj_header(OFS_DELTA, 2) + encode_ofs(0) + zlib.compress(b"\x00\x00")
        pack = body + hashlib.sha1(body).digest()
        with self.assertRaisesRegex(PackError, "非法"):
            parse_pack(pack)


class MaliciousDeltaTest(unittest.TestCase):
    def test_copy_out_of_range(self):
        base = b"hello"
        delta = make_delta(base, copy_cmd(3, 10), 13)  # 3+10 > 5
        pack = simple_pack([(BLOB, b"", base),
                            (REF_DELTA, bytes.fromhex(compute_oid("blob", base)), delta)])
        with self.assertRaisesRegex(PackError, "超出基对象"):
            resolve_all(parse_pack(pack))

    def test_source_size_mismatch(self):
        base = b"hello"
        delta = varint(999) + varint(5) + copy_cmd(0, 5)
        pack = simple_pack([(BLOB, b"", base),
                            (REF_DELTA, bytes.fromhex(compute_oid("blob", base)), delta)])
        with self.assertRaisesRegex(PackError, "源长度"):
            resolve_all(parse_pack(pack))

    def test_result_size_mismatch(self):
        base = b"hello"
        delta = make_delta(base, copy_cmd(0, 3), 5)  # 只产出 3 字节
        pack = simple_pack([(BLOB, b"", base),
                            (REF_DELTA, bytes.fromhex(compute_oid("blob", base)), delta)])
        with self.assertRaisesRegex(PackError, "目标长度"):
            resolve_all(parse_pack(pack))

    def test_opcode_zero(self):
        base = b"hello"
        delta = make_delta(base, b"\x00", 0)
        pack = simple_pack([(BLOB, b"", base),
                            (REF_DELTA, bytes.fromhex(compute_oid("blob", base)), delta)])
        with self.assertRaisesRegex(PackError, "操作码"):
            resolve_all(parse_pack(pack))

    def test_insert_truncated(self):
        base = b"hello"
        delta = make_delta(base, bytes([10]) + b"ab", 10)  # 声称插入 10 字节只有 2
        pack = simple_pack([(BLOB, b"", base),
                            (REF_DELTA, bytes.fromhex(compute_oid("blob", base)), delta)])
        with self.assertRaisesRegex(PackError, "截断"):
            resolve_all(parse_pack(pack))

    def test_delta_chain_too_deep(self):
        pack = chain_pack(9)  # 9 层，超过上限 8
        with self.assertRaisesRegex(PackError, "超过.*层|差量链"):
            resolve_all(parse_pack(pack))

    def test_delta_expansion_over_1mb(self):
        # 小基对象 + 反复拷贝，目标长度超过 1 MB
        base = b"B" * 100
        tgt = MAX_OBJECT_SIZE + 24  # 超过上限，且为 100 的倍数
        ops = copy_cmd(0, 100) * (tgt // 100)
        delta = make_delta(base, ops, tgt)
        self.assertLess(len(delta), MAX_OBJECT_SIZE)  # 差量程序本身很小
        pack = simple_pack([(BLOB, b"", base),
                            (REF_DELTA, bytes.fromhex(compute_oid("blob", base)), delta)])
        with self.assertRaisesRegex(PackError, "超过上限"):
            resolve_all(parse_pack(pack))

    def test_ref_delta_missing_base(self):
        delta = make_delta(b"whatever", insert_cmd(b"x"), 1)
        pack = simple_pack([(REF_DELTA, b"\x11" * 20, delta)])
        with self.assertRaisesRegex(PackError, "缺失"):
            resolve_all(parse_pack(pack))

    def test_ofs_delta_missing_base_whitebox(self):
        # 绕过解析器，直接构造指向不存在偏移的条目
        e = RawEntry(offset=100, type_id=OBJ_OFS_DELTA, declared_size=2,
                     data=b"\x00\x00", base_offset=999)
        with self.assertRaisesRegex(PackError, "缺失"):
            resolve_all([e])

    def test_delta_dependency_cycle_whitebox(self):
        # 两个 OFS_DELTA 互相以对方为基：解析器不会放行，但重建器必须防御
        e0 = RawEntry(offset=100, type_id=OBJ_OFS_DELTA, declared_size=2,
                      data=b"\x00\x00", base_offset=200)
        e1 = RawEntry(offset=200, type_id=OBJ_OFS_DELTA, declared_size=2,
                      data=b"\x00\x00", base_offset=100)
        with self.assertRaisesRegex(PackError, "依赖环"):
            resolve_all([e0, e1])


class ApplyDeltaTest(unittest.TestCase):
    def test_copy_64k_length_encoding(self):
        base = bytes(65536 + 10)
        delta = make_delta(base, copy_cmd(0, 0x10000), 0x10000)
        self.assertEqual(apply_delta(base, delta), bytes(0x10000))

    def test_delta_header_truncated(self):
        with self.assertRaisesRegex(PackError, "截断"):
            apply_delta(b"abc", b"\x83")  # 源长度变长整数未结束

    def test_copy_instruction_truncated(self):
        base = b"hello"
        delta = make_delta(base, bytes([0x81]), 5)  # 声明偏移字节但缺失
        with self.assertRaisesRegex(PackError, "截断"):
            apply_delta(base, delta)


if __name__ == "__main__":
    unittest.main()
