"""与真实 Git 的集成测试：

* 用 git pack-objects 生成合法包（OFS_DELTA 与 REF_DELTA 两种）；
* 导入后用 git cat-file 逐对象核对类型与内容；
* 覆盖命令行入口。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from gitpack_import.packfile import OBJ_OFS_DELTA, OBJ_REF_DELTA, parse_pack
from gitpack_import.store import ObjectStore

PROJECT_ROOT = Path(__file__).resolve().parent.parent
GIT = shutil.which("git")


def git_env(extra=None):
    env = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", "/tmp"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "Pack Test",
        "GIT_AUTHOR_EMAIL": "pack@example.com",
        "GIT_COMMITTER_NAME": "Pack Test",
        "GIT_COMMITTER_EMAIL": "pack@example.com",
    }
    if extra:
        env.update(extra)
    return env


def git(args, cwd, input_bytes=None, env_extra=None):
    return subprocess.run(
        [GIT, *args], cwd=cwd, input=input_bytes, env=git_env(env_extra),
        check=True, capture_output=True)


def cat_file_batch(oids, cwd, env_extra=None):
    """批量读取对象，返回 {oid: (type, data)}。"""
    proc = git(["cat-file", "--batch"], cwd,
               input_bytes=("\n".join(sorted(oids)) + "\n").encode(),
               env_extra=env_extra)
    out = {}
    buf = proc.stdout
    pos = 0
    while pos < len(buf):
        nl = buf.index(b"\n", pos)
        oid, type_name, size = buf[pos:nl].decode().split(" ")
        pos = nl + 1
        size = int(size)
        out[oid] = (type_name, buf[pos:pos + size])
        pos += size + 1  # 内容后跟一个换行
    return out


@unittest.skipUnless(GIT, "需要 git 可执行文件")
class GitIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="gitpack-git-"))
        cls.repo = cls.tmp / "repo"
        cls.repo.mkdir()
        git(["init", "-q"], cls.repo)
        # 15 个版本的相似文件，确保产生差量链
        content = ""
        for i in range(15):
            content += f"line {i:02d} " + "common-prefix " * (i + 1) + "\n"
            (cls.repo / "growing.txt").write_text(content, encoding="utf-8")
            (cls.repo / "static.bin").write_bytes(
                bytes(range(256)) * (i + 1))
            git(["add", "."], cls.repo)
            git(["commit", "-q", "-m", f"commit {i}"], cls.repo)
        (cls.repo / "empty.txt").write_bytes(b"")
        git(["add", "."], cls.repo)
        git(["commit", "-q", "-m", "add empty file"], cls.repo)
        git(["tag", "-a", "-m", "release", "v1.0"], cls.repo)

        rev = git(["rev-list", "--objects", "--all"], cls.repo)
        cls.oids = {line.split(b" ")[0].decode()
                    for line in rev.stdout.splitlines()}
        refs = git(["for-each-ref", "--format=%(objectname)"], cls.repo)
        cls.oids |= set(refs.stdout.decode().split())
        cls.oids.discard("")
        # 验证仓库：以 GIT_OBJECT_DIRECTORY 指向导入库来读对象
        cls.verify = cls.tmp / "verify"
        cls.verify.mkdir()
        git(["init", "-q"], cls.verify)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, True)

    def make_pack(self, name, extra_args):
        pack_dir = self.tmp / name
        pack_dir.mkdir()
        stdin = ("\n".join(sorted(self.oids)) + "\n").encode()
        # pack-objects 需在仓库内运行，输出用绝对路径前缀
        git(["pack-objects", "--depth=8", "--threads=1", *extra_args,
             str(pack_dir / "pack")], self.repo, input_bytes=stdin)
        return next(pack_dir.glob("pack-*.pack")).read_bytes()

    def import_and_verify(self, pack_bytes, label):
        store = ObjectStore(self.tmp / f"store-{label}")
        manifest = store.import_pack(pack_bytes, source=label)
        # 清单对象集合与源仓库对象集合一致
        manifest_oids = {o["oid"] for o in manifest["objects"]}
        self.assertEqual(manifest_oids, self.oids)
        self.assertEqual(manifest["object_count"], len(self.oids))
        # 用 git cat-file 逐对象核对：类型与内容必须与源仓库一致
        truth = cat_file_batch(self.oids, cwd=self.repo)
        got = cat_file_batch(
            self.oids, cwd=self.verify,
            env_extra={
                "GIT_OBJECT_DIRECTORY": str(store.objects_dir),
                "GIT_ALTERNATE_OBJECT_DIRECTORIES": "",
            })
        self.assertEqual(got, truth)
        return store

    def test_ofs_delta_pack(self):
        pack = self.make_pack("pack-ofs", ["--delta-base-offset"])
        entries = parse_pack(pack)
        kinds = {e.type_id for e in entries}
        self.assertIn(OBJ_OFS_DELTA, kinds, "测试包应包含 OFS_DELTA")
        self.assertNotIn(OBJ_REF_DELTA, kinds)
        self.import_and_verify(pack, "ofs")

    def test_ref_delta_pack(self):
        pack = self.make_pack("pack-ref", [])
        entries = parse_pack(pack)
        kinds = {e.type_id for e in entries}
        self.assertIn(OBJ_REF_DELTA, kinds, "测试包应包含 REF_DELTA")
        self.import_and_verify(pack, "ref")

    def test_tampered_checksum_rejected_by_importer(self):
        pack = bytearray(self.make_pack("pack-tamper", ["--delta-base-offset"]))
        pack[-3] ^= 0xFF
        store = ObjectStore(self.tmp / "store-tamper")
        with self.assertRaises(Exception):
            store.import_pack(bytes(pack))
        self.assertIsNone(store.read_manifest())


@unittest.skipUnless(GIT, "需要 git 可执行文件")
class CliTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="gitpack-cli-"))
        repo = cls.tmp / "repo"
        repo.mkdir()
        git(["init", "-q"], repo)
        (repo / "f.txt").write_text("hello\n", encoding="utf-8")
        git(["add", "."], repo)
        git(["commit", "-q", "-m", "c0"], repo)
        oids = git(["rev-list", "--objects", "HEAD"], repo).stdout
        stdin = b"\n".join(line.split(b" ")[0] for line in oids.splitlines())
        git(["pack-objects", "--depth=8", "pack"], repo, input_bytes=stdin)
        cls.pack = next(repo.glob("pack-*.pack"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, True)

    def run_cli(self, *args):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(PROJECT_ROOT)
        return subprocess.run(
            [sys.executable, "-m", "gitpack_import", *args],
            capture_output=True, text=True, cwd=self.tmp, env=env)

    def test_cli_import_manifest_cleanup(self):
        store = self.tmp / "store"
        proc = self.run_cli("import", str(self.pack), "--store", str(store))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("导入成功", proc.stdout)

        proc = self.run_cli("manifest", "--store", str(store))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('"object_count"', proc.stdout)

        # 伪造崩溃残留的暂存目录，cleanup 应清理它
        stale = store / "imports" / "staging-deadbeef"
        stale.mkdir(parents=True)
        (stale / "junk").write_text("x", encoding="utf-8")
        proc = self.run_cli("cleanup", "--store", str(store))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(stale.exists())
        # 清单在清理后仍可读
        proc = self.run_cli("manifest", "--store", str(store))
        self.assertIn('"object_count"', proc.stdout)

    def test_cli_rejects_tampered_pack(self):
        store = self.tmp / "store2"
        bad = self.tmp / "bad.pack"
        data = bytearray(self.pack.read_bytes())
        data[-1] ^= 0xFF
        bad.write_bytes(bytes(data))
        proc = self.run_cli("import", str(bad), "--store", str(store))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("导入被拒绝", proc.stderr)
        proc = self.run_cli("manifest", "--store", str(store))
        self.assertIn("尚无导入清单", proc.stdout)

    def test_cli_rejects_oversized_pack(self):
        store = self.tmp / "store3"
        big = self.tmp / "big.pack"
        big.write_bytes(b"\x00" * (8 * 1024 * 1024 + 1))
        proc = self.run_cli("import", str(big), "--store", str(store))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("超过上限", proc.stderr)


if __name__ == "__main__":
    unittest.main()
