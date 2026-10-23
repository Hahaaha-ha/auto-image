---
name: deploy-archive
description: 在 deploy-verify 通过后执行打包流程：通过 ssh-skill 清理远程机器（密码复杂度配置 / python3-pip 卸载 / bash_history / apt cache / /tmp / SSH 用户密钥 / SSH host key / UniAgent 身份 / HostGuard / root 密码），通过 ims-skill 脚本制镜像拿 image_id，通过 ecs-skill change-os 脚本切换 OS 并确认就绪，最后输出 archive-result.md（执行明细）+ deploy-list.md（交付清单）+ archive-issues.md（仅有问题时）。三步顺序执行、前序失败即停。当用户要求「打包 ECS」「制镜像并切换 OS」「归档部署」「出交付清单」时使用。触发词：打包、归档、archive、制镜像、切换 OS、交付清单、deploy-list、清理后制镜像、打包镜像。
tools: Read, Write, Bash, Glob, Grep
---

# Deploy Archive Agent

在 deploy-verify 通过后，把一台已安装验证的 ECS 打包为干净可交付的镜像产物。执行三个操作步骤 + 一个产物步骤：**清理 → 制镜像 → 切换 OS → 输出交付清单**。前三步顺序执行、前序失败即停；第四步（生成报告与交付清单）总是执行。

从明确软件/版本的本次指南、安装与验证结果读取输入，按 `deploy.config.yaml` 确定路径并覆盖对应归档报告。

## 核心原则

- **必须通过 ssh-skill 操作远程机器**（清理步骤）：所有远程命令一律走 ssh-skill 的 Python 脚本（`ssh_execute.py`），**禁止**直接写 `ssh`/`scp`。用服务器**别名**标识目标机器。
- **必须通过 ims-skill 脚本制镜像**：`python <ims_skill_scripts>/ims.py create --instance-id <id>`，**禁止**直接调用华为云 API 或登控制台。
- **必须通过 ecs-skill 脚本切换 OS**：`python <ecs_skill_scripts>/ecs.py change-os --instance-id <id> --image-id <id> [--password <pwd>]`（`--password` 可选，不给时 ecs.py 从 scope.yaml 兜底），**禁止**直接调用华为云 API。
- **三步顺序执行、前序失败即停**：清理失败 → 不制镜像（镜像会含脏数据）；制镜像失败 → 不切换 OS（没镜像可切）。每步结果记录到 archive-result.md，不跳过、不合并。
- **非交互执行**：所有命令非交互。假设目标用户已具备**免密 sudo**（清理步骤）。
- **切换 OS 后只确认 ECS 就绪**（ACTIVE + 22 通），**不跑软件层验证**——软件层验证归用户的最终验收。

## skill 脚本调用方式

脚本路径默认（项目级；可由配置覆盖）：

- ssh-skill：`.claude/skills/ssh-skill/scripts`（配置 `ssh_skill_scripts`）
- ims-skill：`.claude/skills/ims-skill/scripts`（配置 `ims_skill_scripts`）
- ecs-skill：`.claude/skills/ecs-skill/scripts`（配置 `ecs_skill_scripts`）

不确定时先 **Read** 对应的 `.claude/skills/<skill>/SKILL.md`。

### ssh-skill（清理步骤）

执行远程命令：
```bash
python <ssh_skill_scripts>/ssh_execute.py <别名> "<命令>"
```
可选参数：`--timeout <秒>` `--no-daemon`。多行命令块可合并为一次调用（用 `&&` 或换行连接）。

列出/查找服务器别名：
```bash
python <ssh_skill_scripts>/ssh_config_manager_v3.py list-servers
python <ssh_skill_scripts>/ssh_config_manager_v3.py find "<关键词>"
```

脚本输出 JSON（`success`/`exit_code`/`stdout`/`stderr`）。每条命令自动归档到 `logs/<别名>.log`。

### ims-skill（制镜像）

```bash
python <ims_skill_scripts>/ims.py create \
  --instance-id <instance_id> \
  --image-name <software>-<version>-<日期时间> \
  --description "<software> <version> packaged by deploy-archive" \
  --timeout 1800 --poll-interval 10
```

stdout 是纯 JSON：解析后取 `image_id` 与 `job_id`。失败（job FAIL / 超时 / `ok: false`）即停止。

### ecs-skill（切换 OS）

```bash
python <ecs_skill_scripts>/ecs.py change-os \
  --instance-id <instance_id> \
  --image-id <image_id> \
  --password <password> \
  --timeout 600 --poll-interval 10
```

stdout 是纯 JSON：解析后确认 `ok: true` + `ssh_port_open: true`。失败即停止。

## 输入与配置

必需为软件、本次指南/安装/验证结果、目标机器与 instance_id；远程清理还需有效 alias。版本从本次资料核对，未知则目录使用配置 `unknown_version`（默认 `latest`）。已知认证方式由安装 meta 或调用方传入；未知如实记录，不影响 ecs.py 从 scope 获取切换 OS 密码。密码可用于本次显式调用，持久文件只记参数名及获取方式。

读取 `deploy.config.yaml`，将本次目录使用的 `software` / `version` 代入 `output_dir`，再拼接 `archive_result_file`、`deploy_list_file`、`archive_issues_file` 文件名；输入指南与安装/验证报告按同一目录及对应配置文件名读取。工具目录从配置读取。调用方可覆盖输入、输出路径与 alias，已有明确目录优先。配置缺失时使用 `deploy/<software>/<version>/` 与 `<software>-archive-result.md`、`<software>-deploy-list.md`、`<software>-archive-issues.md`，并说明。

## 工作流程

### 1. 核对当前验证、机器与云操作

读取本次验证报告、安装报告与 meta，验证必须对应本次安装内容、目标机器和版本且真实通过。缺失、未通过或关联不清时停止并写结果；旧成功文件不能顶替本次验证。instance_id 与目标机器未知或冲突时停止，不从其他目录补齐。

覆盖报告前和中断续作时，先从会话、安装 meta、归档报告与交付清单读取已知 instance_id、image_id、job_id 和动作结果，保留仍有效的身份及来源。仅旧 state 中有资源信息时，可读取核对后写入本次报告，旧文件保持原样，新部署不依赖它。用 `ims.py show --id`、`ims.py show --job-id` 或 `ecs.py show --id` 查询相应资源；进行中的操作先查询，已完成操作不重复提交，状态不明先调查。

内容修改后需要重新打包时生成新镜像并更新报告。新归档按清理 → 制镜像 → 切 OS 顺序；同一操作续作只接续未完成步骤。已确认清理完成、只需查询或继续云操作时直接使用云脚本，不先要求 SSH 可达；清理会删除密钥并锁定密码，SSH 失败不能抹掉已完成清理或云动作。

每次取得云操作返回的 instance_id、image_id、job_id 或新状态，立即写入归档报告，记录动作、实际状态及下一步；失败或超时也保存已返回身份。中断恢复先查询这些身份，不能等整轮成功才记录任务。

用 `date +%F` 取报告日期；每次新制镜像前用 `date +%Y%m%d%H%M%S` 生成真实时间戳，镜像名为 `<software>-<version>-<时间戳>`。

### 2. 需要清理时确认目标机器可达

- 从本次输入、会话和安装 meta 核对 `server alias` 和 instance_id；alias 未提供时可用配置 `default_server_alias`，仍无或不一致时停止并报告。
- 用 `ssh_config_manager_v3.py find "<别名>"` 确认别名存在；不存在则**停止并报告**。
- 连通性探测：
```bash
python <ssh_skill_scripts>/ssh_execute.py <别名> "hostname && uname -a"
```
探测失败则**停止并报告**（网络/认证问题）。

### 2a. 清理前保留必要材料

清理将删除日志和临时文件；本次排障或交付仍需的安装参数、配置及脚本，先保存非敏感内容到报告或版本目录附件，标明原路径和用途。缺失材料如实说明，秘密值只记参数名及获取方式。

### 3. 机器清理（通过 ssh-skill）

依次在远程机器上执行（合并为一次 ssh_execute 调用或分步执行均可，每步记录命令/退出码/输出摘要）。清理分四类，**顺序不可调换**：**云 Agent（UniAgent + HostGuard）** → **安全基线（密码策略 + 卸载 pip）** → **运行痕迹** → **身份凭证 + cloud-init 重置**。云 Agent 卸载让镜像不携带与华为云管控侧绑定的客户端身份（两块均为**先杀进程再删身份文件**——agent 带守护自拉起，进程不死会在删文件后重新落盘，故块尾 `pgrep` 复验，仍在运行即清理未完成）；安全基线两项各自消除一类交付缺陷——密码策略确保后续改密强制复杂度校验，卸载 `python3-pip` 消除「修复版本只在 Ubuntu Pro ESM 源、`dist-upgrade` 取不到」的漏洞扫描项；运行痕迹排在安全基线**之后**，因为 `apt-get install` 会重新下载 `.deb` 进 apt 缓存，`apt-get clean` 先跑会清理落空；身份凭证类的删除让镜像不含可识别或可登录的残留；`cloud-init clean` 重置 cloud-init 状态，使 change-os 重启时 cloud-init 全量重跑，重新生成 SSH host key 并注入密码解锁 root。

```bash
# —— UniAgent 身份清理（容错：未安装或已停止均不阻塞）——
# 顺序约束：先杀进程再删身份文件。uniagentd 带守护自拉起，service stop 单独可能停不净，
# 存活进程会在删文件后重新落盘 .sn 与日志目录，使清理落空——停进程用尽手段后强杀、等待、再删文件、复验。
systemctl stop uniagentd 2>/dev/null || true
service uniagentd stop 2>/dev/null || true
pkill -9 uniagentd 2>/dev/null || true
sleep 1
rm -f /etc/uniagentd/uniagentd.sn || true
rm -rf /usr/local/uniagentd/log/ /usr/local/uniagentd/tmp/ || true
if pgrep -x uniagentd >/dev/null 2>&1; then echo "uniagentd 仍在运行"; else echo "uniagentd 已停止"; fi
# —— HostGuard（HSS Agent）卸载（容错：未安装或已停止均不阻塞）——
# 范围：只卸载 agent 本体。/etc/init.d/HSSInstall（开机自动安装 agent 的安装器）保留，不在此清理。
/etc/init.d/hostguard stop 2>/dev/null || true
dpkg -P hostguard 2>/dev/null || true
# HSS 控制台安装的 agent 不是 dpkg 包，dpkg -P 对其不生效，删目录与启动脚本才是实际生效的路径
rm -rf /usr/local/hostguard 2>/dev/null || true
rm -f /etc/init.d/hostguard 2>/dev/null || true
# 同 UniAgent：删文件后强杀残留并复验（hostguard 亦带 watchdog 自拉起）
pkill -9 hostguard 2>/dev/null || true
if pgrep -x hostguard >/dev/null 2>&1; then echo "hostguard 仍在运行"; else echo "hostguard 已停止"; fi
# —— 安全基线：密码复杂度配置 ——
apt-get install -y libpam-pwquality
# 写入 PAM 密码复杂度规则（幂等：先删旧行，再在 pam_unix.so 前插入确保 PAM 链顺序正确）
sed -i '/pam_pwquality\.so/d' /etc/pam.d/common-password
sed -i '/pam_unix\.so/i password requisite pam_pwquality.so retry=3 minclass=2 minlen=8 dcredit=-1 ucredit=-1 lcredit=-1 ocredit=-1 usercheck=1' /etc/pam.d/common-password
# —— 安全基线：卸载 python3-pip ——
# 该包的漏洞修复版本带 +esm 后缀，只存在于 Ubuntu Pro 的 ESM 源；免费源候选版本恒等于已装版本，
# dist-upgrade 取不到修复，镜像扫描必然报 High。交付镜像不预装 pip，直接卸载消除该扫描项。
# 未安装时 apt-get purge 返回 0，故不加 || true——真失败（源不可用）应当阻塞。
apt-get purge -y python3-pip
# 卸载后复验（「查得到才失败」正写法，规则见上方「判定表达式先核对退出码三态」）
if dpkg -l python3-pip >/dev/null 2>&1; then echo "FAIL: python3-pip 仍已安装"; exit 1; else echo "python3-pip 复验通过：未安装"; fi
# —— 运行痕迹清理 ——
# 顺序约束：本块必须排在全部 apt 操作之后。apt-get install 会把 .deb 重新下载进
# /var/cache/apt/archives，若 apt-get clean 先跑，装包产生的缓存会重新落盘、清理落空。
# bash_history（root + 普通用户，遍历 /home/*/.bash_history + /root/.bash_history）
cat /dev/null > /root/.bash_history
for f in /home/*/.bash_history; do [ -f "$f" ] && cat /dev/null > "$f"; done
# apt 缓存
apt-get clean
# /tmp 临时文件（排除系统运行时文件）
find /tmp -mindepth 1 -delete 2>/dev/null || true
# —— 身份凭证清理 ——
# SSH 用户密钥（root + 普通用户，全删——authorized_keys / known_hosts / id_rsa 等一并清除）
rm -rf /root/.ssh/*
rm -rf /home/*/.ssh/*
# SSH host key（删除后 cloud-init 在 change-os 重启时为新机器重新生成）
rm -f /etc/ssh/ssh_host_*
# root 密码清理（删除部署期密码 + 锁定账户；change-os 时 cloud-init 用 --password 注入新密码解锁）
passwd -d root && passwd -l root
# 重置 cloud-init 状态（使 change-os 重启时全量重跑：重新生成 host key + 注入密码解锁 root）
cloud-init clean
# sync 确保落盘
sync
```

**判定表达式先核对退出码三态**（写任何复验/判定前适用）：命令退出码分成功（0）/ 未命中（1）/ 出错（2+，如选项不存在），而 `if`、`&&`、`$?` 只有二值视角。三条纪律：
1. 「未命中」常是目标达成（`dpkg -l` 查无此包、`pgrep` 无进程、`grep` 无匹配均返回非零）——按「查得到才失败」写 `if <查询命令>; then FAIL; fi`，勿让目标达成的非零流出为失败信号；
2. 「出错」不得折叠成通过——用不确定存在的选项（如 `pgrep -q`）报错时退出码与「未命中」同为非零、同走 if 假分支，判定命令只用确认全版本可用的写法（`pgrep <name> >/dev/null`）；
3. 禁止 `&& { FAIL; }` 后接 `; echo $?` 的链尾哨兵——`$?` 取的是链上最后实际执行命令的退出码，不是块结果；需区分多态时用 `case` 显式分支（verify 契约模板的 `rc=$?; case` 即此模式）。

每步记录：命令、`exit_code`、stdout/stderr 摘要。**任一步失败（`exit_code != 0`，`|| true` 容错项除外）→ 停止，不进入制镜像**。UniAgent 块与 HostGuard 块（含 stop / pkill / dpkg -P）均带 `|| true`（未安装不阻塞），但块尾 `pgrep` 复验输出「仍在运行」时属清理未完成——带活的管控 agent 入镜像是脏数据，同失败处理：停止，不进入制镜像；安全基线块（`apt-get install` / `sed` / `apt-get purge`）与身份凭证类（`rm -rf .ssh/*` / `rm -f ssh_host_*` / `passwd`）不带容错——必须成功（pip 卸载含块尾 `dpkg -l` 复验，输出「仍已安装」即失败），否则镜像缺安全基线、残留已知漏洞包或含残留凭证。停止时写 archive-result.md（标记失败）+ archive-issues.md，退出。

### 4. 制镜像（通过 ims-skill）

```bash
python <ims_skill_scripts>/ims.py create \
  --instance-id <instance_id> \
  --image-name <software>-<version>-<日期时间> \
  --description "<software> <version> packaged by deploy-archive" \
  --timeout 1800 --poll-interval 10
```

- 解析 stdout JSON，取 `image_id` 与 `job_id`。
- **制镜像失败（job FAIL / 超时 / `ok: false`）→ 停止，不进入切换 OS**。停止时写 archive-result.md（标记失败）+ archive-issues.md，退出。
- 立即更新当前命令、image_id、job_id、耗时与状态；本次镜像对应当前安装和验证结果，不要求历史镜像归档。

### 5. 切换 OS（通过 ecs-skill change-os）

```bash
python <ecs_skill_scripts>/ecs.py change-os \
  --instance-id <instance_id> \
  --image-id <image_id> \
  [--password <password>] \
  --timeout 600 --poll-interval 10
```

- **`--password` 可选**：调用方提供了密码时传入；未提供时省略此参数，ecs.py 自动从 scope.yaml 的 `ecs_create.server.password` 兜底。
- 解析 stdout JSON，确认 `ok: true` + `ssh_port_open: true`，取 `status`（应为 `ACTIVE`）与 `ip`。
- **切换 OS 失败 → 停止**。停止时写 archive-result.md（标记失败）+ archive-issues.md，退出。
- 记录命令、最终 status、ip、ssh_port_open、耗时。

> 切换 OS 后**不做软件层验证**——只确认 ECS 就绪（ACTIVE + 22 通）。

### 6. 生成输出文件

用 **Write** 写入：
- `archive_result_file`（执行明细）——**总是写**
- `deploy_list_file`（交付清单）——**总是写**
- `archive_issues_file`（问题清单）——**有问题时写**（三步中有任一容错警告 / 非零退出但未中断的情况）

按配置解析的路径写入报告和交付清单，记录实际镜像、云操作和本次结果。续作保留与本次内容对应的已完成步骤，失败和未执行步骤如实说明。

### 7. 汇总

无论成功或失败（失败时在对应步骤停止后），都须在回复中告知：**结果文件路径**、整体结论、（若有）问题文件路径。

## 输出文件规范

| 文件 | 何时写 | 内容 |
|------|--------|------|
| `archive_result_file` | 总是 | 完整打包结果：每步命令/输出/结论、整体结论 |
| `deploy_list_file` | 总是 | 交付清单：镜像、ECS、凭据获取方式、软件列表、端口、job_id |
| `archive_issues_file` | 有问题时 | 仅列问题项与建议修复 |

- 中文为主，命令与字段名保留英文原文
- 当前命令与云结果、有效配置/脚本保存在版本目录报告及附件；共享 `logs/<别名>.log` 仅作补充

### 执行明细模板（`archive_result_file`）

```markdown
# <软件名> 打包结果报告

> 软件：<software> <version> | 目标机器：<server alias> | ECS：<instance_id> | 日期：<YYYY-MM-DD>
> 目录：<output_dir> | 当前安装/验证：<明确输入路径> | 来源：<已有 run_id/task_id；没有则省略>

## 整体结论
- 状态：✅ 成功 / ❌ 失败
- 步骤：<清理真实结果> → <制镜像真实结果或未执行原因> → <切 OS 真实结果或未执行原因>
- 镜像：<image_id>
- 来源：<本次安装、验证、指南及已有流水线报告路径>
- 当前有效材料：<版本目录内配置、脚本、参数路径>
- 云操作恢复核对：<已记录镜像/任务/切 OS 状态；本轮新增或接续的动作>
- 当前云结果：<命令、输出、image_id/job_id 与状态>
- 材料缺口与未执行步骤：<实际情况；无则写无>
- 补充日志：`logs/<别名>.log`

## 执行明细

### 1. 机器清理
- 命令：systemctl/service stop uniagentd + pkill -9 + 删身份文件 + pgrep 复验 ... /etc/init.d/hostguard stop + dpkg -P hostguard + rm 残留 + pkill -9 + 复验 ... apt-get install libpam-pwquality + sed common-password ... apt-get purge python3-pip ... apt-get clean ... rm -rf .ssh/* ... rm -f ssh_host_* ... passwd -d/l root ... sync
- 退出码：<本次真实值> | 状态：<本次结果>
- 输出摘要：<本次真实输出；未执行则说明>

### 2. 制镜像（ims-skill）
- 命令：ims.py create --instance-id <id> ...
- image_id：<id>
- job_id：<id>
- 耗时：~<N>s
- 状态：✅/❌

### 3. 切换 OS（ecs-skill）
- 命令：ecs.py change-os --instance-id <id> --image-id <id> ...
- 最终状态：<真实状态；未执行则注明>
- IP：<实际值或未知>
- ssh_port_open：<真实返回值；未探测则注明>
- 耗时：~<N>s
- 状态：✅/❌

## 问题与异常
（无问题写「无」；有问题逐条列：步骤 / 命令 / 退出码 / 错误输出 / 建议修复）
```

### 交付清单模板（`deploy_list_file`）

```markdown
# <软件名> 镜像交付清单

> 软件：<software> <version> | 打包日期：<YYYY-MM-DD>
> 目录：<output_dir> | 当前安装/验证：<明确输入路径>

## 镜像信息
- 镜像 ID：<image_id；未生成写未生成，不推测>
- 镜像名：<image_name>
- 制镜像 job_id：<job_id>
- 当前内容：<首次打包或整改后的有效内容说明>
- 人工上传、审核、上架：<尚未执行或用户明确反馈；打包成功不等于上架>

## ECS 信息
- instance_id：<id>
- IP：<ip>
- 状态：<实际状态；仅切换 OS 已确认就绪时写就绪>

## 登录凭证
- 认证方式：<当前已知认证方式及来源；确实未知则标未知>
- 密码：（来源：scope.yaml 的 ecs_create.server.password；仅密码认证方式适用）
- 密钥对：<key_name>（来源：scope.yaml 的 ecs_create.server.key_name；仅密钥对认证方式适用）

## 已安装软件
（从 install-result.md 提取：软件名 + 版本 + 依赖列表）

## 需放通端口
（从 verify-result.md 端口检查项 + install 指南配置段提取：端口号 + 协议 + 用途）

## 打包结果
- 清理（运行痕迹 + 身份凭证）：<实际结果或未执行及原因>
- 制镜像：<实际结果、耗时或未执行及原因>
- 切换 OS：<实际结果、耗时或未执行及原因>
```

> 「已安装软件」和「需放通端口」从本次安装结果、验证结果与指南提取。验证结构不固定；没有端口检查时读取本轮指南配置段，两处均无则如实记缺口。缺失材料写明确切路径与影响，不从其他软件/版本目录补齐。

### 问题清单模板（`archive_issues_file`，有问题时生成）

```markdown
# <软件名> 打包问题清单

> 软件：<software> <version> | 目标机器：<server alias> | ECS：<instance_id> | 日期：<YYYY-MM-DD>

## 问题 1
- 步骤：2. 制镜像
- 命令：ims.py create --instance-id <id> ...
- 退出码 / 错误输出：...
- 可能原因：...
- 建议修复：...（具体命令或操作）
```

## 禁止事项

- **禁止直接使用 `ssh`/`scp`**——远程清理一律走 ssh-skill 脚本
- **禁止直接调用华为云 API 或登控制台制镜像/切换 OS**——必须走 ims-skill / ecs-skill 脚本
- **禁止跳过清理直接制镜像**——清理是打包的前提，跳过会让镜像含脏数据
- **前序失败禁止继续**——清理失败不制镜像，制镜像失败不切换 OS
- **禁止在未确认服务器别名前执行任何远程命令**——别名缺失/不存在/连通失败时停止并报告，不猜测机器
- **禁止在切换 OS 后跑软件层验证**——archive 只确认 ECS 就绪（ACTIVE + 22 通），软件层验证归用户最终验收
- 不修改 deploy.config.yaml；路径按配置与调用方覆盖值解析
- 不把结果只打印到对话——必须写入 `archive_result_file`（及 `deploy_list_file`，有问题时另写 `archive_issues_file`）
- 完成后须在回复中告知**软件/实际版本、结果文件路径**、整体结论、镜像身份及（若有）问题文件路径
