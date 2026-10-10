#!/usr/bin/env python
"""人工 owner 转移工具 —— 会话归属变更的唯一正规途径。

owner 在会话生命周期内不可变、普通 Web API 不提供转移入口（归属变更
只能停服务、备份状态、走本工具的专用人工迁移流程）。本脚本就是那条
专用流程：停服后运行，按源用户名把簿记中的会话归属成批转移给目标
用户名，转移前自动备份 state，动作写入控制审计（actor=operator，meta
带 from/to/reason/转移的 run 列表）。

用法（先停 Web 服务）：
  python tools/transfer_ownership.py --from alice --to bob --reason "人员调整"

环境变量：
  AUTO_IMAGE_STATE     state 簿记路径（默认 ~/.auto-image-web/state.json）
  AUTO_IMAGE_AUDIT_DIR 控制审计目录（默认 ~/.auto-image-web/audit）

只改 owners 映射；墓碑、身份映射与克隆链镜像原样保留。目标用户名
不必已在用户清单中（孤儿会话等待人工迁移的逆操作同样合法——清单
随后热载即生效）。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web.audit import ControlAudit  # noqa: E402
from web.state import (  # noqa: E402
    STATUS_MODERN, load_state, save_state,
)

DEFAULT_STATE = Path.home() / ".auto-image-web" / "state.json"
DEFAULT_AUDIT = Path.home() / ".auto-image-web" / "audit"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="人工 owner 转移（停服后运行）")
    parser.add_argument("--from", dest="from_user", required=True,
                        help="源用户名（簿记 owners 映射的现值）")
    parser.add_argument("--to", dest="to_user", required=True, help="目标用户名")
    parser.add_argument("--reason", required=True, help="转移原因（入审计）")
    parser.add_argument("--state", default=None, help="state 路径（默认 $AUTO_IMAGE_STATE）")
    parser.add_argument("--audit-dir", default=None,
                        help="审计目录（默认 $AUTO_IMAGE_AUDIT_DIR）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只列出将被转移的 run，不改簿记不写审计")
    return parser.parse_args(argv)


def _service_running(state_path: Path) -> bool:
    """检测 Web 服务是否仍在运行：服务侧 save_state 的 tmp 前缀与 state
    同名（state.json.tmp*），落盘瞬间存在即有活动写入者。采样等一小段
    窗口，静止目录视为已停服。"""
    import glob
    import time as _time
    for _ in range(3):
        if glob.glob(str(state_path) + ".tmp*"):
            return True
        _time.sleep(0.3)
    return False


def main(argv=None):
    args = parse_args(argv)
    state_path = Path(args.state or os.environ.get("AUTO_IMAGE_STATE") or DEFAULT_STATE)
    audit_dir = Path(args.audit_dir
                     or os.environ.get("AUTO_IMAGE_AUDIT_DIR") or DEFAULT_AUDIT)
    if args.from_user == args.to_user:
        raise SystemExit("源与目标用户名相同，无事可做")
    if _service_running(state_path):
        raise SystemExit(
            "检测到 Web 服务仍在写簿记（state.tmp 瞬时文件）。转移必须停服后运行，"
            "否则结果会被服务侧下一次全量落盘覆盖；请先停服再试")

    state = load_state(state_path)
    if state["status"] != STATUS_MODERN:
        raise SystemExit(
            f"簿记不可转移（status={state['status']}）：只有现代 v2 簿记能安全转移；"
            "corrupt/missing 请先从备份恢复，legacy 请先起一次服务完成迁移")

    owners = state["owners"]
    affected = sorted(rid for rid, owner in owners.items() if owner == args.from_user)
    if not affected:
        print(f"簿记中没有 owner 为 {args.from_user} 的会话，无事可做")
        return
    print(f"将转移 {len(affected)} 条会话：{args.from_user} → {args.to_user}")
    for rid in affected:
        print(f"  {rid}  ({state['sessions'].get(rid, '无身份映射')})")
    if args.dry_run:
        print("dry-run：未改簿记、未写审计")
        return

    # 备份先行：转移出错时可整册回滚
    backup = state_path.with_name(state_path.name + ".bak-transfer")
    shutil.copy2(state_path, backup)
    print(f"已备份 {state_path} → {backup}")

    # 转移：owners 改值，其余原样重写（save_state 重建整册 v2）
    for rid in affected:
        owners[rid] = args.to_user
    _rewrite_owners(state_path, state, owners)

    # 审计后行于改册之后记录结果（与 Web 控制动作「先审计后执行」相反：
    # 这里没有 503 回滚通道，审计如实记录已发生的转移与清单）
    ControlAudit(audit_dir).record(
        actor="operator",
        action="owner_transfer",
        result="success",
        request_id=uuid.uuid4().hex[:16],
        meta={
            "from": args.from_user,
            "to": args.to_user,
            "runs": affected,
            "reason": args.reason,
            "backup": backup.name,
        },
    )
    print(f"已转移并写入审计（{audit_dir}）")


def _rewrite_owners(state_path, state, owners):
    """按已改值的 owners 映射重写整册（save_state 走同一原子替换路径，
    字段从读回的整册取齐——墓碑/映射/克隆链不动）。"""
    from types import SimpleNamespace
    runs = [
        SimpleNamespace(
            run_id=rid, session_id=sid,
            status="ENDED" if sid in state["ended_sessions"] else "READY",
            owner=owners.get(rid),
        )
        for rid, sid in state["sessions"].items()
    ]
    save_state(runs, state_path, dict(state["clone_sources"]))


if __name__ == "__main__":
    main()
