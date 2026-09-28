"""控制审计：本地追加式 JSONL 文件，按天轮转，默认保留 90 天。

每条记录：time（ISO 本地时）、actor、action、result、request_id 必填，
target_username（用户变更目标）/ run_id / run_owner / reason（拒绝原因）/ meta（动作元数据，如发送指令
只记长度与摘要 hash）按动作带上。审计不写 Cookie、密码、AK/SK、
完整 prompt 或 OBS 对象内容——调用方只传元数据。

写入失败抛 AuditWriteError：控制动作（登录成功、登出等状态变更类）先落
审计再执行，审计失败即 503 不放行；只读路径的审计失败不阻断（如常返回）。
普通用户无 Web 下载能力——审计文件只落在服务端本地。
"""
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("web")

DEFAULT_RETENTION_DAYS = 90
# 审计文件名约定：audit-YYYY-MM-DD.jsonl（清理与测试定位同源引用）
_FILE_PATTERN = re.compile(r"^audit-\d{4}-\d{2}-\d{2}\.jsonl$")


class AuditWriteError(RuntimeError):
    """审计写入失败（控制动作据此返回 503 并放行与否的判定载体）。"""


class ControlAudit:
    def __init__(self, directory, retention_days=DEFAULT_RETENTION_DAYS):
        self.directory = Path(directory)
        self.retention_days = retention_days
        self._current_day = None
        self._handle = None

    def _file_for(self, day: str) -> Path:
        return self.directory / f"audit-{day}.jsonl"

    def _prune(self, now: float):
        """按文件名日期清理超期档案；清理失败只告警（保留是软承诺）。"""
        cutoff = time.strftime("%Y-%m-%d", time.localtime(now - self.retention_days * 86400))
        try:
            entries = list(self.directory.glob("audit-*.jsonl"))
        except OSError:
            return
        for f in entries:
            name = f.name
            if not _FILE_PATTERN.match(name):
                continue
            if name < f"audit-{cutoff}":
                try:
                    f.unlink()
                except OSError:
                    logger.warning("审计档案 %s 清理失败", f, exc_info=True)

    def _write(self, entry: dict):
        day = entry["day"]
        now_ts = entry["_ts"]
        if self._current_day != day:
            if self._handle is not None:
                try:
                    self._handle.close()
                except OSError:
                    pass
                self._handle = None
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                self._handle = self._file_for(day).open("a", encoding="utf-8")
                self._current_day = day
            except OSError as exc:
                raise AuditWriteError(str(exc)) from exc
        try:
            line = json.dumps(entry["record"], ensure_ascii=False) + "\n"
            self._handle.write(line)
            self._handle.flush()
            # 打开的句柄在目录被外部删除后仍会「写成功」（数据落进已 unlink
            # 的 inode，静默丢失）——每次写入前确认目标仍在目录树上
            if not self._file_for(day).exists():
                raise OSError("audit file vanished from directory")
        except OSError as exc:
            self._handle = None  # 句柄不可信，下次写入重开
            self._current_day = None
            raise AuditWriteError(str(exc)) from exc
        # 清理每次写入后顺带执行（轻量 glob + 文件名比较；写失败不告警升级）
        self._prune(now_ts)

    def record(self, *, actor, action, result, request_id,
               run_id=None, run_owner=None, reason=None, meta=None, target_username=None):
        """追加一条审计。失败抛 AuditWriteError（由调用方决定 503 与否）；
        reason 是拒绝原因（拒绝类记录专用），meta 是动作元数据（如发送
        指令的长度与摘要 hash）——两者不混用同一字段。记录内容不含敏感值
        ——调用方约定。"""
        now = time.time()
        record = {
            "time": datetime.fromtimestamp(now).isoformat(timespec="seconds"),
            "actor": actor,
            "action": action,
            "result": result,
            "request_id": request_id,
        }
        if run_id is not None:
            record["run_id"] = run_id
        if target_username is not None:
            record["target_username"] = target_username
        if run_owner is not None:
            record["run_owner"] = run_owner
        if reason is not None:
            record["reason"] = reason
        if meta is not None:
            record["meta"] = meta
        self._write({"day": time.strftime("%Y-%m-%d", time.localtime(now)),
                     "_ts": now, "record": record})

    def close(self):
        if self._handle is not None:
            try:
                self._handle.close()
            finally:
                self._handle = None
