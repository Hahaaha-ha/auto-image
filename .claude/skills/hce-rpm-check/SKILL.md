---
name: hce-rpm-check
version: 1.2.0
description: "CRITICAL: 在 HCE（Huawei Cloud Euler）基础系统上照操作指导安装 RPM，并验证装出来的软件是否可用——rpm-check 的 HCE 目标 profile。用 HCE 公共镜像自动拉起验证机（x86_64 包 → HCE 3.0 Standard 0b0f2f44-…；aarch64 包 → HCE 2.0 Standard for ARM 04f8c759-…），OS 门禁按 HCE 宽松匹配（ID/名称命中即认定，2.0/3.0 版本仅记录不校验），随后完整执行 rpm-check 流程：逐安装方式忠实实测（下载/改源/安装）→ 安装后功能验证（rpm 完整性/关键文件/ldd 缺库/服务状态/主命令冒烟/日志）→ 失败归因 → 修复尝试（补依赖/修源 URL 等，逐条留痕、限次、危险不试）→ 报告与问题清单落 rpmcheck/{{target_image}} 树（按目标镜像：HCE 3.0 → hce_3p0、HCE 2.0 → hce_2p0）。当用户要求『在 HCE 上验证 rpm 安装』『HCE 上按指导装这个包』『华为云 EulerOS 上安装验证软件』时使用。Triggers: hce-rpm-check, hce rpm验证, 在hce上装rpm, hce安装验证, hce上验证rpm, 华为云euler装rpm, hce rpm check, hce验证rpm。需要：操作指导（全文/路径/URL）；镜像/规格/已有别名可选（没给别名自动拉起对应架构 HCE 机器 + 注册）。"
allowed-tools: Read, Write, Bash, Glob, Grep
keywords: hce, euleros, huawei cloud euler, rpm, 安装, 验证, 指导, 兼容性, 冒烟
---

# HCE-RPM Check —— 在 HCE 上按操作指导验证 RPM 安装与软件可用性

在 **HCE（Huawei Cloud Euler）** 基础镜像上照一份操作指导安装 RPM 包，并验证装出来的软件**能用**（装不上给根因与修复尝试，装得上给功能冒烟结论）。

**本 skill 是 rpm-check 的 HCE 目标 profile**：执行规范完全继承 rpm-check——**执行前必须 Read `.claude/skills/rpm-check/SKILL.md`**，其全部铁律（ssh-skill、忠实指导、多方式隔离、只读归因、修复尝试限次留痕、结论落盘、凭据纪律）与步骤 0-5 原样适用；本文件只定义对 HCE 的**覆盖项**。

## 覆盖项（相对 rpm-check 内置默认）

| 维度 | rpm-check 默认 | 本 profile 覆盖（HCE） |
|---|---|---|
| 目标 OS 门禁 | openEuler：ID=openEuler 且 VERSION_ID=22.03（强校验） | HCE：`ID ∈ os_id_match`（hce/HCE）**或** `NAME`/`PRETTY_NAME` 含 `Huawei Cloud Euler`（大小写不敏感）；**版本不校验，仅记录**（HCE 2.0 / 3.0 皆可） |
| 默认镜像（无别名创建路径） | 镜像 ID 未提供则停止要求提供 | **按包架构自动选**：x86_64 包 → `default_image_id_x86`（HCE 3.0 Standard 64bit）；aarch64 包 → `default_image_id_arm`（HCE 2.0 Standard 64bit for ARM）；noarch → 优先 x86。调用方显式给镜像 ID 则覆盖 |
| flavor 默认 | 无默认 | x86 → `default_flavor_x86`（c7.xlarge.2）；aarch64 → `default_flavor_arm`（kc1.xlarge.2） |
| 输出树 | `rpmcheck/{{target_image}}/{{software}}/{{version}}/`（默认镜像 → `openeuler_22p03`） | `rpmcheck/{{target_image}}/{{software}}/{{version}}/{{software}}-rpm-check-result.md`（+ `…-rpm-check-issues.md`）；`{{target_image}}` 按实际所用目标镜像取 slug：HCE 3.0 → `hce_3p0`、HCE 2.0 → `hce_2p0`——与 rpm-check 的 `openeuler_*` 目录天然分离 |
| 报告标题 | `<软件> RPM 安装验证报告（openEuler 22.03 LTS）` | `<软件> RPM 安装验证报告（HCE <实际版本>）`——版本取自实测 os-release（如 Huawei Cloud EulerOS 3.0） |

> 架构镜像不对称说明（ap-southeast-1 实测）：HCE 3.0 Standard 仅 x86 公共镜像；ARM Standard 只有 HCE 2.0。故 aarch64 包默认落在 HCE 2.0 ARM 上——报告须如实记录实际 HCE 版本，产物目录的 `{{target_image}}` 随之取 `hce_2p0`。

## 配置（`deploy.config.yaml` 的 `hce_rpm_check` 段；缺失用内置默认）

```yaml
hce_rpm_check:
  os_id_match: ["hce", "HCE"]
  os_name_contains: "Huawei Cloud Euler"
  default_image_id_x86: "0b0f2f44-2a85-48a3-a259-8b4b36f9cc19"   # Huawei Cloud EulerOS 3.0 Standard 64bit (x86_64)
  default_image_id_arm: "04f8c759-2a22-412f-8fd5-969ddc61afec"   # Huawei Cloud EulerOS 2.0 Standard 64bit for ARM
  default_flavor_x86: "c7.xlarge.2"
  default_flavor_arm: "kc1.xlarge.2"
  result_file: "rpmcheck/{{target_image}}/{{software}}/{{version}}/{{software}}-rpm-check-result.md"
  issues_file: "rpmcheck/{{target_image}}/{{software}}/{{version}}/{{software}}-rpm-check-issues.md"
```

全局字段（`unknown_version`、`default_server_alias`、`ssh_skill_scripts`、`ecs_skill_scripts`）沿用 `deploy.config.yaml` 顶层值。输出落 `rpmcheck/{{target_image}}/` 树（**不再使用 `hce/`、`rpm/` 树**；Web 产物区已注册 `rpmcheck` 根）。

## 输入

同 rpm-check（**操作指导必需**：粘贴全文 / 路径 / URL；目标服务器别名可选），额外：

1. **镜像 ID**（可选）：不给则按包架构选默认；给了则覆盖并记录。
2. **ECS 规格**（可选）：flavor / disk-size / bandwidth 透传 ecs.py；flavor 未给按架构取默认。
3. rpm-check 的输入 6（测后处置，默认保留 ECS）/ 输入 7（修复尝试开关，默认开）同样适用。

## 执行流程

1. **Read** `.claude/skills/rpm-check/SKILL.md`——以其为唯一执行规范。
2. **Read** `deploy.config.yaml` 解析 `hce_rpm_check` 段（缺失用内置默认并说明）。
3. 按 rpm-check **步骤 0-5** 完整执行，全程套用上表覆盖项：
   - 创建路径机器名 `hcerpm-<software>-<rand>`，ssh 别名 `hcerpm-<software>`；
   - 门禁按本 profile 匹配规则判定（openEuler 的 ID/VERSION_ID 强校验**不适用**）；
   - 报告/问题清单落 `rpmcheck/{{target_image}}/` 树（target_image 按实际所用镜像取 slug：HCE 3.0 → `hce_3p0`，HCE 2.0 → `hce_2p0`），标题与「环境信息」写明实际 HCE 版本与**目标机器的基础镜像**：镜像 ID + 镜像名称（如 `0b0f2f44-…`（HCE 3.0 Standard 64bit x86_64））；按包架构自动选镜像时写明选择依据（noarch→优先 x86 等），显式指定或区域适配等偏离默认的情况一并如实记录。
4. 对话回复：整体结论（含修复后状态）+ 报告路径 +（若有）问题文件路径 + 验证机处置（保留时给出 alias/instance_id/ip 与清理命令）。

## 禁止（在 rpm-check 禁止事项之上追加）

- **禁止**用 openEuler 的门禁值（ID=openEuler / VERSION_ID=22.03）校验 HCE 机器，反之亦然——按本 profile 的匹配规则判定。
- **禁止**镜像架构与包架构错配（aarch64 包上 x86 镜像属典型错配）。
- **禁止**绕过 profile 直接沿用 rpm-check 的默认输出树/默认镜像（`rpm/` 树、`rpmcheck/openeuler_*` 目录、openEuler 镜像），也**禁止**再往旧 `hce/` 树落盘。

## 调用示例

### 自动拉起（按包架构选镜像）

> hce-rpm-check 在 HCE 上验证 opengauss 6.0.0 的 rpm 安装，操作指导：<全文（安装方式1 wget+rpm -ivh / 安装方式2 改源+yum install）>

将：解析指导（包 opengauss-6.0.0-24.oe2503.aarch64 → aarch64）→ 用 HCE 2.0 Standard for ARM 镜像（04f8c759-…）+ kc1.xlarge.2 拉起 → 门禁（HCE 身份宽松匹配 + 架构匹配，记录实际版本）→ 方式1/方式2 忠实实测 → 失败归因 → 修复尝试（如源 URL 缺版本段按 HCE 实际路径修正）→ 报告落 `rpmcheck/hce_2p0/opengauss/6.0.0-24/opengauss-rpm-check-result.md`（+ issues）。

### 已有 HCE 机器 / 指定镜像

> hce-rpm-check 在 HCE 上验证这份指导的 rpm 安装，目标机 hce-prod-02（或：用镜像 0b0f2f44-2a85-48a3-a259-8b4b36f9cc19 拉新机），规格 c7.2xlarge.2，指导：<全文>

x86_64 包 + 显式镜像 → HCE 3.0 验证；其余流程同上。
