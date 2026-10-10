---
name: hce-rpm-check
version: 1.5.0
description: "CRITICAL: 在 HCE（Huawei Cloud Euler）基础系统上照操作指导安装 RPM，并验证装出来的软件是否可用——rpm-check 的 HCE 目标 profile。用 HCE 公共镜像自动拉起验证机，镜像一律使用 ARM（鲲鹏），不提供 x86 镜像：按包目标 openEuler 版本对标选 HCE 版本——openEuler 22.03 LTS 系（oe2203/oe2203sp1..sp4）→ HCE 2.0 Standard for ARM 04f8c759-…（唯一默认镜像，规格默认 kc1.xlarge.2，不按包架构分支，x86_64/aarch64/noarch 皆用 ARM 镜像）；openEuler 24.03 LTS SP1 系（oe2403sp1）→ 区域无 HCE 3.0 ARM 公共镜像，停止要求显式给镜像 ID。OS 门禁按 HCE 宽松匹配（ID/名称命中即认定，2.0/3.0 版本仅记录不校验），随后完整执行 rpm-check 流程：逐安装方式忠实实测（下载/改源/安装）→ 安装后功能验证（rpm 完整性/关键文件/ldd 缺库/服务状态/主命令冒烟/日志）→ 失败归因 → 修复尝试（补依赖/修源 URL 等，逐条留痕、限次、危险不试）→ 报告与问题清单落 rpmcheck/{{target_image}} 树（按目标镜像：HCE 3.0 → hce_3p0、HCE 2.0 → hce_2p0）；验证通过后新增归档环节——按 .claude/agents/rpm-archive.md 的安装脚本模板生成可直接执行的 install-rpm.sh（三个华为云鲲鹏源自动探测下载 RPM、安装前备份/安装后恢复原有 YUM 源、标准/提取双安装模式、systemd 服务与 PATH 配置；变量由操作指导与实测结果注入），并以初始镜像实测为交付门禁：脚本生成后须在全新拉起的初始 HCE ARM 镜像验证机上真实执行（执行前快照 yum.repos.d 与 yum.conf → 忠实运行脚本 → 安装冒烟 → YUM 源恢复与无残留比对），跑通才算 ✅ 交付；失败归因后仅允许修正变量重测（模板缺陷如实上报不改模板），脚本结论三态：✅ 初始镜像可直接执行成功 / 🔧 变量修正后可用 / ❌ 模板缺陷。当用户要求『在 HCE 上验证 rpm 安装』『HCE 上按指导装这个包』『华为云 EulerOS 上安装验证软件』时使用。Triggers: hce-rpm-check, hce rpm验证, 在hce上装rpm, hce安装验证, hce上验证rpm, 华为云euler装rpm, hce rpm check, hce验证rpm, hce rpm归档, hce安装脚本实测。需要：操作指导（全文/路径/URL）；镜像/规格/已有别名可选（没给别名自动按对标关系拉起 HCE ARM 机器 + 注册）。"
allowed-tools: Read, Write, Bash, Glob, Grep
keywords: hce, euleros, huawei cloud euler, rpm, 安装, 验证, 指导, 兼容性, 冒烟, 归档, archive, aarch64, arm, 鲲鹏, 安装脚本, 脚本实测, 初始镜像
---

# HCE-RPM Check —— 在 HCE（ARM）上按操作指导验证 RPM 安装，并产出经初始镜像实测的归档安装脚本

在 **HCE（Huawei Cloud Euler）** 基础镜像上照一份操作指导安装 RPM 包，并验证装出来的软件**能用**（装不上给根因与修复尝试，装得上给功能冒烟结论）；验证通过后按 rpm-archive 模板生成归档安装脚本 `install-rpm.sh`，并在**全新拉起的初始 HCE ARM 镜像机**上真实执行验证——**跑通才算交付**（见「归档脚本初始镜像实测」）。

**本 skill 是 rpm-check 的 HCE 目标 profile**：执行规范完全继承 rpm-check——**执行前必须 Read `.claude/skills/rpm-check/SKILL.md`**，其全部铁律（ssh-skill、忠实指导、多方式隔离、只读归因、修复尝试限次留痕、结论落盘、凭据纪律）与步骤 0-5 原样适用；本文件只定义对 HCE 的**覆盖项**与**归档脚本产出环节**。

## 覆盖项（相对 rpm-check 内置默认）

| 维度 | rpm-check 默认 | 本 profile 覆盖（HCE） |
|---|---|---|
| 目标 OS 门禁 | openEuler：ID=openEuler 且 VERSION_ID=22.03（强校验） | HCE：`ID ∈ os_id_match`（hce/HCE）**或** `NAME`/`PRETTY_NAME` 含 `Huawei Cloud Euler`（大小写不敏感）；**版本不校验，仅记录**（HCE 2.0 / 3.0 皆可） |
| 默认镜像（无别名创建路径） | 镜像 ID 未提供则停止要求提供 | **一律 ARM（鲲鹏），不提供 x86 镜像**：按「包目标 openEuler 版本 → 对标 HCE 版本」选，不再按包架构分支。包 release tag 为 `oe2203*`（openEuler 22.03 LTS 系，含 sp1..sp4）→ **HCE 2.0 Standard for ARM**（`default_image_id_arm` = 04f8c759-…）——x86_64 / aarch64 / noarch 包皆用此镜像；`oe2403sp1`（openEuler 24.03 LTS SP1 系）→ 对标 HCE 3.0，但区域无 HCE 3.0 ARM 公共镜像 → **停止并要求显式给镜像 ID**；tag 无法对标 openEuler 版本（如 `oe2503`/无 tag）→ 同样停止。调用方显式给镜像 ID 则覆盖 |
| flavor 默认 | 无默认 | 一律 `default_flavor_arm`（kc1.xlarge.2，鲲鹏 ARM 规格） |
| 输出树 | `rpmcheck/{{target_image}}/{{software}}/{{version}}/`（默认镜像 → `openeuler_22p03`） | `rpmcheck/{{target_image}}/{{software}}/{{version}}/{{software}}-rpm-check-result.md`（+ `…-rpm-check-issues.md` + **归档安装脚本 `install-rpm.sh`**（附初始镜像实测三态结论 ✅/🔧/❌））；`{{target_image}}` 按实际所用目标镜像取 slug：HCE 3.0 → `hce_3p0`、HCE 2.0 → `hce_2p0`——与 rpm-check 的 `openeuler_*` 目录天然分离 |
| 报告标题 | `<软件> RPM 安装验证报告（openEuler 22.03 LTS）` | `<软件> RPM 安装验证报告（HCE <实际版本>）`——版本取自实测 os-release（如 Huawei Cloud EulerOS 2.0）；报告含**「归档产物」节**与**「归档脚本初始镜像实测」节**（脚本路径、关键变量、实测三态结论，见下） |

> 版本对标依据：**HCE 2.0 基于 openEuler 22.03 LTS，HCE 3.0 基于 openEuler 24.03 LTS SP1**——按包的 openEuler 目标版本（release tag `oe2203*` / `oe2403sp1`）对标选 HCE 版本。**镜像一律 ARM（鲲鹏）**：唯一默认镜像为 HCE 2.0 Standard for ARM（ap-southeast-1 实测 `04f8c759-…`），不提供也不允许回落 x86 镜像；x86_64 架构的包同样落 ARM 镜像实测，若因架构不匹配装不上，按 rpm-check 归因规则如实记录（属正常根因分类，不视为流程错误）。`oe2403sp1` 的包对标 HCE 3.0，但区域无 HCE 3.0 ARM Standard 公共镜像——**不做静默降级**（落到 HCE 2.0 ARM 会得出「核心库过旧」的失真结论），停止并要求调用方显式给镜像 ID 或确认目标。报告须如实记录实际 HCE 版本，产物目录的 `{{target_image}}` 随之取 `hce_3p0` / `hce_2p0`。

## 配置（`deploy.config.yaml` 的 `hce_rpm_check` 段；缺失用内置默认）

```yaml
hce_rpm_check:
  os_id_match: ["hce", "HCE"]
  os_name_contains: "Huawei Cloud Euler"
  # 版本对标：包目标 openEuler 22.03 LTS 系（oe2203 / oe2203sp1..sp4）→ HCE 2.0；
  #          openEuler 24.03 LTS SP1 系（oe2403sp1）→ HCE 3.0
  # 镜像一律 ARM（鲲鹏）：oe2203* → 下列唯一默认 ARM 镜像；oe2403sp1 → 区域无
  # HCE 3.0 ARM 公共镜像，停止要求显式给镜像；不提供 x86 镜像与 x86 规格
  default_image_id_arm: "04f8c759-2a22-412f-8fd5-969ddc61afec"   # Huawei Cloud EulerOS 2.0 Standard 64bit for ARM (aarch64)
  default_flavor_arm: "kc1.xlarge.2"
  result_file: "rpmcheck/{{target_image}}/{{software}}/{{version}}/{{software}}-rpm-check-result.md"
  issues_file: "rpmcheck/{{target_image}}/{{software}}/{{version}}/{{software}}-rpm-check-issues.md"
  install_script_file: "rpmcheck/{{target_image}}/{{software}}/{{version}}/install-rpm.sh"   # 归档安装脚本（rpm-archive 模板生成）
```

全局字段（`unknown_version`、`default_server_alias`、`ssh_skill_scripts`、`ecs_skill_scripts`）沿用 `deploy.config.yaml` 顶层值。输出落 `rpmcheck/{{target_image}}/` 树（**不再使用 `hce/`、`rpm/` 树**；Web 产物区已注册 `rpmcheck` 根）。

## 输入

同 rpm-check（**操作指导必需**：粘贴全文 / 路径 / URL；目标服务器别名可选），额外：

1. **镜像 ID**（可选）：不给则按对标规则取唯一默认 ARM 镜像（`oe2203*` → HCE 2.0 ARM）；给了则覆盖并记录。
2. **ECS 规格**（可选）：flavor / disk-size / bandwidth 透传 ecs.py；flavor 未给一律取 `default_flavor_arm`。
3. rpm-check 的输入 6（测后处置，默认保留 ECS）/ 输入 7（修复尝试开关，默认开）同样适用。
4. **归档脚本变量**（可选）：服务名 / 端口 / 配置路径 / YUM 源相对路径等未在指导中给出时，按「归档安装脚本生成」的缺省规则填充并在报告标注。
5. **初始镜像实测机**（可选）：默认全新拉起 HCE ARM 机（与主验证机同镜像）；显式给别名时须先取证确认初始状态（未装目标包、`/etc/yum.repos.d` 为出厂内容），不满足则停止并说明。

## 执行流程

1. **Read** `.claude/skills/rpm-check/SKILL.md`——以其为唯一执行规范。
2. **Read** `deploy.config.yaml` 解析 `hce_rpm_check` 段（缺失用内置默认并说明）。
3. 按 rpm-check **步骤 0-5** 完整执行，全程套用上表覆盖项：
   - 创建路径机器名 `hcerpm-<software>-<rand>`，ssh 别名 `hcerpm-<software>`；
   - 门禁按本 profile 匹配规则判定（openEuler 的 ID/VERSION_ID 强校验**不适用**）；
   - 报告/问题清单落 `rpmcheck/{{target_image}}/` 树（target_image 按实际所用镜像取 slug：HCE 3.0 → `hce_3p0`，HCE 2.0 → `hce_2p0`），标题与「环境信息」写明实际 HCE 版本与**目标机器的基础镜像**：镜像 ID + 镜像名称（如 `04f8c759-…`（HCE 2.0 Standard 64bit ARM aarch64））；自动选镜像时写明选择依据（包 release tag `oe2203*` → 对标 HCE 2.0 ARM；镜像一律 ARM、不按包架构分支），显式指定或区域适配等偏离默认的情况一并如实记录。
4. **生成归档安装脚本 `install-rpm.sh`**（验证结论达成后，见「归档安装脚本生成」）：Read rpm-archive 模板 → 注入变量 → 落 `install_script_file` → 本机 `bash -n` 语法自检（仅查语法，不执行）→ 报告写「归档产物」节。验证未通过且未修复时**不生成**，在报告注明缺此产物的原因。
5. **初始镜像实测 `install-rpm.sh`**（交付门禁，见「归档脚本初始镜像实测」）：全新拉起初始 HCE ARM 验证机（**不复用主验证机**）→ 门禁与初始性取证 → 执行前快照 → ssh-skill 上传并忠实执行脚本 → 安装冒烟 + 环境无污染检查 → 失败归因与限次修复（仅变量）→ 报告写「归档脚本初始镜像实测」节与三态结论。
6. 对话回复：整体结论（含修复后状态）+ 报告路径 +（若有）问题文件路径 + **归档脚本路径与初始镜像实测结论（✅/🔧/❌）** + 两台验证机处置（保留时给出 alias/instance_id/ip 与清理命令；机器默认 24h 后自动删除——ecs-skill 临时机默认，见 rpm-check 输入 6）。

## 归档安装脚本生成（新增环节）

验证通过（含修复后通过）后，把「在干净 HCE ARM 机器上装这个 RPM」固化为可直接执行的归档 bash 脚本。**模板与铁律取自 `.claude/agents/rpm-archive.md`**：

1. **模板来源（唯一）**：Read `.claude/agents/rpm-archive.md`，取其 **「安装脚本模板（最终版，含备份/恢复）」** 代码块整段为唯一模板。模板结构**原样保留**——颜色输出、`set -eo pipefail`、参数解析（`-p/--prefix`）、`preflight_check` 架构校验、三个华为云鲲鹏 YUM 源自动探测（`YUM_REPOS`）、`backup_repos`/`restore_repos` 备份恢复、`download_rpm`（前两源 `dnf download`、第三源 `curl`）、`install_deps`、标准/提取双安装模式、`create_systemd_service`、`auto_adjust_config`、`setup_env_path`、`print_summary`——**只注入「默认配置」区变量，不增删函数、不改控制流、不换源列表**。
2. **变量注入**（来源 = 操作指导 + 验证实测；**修复尝试修正过的值优先**——修好的源 URL / 补装的依赖回填脚本，让脚本带走修复成果；验证机上装过、查得到的值用 `rpm -q` / `rpm -ql` 实测回填）：

   | 变量 | 取值来源 | 缺省 |
   |---|---|---|
   | `PKG_NAME` | 操作指导中的包名 | 必需，缺失则停止并说明 |
   | `TARGET_VER` | 操作指导；缺失时取验证机实测 `rpm -q <PKG_NAME>` 的版本 | 仍无则停止并说明 |
   | `ARCH` | 固定 `aarch64`（与验证机 ARM 架构一致） | 固定值 |
   | `RPM_DIR` | 模板默认 | `/root` |
   | `RPM_REPO_PATH` | 指导中的 YUM 源相对路径；验证中修正过的 URL 按实测可用值换算 | `"<请补充RPM在源中的目录>"` 并在 issues 标记 ⚠️ |
   | `BIN_NAME` | 指导 / RPM 文件列表（`rpm -ql`）中的主二进制 | = `PKG_NAME` |
   | `SERVICE_NAME` | 指导 | = `PKG_NAME` |
   | `CONF_FILE` | 指导 / RPM `%files` / 实测路径 | `/etc/<PKG_NAME>/<PKG_NAME>.conf` |
   | `DEFAULT_PORT` | 验证中的端口检查项 | `80` |
   | `RUNTIME_DEPS` | 指导依赖清单或修复尝试实测补装的依赖，去 `-devel` 后缀、去重、空格分隔 | 空串（模板空分支） |

3. **生成与落盘**：写入 `install_script_file`（默认 `rpmcheck/{{target_image}}/{{software}}/{{version}}/install-rpm.sh`）；落盘后本机 `bash -n` 语法自检（**仅查语法，不执行**）。
4. **报告呈现**：报告新增「归档产物」节——脚本路径、关键变量取值、未填充占位符警告；对话回复一并给出脚本路径。
5. **生成后必测（profile 对 rpm-archive 铁律的显式覆盖）**：rpm-archive 的「仅生成不执行」在本 profile 覆盖为「**必须在初始镜像实测通过才算交付**」——脚本允许且仅允许在「归档脚本初始镜像实测」阶段的专用初始镜像验证机上执行，见下节。

## 归档脚本初始镜像实测（交付门禁）

`install-rpm.sh` 的可用性以**初始 HCE 镜像实测**为交付门禁——未在初始镜像上真实跑通的脚本不作为 ✅ 交付物，交付结论必须由实测背书。本节是本 profile 对 rpm-archive「仅生成不执行」铁律的**显式覆盖**（覆盖范围仅限本 profile 的脚本交付环节）。

1. **验证机必须是初始状态**：
   - 默认全新拉起：ecs-skill 用 `default_image_id_arm`（与主验证机同一 ARM 镜像）+ `default_flavor_arm` 创建，机器名 `hcerpm-script-<software>-<rand>`、ssh 别名 `hcerpm-script-<software>`；创建后跑门禁（HCE 宽松匹配 + aarch64）与**初始性取证**（`rpm -q <PKG_NAME>` 未安装、`/etc/yum.repos.d` 为镜像出厂内容）并记录；
   - **禁止复用主验证机**（rpm 实测流程已装包/改源，非初始状态，测不出「初始镜像能否直接用」这一命题）；用户显式给别名 → 先做初始性取证，不满足则停止并说明；
   - 测后处置同 rpm-check 输入 6：默认保留至 24h 自动删除，报告注明 alias / instance_id / ip 与清理命令。
2. **执行前快照（无污染基线）**：`ls -la /etc/yum.repos.d/` 与 `md5sum /etc/yum.repos.d/* /etc/yum.conf` 采集落日志，作为事后比对基线。
3. **忠实执行脚本**：ssh-skill 上传 `install-rpm.sh` → `bash install-rpm.sh` 标准模式完整执行（全量输出采集；下载/装依赖/安装全链路超时预算 ≥ 900s）；**脚本外不做任何预配置**——不预装依赖、不预改源，脚本自身的「鲲鹏源探测 → 下载 → 依赖 → 安装」链路就是被测对象。用户要求提取模式时另测 `bash install-rpm.sh -p /opt/<software>`。
4. **安装后冒烟**（rpm-check 步骤 3 同构，逐项记录命令/期望/实际/判定）：`rpm -q` / `rpm -qi` / 关键文件（`rpm -ql` 抽查）/ `ldd` 无 `not found` / 服务状态 / 端口监听 / 主命令或 curl 冒烟 / `journalctl` 日志。
5. **环境无污染检查**：
   - `/etc/yum.repos.d`：与快照一致——无 `hce.repo` 残留、无 `yum.repos.d.bak.*` 目录残留、原 repo 文件在位；
   - `/etc/yum.conf`：与快照 diff——脚本执行中会向 yum.conf 追加 `sslverify=0`/`gpgcheck=0` 行，**任何相对快照的差异都要如实记录并评估影响**（含多次执行的累积风险）；
   - `dnf makecache`：仍可用（源恢复有效性的行为级证据）；
   - 可选幂等二跑：首轮全过后再执行一次脚本，验证重复执行成功且无残留累积。
6. **失败语义与修复**：失败按 rpm-check 步骤 4 归因表分类 + 只读取证；修复尝试每问题限 2 轮，**只允许修改「默认配置」区变量值**（如 `RPM_REPO_PATH` / `TARGET_VER` / `RUNTIME_DEPS`），回填 `install-rpm.sh` 后整脚本重测；**根因在模板本身 → 记「模板缺陷」**，issues 上报（面向模板维护方），不私改模板结构。
7. **落盘与结论**：实测全程写 result_file 的「归档脚本初始镜像实测」节（验证机信息、执行输出摘要、冒烟表、无污染检查、变量修正记录）；问题进 issues_file。脚本交付结论三态：**✅ 初始镜像可直接执行成功 / 🔧 变量修正后可用（列明修正点，交付的为修正版脚本）/ ❌ 模板缺陷无法交付（附根因与出路）**。

## 禁止（在 rpm-check 禁止事项之上追加）

- **禁止**用 openEuler 的门禁值（ID=openEuler / VERSION_ID=22.03）校验 HCE 机器，反之亦然——按本 profile 的匹配规则判定。
- **禁止**使用或默认回落 x86 镜像——本 profile 镜像一律 ARM（鲲鹏）；x86_64 包落 ARM 镜像属既定策略，实测的架构不匹配按归因规则如实记录，**不得因此私换 x86 镜像**。
- **禁止**版本对标错配（oe2203* 的包默认拉 HCE 3.0 属典型错配；调用方显式指定镜像除外，且须留痕）。`oe2403sp1` 无 HCE 3.0 ARM 公共镜像时**禁止静默降级**到 HCE 2.0 ARM——停止并说明。
- **禁止**绕过 profile 直接沿用 rpm-check 的默认输出树/默认镜像（`rpm/` 树、`rpmcheck/openeuler_*` 目录、openEuler 镜像），也**禁止**再往旧 `hce/` 树落盘。
- `install-rpm.sh` **仅允许**在「归档脚本初始镜像实测」阶段的专用初始镜像验证机上执行（全新拉起，或用户显式提供并经取证确认初始状态）；**禁止**在主验证机（rpm 实测机）或任何非初始状态的机器上试跑。
- **禁止偏离 rpm-archive.md 模板结构**（增删函数、改控制流、更换鲲鹏源列表属典型偏离；只允许注入「默认配置」区变量）——初始镜像实测失败时同样**只允许修改变量重测**，不得为通过实测而改模板；根因在模板本身 → 记「模板缺陷」如实上报。
- **禁止**在验证结论未达成（未通过且未修复）时生成归档脚本冒充已验证产物；也**禁止**跳过初始镜像实测就把脚本标注为「初始镜像可用」。

## 调用示例

### 自动拉起（对标选 ARM 镜像 + 产出归档脚本）

> hce-rpm-check 在 HCE 上验证 opengauss 6.0.0 的 rpm 安装，操作指导：<全文（安装方式1 wget+rpm -ivh / 安装方式2 改源+yum install）>

将：解析指导（包 opengauss-6.0.0-24.oe2203sp3.aarch64 → 目标 openEuler 22.03 LTS 系 → 对标 HCE 2.0；镜像一律 ARM）→ 用 HCE 2.0 Standard for ARM 镜像（04f8c759-…）+ kc1.xlarge.2 拉起 → 门禁（HCE 身份宽松匹配，记录实际版本与 aarch64 架构）→ 方式1/方式2 忠实实测 → 失败归因 → 修复尝试（如源 URL 缺版本段按 HCE 实际路径修正）→ 报告落 `rpmcheck/hce_2p0/opengauss/6.0.0-24/opengauss-rpm-check-result.md`（+ issues）→ 验证通过后按 rpm-archive 模板生成 `rpmcheck/hce_2p0/opengauss/6.0.0-24/install-rpm.sh`（`ARCH=aarch64`、`RPM_REPO_PATH` 用修正后的可用源路径、`RUNTIME_DEPS` 取指导依赖清单去 `-devel`）并 `bash -n` 自检 → **初始镜像实测**：另拉一台全新 HCE 2.0 ARM 机（`hcerpm-script-opengauss-…`，不复用主验证机）上传 `install-rpm.sh` 忠实执行 → 冒烟 + YUM 源恢复/无残留比对 → 报告记三态结论（如 ✅ 初始镜像可直接执行成功）。若包为 `…-24.oe2403sp1.aarch64` → 对标 HCE 3.0 但区域无 ARM 公共镜像，停止要求显式给镜像。

### 已有 HCE 机器 / 指定镜像

> hce-rpm-check 在 HCE 上验证这份指导的 rpm 安装，目标机 hce-prod-02（或：用镜像 04f8c759-2a22-412f-8fd5-969ddc61afec 拉新机），规格 kc1.2xlarge.2，指导：<全文>

显式镜像/已有机器 → 不做对标自动选（覆盖默认），如实记录所用镜像与偏离原因；其余流程同上（验证通过后同样生成归档脚本并做初始镜像实测）。
