"""产物多根全量浏览：目录分组清单、内容读取、单文件下载与批量 zip。

产物由流水线落盘在多根目录下（deploy.config.yaml 的 output_dir 固定
前缀 deploy/ + rpm 流水线产物目录 rpm/ + rpm-check/hce-rpm-check 按目标
镜像分目录的报告树 rpmcheck/）；本模块只读浏览全部产物（含
历史轮次、重跑备份与杂项文件），不做任何按会话/阶段的过滤——侧栏产物
区即各根目录的镜像，清单组键带根前缀（deploy/…、rpm/…、rpmcheck/…），约定命名的
文件带阶段徽标（deploy 文件名从 config 派生，rpm 约定单点维护于
RPM_FILE_STAGES；{{software}} 占位剥去得后缀），非约定的（.v1 备份、
人工杂项）无徽标平铺。

路径约束（内容/下载/zip 端点共用）：rel_path 首段必须是已知根名，拼接
resolve 后必须仍位于该根内且是普通文件，否则按 404 处理——服务不是任意
文件读取器。
"""
import io
import os
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

import yaml

from .normalize import ARCHIVE, BUILD, GUIDE, INSTALL, VERIFY

# deploy.config.yaml 的产物文件键 → 所属阶段（config 是文件名的唯一权威源，
# 改名 / 加键只动 config：改名自动跟随，新键不在此映射即无徽标）
CONFIG_KEY_STAGES = {
    "install_file": GUIDE,
    "verify_file": GUIDE,
    "install_result_file": INSTALL,
    "install_issues_file": INSTALL,
    "install_meta_file": INSTALL,
    "verify_result_file": VERIFY,
    "verify_issues_file": VERIFY,
    "archive_result_file": ARCHIVE,
    "deploy_list_file": ARCHIVE,
    "archive_issues_file": ARCHIVE,
}

# rpm 流水线产物约定（rpm-build / rpm-verify / rpm-archive 的内置默认文件
# 名，rpm_archive 段在 deploy.config.yaml 落档；rpm-check / hce-rpm-check
# 的报告落 rpmcheck/{{target_image}}/ 树，后缀约定相同、在此一并维护）。
# 与 deploy 派生后缀无冲突：-rpm-verify-* /
# -rpm-archive-* 被 deploy 通用后缀先命中且阶段一致，-rpm-result /
# -rpm-issues / -rpm-deliver-list / -rpm.sh / -rpm-check-* 仅此处能匹配。.rpm 同时覆盖 .src.rpm 与依赖包（rpms/{binary,source,deps}/ 归档
# 收集的包，归档阶段落盘、本质是构建产物 → BUILD）。
# 阶段值取 normalize 常量（单来源）；BUILD 为 rpm 流水线独有阶段
# （rpm-build 子 agent 派发时事件面也用同一常量）。
RPM_FILE_STAGES = (
    ("-rpm-verify-result.md", VERIFY),
    ("-rpm-verify-issues.md", VERIFY),
    ("-rpm-check-result.md", VERIFY),
    ("-rpm-check-issues.md", VERIFY),
    ("-rpm-result.md", BUILD),
    ("-rpm-issues.md", BUILD),
    ("-rpm-archive-result.md", ARCHIVE),
    ("-rpm-deliver-list.md", ARCHIVE),
    ("-rpm.sh", ARCHIVE),
    (".rpm", BUILD),
)

# 二进制产物后缀：内容端点（文本浏览）不适用，清单带 binary 标记、前端
# 走占位视图 + 下载（下载/zip 端点按原始字节服务，不受此影响）
BINARY_SUFFIXES = (
    ".rpm", ".zip", ".gz", ".xz", ".bz2", ".tgz", ".tar",
    ".jar", ".war", ".bin", ".iso", ".img",
)


def is_binary_file(name):
    """产物文件名 → 是否二进制（按后缀判定；.rpm 含 .src.rpm）。"""
    return name.endswith(BINARY_SUFFIXES)


def load_file_stages(config_path):
    """deploy.config.yaml + rpm 约定 → ((后缀, 阶段), …)：{{software}} 占位
    剥去得匹配后缀。

    config 是 deploy 流水线与 Web 的共同契约，读不到或缺键即失败
    （fail fast），不落第二套默认表——同源漂移比产物列不出来更隐蔽。
    """
    data = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    suffixes = []
    for key, stage in CONFIG_KEY_STAGES.items():
        pattern = data[key]
        suffixes.append((pattern.replace("{{software}}", ""), stage))
    # 流水线汇总由 deploy skill 编排层直接落盘（不进 deploy.config.yaml）
    suffixes.append(("-pipeline-result.md", ARCHIVE))
    suffixes.extend(RPM_FILE_STAGES)
    return tuple(suffixes)


def stage_for_file(name, file_stages):
    """产物文件名 → 所属阶段；非约定产物名返回 None（无徽标平铺）。"""
    for suffix, stage in file_stages:
        if name.endswith(suffix):
            return stage
    return None


def browse(artifact_roots, file_stages):
    """多根全树清单：按「根名/相对目录」分组（根下散落文件归根名组），
    组间最新落盘时间降序、组内文件名升序。

    组的排序键取组内文件 mtime 的最大值（逐文件 stat 已为 size 做，零额外
    开销；目录自身 mtime 在原位重写文件时不更新，不可靠）。根目录不存在
    时 os.walk 直接无产出（如实缺席）。stat 失败的文件跳过（竞态消失，
    如实缺席）。
    """
    groups = {}
    for root_name, root_path in artifact_roots.items():
        for dirpath, _dirnames, filenames in os.walk(root_path):
            rel_dir = Path(dirpath).relative_to(root_path).as_posix()
            group_dir = f"{root_name}/{rel_dir}" if rel_dir != "." else root_name
            for name in filenames:
                try:
                    stat = os.stat(Path(dirpath) / name)
                except OSError:
                    continue
                stage = stage_for_file(name, file_stages)
                entry = {"name": name, "stage": stage, "size": stat.st_size}
                if is_binary_file(name):
                    entry["binary"] = True
                group = groups.setdefault(group_dir, {"mtime": 0.0, "files": []})
                group["mtime"] = max(group["mtime"], stat.st_mtime)
                group["files"].append(entry)
    ordered = []
    for group_dir in sorted(groups, key=lambda d: (-groups[d]["mtime"], d)):
        files = sorted(groups[group_dir]["files"], key=lambda f: f["name"])
        ordered.append({"dir": group_dir, "files": files})
    return {"groups": ordered}


def resolve(artifact_roots, file_stages, rel_path):
    """rel_path → (条目, 绝对路径)：首段为根名，余段须落在该根内的普通
    文件；未知根、越界、缺失或非文件返回 None（调用方按 404 处理）。
    内容 / 下载 / zip 三端点共用的唯一约束入口。"""
    if not rel_path or Path(rel_path).is_absolute():
        return None  # 绝对路径与空串前置拒绝（Path / "/abs" 会整体替换根）
    parts = PurePosixPath(rel_path).parts
    if ".." in parts:
        return None  # 目录段夹带 .. 前置拒绝（resolve 兜底之外的显式语义）
    root_path = artifact_roots.get(parts[0])
    if root_path is None or len(parts) < 2:
        return None  # 未知根 / 只剩根名没有文件段
    root = Path(root_path).resolve()
    target = (root / PurePosixPath(*parts[1:])).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        return None
    entry = {
        "dir": PurePosixPath(*parts[:-1]).as_posix(),
        "name": target.name,
        "stage": stage_for_file(target.name, file_stages),
    }
    if is_binary_file(target.name):
        entry["binary"] = True
    return entry, target


# 内容端点的读取入口（resolve 的别名形态：语义等价，保留原名供 app 调用）
def read(artifact_roots, file_stages, rel_path):
    return resolve(artifact_roots, file_stages, rel_path)


def zip_files(artifact_roots, file_stages, rel_paths):
    """批量打包：给定 rel_paths（根前缀）产出一个内存 zip。包内不带目录
    结构——文件一律按自身文件名平铺在 zip 根层（完整相对路径层级太深，
    解包即得文件本体）。仅收录根内存在的普通文件，缺失/越界/未知根如实
    跳过；按包内文件名去重、保持给定顺序（不同目录的同名文件首个胜出、
    后者跳过——同名 zip 条目解包行为因工具而异，不留歧义）。返回
    (zip 字节流, 收录数)；一个都没收到返回 (None, 0)。"""
    buf = io.BytesIO()
    seen = set()
    count = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in rel_paths:
            found = resolve(artifact_roots, file_stages, rel)
            if found is None:
                continue
            _entry, target = found
            arcname = target.name
            if arcname in seen:
                continue
            seen.add(arcname)
            zf.writestr(_zip_info(target, arcname), target.read_bytes())
            count += 1
    if count == 0:
        return None, 0
    buf.seek(0)
    return buf, count


# ZIP 纪元（1980-01-01 UTC 的 Unix 时间戳）：更早的 mtime zipfile 拒收，
# 钳制到该值（mtime 异常不阻断打包，内容才是主体）
_ZIP_EPOCH = 315532800.0


def _zip_info(target, arcname):
    """显式 ZipInfo：mtime 钳制进 ZIP 可表示区间、保留原权限位（.sh 的
    可执行位不丢），压缩方式随 ZipFile 全局设置。"""
    stat = target.stat()
    date_time = datetime.fromtimestamp(max(stat.st_mtime, _ZIP_EPOCH)).timetuple()[:6]
    info = zipfile.ZipInfo(arcname, date_time=date_time)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (stat.st_mode & 0xFFFF) << 16
    return info
