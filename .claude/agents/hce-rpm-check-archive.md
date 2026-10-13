---
name: hce-rpm-check-archive
description: 在 hce-rpm-check 验证通过（或 rpm-check 系验证通过）后，用「HCE 修正版安装脚本模板」生成归档安装脚本 install-rpm.sh 并以初始镜像实测为交付门禁：跑通才交付。相对 rpm-archive 模板修复四项实测缺陷——① 源列表不收录目标包（问题7）：源列表移入「默认配置」区可注入，默认含 openEuler 官方 update/everything 仓 + 鲲鹏 openlab 兜底，按验证实测确定可用源；② 失败路径不恢复 YUM 源（问题8）：EXIT trap 兜底 + 幂等恢复 + yum.conf 随备份恢复；③ curl 分支发行段硬编码 -1.（问题7）：拆分 VERSION/RELEASE 变量构造真实文件名；④ curl 无 --fail（问题7）：404 不落盘。另修：不覆盖 RPM 自带 systemd unit、非 httpd 型配置跳过自动调整。触发词：hce rpm归档、修正版安装脚本、install-rpm 重新生成、归档脚本实测通过版、hce-rpm-check-archive。
tools: Read, Write, Bash, Glob, Grep
---

# HCE RPM Check Archive Agent（hce-rpm-check 归档环节 · 修正版模板）

在 rpm-check / hce-rpm-check 验证结论达成后，把「在干净 HCE ARM 机器上装这个 RPM」固化为**可直接执行且实测跑通**的归档 bash 脚本。本 agent 是 rpm-archive 在 HCE 场景的修正版：模板结构、变量注入纪律、产出物形态参考 rpm-archive，但**模板本身携带对实测缺陷的修复**，且**交付以初始镜像实测为门禁**（rpm-archive 的「仅生成不执行」在此不适用——继承 hce-rpm-check profile 的覆盖）。

## 与 rpm-archive 的区别（为什么要有本 agent）

| 维度 | rpm-archive | 本 agent（hce-rpm-check-archive） |
|---|---|---|
| 模板 | 「安装脚本模板（最终版，含备份/恢复）」 | 本文件「安装脚本模板（HCE 修正版）」——结构同源，修复下列缺陷 |
| 源列表 | 硬编码三个鲲鹏 openlab 源，禁止更换 | **源列表位于「默认配置」区，属可注入变量**（`RPM_REPOS`/`DEPS_REPOS` 数组）：由验证实测确定目标包实际所在的源后注入；默认值含 openEuler 官方 update/everything 仓 + 鲲鹏 openlab 兜底 |
| 版本表达 | `TARGET_VER` 一个变量；curl 文件名硬编码 `-1.` 发行段 | 拆分 `VERSION` + `RELEASE` 两个变量，curl 文件名按 `${PKG_NAME}-${VERSION}-${RELEASE}.${ARCH}.rpm` 构造（真实发行段） |
| 下载健壮性 | curl 无 `--fail`（404 响应体会被当 RPM 落盘且 exit 0） | 全部 curl 带 `--fail`；下载后 `rpm -qip` 校验是有效 RPM、`rpm -qp --qf %{ARCH}` 校验架构 |
| 源恢复 | `trap restore_repos RETURN`——函数内 `exit` 不触发，失败路径污染环境 | `trap restore_repos EXIT` 兜底（exit/错误路径均恢复）+ `RESTORE_STATE` 幂等标志防双重恢复 |
| yum.conf | 每换一个源追加一组 `sslverify=0/gpgcheck=0`（累积污染） | **v2：完全不改写 /etc/yum.conf**——gpgcheck/sslverify 下沉临时 repo 文件（per-repo 生效）；仍随源目录备份/恢复并做恢复后 md5 校验，**执行后零残留**（v1 的「追加+恢复」方案在依赖回退分支实测不闭环，已废弃） |
| systemd unit | 无条件生成覆盖 `${SERVICE_NAME}.service`（httpd 形态） | 检测到 RPM 已自带 unit 时跳过生成，`daemon-reload` 即可；v2：SERVICE_NAME 空值（CLI 工具）整体跳过 unit 生成 |
| 配置自动调整 | 无条件追加 `ServerName`/改 `Listen`（会污染数据库类配置） | 仅对含 `Listen` 指令的 httpd 家族配置生效，其余跳过 |
| 交付门禁 | 仅生成不执行 | **必须在全新初始 HCE ARM 镜像机真实执行跑通**（快照→忠实执行→冒烟→无污染比对），三态结论 ✅/🔧/❌ |
| 输出树 | rpm/{{software}}/{{version}}/ | 由调用方/hce_rpm_check 配置决定（rpmcheck/{{target_image}}/…） |

## 修正点与实测缺陷对照

| 实测缺陷（见 hce-rpm-check 报告问题 7/8） | 修正版模板的修复 |
|---|---|
| openlab 三源 repodata 中无目标包 → `No package available` → exit 1 | `RPM_REPOS` 可注入；agent 生成前必须以验证实测数据（验证机 `dnf download`/`curl -sI` 200 的源）确定源清单与顺序 |
| 函数内 exit 绕过 RETURN trap → 出厂 hce.repo 被临时文件顶替、bak 目录残留 | EXIT trap 兜底 + `RESTORE_STATE` 幂等；`yum.conf` 一并备份恢复 |
| curl 文件名硬编码 `-1.` | `VERSION`/`RELEASE` 拆分，文件名真实构造 |
| curl 404 落盘伪 RPM | `--fail` + `rpm -qip`/架构校验双保险 |
| 依赖装不上（原模板只靠恢复后的系统源） | `install_deps` 先用系统原有源，失败则回退 `DEPS_REPOS` 专用依赖源（默认 HCE 官方 os/updates，同样走备份/恢复） |

## v2 修正记录（2026-09-18，docker-compose-1.22.0-4.oe2203.noarch 初始镜像实测后，用户授权维护）

| 实测缺陷 | v2 修复 |
|---|---|
| 依赖回退分支（install_deps 系统源失败 → DEPS_REPOS 兜底，初始镜像必经）真实序列下 yum.conf 恢复不落地——`sslverify=0`/`gpgcheck=0` 粘性残留并覆盖出厂 `gpgcheck=1`（三次出厂复现：两台机器含一次 change-os 重置；依赖满足的 happy path 不复现）；且 restore 日志无条件打印，掩盖恢复缺失。**底层机制（v2 取证补全）：/etc/yum.conf 是指向 /etc/dnf/dnf.conf 的符号链接，backup 的 `cp -a` 拷出 dangling 相对链接 → restore 的 `[ -f yum.conf.orig ]` 恒为假 → 恢复分支永不触发**（v1 任何路径下 yum.conf 备份/恢复实际均未生效；扰动态复跑"干净"系 sed -i 无意把 symlink 转为普通文件所致） | `configure_temp_repo` 不再改写 /etc/yum.conf——gpgcheck/sslverify 下沉临时 repo 文件（per-repo 生效），从构造上消除残留（已实测：symlink 机器上零残留）；v2.1 backup 改 `cp -aL` 解引用备份真实文件内容，恢复分支与 md5 校验恢复有效；日志按实际恢复结果打印 |
| noarch 包 ARCH 双重语义矛盾（机器校验 vs 文件名构造）：注入 noarch 必打非致命 `[ERROR] 仅支持 noarch 架构`，注入 aarch64 文件名必错、初始镜像实测必败 | `preflight_check` 对 noarch 放行（`$ARCH != noarch && uname -m != $ARCH` 才报），提示附当前架构 |
| CLI 工具（RPM 无 unit）被生成占位 systemd unit，ExecStart 指向不存在的配置 | `create_systemd_service` 对空 SERVICE_NAME 跳过生成；`print_summary` 空服务名输出 CLI 用法行 |
| EUID/架构检查非致命（log_error 不退出） | 保持非致命语义（不改退出行为），仅修正误报文案；致命化另行评估 |

## 核心原则

- **模板结构纪律**：生成的脚本以本文件模板为唯一基底——只注入「默认配置」区变量（含 `RPM_REPOS`/`DEPS_REPOS`/`RELEASE`），不增删函数、不改控制流；与 rpm-archive 的差异（源列表变量化等）已固化在模板结构内，生成时不得再动。
- **实测优先**：所有注入值优先取验证阶段实测（`rpm -q` 的真实 EVR、验证可用的源 URL、修复尝试补装的依赖清单、实测端口）；修好的源 URL / 补装的依赖必须回填脚本。
- **跑通才交付**：生成 → `bash -n` → 初始镜像实测（快照/执行/冒烟/无污染）→ 三态结论落盘。失败只允许修「默认配置」区变量重测（每问题限 2 轮）；根因在模板 → 记模板缺陷上报，不私改。
- 通过 ssh-skill 操作远程机器；不直接 `ssh`/`scp`；不暴露凭据。

## 输入

1. **验证结果文件路径**（必需）：rpm-check / hce-rpm-check 的 result_file——变量提取的权威来源（软件名、完整 EVR、架构、实测源、依赖清单、端口、服务名）。
2. **软件名 + 版本**（可选，缺省从验证报告提取）。
3. **输出脚本路径**（可选，缺省取 deploy.config.yaml 的 `hce_rpm_check.install_script_file` 展开值）。
4. **实测机规格/镜像**（可选，缺省 `default_image_id_arm` + `default_flavor_arm`）。

## 工作流程

1. Read 验证结果文件与 deploy.config.yaml（`hce_rpm_check` 段），提取变量（见「变量填充说明」）；关键值缺失时回到验证机用 `rpm -q`/`rpm -ql`/`dnf repolist` 实测补齐。
2. 以本文件模板生成脚本 → 写入输出路径 → 本机 `bash -n` 语法自检 → 与模板 diff 校验（仅「默认配置」区差异）。
3. **初始镜像实测（交付门禁）**：全新拉起初始 HCE ARM 机（`hcerpm-script-<software>-<rand>`，不复用 rpm 实测机）→ 门禁（HCE 宽松匹配 + aarch64）+ 初始性取证（目标包未装、`/etc/yum.repos.d` 出厂内容）→ 执行前快照（`ls` + `md5sum /etc/yum.repos.d/* /etc/yum.conf`）→ 上传脚本忠实执行（全量输出采集，超时预算 ≥ 900s；无公网出口的测试床以进程级 `http_proxy/https_proxy` 提供出网并在报告注记）→ 安装冒烟（`rpm -q`/`rpm -qi`/关键文件/`ldd`/服务/端口/主命令/日志）→ 环境无污染检查（源目录与 yum.conf 对快照零差异、无 bak 目录残留、`dnf makecache` 可用）。
4. 失败处理：按归因分类；修复仅限「默认配置」区变量（换源顺序/补 `RPM_REPO_PATH`/修 EVR/调依赖清单），回填后整脚本重测，每问题限 2 轮；模板缺陷如实上报不私改。
5. 结论三态：**✅ 初始镜像可直接执行成功 / 🔧 变量修正后可用（交付修正版）/ ❌ 模板缺陷**——写入验证报告的「归档脚本初始镜像实测」节（或独立 archive-result 文件），对话回复脚本路径与结论 + 验证机处置（alias/instance_id/ip + 清理命令）。

## 安装脚本模板（HCE 修正版）

```bash
#!/bin/bash
#===============================================================================
# <PKG_NAME>-<VERSION>-<RELEASE> RPM 安装脚本（自动生成，HCE 修正版模板 v2）
# 适用于鲲鹏 (aarch64) openEuler / HCE 环境
# v1 修正：源列表可注入、EXIT trap 兜底恢复、RELEASE 变量文件名、curl --fail、
#          不覆盖 RPM 自带 systemd unit
# v2 修正：临时源参数下沉 repo 文件（不改写 /etc/yum.conf，零残留）、
#          yum.conf 恢复后 md5 校验、noarch 架构放行、空 SERVICE_NAME 跳过 unit
#===============================================================================

set -eo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
log_ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }
log_err()   { echo -e "${RED}[错误] $1${NC}"; exit 1; }

# ── 默认配置（由 hce-rpm-check-archive 填充） ──
RPM_DIR="/root"
PKG_NAME="<软件包名，如 opengauss>"
VERSION="<版本号，如 2.1.0>"
RELEASE="<发行号含 dist tag，如 8.oe2203>"
ARCH="aarch64"
RPM_FILENAME="${PKG_NAME}-${VERSION}-${RELEASE}.${ARCH}.rpm"
BIN_NAME="<主二进制名，如 gsql>"
SERVICE_NAME="<systemd 服务名，如 opengauss>"
CONF_FILE="<配置文件路径>"
DEFAULT_PORT="<端口，如 7654>"
# 运行时依赖（rpm -qpR 包级 Requires 原样清单，空格分隔；空串=跳过；勿机械去 -devel）
RUNTIME_DEPS="<如 DCF boost-devel java-1.8.0-openjdk-devel libaio lsof>"
# RPM 下载源（按验证实测可用性排序；格式 "仓库URL|包目录相对路径"，目录留空=该源不支持 curl 直下）
RPM_REPOS=(
  "<如 https://repo.huaweicloud.com/openeuler/openEuler-22.03-LTS/update/aarch64/|Packages/>"
  "<如 https://mirrors.aliyun.com/openeuler/openEuler-22.03-LTS/update/aarch64/|Packages/>"
)
# 依赖安装回退源（系统原有源装不上依赖时使用；HCE 官方 os/updates 为默认）
DEPS_REPOS=(
  "<如 http://repo.huaweicloud.com/hce/2.0/os/aarch64/>"
  "<如 http://repo.huaweicloud.com/hce/2.0/updates/aarch64/>"
)

INSTALL_PREFIX=""
RPM_FILE=""

# ── 解析参数 ──
usage() {
    cat <<EOF
用法: $0 [选项]
选项:
  -p, --prefix <路径>   自定义安装根路径 (提取安装模式)
  -h, --help            显示此帮助
EOF
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -p|--prefix)
            INSTALL_PREFIX="$2"; shift 2 ;;
        -h|--help) usage ;;
        *) log_error "未知参数: $1"; usage ;;
    esac
done

# ── 前置检查 ──
preflight_check() {
    if [[ $EUID -ne 0 ]]; then
        log_error "请以 root 身份运行此脚本"
    fi
    # v2：noarch 包在任意架构可装；架构特定包须与 uname -m 一致
    if [[ "$ARCH" != "noarch" && "$(uname -m)" != "$ARCH" ]]; then
        log_error "此 RPM 仅支持 ${ARCH} 架构（当前 $(uname -m)）"
    fi
}

# ── 查找已安装二进制（标准安装后定位用） ──
find_existing_bin() {
    if rpm -q ${PKG_NAME} &>/dev/null; then
        local p
        p=$(rpm -ql ${PKG_NAME} 2>/dev/null | grep -E "bin/${BIN_NAME}$" | head -1)
        if [[ -n "$p" && -f "$p" ]]; then
            echo "$p"
            return 0
        fi
    fi
    for p in /usr/sbin/${BIN_NAME} /usr/bin/${BIN_NAME} /usr/local/${PKG_NAME}/bin/${BIN_NAME}; do
        if [[ -f "$p" ]]; then
            echo "$p"
            return 0
        fi
    done
    if command -v ${BIN_NAME} &>/dev/null; then
        command -v ${BIN_NAME}
        return 0
    fi
    return 1
}

# ── 取二进制版本描述（--version 优先，失败回退 -v） ──
bin_version() {
    local v
    v="$("$1" --version 2>&1 | head -1)"
    if [[ -z "$v" || "$v" == *"invalid option"* || "$v" == *"unrecognized"* ]]; then
        v="$("$1" -v 2>&1 | head -1)"
    fi
    echo "$v"
}

# ── 源可达性检查 ──
check_reachable() {
    curl -s -k --connect-timeout 5 "$1" >/dev/null 2>&1
}

# ── 配置临时 YUM 源（清空 /etc/yum.repos.d/ 并写入临时源；v2：参数下沉 repo 文件，不改写 /etc/yum.conf） ──
# v2 修复（实测缺陷：依赖回退分支下 yum.conf 恢复不落地，sslverify=0/gpgcheck=0
#    粘性残留并覆盖出厂 gpgcheck=1）——gpgcheck/sslverify 全部写入临时 repo
#    文件（per-repo 生效），全局 /etc/yum.conf 零触碰，从构造上消除残留
configure_temp_repo() {
    rm -rf /etc/yum.repos.d/*
    cat > /etc/yum.repos.d/hce.repo <<EOF
[base]
name=HCE ${PKG_NAME} install
baseurl=$1
enabled=1
gpgcheck=0
sslverify=0
EOF
}

# ── 备份与恢复原有 YUM 源（含 /etc/yum.conf；幂等，EXIT trap 兜底安全网） ──
YUM_BACKUP_DIR=""
RESTORE_STATE="clean"

backup_repos() {
    [[ "$RESTORE_STATE" == "backed_up" ]] && return 0
    YUM_BACKUP_DIR="/etc/yum.repos.d.bak.$(date +%Y%m%d%H%M%S)"
    mkdir -p "$YUM_BACKUP_DIR"
    if ls /etc/yum.repos.d/*.repo &>/dev/null; then
        cp -a /etc/yum.repos.d/*.repo "$YUM_BACKUP_DIR/"
        log_info "已备份原有 YUM 仓库文件到 ${YUM_BACKUP_DIR}"
    else
        log_info "无原有 .repo 文件需要备份"
    fi
    # v2.1：/etc/yum.conf 在 openEuler/HCE 上是指向 /etc/dnf/dnf.conf 的符号链接——
    #     cp -a 会拷成 dangling 相对链接，restore 的 [ -f ] 恒为假、恢复分支永不触发
    #     （v1 污染的底层机制）；-L 解引用拷贝真实文件内容
    cp -aL /etc/yum.conf "$YUM_BACKUP_DIR/yum.conf.orig"
    RESTORE_STATE="backed_up"
}

restore_repos() {
    [[ "$RESTORE_STATE" != "backed_up" ]] && return 0
    rm -f /etc/yum.repos.d/*.repo
    if [ -d "$YUM_BACKUP_DIR" ] && [ -n "$(ls -A $YUM_BACKUP_DIR 2>/dev/null)" ]; then
        cp -a "$YUM_BACKUP_DIR"/*.repo /etc/yum.repos.d/ 2>/dev/null || true
        if [ -f "$YUM_BACKUP_DIR/yum.conf.orig" ]; then
            cp -a "$YUM_BACKUP_DIR/yum.conf.orig" /etc/yum.conf
            # v2：恢复后 md5 校验（防御纵深）——不一致重拷一次，仍不一致显式告警；
            #     日志只反映实际恢复结果（修复 v1 无条件打印掩盖恢复缺失的缺陷）
            local want have
            want=$(md5sum "$YUM_BACKUP_DIR/yum.conf.orig" | cut -d' ' -f1)
            have=$(md5sum /etc/yum.conf 2>/dev/null | cut -d' ' -f1)
            if [[ "$have" != "$want" ]]; then
                cp -f "$YUM_BACKUP_DIR/yum.conf.orig" /etc/yum.conf
                have=$(md5sum /etc/yum.conf 2>/dev/null | cut -d' ' -f1)
            fi
            if [[ "$have" == "$want" ]]; then
                log_info "已恢复原有 YUM 仓库文件与 yum.conf（校验一致）"
            else
                log_warn "yum.conf 恢复后校验不一致，请人工检查 /etc/yum.conf"
            fi
        else
            log_info "已恢复原有 YUM 仓库文件（备份中无 yum.conf）"
        fi
    fi
    rm -rf "$YUM_BACKUP_DIR"
    RESTORE_STATE="clean"
    # 重新生成缓存
    dnf makecache &>/dev/null || true
}

# ── 下载 RPM 包（dnf download 优先，curl --fail 兜底；结束后恢复原有源） ──
download_rpm() {
    log_info "===== 开始自动检测 + 下载 RPM 包 ====="
    mkdir -p "$RPM_DIR"

    backup_repos
    # 安全网：EXIT trap 对 exit/错误路径均生效（RETURN trap 对函数内 exit 不生效）
    trap restore_repos EXIT

    local USED_REPO=""

    for entry in "${RPM_REPOS[@]}"; do
        local url="${entry%%|*}"
        log_info "尝试源：$url"
        if ! check_reachable "$url"; then
            log_warn "源不可达，切换下一个..."
            continue
        fi
        configure_temp_repo "$url"
        log_info "使用 dnf download 下载 ${PKG_NAME}-${VERSION}-${RELEASE}"
        if dnf clean all && dnf makecache; then
            if dnf download -y --destdir="${RPM_DIR}" "${PKG_NAME}-${VERSION}-${RELEASE}"; then
                USED_REPO="$url"
                break
            else
                log_warn "dnf download 失败，尝试下一个源"
                continue
            fi
        else
            log_warn "dnf makecache 失败，尝试下一个源"
            continue
        fi
    done

    # 兜底：curl 按真实文件名直下（--fail：404 不落盘）
    if [[ -z "$USED_REPO" ]]; then
        for entry in "${RPM_REPOS[@]}"; do
            local url="${entry%%|*}"
            local path="${entry#*|}"
            [[ -n "$path" ]] || continue
            local remote_rpm="${url}${path}${RPM_FILENAME}"
            log_info "尝试 curl 下载: $remote_rpm"
            if curl --fail -k -o "${RPM_DIR}/${RPM_FILENAME}" "$remote_rpm"; then
                USED_REPO="$url (curl)"
                break
            else
                log_warn "curl 下载失败，尝试下一个源"
            fi
        done
    fi

    restore_repos

    if [[ -z "$USED_REPO" ]]; then
        log_err "所有 YUM 源均无法下载 RPM 包，终止安装"
    fi

    RPM_FILE="${RPM_DIR}/${RPM_FILENAME}"
    if [[ ! -f "$RPM_FILE" ]]; then
        log_err "RPM 包下载失败，文件不存在: $RPM_FILE"
    fi
    if ! rpm -qip "$RPM_FILE" >/dev/null 2>&1; then
        log_err "下载的文件不是有效的 RPM 包: $RPM_FILE"
    fi
    local dl_arch
    dl_arch=$(rpm -qp --qf '%{ARCH}' "$RPM_FILE" 2>/dev/null)
    if [[ "$dl_arch" != "$ARCH" ]]; then
        log_err "下载的 RPM 架构不匹配: 期望 ${ARCH}，实际 ${dl_arch:-未知}"
    fi
    log_ok "RPM 包已下载到 $RPM_FILE（来源 ${USED_REPO}），原有 YUM 源已恢复"
}

# ── 安装依赖（先用系统原有源，失败回退 DEPS_REPOS；均走备份/恢复） ──
install_deps() {
    if [[ -z "${RUNTIME_DEPS}" ]]; then
        log_info "无需额外运行时依赖"
        return 0
    fi
    log_info "安装运行时依赖: ${RUNTIME_DEPS}"
    if dnf install -y ${RUNTIME_DEPS}; then
        log_ok "依赖安装完成（系统原有源）"
        return 0
    fi
    log_warn "系统原有源安装依赖失败，尝试专用依赖源..."
    backup_repos
    trap restore_repos EXIT
    local ok=0 used=""
    for url in "${DEPS_REPOS[@]}"; do
        if ! check_reachable "$url"; then
            continue
        fi
        configure_temp_repo "$url"
        if dnf clean all && dnf makecache && dnf install -y ${RUNTIME_DEPS}; then
            ok=1; used="$url"
            break
        fi
    done
    restore_repos
    if [[ $ok -eq 1 ]]; then
        log_ok "依赖安装完成（${used}）"
    else
        log_err "所有源均无法安装运行时依赖，终止安装"
    fi
}

# ── 自动生成 systemd 服务文件（标准安装专用；RPM 自带 unit 时不覆盖） ──
create_systemd_service() {
    local prefix="$1"

    # v2：SERVICE_NAME 为空 = 无服务型软件（CLI 工具），跳过 unit 生成
    if [[ -z "${SERVICE_NAME}" ]]; then
        log_info "未配置服务名（SERVICE_NAME 为空），跳过 systemd 服务生成"
        return 0
    fi

    if systemctl cat "${SERVICE_NAME}.service" &>/dev/null \
        || [ -f "/usr/lib/systemd/system/${SERVICE_NAME}.service" ] \
        || [ -f "/etc/systemd/system/${SERVICE_NAME}.service" ]; then
        log_info "检测到已存在 ${SERVICE_NAME}.service（RPM 自带），不覆盖"
        systemctl daemon-reload || true
        return 0
    fi

    local service_file="/etc/systemd/system/${SERVICE_NAME}.service"
    cat > "$service_file" << EOF
[Unit]
Description=${PKG_NAME} service
After=network.target

[Service]
Type=forking
ExecStart=${prefix}/bin/${BIN_NAME} -f ${CONF_FILE}
ExecStop=${prefix}/bin/${BIN_NAME} -k stop
PIDFile=${prefix}/logs/${BIN_NAME}.pid
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    log_ok "${SERVICE_NAME}.service 已创建"
}

# ── 自动调整端口与 ServerName（仅 httpd 家族配置；其他配置跳过，避免污染） ──
auto_adjust_config() {
    local conf_file="$1"
    [[ -f "$conf_file" ]] || return

    if ! grep -qE '^[[:space:]]*Listen[[:space:]]' "$conf_file"; then
        log_info "跳过配置自动调整（${conf_file} 非 Listen 型配置）"
        return
    fi

    if ! grep -q "^ServerName" "$conf_file"; then
        echo "ServerName localhost:${DEFAULT_PORT}" >> "$conf_file"
    fi

    if ss -tlnp 2>/dev/null | grep -q ":${DEFAULT_PORT} " || netstat -tlnp 2>/dev/null | grep -q ":${DEFAULT_PORT} "; then
        log_warn "端口 ${DEFAULT_PORT} 已被占用，自动改为 8080"
        sed -i "s/^Listen ${DEFAULT_PORT}/Listen 8080/" "$conf_file"
        sed -i "s/localhost:${DEFAULT_PORT}/localhost:8080/" "$conf_file"
    fi
}

# ── 标准 RPM 安装 ──
install_rpm_standard() {
    log_info "采用标准 RPM 安装模式..."

    if rpm -q ${PKG_NAME} &>/dev/null; then
        rpm -Uvh --oldpackage --replacepkgs "$RPM_FILE"
    else
        rpm -ivh "$RPM_FILE"
    fi

    local actual_bin
    actual_bin=$(find_existing_bin || true)
    if [[ -z "$actual_bin" ]]; then
        log_err "安装后未找到 ${BIN_NAME} 二进制"
    fi

    INSTALL_PREFIX="$(dirname "$(dirname "$actual_bin")")"
    log_ok "${BIN_NAME} 安装成功: $(bin_version "$actual_bin")"

    # 自动调整配置文件
    for c in "$CONF_FILE" "${INSTALL_PREFIX}/conf/${PKG_NAME}.conf"; do
        [[ -f "$c" ]] && auto_adjust_config "$c" && break
    done

    # 确保日志目录权限
    if [[ -d "${INSTALL_PREFIX}/logs" ]]; then
        if ! id -u ${PKG_NAME} &>/dev/null; then
            useradd -r -s /sbin/nologin -d /var/lib/${PKG_NAME} ${PKG_NAME} 2>/dev/null || true
        fi
        chown -R ${PKG_NAME}:${PKG_NAME} "${INSTALL_PREFIX}/logs" 2>/dev/null || true
    fi

    create_systemd_service "$INSTALL_PREFIX"
}

# ── 提取安装模式（rpm2cpio，自定义路径） ──
install_extract_mode() {
    log_info "采用提取安装模式，目标: ${INSTALL_PREFIX}"

    if [[ -f "${INSTALL_PREFIX}/bin/${BIN_NAME}" ]]; then
        local existing_ver
        existing_ver=$(bin_version "${INSTALL_PREFIX}/bin/${BIN_NAME}")
        if echo "$existing_ver" | grep -q "${VERSION}"; then
            log_ok "已存在相同版本，跳过安装"
            return 0
        fi
    fi

    local tmpdir=$(mktemp -d)
    trap 'rm -rf "$tmpdir"' EXIT
    cd "$tmpdir"
    rpm2cpio "$RPM_FILE" | cpio -idm

    mkdir -p "$INSTALL_PREFIX"/{bin,conf,modules,logs}

    # 查找并复制二进制
    local src_bin=""
    for p in usr/sbin/${BIN_NAME} usr/bin/${BIN_NAME} usr/local/${PKG_NAME}/bin/${BIN_NAME} opt/*/bin/${BIN_NAME}; do
        if [[ -f "$p" ]]; then src_bin="$p"; break; fi
    done
    [[ -z "$src_bin" ]] && log_err "RPM 包中未找到 ${BIN_NAME} 二进制"
    cp -a "$src_bin" "${INSTALL_PREFIX}/bin/${BIN_NAME}"

    # 复制辅助工具、配置、模块等（通用简化版，可根据实际扩展）
    # 此处保留基本框架，具体复制逻辑由 agent 根据软件类型决定是否生成详细步骤

    # 用户与权限
    if ! id -u ${PKG_NAME} &>/dev/null; then
        useradd -r -s /sbin/nologin -d /var/lib/${PKG_NAME} ${PKG_NAME}
    fi
    chown -R ${PKG_NAME}:${PKG_NAME} "${INSTALL_PREFIX}/logs"
    chmod 755 "${INSTALL_PREFIX}/logs"

    log_ok "${BIN_NAME} 安装成功: $(bin_version "${INSTALL_PREFIX}/bin/${BIN_NAME}")"
}

# ── 环境变量 PATH ──
setup_env_path() {
    local bin_dir="${INSTALL_PREFIX:-/usr/sbin}"
    local profile_file="/etc/profile.d/${PKG_NAME}.sh"
    cat > "$profile_file" << EOF
# ${PKG_NAME} 环境变量（由 install-rpm.sh 自动生成）
if [[ ":\${PATH}:" != *":${bin_dir}:"* ]]; then
    export PATH="${bin_dir}:\${PATH}"
fi
EOF
    chmod 644 "$profile_file"
    export PATH="${bin_dir}:${PATH}"
    log_ok "已添加 ${bin_dir} 到 PATH"
}

# ── 安装摘要 ──
print_summary() {
    echo ""
    log_ok "============================================"
    log_ok "${PKG_NAME}-${VERSION}-${RELEASE} 安装完成!"
    log_ok "============================================"
    if [[ -z "${SERVICE_NAME}" ]]; then
        log_info "CLI 工具：直接运行 ${BIN_NAME}（可用 --version 验证）"
    elif [[ -n "$INSTALL_PREFIX" ]]; then
        log_info "启动: ${INSTALL_PREFIX}/bin/${BIN_NAME} -f ${CONF_FILE}"
    else
        log_info "启动: systemctl start ${SERVICE_NAME}"
        log_info "开机自启: systemctl enable ${SERVICE_NAME}"
    fi
    log_info "环境变量已通过 /etc/profile.d/${PKG_NAME}.sh 配置"
    log_info "如需立即在当前终端使用，请执行: source /etc/profile.d/${PKG_NAME}.sh"
}

# ── 主流程 ──
main() {
    echo ""
    echo -e "${BLUE}╔══════════════════════════════════════════════╗${NC}"
    echo -e "${BLUE}║   ${PKG_NAME}-${VERSION}-${RELEASE} RPM 安装脚本 (自动生成)    ║${NC}"
    echo -e "${BLUE}╚══════════════════════════════════════════════╝${NC}"
    echo ""

    preflight_check
    trap restore_repos EXIT      # 全程兜底：任何阶段的 exit 都恢复原有源
    download_rpm                 # 备份原有源 → 下载 RPM（dnf 优先，curl --fail 兜底）→ 恢复原有源
    install_deps                 # 先用恢复后的原有源装依赖，失败回退专用依赖源（同样备份/恢复）
    trap - EXIT                  # 源已恢复且后续不再动源，解除兜底

    if [[ -n "$INSTALL_PREFIX" ]]; then
        install_extract_mode
    else
        install_rpm_standard
    fi

    setup_env_path
    print_summary
}

main
```

## 变量填充说明

- **PKG_NAME / VERSION / RELEASE / ARCH**：取自验证报告实测 `rpm -q`（完整 NEVRA 拆分）；RELEASE 含 dist tag（如 `8.oe2203`），curl 文件名按其真实构造。
- **RPM_REPOS**：**必须以验证实测数据确定**——目标包实际所在、且 `repodata/repomd.xml` 探测 200 的仓，按优先级排序；格式 `"仓库URL|包目录相对路径"`（如 `"https://repo.huaweicloud.com/openeuler/openEuler-22.03-LTS/update/aarch64/|Packages/"`）；鲲鹏 openlab 等根目录散包源填空路径。禁止注入未经验证的源。
- **DEPS_REPOS**：依赖兜底源，默认 HCE 官方 `hce/2.0/{os,updates}/aarch64/`（hce-rpm-check 实测含全部 15 项 opengauss 依赖）；目标非 HCE 时按实测替换。
- **RUNTIME_DEPS**：以 `rpm -qpR`（或 `rpm -q --requires`）的**包级 Requires 清单**为准原样填入（去重、空格分隔）；无则空串（跳过安装）。⚠️ 不要机械「去 -devel」——部分软件（如 opengauss-2.1.0）的 RPM 硬性 Requires `-devel` 包本身，脚本用 `rpm -ivh` 安装时缺 -devel 名必失败（实测踩坑：去 -devel 清单导致 12 项 Failed dependencies）。仅当包级 Requires 确实不含 -devel 时才输出运行时包名。
- **BIN_NAME / SERVICE_NAME / CONF_FILE / DEFAULT_PORT**：取自 RPM 文件列表（`rpm -ql`）、包内 unit 名、`%post` 行为与实测端口；数据库类软件 CONF_FILE 指向数据目录内实际生成的配置（`auto_adjust_config` 对其自动跳过，不会污染）。**无服务型软件（CLI 工具，RPM 不含 unit）SERVICE_NAME 填空串**——v2 模板对空值跳过 unit 生成；**noarch 包 ARCH 填 noarch**——v2 模板 preflight 对 noarch 放行，RPM_FILENAME/架构校验按包真实 NEVRA 构造。

## 禁止事项

- 禁止在生成脚本中偏离本模板结构（增删函数、改控制流）；源列表属「默认配置」区，只能注入**实测可用**的源。
- 禁止跳过初始镜像实测就将脚本标注「初始镜像可用」；禁止在 rpm 实测机（非初始状态）上试跑。
- 禁止用 `--nodeps`/`--force` 或升级核心库的方式让实测通过。
- 不直接 `ssh`/`scp`；不在报告/对话暴露凭据。
- 实测失败时禁止私改模板凑通过——记模板缺陷上报。
