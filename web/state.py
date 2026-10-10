"""落盘的 run 簿记（薄）：墓碑 + 身份映射 + 克隆链镜像 + owner 归属。

对话内容的单一事实源是 CLI 侧 transcript（~/.claude/projects）——
stage/title/first_prompt/created_at 全部可从 transcript 重放推导（title 本就
写回 transcript 的 custom-title 行），不入册。簿记只存 transcript 里没有的：

- 墓碑（ended_sessions）：用户显式结束（ENDED）的 session_id 集合——
  transcript 是 CLI 的地盘写不进去，不落册重启后会话就复活成可续聊；
- 身份映射（sessions）：run_id ↔ session_id——重启后 run_id 稳定，开着的
  标签页不死。映射只登记有自身 session_id 的会话（首回合被接受后）；克隆
  未发首条指令的空会话身份天然丢失，按接受处理（无内容可恢复）；
- 克隆链镜像（clone_sources）：session_id → 来源 run_id——transcript 里
  没有克隆血缘，重放会话凭镜像找回克隆链父指针 resumed_from（前端
  「⑂ 克隆自」标记）；
- owner 归属（owners）：run_id → 归属用户——与身份映射同生命周期登记，
  重启后会话仍归原用户（transcript 里没有归属）。

写入为全量原子替换（tmp + rename），每次状态变更即写；strict=True 时
失败抛 StatePersistError（控制动作的落盘门，503 且不执行），缺省只告警
——落盘是恢复增强，不能反过来打断会话执行。

读取带显式版本并给出分类（status，见 load_state）：version 2 为现代格式
（owner 归属可信；单条会话缺 owner 记录不补默认——保持未知归属等待
人工处置）；无 version 且四段形状完好的旧字符串映射为 legacy（仅初始化
标记缺席的首次启动允许迁移归默认 owner，判定在 app）；文件缺失为
missing；损坏、形状不对、v2 缺 owners 段或未知版本为 corrupt——一律
不自动补默认 owner，由 app 进受限恢复。更早的 runs 记录数组旧格式缺
ended/sessions 段，按形状不对落 corrupt（其会话身份与归属本就弃读，
受限只多拦了控制动作）。
"""
import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path

from .runs import ENDED

logger = logging.getLogger("web")

STATE_VERSION = 2

# 簿记状态分类（load_state 的 status 值；app 的恢复判定与转移工具校验
# 同源引用，字面量只写这一处）
STATUS_MODERN = "modern"
STATUS_LEGACY = "legacy"
STATUS_MISSING = "missing"
STATUS_CORRUPT = "corrupt"


class StatePersistError(RuntimeError):
    """簿记无法安全落盘（控制动作据此 503 阻断，不执行）。"""


def save_state(runs, path, clone_sources=None, strict=False):
    """全部 run 的墓碑、身份映射与克隆链镜像全量落盘（原子替换）。
    strict=True 时失败抛 StatePersistError——归属无法安全落盘的控制动作
    宁可拒绝（重启不得靠默认 owner 兜底找回）；缺省只告警（落盘是恢复
    增强，不能反过来打断会话执行）。"""
    ended = sorted(r.session_id for r in runs if r.status == ENDED and r.session_id)
    sessions = {r.run_id: r.session_id for r in runs if r.session_id}
    # owner 随身份映射同界登记（无 session_id 的空会话不承诺跨重启存在，
    # owner 无从找回；恢复时这类会话本就不回来）
    owners = {r.run_id: r.owner for r in runs if r.session_id and r.owner}
    target = Path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=target.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({
                    "version": STATE_VERSION,
                    "ended_sessions": ended,
                    "sessions": sessions,
                    "clone_sources": clone_sources or {},
                    "owners": owners,
                }, f, ensure_ascii=False)
            os.replace(tmp, target)
        except BaseException:
            os.unlink(tmp)
            raise
    except OSError as exc:
        if strict:
            raise StatePersistError(str(exc)) from exc
        logger.warning("状态落盘失败（重启恢复能力降级，不影响会话执行）", exc_info=True)


def load_state(path):
    """读回 {ended_sessions: set, sessions: {run_id: session_id}, clone_sources:
    {session_id: 来源 run_id}, owners: {run_id: 归属用户}, status}。

    status 分类（app 据此决定 legacy 迁移还是受限恢复）：modern（v2 且
    四段形状完好，owners 段必在）/ legacy（无 version 的旧字符串映射，
    owners 段可选）/ missing（文件不存在）/ corrupt（损坏、形状不对、
    v2 缺 owners 段、未知版本）。corrupt 一律整体空册——半份无从判真，
    归属不可信时宁可不暴露。"""
    empty = {"ended_sessions": set(), "sessions": {}, "clone_sources": {}, "owners": {}}
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {**empty, "status": STATUS_MISSING}
    except (OSError, ValueError):
        return {**empty, "status": STATUS_CORRUPT}
    try:
        data = json.loads(raw)
    except ValueError:
        return {**empty, "status": STATUS_CORRUPT}
    ended = data.get("ended_sessions") if isinstance(data, dict) else None
    sessions = data.get("sessions") if isinstance(data, dict) else None
    clones = data.get("clone_sources") if isinstance(data, dict) else None
    owners = data.get("owners") if isinstance(data, dict) else None
    well_formed = (
        isinstance(ended, list) and all(isinstance(s, str) and s for s in ended)
        and isinstance(sessions, dict) and all(
            isinstance(k, str) and isinstance(v, str) and k and v for k, v in sessions.items())
        and isinstance(clones, dict) and all(
            isinstance(k, str) and isinstance(v, str) and k and v for k, v in clones.items())
        and (owners is None or (isinstance(owners, dict) and all(
            isinstance(k, str) and isinstance(v, str) and k and v for k, v in owners.items())))
    )
    if not well_formed:
        return {**empty, "status": STATUS_CORRUPT}
    version = data.get("version")
    if version == STATE_VERSION:
        if owners is None:
            return {**empty, "status": STATUS_CORRUPT}  # v2 必带 owners 段
        return {"ended_sessions": set(ended), "sessions": sessions,
                "clone_sources": clones, "owners": owners, "status": STATUS_MODERN}
    if version is None and isinstance(ended, list) and isinstance(sessions, dict):
        # 旧字符串映射：是否允许迁移归默认 owner 由初始化标记决定（app 侧）。
        # runs 记录数组等更早的旧格式落不到这里（ended/sessions 缺段即 corrupt）
        return {"ended_sessions": set(ended), "sessions": sessions,
                "clone_sources": clones, "owners": owners or {}, "status": STATUS_LEGACY}
    return {**empty, "status": STATUS_CORRUPT}  # 未知版本（未来格式）不猜


def init_marker_path(state_path):
    """初始化标记路径（state 文件同名加 .initialized）：存在即服务完成过
    一次成功初始化——此后 state 缺失/回退旧格式不再走 legacy 迁移，而是
    受限恢复（删除 state 不能把新用户会话交给默认 owner）。"""
    p = Path(state_path)
    return p.parent / (p.name + ".initialized")


def write_init_marker(state_path):
    """写入初始化标记（幂等，成功初始化的收尾一步）。失败只告警：标记
    缺席的后果偏保守（下次启动重复一次 legacy 迁移判定），且同目录簿记
    写不进时控制动作的落盘门已先行 503。"""
    marker = init_marker_path(state_path)
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(datetime.now().isoformat(timespec="seconds") + "\n",
                          encoding="utf-8")
    except OSError:
        logger.warning("初始化标记写入失败 %s", marker, exc_info=True)
