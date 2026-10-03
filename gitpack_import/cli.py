"""命令行入口：import / manifest / cleanup。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .packfile import MAX_PACK_SIZE, PackError
from .store import ObjectStore


def _cmd_import(args) -> int:
    pack_path = Path(args.pack)
    try:
        size = pack_path.stat().st_size
    except OSError as exc:
        print(f"无法读取包文件: {exc}", file=sys.stderr)
        return 2
    if size > MAX_PACK_SIZE:
        print(f"导入被拒绝: 包大小 {size} 字节超过上限 {MAX_PACK_SIZE}",
              file=sys.stderr)
        return 1
    store = ObjectStore(args.store)
    try:
        manifest = store.import_pack(pack_path.read_bytes(),
                                     source=str(pack_path))
    except PackError as exc:
        print(f"导入被拒绝: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已取消；旧清单保持有效，可用 cleanup 清理暂存目录",
              file=sys.stderr)
        return 130
    counts: dict[str, int] = {}
    for obj in manifest["objects"]:
        counts[obj["type"]] = counts.get(obj["type"], 0) + 1
    detail = ", ".join(f"{t}×{n}" for t, n in sorted(counts.items()))
    print(f"导入成功: {manifest['object_count']} 个对象"
          + (f"（{detail}）" if detail else "")
          + f" → {store.root}")
    return 0


def _cmd_manifest(args) -> int:
    manifest = ObjectStore(args.store).read_manifest()
    if manifest is None:
        print("（尚无导入清单）")
        return 0
    print(json.dumps(manifest, indent=2, ensure_ascii=False))
    return 0


def _cmd_cleanup(args) -> int:
    removed = ObjectStore(args.store).cleanup()
    for name in removed:
        print(f"已清理 {name}")
    print(f"共清理 {len(removed)} 个暂存目录")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="gitpack-import",
        description="Git PACK v2 (SHA-1) 安全导入工具（自实现解析，"
                    "不调用 git index-pack）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_import = sub.add_parser("import", help="校验并导入 PACK 文件")
    p_import.add_argument("pack", help="PACK 文件路径")
    p_import.add_argument("--store", required=True, help="对象库根目录")
    p_import.set_defaults(func=_cmd_import)

    p_manifest = sub.add_parser("manifest", help="显示当前导入清单")
    p_manifest.add_argument("--store", required=True, help="对象库根目录")
    p_manifest.set_defaults(func=_cmd_manifest)

    p_cleanup = sub.add_parser("cleanup", help="清理崩溃/取消残留的暂存目录")
    p_cleanup.add_argument("--store", required=True, help="对象库根目录")
    p_cleanup.set_defaults(func=_cmd_cleanup)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
