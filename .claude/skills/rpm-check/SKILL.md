---
name: rpm-check
version: 1.4.0
description: "CRITICAL: 验证已有 RPM 包在目标操作系统（目标 OS 可配：默认 openEuler 22.03 LTS，HCE 目标用 hce-rpm-check profile）下能否正常安装。输入是一份操作指导（粘贴文本/本地文件/URL，可含多种安装方式，如 RPM 工具安装、YUM 源安装），经 ssh-skill 在目标机按指导逐方式实测：安装 + 安装后功能冒烟（完整性/关键文件/ldd 缺库/服务状态/主命令/日志），输出「正常安装报告」，或「装不上的根因分类与证据」（依赖缺失/架构不匹配/仓库无包/文件冲突/核心库过旧/GPG/脚本失败/指导缺陷）；**有问题时在归因后做修复尝试（补依赖/修源 URL/导 key 等，偏离点逐条留痕）并将解决方式与结果记录进报告**。目标机可选：已有 ssh-skill 别名（须为目标 OS）或不给别名自动经 ecs-skill 拉起目标 OS 镜像并注册。当用户要求『验证这个 rpm 包在 openEuler 22.03 能不能装』『按安装指导实测 rpm 安装』『rpm 兼容性验证』时使用。Triggers: rpm-check, 验证rpm安装, rpm安装测试, rpm兼容性, 已有rpm包验证, 按指导安装rpm, openEuler安装验证, 测试rpm包, install test rpm, verify rpm install。需要同时给出：操作指导（全文/路径/URL）；目标服务器别名可选（给了→已有机器验证；没给→自动拉起目标 OS 的 ECS + 注册）。"
allowed-tools: Read, Write, Bash, Glob, Grep
keywords: rpm, 验证, 安装, 兼容性, openEuler, 指导, install, verify, 依赖, 冲突, 冒烟, 报告, 可安装性, 修复
---

# RPM Check —— 已有 RPM 包在目标 OS 的可安装性验证

输入一份**操作指导**（描述如何安装某个 RPM 包，可能含多个安装方式），在**目标 OS**（默认 openEuler 22.03 LTS）的远程机器上逐方式实测安装，回答四个问题：

1. **装得上吗** —— 按指导的每种安装方式分别实测，得出各方式成败；
2. **装上后能用吗** —— 完整性、关键文件、动态链接、服务、主命令冒烟、日志；
3. **装不上/有问题时，为什么** —— 根因分类 + 取证证据；
4. **有没有可行的修复路径** —— 归因后**尝试解决**（步骤 4.5），并把**解决方式与结果完整记录**进报告；修不好也如实记录「未能解决」与原因。

装得上 → 输出正常安装报告；装不上 → 输出根因报告 + 修复尝试记录；装上但功能异常 → 输出功能问题清单（同样附带修复尝试）。

## 与 rpm-verify 的区别（不要混用）

| | rpm-check（本 skill） | rpm-verify（agent） |
|---|---|---|
| 输入 | **任意的操作指导**（用户/厂商提供，含安装步骤） | rpm-guide 生成的 verify-guide.md |
| 被测对象 | **已有 RPM 包**（官网/第三方下载，非本流水线构建） | rpm-build 刚构建出的 RPM |
| 典型问题 | 跨 OS 版本兼容性（如 oe2503 的包装到 22.03） | 构建正确性 |
| 是否允许变更 | **允许**——安装本身就是测试目的；失败后还可做修复尝试 | 安装后只读 |
| 目标 OS | openEuler 22.03 LTS（可配） | Huawei Cloud Euler |

## 强制铁律（不可妥协）

1. **一切远程操作经 ssh-skill**：`ssh_execute.py` 等脚本执行，**禁止**直接写 `ssh`/`scp`；机器用别名标识。
2. **忠实执行指导（实测阶段）**：指导是**测试对象**。步骤 2 实测阶段除「交互命令 → 非交互等价替换」（见对照表）外不得改变语义；每次替换必须在报告「非交互替换对照表」中记录 原命令 → 实际命令。**实测阶段不得为了让包装上而偏离指导**（换源、`--nodeps`、`--nogpgcheck` 等）；修复类偏离只发生在步骤 4.5，且必须逐条记录偏离点与结果——**忠实实测的结论不被修复尝试覆盖或改写**（「按指导 ❌」与「修复后 ✅」分开呈现）。
3. **门禁先行**：先验证 `/etc/os-release` 是目标 OS、`uname -m` 与包架构匹配，再执行任何安装步骤。门禁不过 → 结论记「⊘ 环境不匹配」，**停止执行**，绝不把环境问题归因为「包不能安装」。
4. **多方式隔离**：指导含多个安装方式时按顺序逐个测试；每个方式开始前清理上一方式痕迹（卸载目标包、卸载修复尝试补装的依赖、恢复 yum 源备份），保证每个方式都在干净现场；**某方式失败仍继续测其余方式**。
5. **诊断用只读取证**：安装失败后的根因分析（步骤 4）只用只读命令（`rpm -qpR`、`yum whatprovides`、`curl -sI` 等），**不用改机实验**（不装 `--nodeps` 试验），保证归因干净；**归因完成后的修复尝试（步骤 4.5）才允许变更**，且逐条留痕、限次。
6. **结论必须落盘**：结果写入 `rpm_check.result_file`，有问题另写 `rpm_check.issues_file`（含修复尝试与结果）；只在对话里说不算完成。
7. **不暴露凭据**：ECS 密码只在命令参数中传递，不写入报告、汇总或对话正文。
8. **修复尝试：先归因、后修复、留痕、限次、危险不试**——只对已归因问题做修复，每个问题最多 2 轮尝试；升级核心库（glibc/openssl/libstdc++ 等 ABI 组件）、`--nodeps`/`--force` 强装不做（记录为不可修复及出路）；云资源计费变更（扩盘等）须用户确认。

## 输入（调用方在 prompt 中提供）

1. **操作指导**（必需）：粘贴全文 / 本地文件路径 / URL（URL 用 Bash `curl -fsSL <url> -o` 下载到本地再 Read）。
2. **软件名 + 版本**（可选）：不提供则从指导推断（RPM 文件名 / URL，如 `opengauss-6.0.0-24.oe2503.aarch64.rpm` → opengauss / 6.0.0-24），推断结果记录在报告里。
3. **目标服务器别名**（可选；ssh-skill 已配置的别名）——**双路径**：
   - 给了 alias → 已有机器路径：先跑门禁（OS/架构），不符则停止并报告；
   - 没给 alias → 创建路径：ecs-skill `create --image <目标OS镜像ID>`（**镜像 ID 未提供则停止并要求提供**，不猜测）+ ssh-skill 注册别名，再跑门禁。
4. **目标 OS**（可选）：默认取配置 `rpm_check.target_os`（openEuler 22.03 LTS）；显式给出时以 prompt 为准（同时要求提供对应的 os-release 匹配值）。
5. **ECS 规格**（可选；仅创建路径）：flavor / disk-size / bandwidth 等自由文本透传 ecs.py。**架构须与包架构一致**：aarch64 包 → ARM（鲲鹏）规格，x86_64 包 → x86 规格。
6. **测后处置**（可选）：默认「卸载测试包 + 恢复 yum 源 + 保留 ECS 至 24h 自动删除」；**镜像/人工验证诉求时保留最有价值状态**——有「修复后可用」状态则保留之，否则保留忠实实测结束时的现场。用户显式要求清理时才调 ecs-skill delete（云操作不可撤销，执行前向用户复述确认）。
7. **修复尝试开关**（可选）：默认开启（步骤 4.5）；用户要求「只测不修」时跳过。

## 配置文件

输出路径与目标 OS 由项目根 `deploy.config.yaml` 的 `rpm_check` 段控制（**Read 之；缺失则用内置默认**）。占位符运行时替换：`{{software}}`/`{{version}}`（version 取完整「版本-发行号」，如 `6.0.0-24`，不含 `.rpm` 后缀与斜杠）；`{{target_image}}` = **目标机器基础镜像 slug**（镜像名称或 os-release 的 ID+VERSION_ID → 小写、非 `[a-z0-9]` 记为 `_`、版本号中 `.` 记为 `p`）：HCE 3.0 → `hce_3p0`、HCE 2.0 → `hce_2p0`、openEuler 22.03 LTS → `openeuler_22p03`。取值优先级：调用方显式给的镜像 > 配置/默认镜像（hce profile 按包目标 openEuler 版本对标选：oe2203* → HCE 2.0、oe2403sp1 → HCE 3.0，再按包架构取镜像）；已有别名路径在步骤 1 门禁后取实测 os-release；实测与计划不一致以实测为准并记录。产物按目标镜像分目录——rpm-check（`openeuler_*`）与 hce-rpm-check（`hce_*`）目录天然不同，且均不落入 rpm-build 的 `rpm/` 树。

本 skill 使用的字段：
- `rpm_check.target_os`：目标 OS 展示名（报告用）
- `rpm_check.target_os_id`：`/etc/os-release` 的 `ID` 匹配值，**字符串或列表**（列表 = 命中任一即可）
- `rpm_check.target_os_name_contains`（可选）：`NAME`/`PRETTY_NAME` 包含该子串（大小写不敏感）即认定匹配——与 `target_os_id` 为「或」关系；两者都配时命中任一即过
- `rpm_check.target_os_version_id`（可选）：**配置了才强校验** `VERSION_ID` 相等；缺省则只记录实际版本、不校验（适用一个 profile 覆盖多个版本的场景，如 HCE 2.0/3.0）
- `rpm_check.result_file`：结果报告路径
- `rpm_check.issues_file`：问题清单路径
- 全局：`unknown_version`、`default_server_alias`、`ssh_skill_scripts`、`ecs_skill_scripts`

内置默认：
```yaml
rpm_check:
  target_os: "openEuler 22.03 LTS"
  target_os_id: "openEuler"
  target_os_version_id: "22.03"
  # target_os_name_contains: ""    # 可选：NAME/PRETTY_NAME 子串匹配
  result_file: "rpmcheck/{{target_image}}/{{software}}/{{version}}/{{software}}-rpm-check-result.md"
  issues_file: "rpmcheck/{{target_image}}/{{software}}/{{version}}/{{software}}-rpm-check-issues.md"
```

> 输出落在 `rpmcheck/{{target_image}}/` 树下——**按目标镜像分目录、新建独立树**，不与 rpm-build/verify/archive 的 `rpm/` 树混放；Web 产物区已注册 `rpmcheck` 根，自动可见可下载。

## ssh-skill 调用方式

脚本路径默认 `.claude/skills/ssh-skill/scripts`（可由 `ssh_skill_scripts` 覆盖）；首次使用请 **Read** `.claude/skills/ssh-skill/SKILL.md`。

```bash
python <scripts>/ssh_execute.py <别名> "<命令>" [--timeout <秒>] [--no-daemon]
python <scripts>/ssh_config_manager_v3.py list-servers | find "<关键词>"
python <scripts>/ssh_config_manager_v3.py create --alias <别名> --host <IP> --user root --password <密码>
```

输出 JSON（`success`/`exit_code`/`stdout`/`stderr`），命令自动归档 `logs/<别名>.log`。ecs-skill：`.claude/skills/ecs-skill/scripts/ecs.py`（Read 其 SKILL.md 后使用）。

## 执行流程

### 步骤 0 — 解析操作指导

- **Read** 指导全文（URL 先 curl 下载）；把原文**存档**到 `<result_file 同目录>/<software>-rpm-check-guide.md`（报告附录引用，便于追溯）。`{{target_image}}` 未定时（已有机器路径，门禁前不知实际镜像），指导存档待步骤 1 门禁实测确定 slug 后再落盘。
- 提取结构：
  - **软件名 / 版本 / 包架构**（`.aarch64` / `.x86_64` / `.noarch`，从文件名或 URL）；
  - **安装方式列表**（按指导顺序：「方式1 RPM 工具安装」「方式2 YUM 工具安装」…），每个方式 = 有序步骤序列（下载 / 改源 / 安装 / …）；
  - 指导**自带的验证或使用步骤**（如有，供步骤 3 优先采用）。
- **非交互替换对照表**（实测阶段唯一允许的改写，逐条记录）：

  | 指导原文 | 实际执行 | 说明 |
  |---|---|---|
  | `vi <file>` + 「替换内容为 X」 | `cp <file> <file>.rpmcheck.bak` → `cat > <file> <<'EOF'\nX\nEOF` | 内容取指导给出的替换文本，逐字保留（含 `$basearch` 等） |
  | `yum install`（无包名） | `yum install -y <推断包名>` | 从 RPM 文件名去版本段推断（opengauss-6.0.0-24… → opengauss）；推断记录在案 |
  | `wget <url>` | `wget -q --tries=3 --timeout=60 <url>` | URL 逐字保留（含写法缺陷，如空版本段 `openeuler//`——如实执行，失败即发现） |
  | 交互确认提示 | 命令加 `-y` | |
  | `systemctl status` / `journalctl` | 自动补 `--no-pager` | |

- 产出「方式 × 步骤」执行计划（每步：原文、实际命令、超时预算），写入报告的执行计划节。

### 步骤 1 — 环境准备与门禁

1. **确认机器**：解析别名（prompt > `default_server_alias`）；仍无 → 走创建路径（见输入 3）。`find` 确认别名存在后连通探测：
   ```bash
   python ssh_execute.py <别名> "hostname && uname -a"
   ```
2. **OS 门禁**：
   ```bash
   python ssh_execute.py <别名> "cat /etc/os-release"
   ```
   匹配规则（ID 与名称为「或」，版本按需强校验）：
   - `ID ∈ target_os_id`（字符串或列表，命中任一即可），**或** `NAME`/`PRETTY_NAME` 包含 `target_os_name_contains`（若配置；大小写不敏感）；
   - `target_os_version_id` 有值 → 须 `VERSION_ID` 与之相等；缺省 → 仅记录实际版本，不校验。
   不匹配 → **停止**，整体结论 ⊘ 环境不匹配（报告写明当前 OS 与目标 OS），提示出路：换正确机器，或给 instance_id 经 ecs-skill change-os 切换（云操作不可逆，须用户确认后才执行）。
3. **架构门禁**：`uname -m` vs 包架构——`.aarch64` 须 aarch64、`.x86_64` 须 x86_64、`.noarch` 皆可。不符 → 同上停止。
4. **环境快照**（合并一次调用，写入报告）：`cat /etc/os-release; uname -m; df -h / /home; free -h; nproc; rpm -qa | grep -i <软件名> || echo NONE`。预装同名包 → 记为初始发现并先卸载（清理动作记录在案）；卸载失败 → 相关方式标记「跳过：已预装且无法卸载」。
5. **创建路径**（无别名时）：ecs-skill `create --name rpmcheck-<software>-<rand> --image <目标OS镜像ID> [规格参数]` → 取 JSON 的 `ip`/`admin_pass`/`id` → `ssh_config_manager_v3 create` 注册别名（密码只进命令参数）→ 回到 2 跑门禁。机器保留至 24h 后自动删除（ecs-skill 临时机默认；须更长保留时 create 加 `--no-auto-terminate`），报告注明 alias / instance_id / IP / 基础镜像（镜像 ID + 名称）。

### 步骤 2 — 逐安装方式实测（忠实执行，不偏离）

对指导中每个安装方式（按序）：

1. **现场隔离**：
   - 卸载残留：`rpm -e <包名>`（失败再 `yum remove -y <包名>`），然后 `rpm -q <包名>` 确认未安装；
   - 卸载上一方式修复尝试补装的依赖包（按修复记录清单），恢复 yum 源备份；
   - 涉及改 yum 源的方式：`tar czf /tmp/yum.repos.d.rpmcheck.<时间戳>.tgz /etc/yum.repos.d` 备份。
2. **逐步执行**（指导步骤顺序）：
   ```bash
   python ssh_execute.py <别名> "<实际命令>" --timeout <预算>
   ```
   超时预算：wget 120s；`yum clean all`/`makecache` 600s；`rpm -ivh`/`yum install` 900s；其余默认 120s。
   - 每步记录：步骤号、指导原文、实际命令、exit_code、stdout/stderr 摘要（截断）、判定（✅ 步骤成功 / ❌ 失败）。
   - **步骤失败 → 立即取证**（见步骤 4 分类表的取证命令），记录根因分类；该方式后续步骤能继续则继续（采证完整），无法继续则本方式终止并标记 ❌。
3. **方式级安装结论（按指导）**：成功 = 安装命令 exit 0 **且** `rpm -q <包名>` 已安装；否则 ❌（附根因分类）。
4. 安装成功 → 立即执行步骤 3（该方式下的功能验证），完成后再进入下一方式（其步骤 2.1 会清理现场）。

### 步骤 3 — 安装后功能验证（仅对安装成功的方式/状态）

逐项执行并记录 命令 / 期望 / 实际 / 判定（期望优先取指导自带的验证步骤；无则按「退出码 0 / 输出非空」判定）：

| # | 检查项 | 典型命令 | 通过标准 |
|---|---|---|---|
| 1 | 安装确认 | `rpm -q <包名>` | 输出包名 |
| 2 | 包元数据 | `rpm -qi <包名>` | 版本/发行号/架构与文件名一致 |
| 3 | 关键文件 | `rpm -ql <包名>` 抽查二进制/配置/systemd unit（`ls -l`） | 文件存在 |
| 4 | 动态链接 | 对主二进制 `ldd` | 无 `not found`（装上跑不起来的典型原因） |
| 5 | 服务 | `systemctl status <svc> --no-pager`；指导含启动步骤则先按指导启动 | active (running)；起不来 → `journalctl -u <svc> --no-pager -n 50` 取证 |
| 6 | 功能冒烟 | 指导的验证步骤（如 gsql 查版本）；无则 `<主命令> --version` | 期望输出 |
| 7 | 端口（服务类） | `ss -tlnp | grep <端口>` | 监听中 |
| 8 | 日志 | `journalctl --no-pager -n 50` | 与本次安装/启动相关的新 ERROR/FATAL 为 0 |

- 单项失败不中断，全量执行后汇总。
- 功能有未通过项 → 该状态结论记「⚠️ 可安装但功能异常」，问题进 issues 文件，并按步骤 4.5 对功能问题同样做修复尝试。

### 步骤 4 — 失败诊断（装不上 / 功能异常时）

每条失败必须落到**一个分类** + 证据（取证命令与输出摘录）+ 修复建议（分别面向「包提供方」与「使用方」）：

| stderr/stdout 关键字 | 根因分类 | 只读取证命令 |
|---|---|---|
| `Failed dependencies` / `is needed by` | 依赖缺失 | `rpm -qpR <rpm文件>`；`yum whatprovides '<缺失库名>'` |
| `No match for argument` / `Nothing to do` / `Unable to find a match` | 仓库无此包（**跨 OS 版本典型**：oe2503 的包不在 22.03 源中） | `yum repolist`；`yum list --showduplicates <包名>` |
| `conflicts with file from package` | 文件冲突 | `rpm -qf <冲突文件>` |
| `does not match` / `incompatible` / `arch` | 架构不匹配 | `rpm -qp --qf '%{ARCH}\n' <rpm文件>` vs `uname -m` |
| `lib*.so*: version '*' not found`（运行时）/ ldd `not found` | 核心库过旧/缺失（装上跑不起来） | `ldd <二进制>`；`strings <库> \| grep GLIBC` 版本比对 |
| `GPG` / `public key` / `NOKEY` | 签名/密钥问题 | 核对指导是否给 `gpgkey`；`curl -sI <gpgkey-url>` |
| `scriptlet` / `%pre(`/`%post(` / `non-zero exit status` | 安装脚本失败 | `rpm -q --scripts <包名>`；`journalctl` |
| `No space left` | 磁盘不足 | `df -h` |
| wget/curl 404、超时、SSL | 下载/网络失败 | `curl -sI <url>`（顺带暴露 URL 缺版本段等**指导缺陷**） |
| `yum makecache` 报错 / `Cannot find a valid baseurl` | yum 源配置问题（含指导给的 baseurl 在目标 OS 上不存在） | `curl -sI <baseurl>` 探测目录 |
| 指导命令不完整 / 自相矛盾 | **指导缺陷** | 在报告中指出具体位置 |

> 指导来自另一 OS 版本（如源 URL 指向 25.03 的包路径）时，「在 22.03 上源里没有该包 / 依赖不满足」是**有效发现**，如实报告。

### 步骤 4.5 — 修复尝试（归因完成后：尝试解决并记录解决方式）

**触发条件**：步骤 2/3/4 产生了 ❌ 或 ⚠️ 结论（且用户未要求「只测不修」）。对每个已归因问题，按下列流程尝试解决；**修得好记录解决方式，修不好如实记录「未能解决」与原因**。

**修复原则**（对应铁律 8）：
1. **先归因后修复**：只对步骤 4 已归因的失败项做修复，不做无方向试错；
2. **逐条留痕**：每个修复尝试是一个记录单元——
   ```
   # | 对应问题（根因分类） | 修复方案（偏离指导的具体点） | 实际命令 | 结果（✅ 解决 / ❌ 未解决+原因） | 后续动作（重试了什么、结果）
   ```
   同时回填到 issues 文件对应问题的「修复尝试与结果」字段；
3. **限次**：每个问题最多 **2 轮**尝试（一轮 = 一个独立修复方案）；仍失败则记「未能解决」，停止该问题的修复；
4. **危险操作不试**：升级核心库（glibc/openssl/libstdc++/gcc 运行时等系统级 ABI 组件）**不做**——记录为「不可修复（需换包/换 OS）」及出路；`--nodeps`/`--force` 强装不做（除非用户显式要求并留痕）；云资源计费变更（扩盘/升配）须先向用户复述确认。

**常见根因 → 允许的修复动作**：

| 根因分类 | 修复尝试（允许的动作） |
|---|---|
| 依赖缺失（普通包 / -devel） | 从目标 OS 官方源 `yum install -y <缺失依赖清单>`，然后**重试指导的安装命令**（命令本身仍按指导原样） |
| yum 源配置问题 / 仓库无此包（URL 缺陷类） | 修正 baseurl 为目标 OS 实际路径（如补版本段 `openEuler-22.03-LTS`，取证时已用 `curl -sI` 验证过 200）→ `yum makecache` → 重试安装；**换包/换源提供方**（如改用目标 OS 版本的软件包）须经用户确认后进行 |
| GPG / NOKEY | `rpm --import <指导给出的 gpgkey>` 后重试 |
| 文件冲突 | 卸载冲突包（`rpm -e <冲突包>`，记录）后重试 |
| 磁盘不足 | 清理 yum 缓存 / journal / 临时文件后重试；扩盘须用户确认 |
| 下载/网络失败 | 换用指导中的备选下载说明（如「直接下载网站的 RPM 包并拷贝」分支）；重试有限次数 |
| 安装脚本失败 | `journalctl` / `rpm -q --scripts` 定位脚本失败点，修正可安全修正的前置条件（如缺失目录/用户）后重试 |
| 核心库过旧 / 架构不匹配 | **不试**（不可修复类）：记录结论与出路（换目标 OS 专用包 / 升级 OS / 换架构机器） |

**修复成功的判定与后续**：
- 修复后重试指导的安装命令 → exit 0 且 `rpm -q <包名>` 已安装 = 修复成功；随即对该状态执行**完整功能验证**（步骤 3）；
- 方式级结论改记「**🔧 修复后可用**（按指导 ❌）」或「🔧 修复后可用但功能异常」；**原「按指导 ❌」记录保留不动**；
- 修复尝试补装的依赖包/改动的源全部记入变更清单，供现场隔离（下一方式开始前清理）与现场恢复记录引用；
- 若用户有镜像/人工验证诉求（输入 6）：优先保留「修复后可用」状态作为镜像现场。

### 步骤 5 — 恢复现场与报告落盘

1. **恢复**（默认执行，逐条记录）：卸载测试包（`rpm -e <包名>`）；恢复 yum 源（`tar xzf` 备份回 `/`，并 diff 校验）；删除 `/home` 下指导下载的 rpm 文件。**有镜像/人工验证诉求时按输入 6 保留最有价值状态**（修复后可用 > 忠实实测现场），并在报告中写明保留的是哪个状态、包含哪些变更。
2. **ECS**：默认保留至 24h 后自动删除（ecs-skill 临时机默认），报告注明 alias / instance_id / IP 与清理命令（`ecs.py delete --id …`，等不及到期可立即删）；须更长保留 → ecs.py 加 `--no-auto-terminate` 重建或在到期前处理；用户显式要求清理 → 先复述不可撤销，确认后执行 delete + 删 ssh 别名。
3. **Write** `<result_file>`（+ 有问题时 `<issues_file>`，问题须含修复尝试与结果）。
4. 对话回复：**整体结论（含修复后状态）+ 报告路径 + 问题文件路径**。

## 判定标准

**方式级（两维呈现，修复尝试不改写实测结论）**：
- 按指导：✅ 安装成功且功能全过 / ⚠️ 安装成功但功能有未过项 / ❌ 安装失败（附根因分类）/ ⊘ 跳过（预装无法卸载等）
- 修复后（仅当按指导非 ✅ 且做了修复尝试）：🔧✅ 修复后可用 / 🔧⚠️ 修复后可用但功能异常 / 🔧❌ 修复尝试未能解决（附原因）

**整体**（环境门禁通过时）：
- 所有方式按指导 ✅ → **✅ 可正常安装**
- 按指导有失败，但全部失败方式经修复后可用且功能全过 → **🔧 按指导无法安装，修复后可用**（一句话注明修复点，如「修正源 URL 版本段 + 补装 3 个依赖」）
- 其余（部分方式修复后仍失败，或安装成功但功能异常且未修复）→ **⚠️ 部分可用 / 功能异常**
- 全部方式失败且无修复成功 → **❌ 无法安装**（附根因与出路）
- 门禁未过 → **⊘ 环境不匹配**（不算包的成败）

## 输出文件规范

### 结果报告（`result_file`）

```markdown
# <软件> RPM 安装验证报告（<目标 OS>）

> 软件包：<name>-<version>.<arch>.rpm | 目标 OS：<target_os> | 目标机器：<alias>（<ip>, <实际OS>, <arch>） | 基础镜像：<镜像ID>（<镜像名称>） | 验证日期：<YYYY-MM-DD> | 指导存档：<guide存档路径>

## 整体结论
- 状态：✅ 可正常安装 / 🔧 按指导无法安装，修复后可用 / ⚠️ 部分可用或功能异常 / ❌ 无法安装 / ⊘ 环境不匹配
- 一句话结论：……（有修复时注明修复点）
- 各安装方式结论：
  | 安装方式 | 安装（按指导） | 修复尝试 | 安装（修复后） | 功能 | 根因分类 |
  |---|---|---|---|---|---|
  | 方式1 RPM 工具安装 | ✅/❌ | 无/🔧… | ✅/❌/— | ✅/❌/— | … |
- 功能验证汇总：N 通过 / M 总数
- 命令日志：logs/<别名>.log

## 环境信息
- **基础镜像：<镜像 ID>（<镜像名称/版本>，如 0b0f2f44-…（HCE 3.0 Standard 64bit x86_64)）**——创建路径取自本次 create 的 `--image` 实参；已有别名路径经 ecs-skill `show` 查询 image_ref（可查则记，查不到则如实标注「未知（非本次创建）」）；显式指定或偏离默认镜像时一并写明原因
- 实际 OS：ID=<id>, VERSION_ID=<ver>, PRETTY_NAME=<…>（门禁：✅ 匹配 / ✗ 不匹配）
- 架构：uname -m=<…>（包架构 <arch>，门禁 ✅/✗）
- 磁盘/内存：<df/free 摘要>；预装检查：<NONE / 已发现并清理记录>

## 执行计划与非交互替换对照表
<方式 × 步骤清单；有替换的逐条列 原命令 → 实际命令>

## 各安装方式明细
### 方式1：<名称>
  | 步骤 | 指导原文 | 实际执行 | 退出码 | 输出摘要 | 判定 |
  |---|---|---|---|---|---|
失败时追加：根因分类 / 证据（取证命令+输出摘录）/ 修复建议（包提供方 & 使用方）。

#### 功能验证（方式1）
  | 检查项 | 命令 | 期望 | 实际 | 判定 |
  |---|---|---|---|---|
（方式2… 同构，各自独立小节）

## 修复尝试记录（仅有修复尝试时）
| # | 对应问题（根因分类） | 修复方案（偏离点） | 实际命令 | 结果 | 后续动作 |
|---|---|---|---|---|---|
修复后可用时补充：修复后功能验证结果（同步骤 3 表格）；变更清单（补装的包 / 改过的文件与源，含恢复情况）。

## 失败根因汇总（仅 ❌/⚠️/🔧 时）
| # | 现象 | 分类 | 证据 | 修复尝试结果 | 建议 |
（无则写「无」）

## 现场恢复记录
- 卸载测试包：rpm -e <包名> → exit <码>
- yum 源恢复：<备份文件> 已恢复并校验一致
- 修复尝试变更处置：<已随现场隔离恢复 / 按镜像诉求保留（列清单）>
- 下载文件清理：/home/<rpm文件> 已删除 / 保留（镜像诉求）
- ECS：<保留，alias/instance_id/ip + 清理命令 / 已按用户要求删除>

## 附录
- 原始操作指导：见 <guide存档路径>
```

### 问题清单（`issues_file`，仅有 ❌/⚠️/🔧 项时生成）

```markdown
# <软件> RPM 安装验证问题清单

> 软件：<name> <version> | 目标：<alias> | 日期：<YYYY-MM-DD>

## 问题 1
- 所属方式/检查项：…
- 现象：…
- 根因分类：…
- 证据：…（命令 + 输出摘录）
- 修复尝试与结果：…（方案、偏离点、命令、✅ 解决 / ❌ 未解决+原因；未尝试须写明原因，如「属核心库过旧，不可修复类」）
- 修复建议：面向包提供方：…；面向使用方：…
```

## 禁止事项

- **禁止**直接 `ssh`/`scp`，全部经 ssh-skill 脚本。
- **禁止**跳过 OS/架构门禁就执行安装；**禁止**把环境不匹配写成「包不能安装」（反之亦然）。
- **禁止在实测阶段（步骤 2）为装上而偏离指导**（换源、`--nodeps`、`--nogpgcheck`、改命令语义）；唯一允许的改写是非交互等价替换且必须留痕。**修复类偏离只能发生在步骤 4.5**，逐条记录偏离点、命令与结果，且不得回改实测记录。
- **禁止在诊断阶段（步骤 4）用改机实验取证**（装 `--nodeps` 试错等）——先只读归因，后修复。
- **禁止**超限次修复（每问题 >2 轮）或尝试危险修复（核心库升级、强装、未经确认的计费类云操作）。
- **禁止**跳过方式间清理（卸载 + 源恢复 + 修复变更清理）。
- 单项/单方式失败不中断——多方式全量测完再汇总。
- 不修改 `deploy.config.yaml`；配置缺失用内置默认并说明。
- 结论必须落盘；完成后必须回复结果文件路径、整体结论（含修复后状态）、（若有）问题文件路径。
- 密码/密钥不得出现在报告、汇总或对话正文中。

## 调用示例

### 已有目标机（给了别名）

> rpm-check 验证 opengauss 6.0.0 的 rpm 包在 openEuler 22.03 LTS 能否正常安装，目标机 k-peng-01，操作指导如下：
> 安装方式1：RPM工具安装 步骤1：cd /home/ && wget https://mirrors.aliyun.com/openeuler/openEuler-25.03/everything/aarch64/Packages/opengauss-6.0.0-24.oe2503.aarch64.rpm 步骤2：rpm -ivh opengauss-6.0.0-24.oe2503.aarch64.rpm
> 安装方式2：YUM工具安装 …（配置 huaweicloud 源 + yum install）

将在 k-peng-01 上：门禁（openEuler 22.03 + aarch64）→ 方式1 实测（隔离现场→wget→rpm -ivh→失败则只读取证归因）→ 方式2 实测（备份源→改源→makecache→yum install -y opengauss→归因）→ **修复尝试**（如：修正源 URL 版本段并 makecache、从 22.03 源补装可满足的依赖后重试指导安装命令；核心库类缺口则记「不可修复」）→ 修复后功能验证 → 恢复源 → 落盘 `rpmcheck/openeuler_22p03/opengauss/6.0.0-24/opengauss-rpm-check-result.md`（含「修复尝试记录」节，+ issues 每条含「修复尝试与结果」）。

### 无目标机（自动拉起）

> rpm-check 验证这份指导的 rpm 包在 openEuler 22.03 LTS 能否安装，镜像 ID：<image-id>，指导：<全文>

自动 ecs-skill create（aarch64 包选 ARM 规格）+ 注册别名 + 门禁 + 同上流程；报告注明保留的 ECS（alias/instance_id/ip，24h 后自动删除）与清理命令。
