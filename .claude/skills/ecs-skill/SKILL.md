---
name: ecs-skill
version: 0.7.0
description: "CRITICAL: 华为云 ECS 拉起/查询/切换OS/删除。把一台华为云 ECS 从无到有拉起到「就绪」（ACTIVE + 可达 IP + 22 通），纯 JSON 输出。默认带公网 EIP（公网浮动 IP 即可达）；本机与新机同 VPC 时加 --no-eip 退回私网路径。支持密钥对与密码两种登录鉴权方式（密码可自动生成）。create（拉起）+ show（查询）+ change-os（切换操作系统/系统盘镜像替换，轮询至新镜像生效 + 探 22）+ delete（级联删除 ECS + 系统盘 + EIP + 数据盘，轮询至实例消失，幂等）；非交互，--dry-run 当确认杠杆；新建机默认 24h 定时删除（联删系统盘+EIP，--no-auto-terminate 关闭）。当用户要求『拉起一台 ecs』『创建华为云服务器』『开一台 ecs』『查那台 ecs 状态』『切换 ecs 操作系统』『重置 ecs 镜像』『删除 ecs』『清理 ecs』时使用。Triggers: 拉起ecs, 创建ecs, 开ecs, 华为云ecs, huawei ecs, create ecs, ecs-skill, ecs 状态, 查询ecs, 就绪, 切换os, change os, 重置镜像, change-os, 删除ecs, 删ecs, 清理ecs, delete ecs, 销毁ecs。需要：仓库根 scope.yaml 已配置（ak/sk/region + ecs_create 默认）或 HUAWEICLOUD_SDK_* 环境变量。"
allowed-tools: Bash, Read
keywords: 华为云, ecs, 拉起, 创建, 服务器, huawei, cloudserver, 就绪, 公网, eip, 私网, 查询, show, create, 系统盘, disk, flavor, 规格, 密码, password, 密钥, keypair, 鉴权, change-os, 切换, 重置, 镜像, 删除, delete, 清理, 销毁
---

# ECS Skill —— 华为云 ECS 拉起 + 查询 + 切换 OS + 删除

把一台华为云 ECS 拉起到**就绪**：`create`（创建→轮询 ACTIVE→取可达 IP→探 22）+ `show`（查询单台）+ `change-os`（切换操作系统→轮询至新镜像生效→探 22；旧系统仍 ACTIVE 时不算就绪）+ `delete`（级联删除 ECS + 系统盘 + EIP + 数据盘→轮询至实例消失）。纯 JSON 输出，`logs/` 归档。基于官方 `huaweicloudsdkecs` SDK；脚本拆为 `scripts/ecs.py`（入口/CLI/编排）+ `ecs_client.py`（客户端+凭证）+ `ecs_ops.py`（请求构造）；单测在 `tests/`，覆盖请求构造与 `--dry-run` 端到端。

**默认带公网 EIP**：运行本 skill 的机器通常与新机不在同一 VPC，公网浮动 IP 是可达的唯一路径。本机恰好与新机同 VPC 时用 `--no-eip` 退回私网路径（省 EIP 费用与权限要求）。公网浮动 IP 是默认的可达路径。

**默认 24h 定时删除**：新建机默认设置 `auto_terminate_time = 创建后 24h`，由华为侧到期自动删除，**系统盘随实例释放、EIP 因创建时带 `delete_on_termination=true` 一并释放**（定时删除路径只认创建时的该字段，事后不可补救；手动 `delete` 走 `delete_publicip=true` 强删，与此正交）。API 约束：仅按需实例、最短半小时、最长三年，系统每 5 分钟执行一次。`--terminate-hours <小时>` 覆盖时长，`--no-auto-terminate` 关闭（长期机）；到期前手动 `delete` 照常级联删除，不受影响。创建成功回显 `auto_terminate_time`（UTC）。新建 EIP 一律 `delete_on_termination=true`（本 skill 的 EIP 随机器走：手动 delete 走 `delete_publicip=true` 强删；长期机事后在控制台删机时 EIP 也会一并释放，如需保留 EIP 复用请在删机前先解绑）。

**职责边界**：只交付**一台**就绪机器并报告其可达 IP。**不**批量创建（无 `--count`）、**不**注册 ssh-skill 别名、**不**做 deploy 编排——本 skill 是被当脚本调用的纯 CLI，怎么串步骤是调用方的事。

> 路径：本 skill 当前在仓库内开发，命令用仓库相对路径。迁到 `~/.claude/skills/` 后，把下列命令前缀换成 `~/.claude/skills/ecs-skill/`。

## 快捷命令

```bash
# 拉起一台（scope 默认规格，名字自动 ecs-<rand>，默认带公网 EIP，密码自动生成）
python .claude/skills/ecs-skill/scripts/ecs.py create

# 看看「将创建什么」，不调 API（确认杠杆）
python .claude/skills/ecs-skill/scripts/ecs.py create --dry-run

# 指定名字/规格/镜像（覆盖 scope）
python .claude/skills/ecs-skill/scripts/ecs.py create --name web-01 --flavor c7.xlarge.2 --image <image-id>

# 指定 VPC/子网/安全组/可用区（覆盖 scope，换网不必改配置文件）
python .claude/skills/ecs-skill/scripts/ecs.py create --vpc <vpc-id> --subnet <subnet-id> --sg <sg-id> --az cn-north-4a

# 指定登录密码（没有密钥对的新区也能直接登录）
python .claude/skills/ecs-skill/scripts/ecs.py create --password 'MyPwd@@123456'

# 不带公网 EIP（本机与新机同 VPC 时省费用；退回私网路径）
python .claude/skills/ecs-skill/scripts/ecs.py create --no-eip

# 系统盘：覆盖类型/大小（scope 完全没给 root_volume 时自动注入默认 {SSD,40}）
python .claude/skills/ecs-skill/scripts/ecs.py create --disk-type GPSSD --disk-size 100

# EIP 带宽覆盖（默认 5 Mbit/s）
python .claude/skills/ecs-skill/scripts/ecs.py create --bandwidth 10

# 定时删除：默认创建后 24h 华为侧自动删除（联删系统盘+EIP）；覆盖时长或关闭
python .claude/skills/ecs-skill/scripts/ecs.py create --terminate-hours 4
python .claude/skills/ecs-skill/scripts/ecs.py create --no-auto-terminate

# 服务端预检（dry_run=true，华为校验请求但不真创建）
python .claude/skills/ecs-skill/scripts/ecs.py create --validate

# 查询单台（按 id 或 name）
python .claude/skills/ecs-skill/scripts/ecs.py show --id <server-id>
python .claude/skills/ecs-skill/scripts/ecs.py show --name web-01

# 切换操作系统（把已有 ECS 的系统盘镜像替换为指定镜像，自动关机→重装→轮询至新镜像生效→探 22）
python .claude/skills/ecs-skill/scripts/ecs.py change-os --instance-id <server-id> --image-id <image-id> --password 'MyPwd@@123456'

# 切换 OS 前先看「将提交什么请求」，不调 API（确认杠杆）
python .claude/skills/ecs-skill/scripts/ecs.py change-os --instance-id <server-id> --image-id <image-id> --password 'MyPwd@@123456' --dry-run

# 切换 OS 用密钥对替代密码
python .claude/skills/ecs-skill/scripts/ecs.py change-os --instance-id <server-id> --image-id <image-id> --key <keypair-name>

# 删除一台 ECS（实例 + 系统盘 + EIP + 数据盘级联清理，轮询至实例消失）
python .claude/skills/ecs-skill/scripts/ecs.py delete --id <server-id>
python .claude/skills/ecs-skill/scripts/ecs.py delete --name web-01

# 删除前先看「将删什么」，不调 API（确认杠杆）
python .claude/skills/ecs-skill/scripts/ecs.py delete --id <server-id> --dry-run
python .claude/skills/ecs-skill/scripts/ecs.py delete --name web-01 --dry-run
```

## 配置（仓库根共享 scope.yaml + 环境变量）

凭证与默认规格在仓库根 `scope.yaml`（已 gitignore；范本见仓库根 `scope.yaml.example`）。该文件为 ecs-skill 与 ims-skill 的唯一凭证源。优先级（高 → 低）：

| 维度 | 优先级 |
|------|--------|
| AK/SK | `HUAWEICLOUD_SDK_AK`/`_SK` 环境变量 → scope.ak/sk |
| region | `HUAWEICLOUD_SDK_REGION` env → scope.region |
| project_id | `HUAWEICLOUD_SDK_PROJECT_ID` env → scope.project_id（可省略，SDK 按 region 自动推导） |
| 规格（flavor/image/name/key/password） | CLI → scope.ecs_create.server |
| 网络（vpc/subnet/sg/az） | CLI → scope.ecs_create.server |
| 系统盘 | `--disk-type`/`--disk-size` → scope `root_volume`；scope 完全没给则注入默认 `{SSD,40}` |
| EIP 带宽 | `--bandwidth` → scope `publicip.eip.bandwidth.size`（默认 5） |
| 定时删除 | `--terminate-hours` → scope `ecs_create.server.terminate_hours`（默认 24）→ `--no-auto-terminate` 关闭；仅按需实例 |
| 登录密码 | `--password` → `ECS_ADMIN_PASSWORD` 环境变量 → scope.password |

`--scope <路径>` 可指定其它 scope 文件。**公网可达前提**：默认带 EIP 后，目标安全组须对调用方出口 IP 放行 22。

### 登录鉴权方式裁决

密钥对与密码为二选一，裁决自上而下命中即停：

| 顺位 | 来源 | 结果 |
|------|------|------|
| 1 | 命令行 `--key` | 密钥对 |
| 2 | 命令行 `--password` | 密码 |
| 3 | 环境变量 `ECS_ADMIN_PASSWORD` | 密码 |
| 4 | scope 的 `key_name` | 密钥对 |
| 5 | scope 的 `password` | 密码 |
| 6 | 以上皆无 | 自动生成强密码 |

**同一层级内密钥对优先**（顺位 1 压 2，顺位 4 压 5），**跨层级 CLI 优先**（顺位 2、3 压 4）。

密码复杂度规则（本地预校验，不合规直接抛错不发 API）：长度 8–26；大写字母、小写字母、数字、特殊字符 `!@$%^-_=+[{}]:,./?` 四类中至少满足三类；不含 `root`/`toor`。自动生成的密码走同一套校验，长度 16。

## 输出（纯 JSON，退出码区分成败）

```json
// create 成功（默认带 EIP，密码登录）
{"ok": true, "action": "create", "name": "ecs-a1b2c3", "id": "...",
 "ip": "94.74.107.97", "ip_type": "floating", "status": "ACTIVE", "ssh_port_open": true,
 "region": "...", "flavor": "...", "image": "...", "job_id": "...",
 "auto_terminate_time": "2026-09-21T10:30:00Z", "auth_method": "password", "admin_pass": "...",
 "log": "~/.claude/skills/ecs-skill/logs/ecs-a1b2c3-<时间>.json"}
// create 成功（密钥对登录时不回显 admin_pass）
{"ok": true, ..., "auth_method": "key_pair"}
// create 成功（--no-eip 时 ip_type 为 private）
{"ok": true, ..., "ip": "192.168.0.52", "ip_type": "private"}
// create 失败（超时/22 不通等）—— 仍输出 JSON，非零退出，机器保留不销毁
{"ok": false, ..., "status": "BUILD", "ssh_port_open": false, "error": "...", "hint": "...", "log": "..."}
// show
{"ok": true, "action": "show", "server": {"id","name","status","ip","ip_type","flavor","image","ssh_port_open"}}
// change-os 成功（系统盘镜像已替换，ACTIVE + 22 通）
{"ok": true, "action": "change-os", "instance_id": "...", "image_id": "...",
 "status": "ACTIVE", "ip": "...", "ip_type": "floating", "ssh_port_open": true,
 "region": "...", "log": "..."}
// change-os 失败（超时/ERROR/22 不通）—— 仍输出 JSON，非零退出，机器保留不回滚
{"ok": false, ..., "status": "TIMEOUT", "error": "...", "hint": "用 show --id 复查", "log": "..."}
// change-os --dry-run（不调 API）
{"ok": true, "action": "change-os", "dry_run": true, "region": "...",
 "request": {"server_id": "...", "body": {"os_change": {"imageid": "...", "adminpass": "******", "mode": "withStopServer"}}}}
// delete 成功（实例 + 系统盘 + EIP + 数据盘已删除）
{"ok": true, "action": "delete", "id": "...", "name": "...", "ip": "...",
 "status": "DELETED", "job_id": "...", "region": "...",
 "hint": "如该机器注册过 ssh 别名，请另行用 ssh-skill 清理。",
 "log": "..."}
// delete 幂等（目标已不存在，视为已删除）
{"ok": true, "action": "delete", "id": "...", "status": "NOT_FOUND",
 "note": "目标 ECS 不存在，视为已删除。"}
// delete --dry-run --id（不调 API）
{"ok": true, "action": "delete", "dry_run": true, "region": "...",
 "id": "...", "request": {"body": {"delete_publicip": true, "delete_volume": true, "servers": [{"id": "..."}]}},
 "note": "未调用 API；实际执行将删除 ECS + 系统盘 + EIP + 数据盘。"}
// delete --dry-run --name（不调 API）
{"ok": true, "action": "delete", "dry_run": true, "region": "...",
 "name": "...", "cascade": {"delete_publicip": true, "delete_volume": true},
 "note": "未调用 API；实际执行将先查询 name→id 再删除 ECS + 系统盘 + EIP + 数据盘。"}
```

`ssh_port_open` 仅代表可达 IP:22 的 TCP 通断，**不**代表能用你的密钥/密码登录（登录验证属 ssh-skill，本 skill 不做）。

密码在归档日志与 `--dry-run` 输出中恒为掩码 `******`；仅在 `create` 成功的最终 stdout JSON 中回显一次。`change-os` 的密码不在 stdout 回显（切换前后密码由调用方传入，无需回显）。

## 约束（强制）

- **非交互**：`create` / `change-os` 直接执行不 prompt。先看「将提交什么」→ `--dry-run`（不调 API）或 `create --validate`（服务端预检不真创建）。真正的 go/no-go 归人或编排层。
- **一次一台**：不支持批量，`--count` 已移除；scope 里即使写了 `count` 也不透传。
- **临时机默认自动回收**：新建机默认 24h 定时删除（见上）；**过程不自动销毁/回滚**——失败时输出当前状态，用 `show --id` 复查或人工处置，排障有时限（到期即删）。
- **change-os 不改变 EIP**：切换 OS 只替换系统盘，EIP 绑定不变。提交前先探测一次 ECS 是否带 EIP，据此决定就绪 IP 路径（浮动 IP 或私网固定 IP）。`mode` 恒为 `withStopServer`（开机状态自动关机再切换），不暴露给 CLI——切换前的清理步骤需要 ECS 开机运行，中间不应人工关机。
- **change-os 不自动生成密码**：切换后必须能登录，故凭证不能省。`--password` 与 `--key` 互斥（同时给时密钥对优先）；都不给时从 scope `ecs_create.server.password` 兜底；仍无则报错。
- **change-os 复用 create 的轮询逻辑**：提交后轮询 ACTIVE + 探 22 的流程与 `create` 一致（复用 `poll_until_ready` + `wait_for_port`），超时/失败均不自动回滚。
- **调用方超时要够长**：真机实测全程约 50-60s，但慢路径（迟迟不 ACTIVE、22 一直不通）会用满 `--timeout`(600s) + `--port-grace`(60s)。用 Bash 工具调用时超时须设到 ≥ 两者之和，否则进程会被上层杀掉。真被杀也不丢机器：创建成功的瞬间 server_id 已落 `logs/` 并打到 stderr，用 `show --id` 复查。
- **delete 级联清理**：`delete_publicip` 恒为 True（释放绑定的 EIP，非解绑悬挂），`delete_volume` 恒为 True（删除数据盘；系统盘随实例默认删除）。不暴露 CLI 覆盖——清理场景下三者应一并清除。
- **delete 幂等**：目标 ECS 已不存在时视为成功（退出码 0），可安全重跑。
- **delete 不碰 ssh-skill**：ecs-skill 从头到尾不注册/删除 ssh 别名。`delete` 输出中附 hint 提醒人工另行清理别名，但不代劳。
- **delete 不接 deploy 流水线**：纯人工触发（验证成功后的资源清理），不在 deploy 编排的任何步骤中被自动调用。
- **delete 轮询至实例消失**：提交 `DeleteServers` 后轮询 `show_server` 直至返回 None（实例已从列表消失）。与 create/change-os 的「交付确定性状态」一致——跑完就知道删干净了。
- **stdout 纯 JSON**：进度/告警（如 `[ecs] created id=...`）只走 stderr，解析 stdout 不受影响。
- **就绪 = ACTIVE + 可达 IP + 22 通**。带 EIP（默认）时可达 IP 为公网浮动 IP——浮动 IP 尚未出现时继续轮询直至超时，**绝不回退私网**；`--no-eip` 时为同 VPC 私网固定 IP。
- **规格库存**：flavor 按 AZ 售罄会报 `Ecs.0018`；scope 可不钉 `availability_zone` 让华为自选有货的 AZ。
- **依赖**：系统级已装 `huaweicloudsdkcore` + `huaweicloudsdkecs`（与 ssh-skill 的 paramiko 同款，系统 python + 裸 `python` 调）。新机器需 `python3 -m pip install --break-system-packages huaweicloudsdkcore==3.1.208 huaweicloudsdkecs==3.1.208`。
- **前置（一次性）**：目标安全组须对**调用方出口 IP**放行 22（默认走公网 EIP，不再假设同 VPC 互访）；镜像 `imageRef`/规格 `flavorRef` 须在 scope 或 CLI 给出；密钥对 `key_name` 与密码至少有一种可用（都没有时自动生成）。

## 依赖

- 系统 python3（3.12）+ `huaweicloudsdkcore` 3.1.208 + `huaweicloudsdkecs` 3.1.208 + `pyyaml`。
- 用官方 ECS 服务 SDK（`EcsClient`/`CreateServersRequest`/`PrePaidServer`），不再手搓签名 HTTP。
- 单测（纯逻辑的请求构造 + 不触网的 `--dry-run` 端到端）：`python .claude/skills/ecs-skill/tests/test_ecs_ops.py`（迁移后路径同上换前缀）。
