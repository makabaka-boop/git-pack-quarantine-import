"""对象库测试：暂存隔离、原子公布、崩溃恢复与清理。"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from gitpack_import.packfile import PackError, compute_oid
from gitpack_import.store import MANIFEST_NAME, ObjectStore
from packutil import BLOB, REF_DELTA, build_pack, insert_cmd, make_delta


def blob_pack(*payloads: bytes) -> bytes:
    return build_pack([(BLOB, b"", p) for p in payloads])[0]


class StoreTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gitpack-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = ObjectStore(self.tmp / "store")

    def staging_dirs(self):
        imports = self.store.imports_dir
        if not imports.is_dir():
            return []
        return [p for p in imports.iterdir() if p.is_dir()]


class ImportTest(StoreTestBase):
    def test_import_publishes_manifest_and_objects(self):
        pack = blob_pack(b"alpha", b"beta")
        manifest = self.store.import_pack(pack, source="t.pack")
        self.assertEqual(manifest["object_count"], 2)
        self.assertEqual(manifest["source"], "t.pack")
        # 清单可读取且内容一致
        on_disk = self.store.read_manifest()
        self.assertEqual(on_disk, manifest)
        # 对象以 Git 松散格式落盘且可回读
        for payload in (b"alpha", b"beta"):
            oid = compute_oid("blob", payload)
            self.assertTrue(self.store.has_object(oid))
            self.assertEqual(self.store.read_object(oid), ("blob", payload))
        # 暂存目录已清理
        self.assertEqual(self.staging_dirs(), [])

    def test_manifest_file_is_valid_json(self):
        self.store.import_pack(blob_pack(b"x"))
        with self.store.manifest_path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data["version"], 1)
        self.assertEqual(len(data["objects"]), 1)

    def test_rejected_pack_leaves_no_trace(self):
        bad = bytearray(blob_pack(b"alpha"))
        bad[-1] ^= 0xFF  # 篡改摘要
        with self.assertRaises(PackError):
            self.store.import_pack(bytes(bad))
        self.assertIsNone(self.store.read_manifest())
        self.assertEqual(self.staging_dirs(), [])
        self.assertFalse(self.store.objects_dir.exists())

    def test_failed_import_preserves_previous_manifest(self):
        first = self.store.import_pack(blob_pack(b"alpha"))
        bad = bytearray(blob_pack(b"beta"))
        bad[20] ^= 0x01
        with self.assertRaises(PackError):
            self.store.import_pack(bytes(bad))
        self.assertEqual(self.store.read_manifest(), first)
        self.assertEqual(self.staging_dirs(), [])

    def test_ref_delta_against_published_object(self):
        # 第一批：基对象
        base = b"published base object"
        self.store.import_pack(blob_pack(base))
        # 第二批：瘦包，REF_DELTA 引用对象库中已有的基对象
        base_oid = bytes.fromhex(compute_oid("blob", base))
        delta = make_delta(base, insert_cmd(b"[" + base + b"]"), len(base) + 2)
        manifest = self.store.import_pack(
            build_pack([(REF_DELTA, base_oid, delta)])[0])
        expect_oid = compute_oid("blob", b"[" + base + b"]")
        self.assertEqual([o["oid"] for o in manifest["objects"]], [expect_oid])
        self.assertEqual(self.store.read_object(expect_oid),
                         ("blob", b"[" + base + b"]"))


class CrashRecoveryTest(StoreTestBase):
    def _crash_on_call(self, call_no):
        """返回一个在第 call_no 次 os.replace 时崩溃的替身函数。"""
        real_replace = os.replace
        state = {"n": 0}

        def flaky(src, dst):
            state["n"] += 1
            if state["n"] == call_no:
                raise RuntimeError("simulated crash")
            return real_replace(src, dst)

        return flaky

    def test_crash_during_object_move_keeps_old_manifest(self):
        old = self.store.import_pack(blob_pack(b"old"))
        pack2 = blob_pack(b"new-1", b"new-2")
        original = os.replace
        os.replace = self._crash_on_call(1)  # 第一次移动对象即崩溃
        try:
            with self.assertRaises(RuntimeError):
                self.store.import_pack(pack2)
        finally:
            os.replace = original
        # 旧清单仍可读取
        self.assertEqual(self.store.read_manifest(), old)
        # 暂存残留可由 cleanup 清理
        self.assertEqual(len(self.staging_dirs()), 1)
        removed = self.store.cleanup()
        self.assertEqual(len(removed), 1)
        self.assertEqual(self.staging_dirs(), [])
        self.assertEqual(self.store.read_manifest(), old)
        # 清理后可正常重新导入
        new = self.store.import_pack(pack2)
        self.assertEqual(new["object_count"], 2)

    def test_crash_at_manifest_replace_keeps_old_manifest(self):
        old = self.store.import_pack(blob_pack(b"old"))
        pack2 = blob_pack(b"new")
        real_replace = os.replace

        def flaky(src, dst):
            if str(dst).endswith(MANIFEST_NAME):
                raise RuntimeError("simulated crash at commit point")
            return real_replace(src, dst)

        os.replace = flaky
        try:
            with self.assertRaises(RuntimeError):
                self.store.import_pack(pack2)
        finally:
            os.replace = real_replace
        # 提交点未到达：旧清单不变
        self.assertEqual(self.store.read_manifest(), old)
        # 对象可能已落盘（内容寻址，无害），暂存可清理
        self.store.cleanup()
        self.assertEqual(self.staging_dirs(), [])
        # 重试成功并更新清单
        new = self.store.import_pack(pack2)
        self.assertEqual(self.store.read_manifest(), new)
        self.assertNotEqual(new, old)

    def test_keyboard_interrupt_style_cancel(self):
        self.store.import_pack(blob_pack(b"old"))
        old = self.store.read_manifest()
        original = ObjectStore._publish

        def cancel(self, staging):
            raise KeyboardInterrupt

        ObjectStore._publish = cancel
        try:
            with self.assertRaises(KeyboardInterrupt):
                self.store.import_pack(blob_pack(b"new"))
        finally:
            ObjectStore._publish = original
        self.assertEqual(self.store.read_manifest(), old)
        self.assertEqual(len(self.staging_dirs()), 1)
        self.store.cleanup()
        self.assertEqual(self.staging_dirs(), [])

    def test_cleanup_ignores_unrelated_files(self):
        self.store.import_pack(blob_pack(b"x"))
        keep = self.store.imports_dir / "notes.txt"
        keep.write_text("keep me", encoding="utf-8")
        stale = self.store.imports_dir / "staging-deadbeef"
        stale.mkdir()
        (stale / "junk").write_text("junk", encoding="utf-8")
        removed = self.store.cleanup()
        self.assertEqual(removed, ["staging-deadbeef"])
        self.assertTrue(keep.exists())
        self.assertIsNotNone(self.store.read_manifest())


if __name__ == "__main__":
    unittest.main()
