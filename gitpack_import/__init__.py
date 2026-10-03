"""Git PACK v2 (SHA-1) 安全导入工具。"""

from .packfile import (
    MAX_DELTA_DEPTH,
    MAX_OBJECTS,
    MAX_OBJECT_SIZE,
    MAX_PACK_SIZE,
    OBJ_OFS_DELTA,
    OBJ_REF_DELTA,
    PackError,
    RawEntry,
    ResolvedObject,
    apply_delta,
    compute_oid,
    parse_pack,
    resolve_all,
)
from .store import ObjectStore

__version__ = "0.1.0"

__all__ = [
    "MAX_DELTA_DEPTH",
    "MAX_OBJECTS",
    "MAX_OBJECT_SIZE",
    "MAX_PACK_SIZE",
    "OBJ_OFS_DELTA",
    "OBJ_REF_DELTA",
    "ObjectStore",
    "PackError",
    "RawEntry",
    "ResolvedObject",
    "apply_delta",
    "compute_oid",
    "parse_pack",
    "resolve_all",
    "__version__",
]
