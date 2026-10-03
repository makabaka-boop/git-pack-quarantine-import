# gitpack-import

Git PACK v2（SHA-1）安全导入工具。解析器完全自实现，**不调用
`git index-pack`**；全部对象核验通过后才原子公布导入清单。

## 安全限制

| 限制 | 取值 |
| --- | --- |
| 包文件大小 | ≤ 8 MB |
| 单包对象数 | ≤ 200 |
| 差量链层数 | ≤ 8 |
| 单对象重建结果 | ≤ 1 MB |

## 解析与校验（`gitpack_import/packfile.py`）

- 核对 `PACK` 签名、版本 2、对象数上限；
- 核对包尾 20 字节 SHA-1 摘要（覆盖此前全部字节）；
- 逐对象解析变长类型/长度头，用限长解压确定 **zlib 流精确边界**，
  解压长度必须等于声明长度，对象区必须恰好衔接包摘要；
- 支持普通对象（commit/tree/blob/tag）及包内 `OFS_DELTA`、`REF_DELTA`；
  OFS 距离必须指向包内已有对象起点，REF 基对象可来自包内或已公布
  的对象库（瘦包）；
- 差量程序逐条校验：源长度与基对象一致、拷贝范围不越界、插入字节
  完整、结果长度等于目标长度、操作码 0 非法；
- 重建规范对象字节并计算对象 ID（`sha1("<type> <len>\0" + data)`）；
- **整批拒绝**：缺失基对象、差量依赖环、链超深、重建超限、摘要或
  长度不符——任何一种失败都不会在对象库留下任何痕迹。

## 原子公布与崩溃恢复（`gitpack_import/store.py`）

```
<store>/
  objects/xx/yyyy...      # 已公布的松散对象（Git 兼容，git cat-file 可读）
  imports/manifest.json   # 当前导入清单（原子替换 = 提交点）
  imports/staging-<uuid>/ # 导入中的暂存区
```

1. 内存中完成全部解析与重建核验（失败时不触碰磁盘）；
2. 对象以 Git 松散格式写入隔离暂存目录，清单先写入暂存；
3. 对象逐个移入 `objects/`（内容寻址，移动幂等安全）；
4. `os.replace` 原子替换清单——这是唯一的提交点。

提交点之前取消或崩溃：旧清单保持可读，残留的 `staging-*` 目录由
`cleanup` 清理；对象移动是幂等的，重试导入即可收敛。

## 用法

```sh
python3 -m gitpack_import import path/to.pack --store /path/to/store
python3 -m gitpack_import manifest --store /path/to/store
python3 -m gitpack_import cleanup  --store /path/to/store
```

退出码：`0` 成功；`1` 包校验失败（整批拒绝）；`2` 文件不可读；
`130` 导入被取消。

## 测试

```sh
python3 -m unittest discover -s tests -v
```

- `tests/test_git_integration.py`：用 `git pack-objects` 生成合法包
  （`--delta-base-offset` 得 OFS_DELTA，默认得 REF_DELTA），导入后用
  `git cat-file --batch`（`GIT_OBJECT_DIRECTORY` 指向导入库）逐对象
  核对类型与内容；另含 CLI 端到端用例；
- `tests/test_packfile.py`：解析器单元测试——篡改摘要、截断/多余
  字节、声明长度不符、恶意差量（越界拷贝、源长度不符、膨胀超 1 MB、
  九层差量链、缺失基对象、依赖环）等；
- `tests/test_store.py`：暂存隔离、原子公布、模拟崩溃/取消后的旧
  清单可读性与暂存清理。
