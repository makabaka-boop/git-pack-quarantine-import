"""对象库：隔离暂存 + 原子公布的 PACK 导入流程。

目录布局::

    <root>/
      objects/xx/yyyy...      # 已公布的松散对象（Git 兼容，zlib 压缩）
      imports/manifest.json   # 当前导入清单（原子替换，即提交点）
      imports/staging-<uuid>/ # 导入中的暂存区（崩溃残留，可清理）

导入流程：先在内存中完成全部解析与重建核验，再把对象写入暂存目录，
全部落盘后把对象逐个移入 objects/（内容寻址，移动本身幂等安全），
最后以 ``os.replace`` 原子替换清单作为提交点。提交点之前的取消或
崩溃不会破坏旧清单；残留的 staging-* 目录由 ``cleanup()`` 清理。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
import zlib
from datetime import datetime, timezone
from pathlib import Path

from .packfile import PackError, parse_pack, resolve_all

MANIFEST_NAME = "manifest.json"
STAGING_PREFIX = "staging-"


def _fsync_dir(path: Path) -> None:
    """尽力同步目录项；不支持的平台静默跳过。"""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_file_atomic(path: Path, payload: bytes) -> None:
    """在同目录写临时文件、fsync 后原子改名。"""
    tmp = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex}")
    with open(tmp, "wb") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


class ObjectStore:
    def __init__(self, root):
        self.root = Path(root)
        self.objects_dir = self.root / "objects"
        self.imports_dir = self.root / "imports"
        self.manifest_path = self.imports_dir / MANIFEST_NAME

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def read_manifest(self) -> dict | None:
        """读取当前清单；不存在时返回 None。"""
        try:
            with self.manifest_path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return None

    def _loose_path(self, oid: str) -> Path:
        return self.objects_dir / oid[:2] / oid[2:]

    def has_object(self, oid: str) -> bool:
        return self._loose_path(oid).is_file()

    def read_object(self, oid: str) -> tuple[str, bytes] | None:
        """读取松散对象，返回 (type, data)；不存在返回 None。"""
        path = self._loose_path(oid)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        try:
            payload = zlib.decompress(raw)
        except zlib.error as exc:
            raise PackError(f"对象库中 {oid} 损坏: {exc}") from exc
        header, sep, data = payload.partition(b"\0")
        if not sep:
            raise PackError(f"对象库中 {oid} 头格式损坏")
        try:
            type_name, size_s = header.decode("ascii").split(" ")
            if int(size_s) != len(data):
                raise ValueError
        except ValueError:
            raise PackError(f"对象库中 {oid} 声明长度与实际不符") from None
        return type_name, data

    # ------------------------------------------------------------------
    # 导入
    # ------------------------------------------------------------------

    def import_pack(self, pack_bytes: bytes, source: str = "") -> dict:
        """导入一个 PACK 文件；任何校验失败都整批拒绝并抛出 PackError。

        成功返回公布的清单。取消或崩溃发生在清单替换之前时，旧清单
        保持可读，暂存目录残留并可由 cleanup() 清理。
        """
        # 1) 内存中完成全部核验：解析、摘要、重建、对象 ID
        entries = parse_pack(pack_bytes)
        resolved = resolve_all(entries, external=self.read_object)
        objects = {}
        for r in resolved:
            objects.setdefault(r.oid, r)  # 按对象 ID 去重

        manifest = {
            "version": 1,
            "source": source,
            "pack_sha1": hashlib.sha1(pack_bytes).hexdigest(),
            "imported_at": datetime.now(timezone.utc).isoformat(),
            "object_count": len(objects),
            "objects": [
                {"oid": r.oid, "type": r.type_name, "size": len(r.data)}
                for r in sorted(objects.values(), key=lambda r: r.oid)
            ],
        }

        # 2) 写入隔离暂存目录
        staging = self._new_staging()
        staged_objects = staging / "objects"
        for r in objects.values():
            if not self.has_object(r.oid):
                self._write_loose(staged_objects, r.oid, r.type_name, r.data)
        _write_file_atomic(
            staging / MANIFEST_NAME,
            (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
            .encode("utf-8"),
        )

        # 3) 公布：对象移入正式目录（幂等），最后原子替换清单（提交点）
        self._publish(staging)

        # 4) 成功后清理暂存；失败路径中暂存保留，交由 cleanup()
        shutil.rmtree(staging, ignore_errors=True)
        return manifest

    def _new_staging(self) -> Path:
        self.imports_dir.mkdir(parents=True, exist_ok=True)
        staging = self.imports_dir / f"{STAGING_PREFIX}{uuid.uuid4().hex}"
        staging.mkdir()
        return staging

    def _write_loose(self, objects_dir: Path, oid: str,
                     type_name: str, data: bytes) -> None:
        """以 Git 松散对象格式写入（zlib 压缩的 "<type> <len>\\0data"）。"""
        path = objects_dir / oid[:2] / oid[2:]
        if path.exists():
            return  # 内容寻址：同名即同内容
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = zlib.compress(f"{type_name} {len(data)}\0".encode("ascii") + data)
        _write_file_atomic(path, payload)

    def _publish(self, staging: Path) -> None:
        staged_objects = staging / "objects"
        synced: set[Path] = set()
        if staged_objects.is_dir():
            for src in sorted(staged_objects.rglob("*")):
                if not src.is_file():
                    continue
                dst = self.objects_dir / src.relative_to(staged_objects)
                dst.parent.mkdir(parents=True, exist_ok=True)
                os.replace(src, dst)  # 内容寻址，覆盖安全
                if dst.parent not in synced:
                    _fsync_dir(dst.parent)
                    synced.add(dst.parent)
        # 提交点：原子替换清单。此调用成功前崩溃，旧清单保持可读。
        os.replace(staging / MANIFEST_NAME, self.manifest_path)
        _fsync_dir(self.imports_dir)

    # ------------------------------------------------------------------
    # 恢复
    # ------------------------------------------------------------------

    def cleanup(self) -> list[str]:
        """清理所有残留的暂存目录，返回被清理的目录名列表。"""
        removed = []
        if self.imports_dir.is_dir():
            for p in sorted(self.imports_dir.glob(f"{STAGING_PREFIX}*")):
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                    removed.append(p.name)
        return removed
