---
name: obs-skill
version: 0.1.0
description: "CRITICAL: 华为云 OBS 对象上传/管理。把本地文件或目录上传到 OBS 桶（putFile ≤5GB，目录逐文件并发），纯 JSON 输出对象 key/etag/public_url/object_url。附带 list（列举）/head（查属性，404=不存在）/delete（删除，--dry-run 确认杠杆）/url（公开 URL + 带签名 URL）。非交互，--dry-run 当确认杠杆，logs/ 归档，输出不含任何凭证。当用户要求『上传文件到 obs』『传产物到对象存储』『看桶里有什么』『删桶里对象』『取 obs 下载链接』时使用。Triggers: 上传obs, 上传到obs, obs上传, 传到obs, 对象存储, 上传产物, 传产物, obs-skill, huawei obs, bucket, 桶, putfile, obs链接, obs下载链接, obs list, upload to obs。需要：仓库根 scope.yaml 已配置（顶层 ak/sk/region + obs.bucket 段）或 HUAWEICLOUD_SDK_* 环境变量 + CLI --bucket。"
allowed-tools: Bash, Read
keywords: 华为云, obs, 对象存储, 上传, 桶, bucket, putFile, object, huawei, 产物
---

# OBS Skill —— 华为云 OBS 上传/管理

把本地**文件或目录**上传到 OBS 桶：`upload`（putFile，≤5GB；目录逐文件并发）+ `list`（列举）+ `head`（查属性）+ `delete`（删除，--dry-run 先看清单）+ `url`（公开 URL + 带签名 URL）。纯 JSON 输出，`logs/` 归档。基于官方 `esdk-obs-python` SDK（`from obs import ObsClient`），按官方文档「上传对象-文件上传(Python SDK)」的 putFile/并发上传范式实现；脚本拆为 `scripts/obscli.py`（入口/CLI/编排）+ `obs_client.py`（凭证/端点解析）+ `obs_ops.py`（对象名/上传计划纯逻辑）；单测在 `tests/`（不触网）。

**职责边界**：只做**对象级**上传/列举/查询/删除/取链接，**不**建桶/改桶策略/改桶 ACL、**不**做多段上传（>5GB 直接报错不降级）、**不**做下载到本地、**不**做 deploy 编排。删除只删对象（`deleteObject`），无级联。

> 路径：本 skill 当前在仓库内开发，命令用仓库相对路径。迁到 `~/.claude/skills/` 后，把下列命令前缀换成 `~/.claude/skills/obs-skill/`。

## 快捷命令

```bash
# 上传单个文件（对象名默认 = 文件名；也可 --key 指定完整对象名）
python .claude/skills/obs-skill/scripts/obscli.py upload ./out/app.tar.gz

# 先看「将传什么」，不触网（确认杠杆）
python .claude/skills/obs-skill/scripts/obscli.py upload ./out/app.tar.gz --key releases/v1.0/app.tar.gz --dry-run

# 真实上传：指定对象名 + 公共读（产物要被匿名访问时）
python .claude/skills/obs-skill/scripts/obscli.py upload ./out/app.tar.gz --key releases/v1.0/app.tar.gz --acl public-read

# 上传整个目录（默认落到 <目录名>/ 前缀下，逐文件并发 8 线程）
python .claude/skills/obs-skill/scripts/obscli.py upload ./out --prefix artifacts/20260909

# 列举 / 查对象属性
python .claude/skills/obs-skill/scripts/obscli.py list --prefix releases/ --max 50
python .claude/skills/obs-skill/scripts/obscli.py head --key releases/v1.0/app.tar.gz

# 健康检查（headBucket 单请求：配置解密 / 网络 / 凭据 / 桶存在，含时延）
python .claude/skills/obs-skill/scripts/obscli.py health

# 取访问链接（私有对象给 signed_url；公共读对象 public_url 可直接用）
python .claude/skills/obs-skill/scripts/obscli.py url --key releases/v1.0/app.tar.gz --expires 86400

# 删除（先 dry-run 看清单；--key 可重复，--prefix 批量）
python .claude/skills/obs-skill/scripts/obscli.py delete --prefix smoke-test/ --dry-run
python .claude/skills/obs-skill/scripts/obscli.py delete --key smoke-test/a.txt
```

## 配置（仓库根共享 scope.yaml + 环境变量）

凭证与其他 skill 共享（该文件为 ecs/ims/obs 的**唯一凭证源**）。优先级（高 → 低）：

| 维度 | 优先级 |
|------|--------|
| AK/SK | `HUAWEICLOUD_SDK_AK`/`_SK` 环境变量 → scope 顶层 `ak`/`sk` |
| region | `HUAWEICLOUD_SDK_REGION` env → scope 顶层 `region` |
| 桶名 | CLI `--bucket` → scope `obs.bucket` |
| endpoint | scope `obs.endpoint`（可省略 → `https://obs.<region>.myhuaweicloud.com`） |
| 访问域名 | scope `obs.domain`（可省略 → `https://<bucket>.obs.<region>.myhuaweicloud.com`） |
| 对象 ACL | CLI `--acl`（默认 `private`；`public-read` 对应 HeadPermission.PUBLIC_READ） |
| 存储类别 | CLI `--storage-class`（默认 STANDARD） |

`--scope <路径>` 可指定其它 scope 文件。obs-skill 只读顶层 `ak`/`sk`/`region` + `obs` 段；其他段无影响。scope `obs` 段范本：

```yaml
obs:
  bucket: test-image-gen
  endpoint: https://obs.ap-southeast-1.myhuaweicloud.com   # 可省略，按 region 推导
  domain: https://test-image-gen.obs.ap-southeast-1.myhuaweicloud.com  # 可省略，按桶+region 推导
```

## 输出（纯 JSON，退出码区分成败）

```json
// upload 成功（节选）
{"ok": true, "action": "upload", "bucket": "test-image-gen",
 "endpoint": "https://obs.ap-southeast-1.myhuaweicloud.com",
 "domain": "https://test-image-gen.obs.ap-southeast-1.myhuaweicloud.com",
 "object_acl": "public-read", "total_count": 1, "total_size": 1024,
 "uploaded": [{"key": "releases/v1.0/app.tar.gz", "size": 1024, "etag": "...",
               "object_url": "https://...", "public_url": "https://test-image-gen.obs.ap-southeast-1.myhuaweicloud.com/releases/v1.0/app.tar.gz",
               "request_id": "..."}],
 "failed": [], "log": ".claude/skills/obs-skill/logs/upload-<时间>.json"}
// upload 失败（部分对象失败时 ok=false，逐对象给出 error_code/request_id）
{"ok": false, ..., "failed": [{"key": "...", "error": {"status": 403, "error_code": "AccessDenied", ...}}]}
// head（对象不存在不算失败）
{"ok": true, "action": "head", "key": "...", "exists": false}
// url
{"ok": true, "action": "url", "key": "...", "public_url": "...", "signed_url": "...", "expires_in": 3600}
```

成功判定：官方约定 `resp.status < 300`。输出与日志**不含任何凭证**（ak/sk 只在进程内使用）。

## 约束（强制）

- **非交互**：`upload`/`delete` 直接执行不 prompt。先看「将传什么/将删什么」→ `--dry-run`（upload 不触网）。真正的 go/no-go 归人或编排层。
- **单文件 ≤5GB**：putFile 官方上限 [0, 5GB]；超限在**本地预检**直接报错（不降级多段上传），改用多段需另行扩展。
- **同名覆盖**：未开多版本的桶，同名对象直接覆盖；开多版本则并存（versionId 会返回）。
- **目录上传**：putFile 对文件夹无内部并发，按官方建议逐文件 `ThreadPoolExecutor` 并发（默认 8，`--concurrency` 调整）；上传计划在上传前全量确定（key 映射可 dry-run 预览）。空文件（0B）允许。
- **key 规范**：去首部 `/`、反斜杠转 `/`、长度 ≤1024；公开 URL 对对象路径做 URL 编码（`/` 保留）。
- **delete 是破坏性操作**：`--prefix` 批量删除前先列举（受 `--max` 限制，默认 1000，超量如实返回 count 不静默截断日志）；务必先 `--dry-run`。
- **stdout 纯 JSON**：进度/告警（如 `[obs] ok <key>`）只走 stderr。
- **用完不自动清理**：上传的对象不自动删除；冒烟/测试对象由调用方用 `delete` 清理。
- **大文件超时**：putFile 大对象耗时与带宽相关，调用方（Bash 工具）超时要放宽；单文件 >5GB 拒绝。每对象带 `request_id` 便于提工单。
- **依赖**：`python3 -m pip install --break-system-packages esdk-obs-python`（≥3.23.9.1；与 huawei SDK 系列共存无冲突）+ `pyyaml`。

## 官方文档

- SDK 开发指南入口：https://support.huaweicloud.com/sdk-python-devg-obs/obs_22_0001.html
- 初始化 ObsClient：https://support.huaweicloud.com/sdk-python-devg-obs/obs_22_0601.html
- 上传对象-文件上传（putFile/并发上传文件夹）：https://support.huaweicloud.com/sdk-python-devg-obs/obs_22_0903.html

## 依赖

- 系统 python3（3.12）+ `esdk-obs-python` + `pyyaml`。
- 单测（纯逻辑 + 不触网的 `--dry-run` 端到端）：`python .claude/skills/obs-skill/tests/test_obs_ops.py`（迁移后路径同上换前缀）。
