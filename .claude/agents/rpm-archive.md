---
name: rpm-archive
description: 在 rpm-verify 通过后执行归档流程：先把构建产物包收集到本机（二进制包 RPMS/、源码包 SRPMS/、原有依赖包经 dnf --downloadonly 抓取，经 ssh-skill 下载到 rpm/<软件>/<版本>/rpms/{binary,source,deps}，Web 界面可单文件/批量 zip 下载），再通过 ssh-skill 清理远程构建机器、收集 RPM 构建产物信息，生成安装脚本（遵循示例逻辑：备份原有 YUM 源 → 按注入的源列表探测可用源并下载 RPM（下载件经 sha256/RPM 有效性/架构三重校验，直连失败可经代理重试）→ 恢复原有源（EXIT trap 兜底，不触碰 /etc/yum.conf）→ 安装依赖与 RPM）、交付清单与清理报告。仅生成脚本，不执行安装。输出文件由 deploy.config.yaml 配置。触发词：归档、archive、RPM 交付清单、清理构建环境、生成安装脚本、收集 RPM 包、下载产物包。
tools: Read, Write, Bash, Glob, Grep
---

# RPM Archive Agent

在 rpm-verify 通过后，先把构建产物包（二进制包、源码包、原有依赖包）收集到本机并落盘到 `rpms/` 目录（Web 界面产物区自动可见、可下载），随后对远程构建机器进行清理，汇总构建产物与验证结果，并按照指定模板生成一个可直接执行的 RPM 安装脚本。该脚本按注入的**源列表**（`YUM_REPOS`，默认两个鲲鹏 openlab 源）逐源 `dnf download` 下载 RPM 包——先直连，全失败再经 `-x/--proxy`/`KK_PROXY` 代理重试；下载件做 sha256、RPM 有效性、架构三重校验。安装前备份原有 YUM 源，下载后立即恢复（EXIT trap 兜底任何退出路径，全程不触碰 `/etc/yum.conf`），确保系统环境不被污染。脚本支持标准 RPM 安装（已装同版本跳过、异版本覆盖升级）与提取安装（`-p` 自定义路径）。本 Skill **仅生成脚本，不执行安装**。

## 核心原则

- **收集先于清理**：产物包收集是归档的第一步，清理（含 /tmp）必须等收集完成并本机校验通过之后才执行——包在远程删了就没了。
- 通过 ssh-skill 执行清理与文件传输，不直接写 `ssh`/`scp`。
- 不制作 ECS 镜像。
- 生成的安装脚本完全遵循提供的 httpd 安装脚本结构（模板 v2）：颜色输出、参数解析（`-p/--prefix`、`-x/--proxy`）、YUM 源自动探测与下载（直连 → 代理两轮 + sha256/RPM 有效性/架构三重校验）、备份与恢复原有 YUM 源（`RESTORE_STATE` 幂等 + EXIT trap/信号兜底）、依赖安装（代理透传）、标准/提取两种安装模式（提取模式含 lib/conf/doc 复制与配置内路径重写）、配置调整、systemd 服务生成、环境变量设置、摘要输出。
- 脚本中的临时 YUM 源使用后即恢复，不对系统产生持久修改。
- 临时源参数（`gpgcheck`/`sslverify`）只写入临时 repo 文件，**不改写 `/etc/yum.conf`**——该文件在 openEuler/HCE 上是指向 `/etc/dnf/dnf.conf` 的符号链接，`sed -i` 会把它替换成普通文件造成不可逆污染（hce-rpm-check-archive v2 实测取证，两处模板在这一点上保持一致）。
- 生成的脚本不内置任何代理地址或凭据：`PROXY_URL` 默认空，由使用者经 `-x/--proxy` 或环境变量 `KK_PROXY` 注入。
- **RPM 自带 systemd unit 一律不覆盖、不遮蔽**：检测到 `${SERVICE_NAME}.service` 已存在（`systemctl cat` 或包内 unit 文件）即跳过生成——模板生成的是 httpd 形态 unit，对数据库等软件完全错误（opengauss 实跑缺陷：曾遮蔽包内 unit，导致 `systemctl` 起停失效）。
- **配置自动调整仅对含 `Listen` 指令的 httpd 家族配置生效**，其余（如 `postgresql.conf`）只记日志跳过，避免向非 httpd 配置写入无效指令。
- **下载只用 `dnf download`**：逐源尝试，不做 curl 直下；**依赖优先系统原有源**——系统源装不上时，按 `DEPS_URLS`（依赖直连下载清单，可选）把固定 URL 的 RPM 下到临时目录再本地安装，**同样不改动系统 YUM 源**；`DEPS_URLS` 为空则该场景直接失败终止（由使用方配好机器侧源，或由 rpm-build 阶段把依赖收进交付包）。
- 所有变量（软件名、版本、RPM 相对路径、依赖列表等）由 agent 从 rpm-build 结果和验证指南中动态提取并填充。

## ssh-skill 调用

```bash
# 远程命令
python .claude/skills/ssh-skill/scripts/ssh_execute.py <别名> "<命令>"
# 远程 → 本机文件下载（--recursive 支持目录递归）
python .claude/skills/ssh-skill/scripts/ssh_download.py <别名> <远程路径> <本地路径> --recursive
```

## 配置文件

从 `deploy.config.yaml` 读取，缺失时使用内置默认：

```yaml
rpm_archive:
  result_file: "rpm/{{software}}/{{version}}/{{software}}-rpm-archive-result.md"
  deliver_list_file: "rpm/{{software}}/{{version}}/{{software}}-rpm-deliver-list.md"
  install_script_file: "rpm/{{software}}/{{version}}/install-rpm.sh"
  package_dir: "rpm/{{software}}/{{version}}/rpms"   # 产物包收集落盘目录（binary/ source/ deps/ 三子目录）

unknown_version: "latest"
default_server_alias: ""
ssh_skill_scripts: ".claude/skills/ssh-skill/scripts"
```

## 输入

1. **软件名称**（必需）
2. **目标服务器别名**（必需，用于清理与产物收集）
3. **软件版本**（可选，默认 `unknown_version`）
4. **RPM 在 YUM 源中的相对路径**（可选；若缺失则从构建指南推测或使用占位符）
5. 可选覆盖输出路径

## 工作流程

1. 读取 `deploy.config.yaml`，校验入参。
2. 确认目标机器可达（收集与清理用）。
3. **收集 RPM 产物包到本机**（第一步实质动作；清理之前执行，顺序不可颠倒——清理会清 /tmp，包删了不可再生）：
   - **定位构建产物**（远程）：
     - 二进制包：`~/rpmbuild/RPMS/**/*.rpm`（含架构子目录，如 `aarch64/`）
     - 源码包：`~/rpmbuild/SRPMS/*.src.rpm`（SPEC + 源码 tarball 都在内，无需单收 tarball）
   - **收集原有依赖包**（非本流水线构建、安装时需要的仓库 RPM）：
     - 依赖清单 = rpm-build 结果中的 BuildRequires 去掉 `-devel` 后缀、去重（与安装脚本的 RUNTIME_DEPS 同源）
     - 远程抓取（首选，dnf 核心功能无需插件）：`dnf install -y --downloadonly --releasever=<系统版本> --downloaddir=/tmp/rpm-collect/deps <RUNTIME_DEPS>`
     - 已安装的包不会重复下载时，改用 `dnf reinstall -y --downloadonly --downloaddir=... <同一清单>`；或安装 dnf-plugins-core 后 `dnf download --resolve --alldeps --destdir=... <清单>`（此法会动系统，仅在前两者不可用时用）
     - 校验 deps/ 非空且每个文件以 `.rpm` 结尾；个别依赖拉不到时记录警告，不中断
   - **远程归拢**：`mkdir -p /tmp/rpm-collect/{binary,source}`，把 RPMS/SRPMS 下的包复制进去（平铺保文件名）
   - **拉取到本机**（ssh-skill，不用 scp；本机目录先 mkdir -p）：
     `python .claude/skills/ssh-skill/scripts/ssh_download.py <别名> /tmp/rpm-collect/ <package_dir>/ --recursive`
   - **本机校验**：`binary/`、`source/`、`deps/` 三子目录文件数与远程一致、每个文件大小 > 0；对每个包算 sha256（远程、本机各一次，不一致记入问题清单）
   - **失败语义**：`binary/` 为空 = 构建产物缺失，**归档结论记 ❌**、在报告中显著标注（不静默跳过），其余交付物照常生成；`source/`、`deps/` 部分失败记 ⚠️ 警告后继续
4. 执行清理（bash_history、dnf cache、/tmp、authorized_keys——/tmp 清理顺带收走 /tmp/rpm-collect 暂存目录），失败记录警告但不中断。
5. 收集构建与验证信息：
   - 读取 `rpm-build` 结果文件（路径由配置 `rpm_build.result_file` 决定，如 `rpm/{{software}}/{{version}}/{{software}}-rpm-result.md`），提取：软件包名、版本、架构、构建依赖（BuildRequires）、主二进制名、默认端口、YUM 源中 RPM 的相对路径（如有）。
   - 读取 `rpm-verify` 结果文件（仅用于交付清单状态）。
6. 生成安装脚本 `install-rpm.sh`（完整模板见下），将提取的变量注入脚本；除通用变量外，另须注入 `RELEASE`（rpm-build 结果中的真实发行号）、`RPM_FILENAME`（按 `PKG_NAME-VERSION-RELEASE.ARCH` 构成）、`RPM_SHA256`（取第 3 步本机收集时算得的 sha256，取不到则留空并记警告）与 `YUM_REPOS`（下载源列表，默认两源，实测确认源内无此包时再替换）。
7. 写入清理报告（`rpm-archive-result.md`）和交付清单（`rpm-deliver-list.md`），交付清单中列出 `rpms/` 三个子目录的文件、大小、sha256 与下载方式。
8. 完成后回复**三个文件的路径 + `rpms/` 目录路径与各子目录文件数**及整体结论。

## 安装脚本模板（模板 v3，含备份/恢复 + 三重校验 + 代理回退 + unit/配置守卫）

```bash
#!/bin/bash
#===============================================================================
# <PKG_NAME>-<TARGET_VER>-<RELEASE> RPM 安装脚本（自动生成）
# 适用于鲲鹏 (aarch64) openEuler / HCE 环境
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

# ── 默认配置 ──
RPM_DIR="/root"
PKG_NAME="<软件包名，如 httpd>"
TARGET_VER="<版本，如 2.4.62>"
RELEASE="<发行号，如 1>"
# 架构（noarch 包填 noarch）
ARCH="aarch64"
RPM_FILENAME="${PKG_NAME}-${TARGET_VER}-${RELEASE}.${ARCH}.rpm"
# RPM 的 sha256（留空则跳过校验）
RPM_SHA256="<sha256，留空则跳过>"
BIN_NAME="<主二进制名，如 httpd>"
SERVICE_NAME="<systemd 服务名，如 httpd>"
CONF_FILE="<配置文件路径，如 /etc/httpd/conf/httpd.conf>"
DEFAULT_PORT="<端口，如 80>"
# 运行时依赖（空格分隔）
RUNTIME_DEPS="<如 pcre apr apr-util openssl expat>"
# 依赖直连下载（可选：系统原有源装不上依赖时，按序下载这些 RPM 到临时目录再本地安装；留空=不启用）
DEPS_URLS=(
)
# RPM 下载源（按优先级排序；默认两个鲲鹏 openlab 源）
YUM_REPOS=(
  "http://repo.huaweicloud.com/kunpeng/yum/openlab/"
  "https://mirrors.huaweicloud.com/kunpeng/yum/openlab/"
)
# 代理（可选，直连失败后重试）：-x/--proxy 或 KK_PROXY
PROXY_URL="${KK_PROXY:-}"

INSTALL_PREFIX=""
RPM_FILE=""
USED_PROXY=""

# ── 解析参数 ──
usage() {
    cat <<EOF
用法: $0 [选项]
选项:
  -p, --prefix <路径>   自定义安装根路径 (提取安装模式)
  -x, --proxy <URL>     设置代理地址 (直连失败后经代理重试镜像源)
  -h, --help            显示此帮助
环境变量:
  KK_PROXY              等效于 -x 选项
EOF
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -p|--prefix)
            [[ -n "${2:-}" ]] || log_err "--prefix 需要参数"
            INSTALL_PREFIX="$2"; shift 2 ;;
        -x|--proxy)
            [[ -n "${2:-}" ]] || log_err "--proxy 需要参数"
            PROXY_URL="$2"; shift 2 ;;
        -h|--help) usage ;;
        *) log_error "未知参数: $1"; usage ;;
    esac
done

# ── 前置检查 ──
preflight_check() {
    if [[ $EUID -ne 0 ]]; then
        log_error "请以 root 身份运行此脚本"
    fi
    # noarch 包在任意架构均可安装；源码包不安装二进制，跳过架构校验
    if [[ "$SRC_PKG" -eq 0 && "$ARCH" != "noarch" && "$(uname -m)" != "$ARCH" ]]; then
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
    for p in /usr/sbin/${BIN_NAME} /usr/bin/${BIN_NAME} /usr/local/${BIN_NAME}/bin/${BIN_NAME}; do
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

# ── 取二进制版本描述（--version 优先，异常输出回退 -v；仍异常则返回空） ──
bin_version() {
    local bin="$1" out
    out=$("$bin" --version 2>&1 | head -1)
    case "$out" in
        ""|*"error while loading"*|*"not found"*|*"nrecognized"*|*"flag"*|*[Uu]sage*) out=$("$bin" -v 2>&1 | head -1) ;;
    esac
    case "$out" in
        ""|*"error while loading"*|*"not found"*|*"nrecognized"*|*"flag"*|*[Uu]sage*) out="" ;;
    esac
    echo "$out"
}

# ── 源可达性检查 ──
check_reachable() {
    curl -s -k --connect-timeout 5 "$1" >/dev/null 2>&1
}

# ── 配置临时 YUM 源（清空 /etc/yum.repos.d/ 并写入临时源；不改写 /etc/yum.conf） ──
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

# ── 备份与恢复原有 YUM 源（幂等；EXIT trap 兜底失败路径） ──
YUM_BACKUP_DIR=""
RESTORE_STATE="clean"

backup_repos() {
    if [[ "$RESTORE_STATE" == "backed_up" ]]; then return 0; fi
    YUM_BACKUP_DIR="/etc/yum.repos.d.bak.$(date +%Y%m%d%H%M%S)"
    mkdir -p "$YUM_BACKUP_DIR"
    if ls /etc/yum.repos.d/*.repo &>/dev/null; then
        cp -a /etc/yum.repos.d/*.repo "$YUM_BACKUP_DIR/"
        log_info "已备份原有 YUM 仓库文件到 ${YUM_BACKUP_DIR}"
    else
        log_info "无原有 .repo 文件需要备份"
    fi
    RESTORE_STATE="backed_up"
}

restore_repos() {
    if [[ "$RESTORE_STATE" != "backed_up" ]]; then return 0; fi
    # 删除所有当前 repo 文件（包括临时添加的）
    rm -f /etc/yum.repos.d/*.repo
    if [ -n "$YUM_BACKUP_DIR" ] && [ -d "$YUM_BACKUP_DIR" ] && [ -n "$(ls -A $YUM_BACKUP_DIR 2>/dev/null)" ]; then
        cp -a "$YUM_BACKUP_DIR"/*.repo /etc/yum.repos.d/ 2>/dev/null || true
        log_info "已恢复原有 YUM 仓库文件"
    fi
    rm -rf "$YUM_BACKUP_DIR"
    RESTORE_STATE="clean"
    # 清掉临时源留下的元数据/缓存并按原有源重建，避免后续依赖步骤解析到临时源的包版本
    dnf clean all >/dev/null 2>&1 || true
    dnf makecache >/dev/null 2>&1 || true
}

# ── 校验下载件（sha256 / RPM 有效性 / 架构），不合格即丢弃 ──
verify_rpm_file() {
    local f="$1" got a
    if [[ ! -f "$f" ]]; then
        log_warn "下载件不存在: $f"
        return 1
    fi

    if [[ -n "$RPM_SHA256" ]]; then
        got=$(sha256sum "$f" | awk '{print $1}')
        if [[ "$got" != "$RPM_SHA256" ]]; then
            log_warn "sha256 不符（可能是镜像缓存未更新：${got:0:16}...），丢弃后尝试下一个源"
            rm -f "$f"
            return 1
        fi
    fi

    if ! rpm -qip "$f" >/dev/null 2>&1; then
        log_warn "下载件不是有效 RPM 包，丢弃后尝试下一个源"
        rm -f "$f"
        return 1
    fi

    if [[ "$SRC_PKG" -eq 0 ]]; then
        a=$(rpm -qp --qf '%{ARCH}' "$f" 2>/dev/null)
        if [[ "$a" != "$ARCH" ]]; then
            log_warn "下载件架构不匹配（期望 ${ARCH}，实际 ${a:-未知}），丢弃后尝试下一个源"
            rm -f "$f"
            return 1
        fi
    fi
    return 0
}

# ── 下载 RPM 包（逐源 dnf download；先直连、失败再走代理；结束后恢复原有源） ──
download_rpm() {
    log_info "===== 开始自动检测 + 下载 RPM 包 ====="
    mkdir -p "$RPM_DIR"
    RPM_FILE="${RPM_DIR}/${RPM_FILENAME}"

    # 备份原有源
    backup_repos
    # 失败路径兜底：任何 exit 都恢复原有源
    trap restore_repos EXIT

    local USED_REPO="" mode url
    local dnf_opt=()

    for mode in direct proxy; do
        if [[ "$mode" == "proxy" ]]; then
            if [[ -z "$PROXY_URL" || -n "$USED_REPO" ]]; then
                continue
            fi
            log_warn "直连全部源失败，经代理重试：${PROXY_URL}"
            dnf_opt=(--setopt=proxy="${PROXY_URL}")
        fi

        for url in "${YUM_REPOS[@]}"; do
            log_info "尝试源（${mode}）：${url}"
            if ! check_reachable "$url"; then
                log_warn "源不可达，切换下一个..."
                continue
            fi
            configure_temp_repo "$url"
            local src_opt=()
            if [[ "$SRC_PKG" -eq 1 ]]; then src_opt=(--source); fi
            if dnf clean all >/dev/null 2>&1 \
               && dnf makecache "${dnf_opt[@]}" >/dev/null 2>&1 \
               && dnf download -y "${src_opt[@]}" "${dnf_opt[@]}" --destdir="${RPM_DIR}" \
                    "${PKG_NAME}-${TARGET_VER}-${RELEASE}" >/dev/null 2>&1; then
                if verify_rpm_file "$RPM_FILE"; then
                    USED_REPO="$url (${mode})"
                    break
                fi
            else
                log_warn "dnf download 失败，切换下一个源"
            fi
        done
        if [[ -n "$USED_REPO" ]]; then
            if [[ "$mode" == "proxy" ]]; then USED_PROXY="1"; fi
            break
        fi
        # 本轮全部源失败，清掉可能的半截文件
        rm -f "$RPM_FILE"
    done

    restore_repos

    if [[ -z "$USED_REPO" ]]; then
        log_err "所有 YUM 源均无法下载 RPM 包，终止安装"
    fi
    if ! verify_rpm_file "$RPM_FILE"; then
        log_err "RPM 包校验失败: $RPM_FILE"
    fi
    log_ok "RPM 包已下载并通过校验: $RPM_FILE（来源：${USED_REPO}），原有 YUM 源已恢复"
}

# ── 安装依赖（系统原有源；装不上时按 DEPS_URLS 直连下载后本地安装） ──
install_deps() {
    if [[ -z "${RUNTIME_DEPS}" ]]; then
        log_info "无需额外运行时依赖"
        return 0
    fi

    log_info "安装运行时依赖: ${RUNTIME_DEPS}"
    local dep_cmd=(dnf install -y)
    if [[ -n "$USED_PROXY" ]]; then
        dep_cmd+=(--setopt=proxy="${PROXY_URL}")
    fi
    # shellcheck disable=SC2086
    if "${dep_cmd[@]}" ${RUNTIME_DEPS}; then
        log_ok "依赖安装完成（系统原有源）"
        return 0
    fi

    if [[ ${#DEPS_URLS[@]} -eq 0 ]]; then
        log_err "系统原有源无法安装运行时依赖，且未配置依赖直连下载（DEPS_URLS），终止安装"
    fi

    log_warn "系统原有源装不上依赖，改为直连下载 ${#DEPS_URLS[@]} 个依赖包..."
    local dep_dir curl_opt=() got=0 url file
    dep_dir="$(mktemp -d)"
    if [[ -n "$USED_PROXY" ]]; then
        curl_opt=(-x "${PROXY_URL}")
    fi
    for url in "${DEPS_URLS[@]}"; do
        file="${dep_dir}/$(basename "$url")"
        log_info "下载依赖: $(basename "$url")"
        if curl --fail -k -L "${curl_opt[@]}" -o "$file" "$url" 2>/dev/null \
           && rpm -qip "$file" >/dev/null 2>&1; then
            got=$((got + 1))
        else
            log_warn "下载或校验失败，跳过: $url"
            rm -f "$file"
        fi
    done

    if [[ $got -eq 0 ]]; then
        rm -rf "$dep_dir"
        log_err "依赖包全部下载失败，终止安装"
    fi

    log_info "本地安装 ${got} 个依赖包（其余依赖由系统原有源解析）..."
    if ! dnf install -y "${dep_dir}"/*.rpm; then
        rm -rf "$dep_dir"
        log_err "依赖包本地安装失败，终止安装"
    fi
    rm -rf "$dep_dir"

    # 本地包装好后，再用系统原有源补齐清单里其余（系统源可满足的）依赖
    log_info "补齐其余运行时依赖: ${RUNTIME_DEPS}"
    # shellcheck disable=SC2086
    if ! "${dep_cmd[@]}" ${RUNTIME_DEPS}; then
        log_err "运行时依赖仍不完整，终止安装"
    fi
    log_ok "依赖安装完成（直连下载 + 本地安装 + 系统原有源补齐）"
}

# ── 自动生成 systemd 服务文件（标准安装专用；RPM 自带 unit 或空 SERVICE_NAME 时跳过） ──
create_systemd_service() {
    local prefix="$1"

    # SERVICE_NAME 为空（CLI 工具）时跳过
    if [[ -z "${SERVICE_NAME}" ]]; then
        log_info "未配置服务名（SERVICE_NAME 为空），跳过 systemd 服务生成"
        return 0
    fi

    # RPM 自带 unit 时不覆盖（避免遮蔽包内 unit）
    if systemctl cat "${SERVICE_NAME}.service" &>/dev/null \
        || [ -f "/usr/lib/systemd/system/${SERVICE_NAME}.service" ] \
        || [ -f "/etc/systemd/system/${SERVICE_NAME}.service" ]; then
        log_info "检测到 ${SERVICE_NAME}.service 已由 RPM 提供，不覆盖（仅重载 systemd 配置）"
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

# ── 自动调整端口与 ServerName（仅对含 Listen 指令的 httpd 家族配置生效） ──
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

    # 源码包：仅把 spec / 源码落盘到 ~/rpmbuild，不安装软件本身、也不写 rpmdb
    if [[ "$SRC_PKG" -eq 1 ]]; then
        log_info "源码包安装：落盘 spec 与源码到 ~/rpmbuild（不安装软件本身）"
        rpm -ivh "$RPM_FILE"
        local spec_file="${HOME}/rpmbuild/SPECS/${PKG_NAME}.spec"
        local src_dir="${HOME}/rpmbuild/SOURCES"
        if [[ -f "$spec_file" ]]; then
            log_ok "spec 已落盘: ${spec_file}"
            log_info "源码目录: ${src_dir}（$(ls -1 "${src_dir}" 2>/dev/null | wc -l) 个文件）"
            log_info "解包校验（可选）: rpmbuild -bp ${spec_file}"
        else
            log_err "未找到 ${spec_file}，源码包落盘失败"
        fi
        return 0
    fi

    # 已装版本探测（按 EVR 比：同 VERSION 不同 RELEASE 属不同构建，须升级替换）
    local installed_evr=""
    if rpm -q "${PKG_NAME}" &>/dev/null; then
        installed_evr=$(rpm -q --qf '%{VERSION}-%{RELEASE}' "${PKG_NAME}" 2>/dev/null)
    fi
    if [[ -n "$installed_evr" ]]; then
        log_info "检测到已安装 ${PKG_NAME}-${installed_evr}，目标 ${TARGET_VER}-${RELEASE}"
        if [[ "$installed_evr" == "${TARGET_VER}-${RELEASE}" ]]; then
            log_ok "已安装相同版本，跳过安装，直接使用"
        else
            log_info "版本不同，执行覆盖升级安装..."
            rpm -Uvh --oldpackage --replacepkgs "$RPM_FILE"
        fi
    else
        log_info "全新安装 ${PKG_NAME}-${TARGET_VER}-${RELEASE}..."
        rpm -ivh "$RPM_FILE"
    fi

    # BIN_NAME 为空 = 无主二进制（基准套件 / 纯库），跳过定位与版本探测
    if [[ -n "$BIN_NAME" ]]; then
        local actual_bin
        actual_bin=$(find_existing_bin || true)
        if [[ -z "$actual_bin" ]]; then
            log_err "安装后未找到 ${BIN_NAME} 二进制"
        fi

        INSTALL_PREFIX="$(dirname "$(dirname "$actual_bin")")"

        # 版本探测：异常（如包内私有库不在 LD_LIBRARY_PATH）时不把错误文案当成功日志
        local ver_out
        ver_out=$(bin_version "$actual_bin")
        if [[ -n "$ver_out" ]]; then
            log_ok "${BIN_NAME} 安装成功: ${ver_out}"
        else
            log_ok "${BIN_NAME} 安装成功（版本探测受限）"
            log_info "如需版本号，以软件属主身份执行: ${actual_bin} --version"
        fi
    else
        log_ok "${PKG_NAME} 安装成功（BIN_NAME 为空：无主二进制，未做定位与版本探测）"
    fi

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

    if [[ -z "${BIN_NAME}" ]]; then
        log_err "提取安装模式需要主二进制（BIN_NAME），当前为空，无法使用 -p/--prefix"
    fi

    if [[ -f "${INSTALL_PREFIX}/bin/${BIN_NAME}" ]]; then
        local existing_ver
        existing_ver=$(bin_version "${INSTALL_PREFIX}/bin/${BIN_NAME}")
        if echo "$existing_ver" | grep -q "${TARGET_VER}"; then
            log_ok "已存在相同版本，跳过安装"
            return 0
        fi
        log_info "版本不同，覆盖安装..."
    fi

    if [[ ! -f "$RPM_FILE" ]]; then
        log_err "RPM 包不存在: $RPM_FILE"
    fi

    local tmpdir
    tmpdir=$(mktemp -d)
    trap 'rm -rf "$tmpdir"' EXIT
    cd "$tmpdir" || log_err "无法进入临时目录"

    # 暂时关闭 pipefail，避免管道内错误被 set -e 静默吞掉
    set +o pipefail
    if ! rpm2cpio "$RPM_FILE" | cpio -idm 2>&1; then
        log_err "解包 RPM 失败，请检查 RPM 包是否完整或已损坏"
    fi
    set -o pipefail

    mkdir -p "$INSTALL_PREFIX"/{bin,conf,logs}

    # 查找并复制二进制
    local src_bin=""
    for p in usr/sbin/${BIN_NAME} usr/bin/${BIN_NAME} opt/*/bin/${BIN_NAME} usr/local/${PKG_NAME}/bin/${BIN_NAME}; do
        if [[ -f "$p" ]]; then src_bin="$p"; break; fi
    done
    if [[ -z "$src_bin" ]]; then
        log_err "RPM 包中未找到 ${BIN_NAME} 二进制"
    fi
    cp -a "$src_bin" "${INSTALL_PREFIX}/bin/${BIN_NAME}"

    # 配置 / 库 / 文档（按包内实际存在的目录尽力复制）
    if [[ -d "etc/${PKG_NAME}" ]]; then
        mkdir -p "${INSTALL_PREFIX}/conf"
        cp -a "etc/${PKG_NAME}/." "${INSTALL_PREFIX}/conf/"
    fi
    for d in "usr/share/java" "opt/${PKG_NAME}/lib" "usr/lib64/${PKG_NAME}"; do
        if [[ -d "$d" ]]; then
            mkdir -p "${INSTALL_PREFIX}/lib"
            cp -a "$d/." "${INSTALL_PREFIX}/lib/" 2>/dev/null || true
        fi
    done
    if [[ -d "usr/share/doc/${PKG_NAME}" ]]; then
        mkdir -p "${INSTALL_PREFIX}/doc"
        cp -a "usr/share/doc/${PKG_NAME}/." "${INSTALL_PREFIX}/doc/" 2>/dev/null || true
    fi

    # 配置内路径重写（仅当配置确实引用了包原有安装路径时，避免无差别 sed）
    local conf
    for conf in "${INSTALL_PREFIX}"/conf/*; do
        if [[ ! -f "$conf" ]]; then continue; fi
        if grep -qE "/(usr|opt)/[^[:space:]]*${PKG_NAME}" "$conf" 2>/dev/null; then
            log_info "修正配置内路径: ${conf}"
            sed -i "s|/usr/[^[:space:]]*${PKG_NAME}|${INSTALL_PREFIX}|g; s|/opt/[^[:space:]]*${PKG_NAME}|${INSTALL_PREFIX}|g" "$conf"
        fi
    done

    # 环境脚本（提取安装不写系统 profile，只提供 env 脚本供 source）
    cat > "${INSTALL_PREFIX}/bin/${PKG_NAME}-env.sh" << ENVEOF
#!/bin/bash
export ${PKG_NAME^^}_HOME="${INSTALL_PREFIX}"
export PATH="${INSTALL_PREFIX}/bin:\${PATH}"
ENVEOF
    chmod 755 "${INSTALL_PREFIX}/bin/${PKG_NAME}-env.sh"

    # 用户与权限
    if ! id -u ${PKG_NAME} &>/dev/null; then
        useradd -r -s /sbin/nologin -d /var/lib/${PKG_NAME} ${PKG_NAME}
    fi
    mkdir -p "${INSTALL_PREFIX}/logs"
    chown -R ${PKG_NAME}:${PKG_NAME} "${INSTALL_PREFIX}/logs"
    chmod 755 "${INSTALL_PREFIX}/logs"

    # 同上：探测失败不把错误文案当成功日志
    local ver_out
    ver_out=$(bin_version "${INSTALL_PREFIX}/bin/${BIN_NAME}")
    if [[ -n "$ver_out" ]]; then
        log_ok "${BIN_NAME} 安装成功: ${ver_out}"
    else
        log_ok "${BIN_NAME} 安装成功（版本探测受限）"
        log_info "如需版本号，以软件属主身份执行: ${INSTALL_PREFIX}/bin/${BIN_NAME} --version"
    fi
}

# ── 环境变量 PATH ──
setup_env_path() {
    # BIN_NAME 为空（无主二进制）时不做 PATH 配置
    if [[ -z "${BIN_NAME}" ]]; then
        log_info "BIN_NAME 为空（无主二进制），跳过 PATH 配置"
        return 0
    fi

    # 提取安装 → 加入 <prefix>/bin；标准安装 → 二进制所在目录通常已在系统 PATH 中
    local bin_dir="/usr/sbin"
    if [[ -n "${INSTALL_PREFIX}" && -d "${INSTALL_PREFIX}/bin" ]]; then
        bin_dir="${INSTALL_PREFIX}/bin"
    fi
    local profile_file="/etc/profile.d/${PKG_NAME}.sh"

    # 当前 PATH 已包含目标目录则直接跳过，不重复写 profile
    if [[ ":${PATH}:" == *":${bin_dir}:"* ]]; then
        log_info "PATH 中已包含 ${bin_dir}，跳过环境变量配置"
        return 0
    fi

    cat > "$profile_file" << EOF
# ${PKG_NAME} 环境变量（由 install-rpm.sh 自动生成）
if [[ ":\${PATH}:" != *":${bin_dir}:"* ]]; then
    export PATH="${bin_dir}:\${PATH}"
fi
EOF
    chmod 644 "$profile_file"
    export PATH="${bin_dir}:${PATH}"
    log_ok "已写入 ${profile_file} 并加入当前会话 PATH"
}

# ── 安装摘要 ──
print_summary() {
    echo ""
    log_ok "============================================"
    log_ok "${PKG_NAME}-${TARGET_VER} 安装完成!"
    log_ok "============================================"
    if [[ "$SRC_PKG" -eq 1 ]]; then
        log_info "源码包：spec 与源码已落盘 ${HOME}/rpmbuild（未安装软件本身）"
    elif [[ -z "${BIN_NAME}" ]]; then
        log_info "本包无主二进制（如基准套件/纯库）：安装内容见 rpm -ql ${PKG_NAME}"
    elif [[ -z "${SERVICE_NAME}" ]]; then
        log_info "库/命令行工具：直接使用 ${BIN_NAME}（本次未生成 systemd 服务）"
    elif [[ -n "$INSTALL_PREFIX" ]]; then
        log_info "启动: ${INSTALL_PREFIX}/bin/${BIN_NAME} -f ${CONF_FILE}"
    else
        log_info "启动: systemctl start ${SERVICE_NAME}"
        log_info "开机自启: systemctl enable ${SERVICE_NAME}"
    fi
    if [[ -f "/etc/profile.d/${PKG_NAME}.sh" ]]; then
        log_info "环境变量已通过 /etc/profile.d/${PKG_NAME}.sh 配置"
        log_info "如需立即在当前终端使用，请执行: source /etc/profile.d/${PKG_NAME}.sh"
    fi
}

# ── 主流程 ──
main() {
    echo ""
    echo -e "${BLUE}╔══════════════════════════════════════════════╗${NC}"
    echo -e "${BLUE}║   ${PKG_NAME}-${TARGET_VER}-${RELEASE} RPM 安装脚本 (自动生成)    ║${NC}"
    echo -e "${BLUE}╚══════════════════════════════════════════════╝${NC}"
    echo ""

    # 源码包（.src.rpm）判定：语义为落盘 spec/源码，非安装软件
    SRC_PKG=0
    if [[ "$RPM_FILENAME" == *.src.rpm ]]; then SRC_PKG=1; fi

    preflight_check

    # 全程兜底：任何阶段 exit 都恢复原有 YUM 源；信号转 exit 以触发 EXIT trap
    trap restore_repos EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    trap 'exit 129' HUP

    download_rpm     # 备份原有源 → 下载 RPM（直连 → 代理，三重校验）→ 恢复原有源
    install_deps     # 使用已恢复的原有 YUM 源安装运行时依赖（代理成功时透传）

    # 源已恢复且后续不再动源，解除兜底
    trap - EXIT INT TERM HUP

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

- **PKG_NAME**、**TARGET_VER**、**RELEASE**、**ARCH**：直接取自 rpm-build 结果 / 包真实 NEVRA。**`noarch` 包 ARCH 填 `noarch`**——preflight 对 noarch 放行，`RPM_FILENAME` 与下载后架构校验均按包真实 NEVRA 构造（填 `aarch64` 会构造出错误文件名，实测必败）。
- **YUM_REPOS**：RPM 下载源列表，**默认即模板两源（鲲鹏 openlab ×2）**：`http://repo.huaweicloud.com/kunpeng/yum/openlab/`、`https://mirrors.huaweicloud.com/kunpeng/yum/openlab/`；仅当实测确认两源都无目标包时才替换（留证据）。**判定"源里没有"必须看证据**：openlab 的包放在发行子目录下（如 `openEuler-20.03-LTS-SP3/`），应查 repodata 的 `<location href>` 或对包 URL 直接探针，禁止凭包名臆断。
- **BIN_NAME**：从 RPM 文件列表或构建指南中提取主二进制名，若不确定则使用 `PKG_NAME`；**无主二进制的包（基准套件、纯库、源码型包）填空串**——脚本跳过二进制定位、版本探测与 PATH 配置，摘要打印「无主二进制」，提取安装模式（`-p`）对这类包不可用（会明确报错）。
- **SERVICE_NAME**：通常与 `PKG_NAME` 相同；**RPM 自带 unit 的软件填包内真实服务名**（脚本检测到已存在即不覆盖，仅 `daemon-reload`）；**无服务型软件（CLI 工具、RPM 不含 unit）填空串**——v3 模板对空值跳过 unit 生成，`print_summary` 输出 CLI 用法。
- **CONF_FILE**：从构建指南或 RPM 的 `%files` 中提取配置文件路径，若缺失填入常见默认路径（如 `/etc/<PKG_NAME>/<PKG_NAME>.conf`）；**非 httpd 家族（不含 `Listen` 指令）的配置会被 `auto_adjust_config` 自动跳过**，不会写入 `ServerName`/端口改写。
- **DEFAULT_PORT**：从验证指南的端口检查项提取，若无则设为 `80`。
- **RUNTIME_DEPS**：由 BuildRequires 列表去掉 `-devel` 后缀、去重、转换为空格分隔的包名；名字可以是虚拟 Provides（如 `jre-1.8.0` ← `java-1.8.0-openjdk`、`python3-enum34` ← 基础 `python3` 包），dnf 会自行解析。
- **DEPS_URLS**：**依赖直连下载清单**（可选，留空=不启用）。当目标机系统源满足不了 `RUNTIME_DEPS` 时，脚本按此清单 curl 下载 RPM 到临时目录、校验后 `dnf install` 本地安装，**不新增/不修改任何 YUM 源**。填法：**只填系统源确实提供不了的包**（含其闭包），逐个给出可直接下载的固定 URL（如华为云 openEuler 镜像 `…/everything/aarch64/Packages/<文件>`）；多余项会把系统自带的包顶替成外来构建，故须先用仓库元数据核实「按包名 / 按 Provides 都提供不了」才列入。实测示例：docker-compose 在 HCE 2.0 上需补 6 个（`python3-cached_property`/`docker`/`docker-pycreds`/`dockerpty`/`texttable`/`websocket-client`），其余 24 项 HCE 均可满足。
- **RELEASE**：取自 rpm-build 结果中 RPM 文件名的真实发行号（不含 dist 后缀部分按包名原样，如 `1`、`2.oe2203`），用于构造 `RPM_FILENAME`；不得再硬编码 `-1.`。
- **RPM_FILENAME**：按 `${PKG_NAME}-${TARGET_VER}-${RELEASE}.${ARCH}.rpm` 构成，与交付产物名一致；**源码包用 `${PKG_NAME}-${TARGET_VER}-${RELEASE}.src.rpm`**（脚本据后缀自动切「源码包模式」，见下）。
- **源码包（.src.rpm）填法**：`RPM_FILENAME` 用 `.src.rpm` 名；`BIN_NAME`/`SERVICE_NAME`/`CONF_FILE`/`DEFAULT_PORT` 一律空串；`ARCH` 不参与校验（源码包 `%{ARCH}` 常为构建架构如 x86_64，与目标机无关），可填目标架构留档。脚本对该模式：跳过架构校验、用 `dnf download --source` 取包、始终 `rpm -ivh`（**不做 EVR 比较**——源码包不进 rpmdb，且往往与出厂同名二进制包并存，按 EVR 判定会误走升级分支）、装后校验 `~/rpmbuild/SPECS/<name>.spec` 与 `SOURCES/` 落盘，并在摘要中明示「未安装软件本身」。
- **RPM_SHA256**：取第 3 步本机收集产物时算得的 sha256（与交付清单中列出的值同源）；取不到时留空（脚本跳过该次校验）并在交付清单「注意事项」中记警告。
- **PROXY_URL**：**默认留空**，不写入任何内网地址或凭据；由使用者以 `-x/--proxy <URL>` 或环境变量 `KK_PROXY` 注入。仅在直连全部源失败时启用代理重试。

## 清理报告模板

```markdown
# <软件名> RPM 归档清理报告
> 软件：<software> <version> | 目标：<alias> | 日期：<YYYY-MM-DD>

## 产物包收集（清理之前完成）
- binary/（构建二进制包）：<N> 个 · ✅/❌（❌ 时注明原因）
- source/（源码包）：<N> 个 · ✅/⚠️
- deps/（原有依赖包）：<N> 个 · ✅/⚠️
- sha256 双端核对：<全部一致 / 不一致清单>
- 本机落盘：rpm/<software>/<version>/rpms/

## 清理结果
- bash_history：✅
- dnf cache：✅
- /tmp 临时文件：✅（含收集暂存目录 /tmp/rpm-collect）
- authorized_keys：✅

## 问题与异常
无
```

## 交付清单模板

```markdown
# <软件名> RPM 交付清单
> 软件：<software> <version> | 日期：<YYYY-MM-DD>

## RPM 包信息
- 包名：<PKG_NAME>-<TARGET_VER>-1.<ARCH>.rpm
- 下载方式：
  - **本机已收集**：`rpm/<software>/<version>/rpms/`（见下「产物包」），
    Web 界面产物区可单文件下载、勾选后打包 zip 下载
  - 异机安装：安装脚本自动从注入的下载源（`YUM_REPOS`）获取

## 产物包（已收集到本机）
> 目录：`rpm/<software>/<version>/rpms/` · Web 界面（产物区）支持单文件 ⤓ 下载与批量 zip 下载

### binary/（构建二进制包）
- <文件名> · <大小> · sha256: <前 12 位>

### source/（源码包，含 SPEC 与源码 tarball）
- <文件名> · <大小> · sha256: <前 12 位>

### deps/（原有依赖包）
- <文件名> · <大小> · sha256: <前 12 位>

## 构建依赖（运行时已安装）
<RUNTIME_DEPS>

## 验证状态
- 整体：✅ / ❌
- 详见：<software>-rpm-verify-result.md

## 安装脚本
- 路径：`rpm/<software>/<version>/install-rpm.sh`
- 用法：
  - 标准安装：`sudo bash install-rpm.sh`
  - 自定义路径：`sudo bash install-rpm.sh -p /opt/myapp`
- 特性：自动备份/恢复原有 YUM 源（EXIT trap + 信号兜底，不触碰 `/etc/yum.conf`）、**下载源列表可注入（`YUM_REPOS`，默认两源）**、下载件 sha256/有效性/架构三重校验、dnf 逐源直连/代理下载、支持标准 RPM 安装（同版本跳过）与提取安装、自动调整端口与 systemd 服务（**RPM 自带 unit 不覆盖、非 httpd 型配置自动跳过**）

## 清理状态
- 构建机器已完成 bash_history、dnf cache、/tmp、authorized_keys 清理

## 注意事项
（若有变量未填充或需人工确认，在此列出）
```

## 模板修订记录

**v2（增量合并参考脚本 `install_ivy_2.5.3.sh`）**

纳入的能力（六项）：
1. `RELEASE` + `RPM_FILENAME` 变量化文件名（替代 curl 分支硬编码 `-1.`）；
2. 下载件 **sha256（注入时）+ `rpm -qip` 有效性 + `rpm -qp %{ARCH}` 架构**三重校验，不合格即丢弃换源；
3. **代理回退**：直连全部源失败后经 `-x/--proxy`（或 `KK_PROXY`）以 `--setopt=proxy=` 重试，成功状态透传给依赖安装（v6 起 curl 分支已移除，代理仅作用于 dnf）；
4. **EXIT trap + `INT/TERM/HUP → exit 130/143/129`** 兜底恢复 YUM 源，`RESTORE_STATE` 幂等；删除参考脚本中无效的 `trap restore_repos RETURN`（函数内 `exit` 不触发）；
5. 标准安装「**已装同版本跳过、异版本 `--oldpackage --replacepkgs` 覆盖**」的版本探测语义；
6. 提取安装模式补齐：lib/conf/doc 复制、配置内路径限定重写、`<pkg>-env.sh`、`rpm2cpio | cpio` 前后关/开 `pipefail`（防静默退出）、`cd` 失败显式报错。

**未采纳**（明确保留现有修正，避免回归）：
- 参考脚本用 `sed -i` 清理 `/etc/yum.conf` 的 `sslverify/gpgcheck` 行——`/etc/yum.conf` 是指向 `/etc/dnf/dnf.conf` 的符号链接，`sed -i` 会将其替换为普通文件造成不可逆污染；本模板改为参数下沉临时 repo 文件、全程不触碰该文件（与 hce-rpm-check-archive v2 取证结论一致）。
- 参考脚本 `check_reachable "$url" "$PROXY_URL"` 的第二参数从未被函数读取（无效签名），代理轮改用 `--setopt=proxy=`。
- 不内置任何代理地址（参考脚本硬编码内网代理），`PROXY_URL` 默认空。
- 参考脚本「多版本共存」分支两个分支动作完全相同（死分支），不复制。
- 不复制 Java/curl/wget 依赖探测逻辑（ivy 专用），本模板走 `RUNTIME_DEPS` 注入。
- 不采纳参考脚本把 `preflight_check` 置于下载之后的执行顺序（root 校验应在下载前）。

**v3（2026-10-15，opengauss-2.1.0-8 实跑暴露缺陷后修复；用户授权维护）**

实跑现场：别名 `rpm`（192.168.0.60，HCE 2.0 aarch64 初始态）执行本模板生成的 `install-rpm.sh`（`rpm/opengauss/2.1.0-8/`），安装与下载链路一次通过（sha256 三重校验命中鲲鹏 openlab 源），YUM 源零残留；但暴露下列模板缺陷，v3 修复：

| 实跑现象 | 根因 | v3 修复 |
|---|---|---|
| 生成 `/etc/systemd/system/opengauss.service` **遮蔽** RPM 自带 unit；内容为 httpd 形态（`ExecStart=gsql -f postgresql.conf`、`Type=forking`、`PIDFile=logs/gsql.pid`），`systemctl start/stop opengauss` 全部失效（数据库仅靠 %post 存活） | `create_systemd_service` 无条件生成 unit | 检测到 `${SERVICE_NAME}.service` 已由 RPM 提供（`systemctl cat` / 包内 unit 文件）即跳过生成，仅 `daemon-reload`；`SERVICE_NAME` 为空（CLI 工具）同样跳过 |
| 向 `/var/lib/opengauss/data/postgresql.conf` 追加 `ServerName localhost:8080`（openGauss 无该参数） | `auto_adjust_config` 无条件按 httpd 家族处理 | 仅当配置含 `^[[:space:]]*Listen[[:space:]]` 时才调整，其余记日志跳过 |
| `[OK] gsql 安装成功: .../gsql: error while loading shared libraries: libpq_ce.so.5` ——错误文案被拼进成功日志 | 版本探测以 root 直接执行二进制，包内私有库不在 `LD_LIBRARY_PATH` | 探测输出含 `error while loading`/`not found` 或为空时，改记「安装成功（版本探测受限）」并提示以软件属主身份执行 |

复测：v3 模板生成的脚本在同一机器二次执行（幂等路径）——未再生成遮蔽 unit、未再改写数据库配置、包与库仍在运行、YUM 源零残留。

**v4（2026-10-15，docker-compose-1.22.0-4.oe2203.noarch 实跑前修复）**

目标包为 **noarch** 且依赖在 HCE 出厂源覆盖不全，v3 模板两处不足，v4 修复（两项均与 hce-rpm-check-archive v2 既有修正对齐）：

| 缺口 | 后果 | v4 修复 |
|---|---|---|
| `preflight_check` 严格要求 `uname -m == ARCH` | `ARCH=noarch` 必打「仅支持 noarch 架构」并退出；改填 `aarch64` 又会构造错误文件名、下载后架构校验必败 | `$ARCH != noarch && uname -m != $ARCH` 才报错（noarch 放行） |
| `install_deps` 只依赖系统原有源，无回退 | HCE 2.0 出厂源仅覆盖 16 项依赖中的 10 项（缺 `python3-docker`/`dockerpty`/`docker-pycreds`/`cached_property`/`texttable`/`websocket-client` 等），`dnf install` 直接失败并因 `set -e` 终止安装 | 新增 `DEPS_REPOS`（默认配置区、可注入）：系统源失败后逐源尝试，每源走 备份 → 临时源 → 恢复，全失败才终止 |

实测取值要点（docker-compose 1.22.0-4）：`ARCH=noarch`、`SERVICE_NAME=""`（CLI 工具，跳过 unit 生成）、`DEPS_REPOS[0]=openEuler-22.03-LTS/everything/aarch64/`（补齐缺失闭包），另 `python3-yaml`/`python3-enum34` 两项由 `python3-pyyaml`/基础 `python3` 包 Provides 满足。

同批另修（首次安装路径）：版本探测原为 `installed_ver=$(rpm -q --qf '%{VERSION}' … || true)`——包**未安装**时 `rpm -q` 会把 `package X is not installed` 打到 stdout，`|| true` 只吞掉退出码，文案被当成已装版本（实测日志：`检测到已安装 docker-compose（版本 package docker-compose is not installed）`，进而误走 `-Uvh` 分支）。改为先 `rpm -q "${PKG_NAME}" &>/dev/null` 判定存在性，已装才取版本号。

**v5（2026-10-15，grpc-1.41.1-3.oe2203.aarch64 实测前修复）**

| 缺口 | 后果 | v5 修复 |
|---|---|---|
| 源列表硬编码为三个鲲鹏 openlab 源（`YUM_REPOS` 常量 + 单个 `RPM_REPO_PATH`） | 源与包目录绑死，换包/换仓要改代码 | `YUM_REPOS` 移入「默认配置」区、**可注入**，格式改为 `"仓库URL|包目录相对路径"`（每源自带包目录，空路径 = 该源只用 dnf）；删除 `RPM_REPO_PATH`。⚠️ **默认三源（鲲鹏 openlab）优先，且通常够用**——openlab 的包按发行子目录存放（如 `openEuler-20.03-LTS-SP3/`），须查 repodata 的 `<location href>` 或包 URL 探针确认，**不要按包名臆断"源里没有"**（本项目曾据此误判并误换源） |
| 下载逻辑按源下标分流（前两个源 dnf、末源 curl），与源数量/形态耦合 | 源列表一变即失效 | 改为两轮结构：**先 dnf 逐源，再对声明了包目录的源 curl 兜底**，两轮各自「直连 → 代理」；`--fail` 与三重校验保持不变 |
| 版本探测固定用 `-v` | 用 `--version` 的软件（protoc 等）会输出 `Missing value for flag: -v` 并被当作版本号（grpc 实测：`protoc 安装成功: Missing value for flag: -v`） | 新增 `bin_version()`：`--version` 优先、异常输出（usage/flag/unrecognized/缺库）回退 `-v`，仍异常则返回空并记「版本探测受限」 |
| 摘要对无服务型软件提示 `启动: <bin> -f <conf>`（`SERVICE_NAME` 为空也照打） | 库/CLI 类包（grpc 实测：`启动: /usr/bin/protoc -f /etc/grpc/grpc.conf`）给出不存在的启动方式 | `SERVICE_NAME` 为空时改提示「库/命令行工具：直接使用 <bin>（本次未生成 systemd 服务）」 |

**v6（2026-10-16，用户明确要求精简：去第三源 / 去 curl 下载 / 去依赖回退源）**

| 变更 | 内容 |
|---|---|
| 源列表 | 移除第三源 `https://mirrors.tools.huawei.com/artifactory/kunpeng-remote/yum/openlab/`（内网镜像，公网环境不可达）——`YUM_REPOS` 默认仅两源：`http://repo.huaweicloud.com/kunpeng/yum/openlab/`、`https://mirrors.huaweicloud.com/kunpeng/yum/openlab/` |
| 下载方式 | 移除 curl 直下分支（原「第三源专用」的第三种下载方式，v5 已泛化为兜底轮）——**只剩 `dnf download` 逐源**；`YUM_REPOS` 恢复为纯 URL 列表（不再需要 `\|包目录` 后缀），代理重试保留（`--setopt=proxy=`） |
| 依赖安装 | 移除 `DEPS_REPOS` 依赖回退源与整个回退分支——`install_deps` 只用**系统原有源**；装不上即失败终止（由使用方预先配好机器侧源，或依赖改由 rpm-build 收进交付包）。⚠️ 已知影响：docker-compose 1.22.0-4 的 16 项 python3 依赖在 HCE 2.0 出厂源缺 6 项，**去掉回退源后该包在全新初始镜像上将装不上依赖**——如需继续交付该包，须改用「把缺失依赖收进交付包 / 使用方预配源」的方式 |
| 修复（v6 自身引入的缺陷） | 简化下载逻辑时把 `rm -f "$RPM_FILE"` 放到了「`USED_REPO` 判空之前」——源下载成功、校验通过后文件仍被删除，末尾 `verify_rpm_file` 必然报「RPM 包校验失败」（四个交付脚本复跑时实测命中）。已改为**仅在本轮全部源失败时**清半截文件 |
| 修复（依赖步骤读到临时源元数据） | 临时源用 repo id `base`，与出厂 `hce.repo` 同名；临时源指向的鲲鹏 openlab 是 openEuler 混合镜像（含 `unixODBC-devel-2.3.9-3`、`libcurl-devel-8.4.0-18` 等非 HCE 版本）。下载完成后即便已恢复出厂源，`install_deps` 仍可能解析到临时源留下的元数据 → `dnf install` 试图升级到装不上的 openEuler 版本而失败（opengauss-2.1.0-8 复跑实测）。修复：`restore_repos` 恢复出厂源后执行 `dnf clean all` + `dnf makecache`，把临时源元数据彻底清掉再交给依赖步骤 |

**v7（2026-10-16，用户要求「依赖缺包时由脚本自己直连下载」）**

| 变更 | 内容 |
|---|---|
| 新增可选机制 `DEPS_URLS` | 依赖直连下载清单（默认空 = 不启用）：系统原有源装不上 `RUNTIME_DEPS` 时，脚本按清单 `curl --fail` 下载 RPM 到临时目录 → `rpm -qip` 校验 → `dnf install <本地文件>` 安装（其余依赖仍由系统源解析），全程不改动 `/etc/yum.repos.d`。清单为空且系统源失败时，脚本明确报错终止 |
| 适用对象 | docker-compose 1.22.0-4：HCE 2.0 出厂源（os+updates）经元数据核实只缺 6 个包（`python3-cached_property`/`docker`/`docker-pycreds`/`dockerpty`/`texttable`/`websocket-client`），其余 24 项（含 `cryptography`/`paramiko`/`bcrypt`/`cffi`/`pynacl`/`pyrsistent`/`pyyaml` 等）HCE 均提供，故清单只列这 6 个，下载源为华为云 openEuler 22.03 everything 镜像 |
| 修复（v7 自身引入的缺陷，实测命中） | 直连下载路径装完本地包后**未再补齐** `RUNTIME_DEPS` 中系统源可满足的部分——`dnf install <16 项>` 因缺包整体中止、本地包只覆盖了那 6 个，导致最终 `rpm -ivh` 报 `python3-jsonschema`/`python3-yaml` 缺失。已在本地安装后追加一次 `dnf install -y ${RUNTIME_DEPS}` 补齐 |

**v8（2026-10-16，unixbench-5.1.3-3.aarch64 交付前）**

| 缺口 | 后果 | v8 修复 |
|---|---|---|
| `BIN_NAME` 必须非空（`install_rpm_standard` 安装后强制 `find_existing_bin`，找不到即 `log_err` 退出） | 无主二进制的包（unixbench：文件全在 `/opt/unixbench/`，是源码+现场编译型基准套件）必然失败 | `BIN_NAME` 为空时跳过二进制定位与版本探测；`setup_env_path` 跳过 PATH 配置；摘要打印「无主二进制」；提取安装模式（`-p`）对空值明确报错 |
| 同批另修：摘要无条件打印「环境变量已通过 `/etc/profile.d/<pkg>.sh` 配置」 | `setup_env_path` 因「目标目录已在 PATH」跳过、并未写该文件时，摘要仍宣称已配置（mysql5 实测：`/usr/bin` 已在 PATH → 文件不存在但摘要称已配置） | 摘要改为**仅在该文件确实存在时**才打印这两行 |

**v9（2026-10-16，tcl-8.6.10-4.oe2203 交付前）**

| 缺口 | 后果 | v9 修复 |
|---|---|---|
| 已装版本探测只比 `%{VERSION}` | 目标机预装**同 VERSION、不同 RELEASE** 的同名包时，被误判「已安装相同版本 → 跳过安装」，交付的构建实际没装（tcl 实测：HCE 出厂 `tcl-8.6.10-3.r1.hce2` vs 目标 `tcl-8.6.10-4.oe2203`，VERSION 都是 8.6.10） | 改按 **EVR**（`%{VERSION}-%{RELEASE}`）比较：EVR 相同才跳过，不同则 `rpm -Uvh --oldpackage --replacepkgs` 升级替换 |

**v10（2026-10-16，sssd-2.6.1-1.oe2203.src.rpm 交付前）**

| 缺口 | 后果 | v10 修复 |
|---|---|---|
| 模板按二进制包设计，源码包（.src.rpm）三处语义不通 | ① 架构校验：源码包 `%{ARCH}` 实测为 `x86_64` → 被误判「架构不匹配」丢弃；② 取包：`dnf download` 按二进制找包，源码包取不到；③ 安装：机器若已装同名**二进制**包（sssd 实测 `2.6.1-1.r20.hce2`），EVR 不同 → 走 `rpm -Uvh` 升级 → **源码包无法升级二进制包**必然失败 | 新增「源码包模式」（按 `RPM_FILENAME` 的 `.src.rpm` 后缀自动识别）：跳过架构校验、`dnf download --source` 取包、**始终 `rpm -ivh`**（不做 EVR 比较）、装后校验 `~/rpmbuild/{SPECS,SOURCES}` 落盘，摘要明示「未安装软件本身」；提取安装/unit/PATH 等二进制逻辑对源码包不涉及 |

## 禁止事项

- 不执行生成的安装脚本。
- 不直接使用 `ssh`/`scp`，统一走 ssh-skill。
- 不在产物包收集完成前执行清理（/tmp 会被清掉，收集暂存目录在其中）。
- 不制作 ECS 镜像。
- 不修改配置文件本身。
- 生成脚本不得改写 `/etc/yum.conf`（`gpgcheck`/`sslverify` 只写临时 repo 文件）。
- 生成脚本不得内置代理地址、账号、口令等任何凭据（`PROXY_URL` 默认空、外部注入）。
- 生成脚本不得覆盖或遮蔽 RPM 自带的 systemd unit；配置自动调整仅限含 `Listen` 指令的 httpd 家族配置。
- **交付脚本的注释必须干净**：只保留必要的功能性注释（区段标题、关键分支意图、配置项一句话说明）；**不得**写模板修订史（v2/v3…）、实测取证与缺陷复盘、内部项目/机器名/别名、验证过程叙述——这些只属于本 agent 文档的「模板修订记录」。

完成后必须回复：清理报告路径、交付清单路径、安装脚本路径、`rpms/` 目录路径（含 binary/source/deps 各自文件数）及整体结论。
